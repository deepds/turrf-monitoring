"""Суточный конвейер.

```text
план → первичный сбор → анализ покрытия → досбор дыр
     → расчёт → ворота качества → финализация снимка
```

Досбор — штатный шаг, а не аварийная мера. Часть наблюдений выпадает
неизбежно: источник ответил таймаутом, размыкатель был разомкнут, воркер
перезапустился на середине. К моменту досбора причина обычно прошла.

Досбор идёт с солью в ключе идемпотентности: без неё повторное исполнение той
же области молча вернуло бы прежний результат вместо новой попытки.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any

from tmo.core.config import get_settings
from tmo.core.enums import CollectionFamily, SnapshotStatus
from tmo.core.logging import get_logger, log_context
from tmo.core.timeutil import now_utc
from tmo.db import models
from tmo.db.session import session_scope
from tmo.execution.runner import run_batch
from tmo.services.calculation import calculate_snapshot
from tmo.services.coverage import compute_coverage, find_holes
from tmo.services.publication import finalize_snapshot
from tmo.services.snapshot import create_snapshot

logger = get_logger(__name__)

#: Измеренная стоимость одного наблюдения по семейству и источнику, секунды.
#: Ночь 08.08.2026, средняя задержка успешных попыток снимка 6.
#:
#: Это не настройка, а результат замера: он задаёт размер пачки так, чтобы она
#: укладывалась в свой бюджет. Пачка фиксированного размера для семейств,
#: различающихся в двадцать раз, гарантированно рвётся на дорогом: 40 авианаблюдений
#: при одновременности 3 требуют 271 секунду против бюджета в 240, и каждая пачка
#: закрывалась `BUDGET_EXHAUSTED`, не дойдя до трети своих наблюдений. За ночь так
#: потерялось 219 наблюдений — не из-за источника, а из-за несогласованности
#: двух констант.
#:
#: Пересматривать после каждого изменения одновременности или поведения источника.
OBSERVATION_COST_SECONDS: dict[str, dict[str, float]] = {
    "AIR": {"tutu_mcp": 20.3},
    "HOTEL": {"tutu_mcp": 7.2},
    "RAIL": {"tutu_mcp": 1.0, "rzd": 1.5},
}

#: Какую долю бюджета пачке позволено занять по расчёту. Остаток — на разброс
#: задержек, повторы и запись: замер даёт среднее, а рвётся пачка по хвосту.
BATCH_BUDGET_FILL = 0.7

#: Границы размера пачки. Снизу — чтобы накладные расходы транзакций не съели
#: выигрыш, сверху — чтобы отказ одной пачки не уносил слишком много работы.
MIN_BATCH_SIZE = 20
MAX_BATCH_SIZE = 400


def batch_size_for_family(family: str | None, *, settings: Any = None) -> int:
    """Сколько наблюдений семейства укладывается в бюджет одной пачки.

    Источники внутри пачки обходятся последовательно, поэтому стоимость
    наблюдения — это сумма по источникам, каждый со своей одновременностью.
    """
    from tmo.catalog.registry import source_registry

    settings = settings or get_settings()
    costs = OBSERVATION_COST_SECONDS.get(str(family or ""))
    if not costs:
        # Досбор идёт по дырам всех семейств сразу, стоимость смешанная —
        # остаётся настроенный размер.
        return settings.batch_size

    registry = source_registry()
    seconds_per_job = 0.0
    for source_code, cost in costs.items():
        concurrency = max(1, registry.get(source_code).concurrency)
        seconds_per_job += cost / concurrency
    if seconds_per_job <= 0:
        return settings.batch_size

    budget = settings.batch_soft_budget_seconds * BATCH_BUDGET_FILL
    return max(MIN_BATCH_SIZE, min(MAX_BATCH_SIZE, int(budget / seconds_per_job)))


@dataclass(slots=True)
class PipelineReport:
    snapshot_id: int
    snapshot_date: date
    planned: int
    collected_attempts: int
    offers: int
    recovery_rounds: int
    recovered_jobs: int
    status: str
    coverage_total: float
    within_sla: bool
    metrics: int = 0
    trip_rows: int = 0
    stages: list[dict[str, Any]] = field(default_factory=list)


def _job_ids(snapshot_id: int, families: tuple[CollectionFamily, ...] | None = None) -> list[int]:
    from sqlalchemy import select

    with session_scope() as session:
        query = select(models.CollectionJob.id).where(
            models.CollectionJob.snapshot_id == snapshot_id
        )
        if families:
            query = query.where(
                models.CollectionJob.family.in_([family.value for family in families])
            )
        return list(session.scalars(query.order_by(models.CollectionJob.id)))


def collect_jobs(
    job_ids: list[int],
    *,
    execution_scope: str = "PRIMARY",
    attempt_salt: str = "",
    batch_size: int | None = None,
    replay_mode: str | None = None,
    soft_budget_seconds: float | None = None,
    family: str | None = None,
) -> dict[str, int]:
    """Прогоняет наблюдения пачками. Каждая пачка — свои три фазы.

    Пачка, целиком пропущенная разомкнутой цепью, останавливает обход до её
    остывания. Без паузы окно семейства расходуется впустую: в ночь
    08.08.2026 сбор авиа прошёл все 218 пачек за шесть секунд, не сделав ни
    одного обращения, и вернул успех. Цепь остывает за пять минут — дешевле
    подождать их один раз, чем потерять восьмичасовое окно.
    """
    settings = get_settings()
    batch_size = batch_size or batch_size_for_family(family, settings=settings)
    totals = {
        "attempts": 0, "offers": 0, "raw": 0, "batches": 0,
        "budget_exhausted": 0, "circuit_waits": 0, "batch_size": batch_size,
    }

    for start in range(0, len(job_ids), batch_size):
        chunk = job_ids[start : start + batch_size]
        report = run_batch(
            chunk,
            execution_scope=execution_scope,
            attempt_salt=attempt_salt,
            replay_mode=replay_mode,
            soft_budget_seconds=soft_budget_seconds,
        )
        totals["attempts"] += report.attempts
        totals["offers"] += report.offers
        totals["raw"] += report.raw_responses
        totals["batches"] += 1
        totals["budget_exhausted"] += int(report.budget_exhausted)

        skipped_whole_batch = report.attempts == 0 and report.skipped_untouched >= len(chunk)
        has_more = start + batch_size < len(job_ids)
        if skipped_whole_batch and has_more:
            logger.warning(
                "Пачка пропущена целиком: пауза до остывания размыкателя",
                skipped=report.skipped_untouched,
                wait_seconds=settings.circuit_reset_seconds,
            )
            totals["circuit_waits"] += 1
            time.sleep(settings.circuit_reset_seconds)
    return totals


def run_daily_pipeline(
    *,
    snapshot_date: date | None = None,
    horizon_days: int | None = None,
    families: tuple[CollectionFamily, ...] = tuple(CollectionFamily),
    replay_mode: str | None = None,
    is_synthetic: bool = False,
    recovery_rounds: int = 2,
    batch_size: int | None = None,
    soft_budget_seconds: float | None = None,
    profile_version: str | None = None,
    golden_result: dict[str, Any] | None = None,
) -> PipelineReport:
    """Полный суточный цикл от плана до опубликованного снимка."""
    stages: list[dict[str, Any]] = []

    with session_scope() as session:
        creation = create_snapshot(
            session,
            snapshot_date=snapshot_date,
            horizon_days=horizon_days,
            families=families,
            is_synthetic=is_synthetic,
            profile_version=profile_version,
        )
    stages.append(
        {
            "stage": "PLAN",
            "planned": creation.planned,
            "by_family": creation.by_family,
            "plan_digest": creation.plan_digest,
        }
    )

    with log_context(snapshot_id=creation.snapshot_id):
        job_ids = _job_ids(creation.snapshot_id)
        primary = collect_jobs(
            job_ids,
            execution_scope="PRIMARY",
            batch_size=batch_size,
            replay_mode=replay_mode,
            soft_budget_seconds=soft_budget_seconds,
        )
        stages.append({"stage": "PRIMARY_COLLECTION", **primary})

        with session_scope() as session:
            snapshot = session.get(models.MarketSnapshot, creation.snapshot_id)
            snapshot.primary_collection_finished_at = now_utc()
            snapshot.status = SnapshotStatus.RECOVERING

        recovered = 0
        rounds_done = 0
        for attempt in range(1, recovery_rounds + 1):
            with session_scope() as session:
                holes = find_holes(session, creation.snapshot_id)
            if not holes:
                break
            rounds_done = attempt
            logger.info("Досбор дыр", round=attempt, holes=len(holes))
            recovery = collect_jobs(
                holes,
                execution_scope="RECOVERY",
                # Соль делает повтор действительно новым исполнением.
                attempt_salt=f"recovery-{attempt}",
                batch_size=batch_size,
                replay_mode=replay_mode,
                soft_budget_seconds=soft_budget_seconds,
            )
            recovered += recovery["attempts"]
            stages.append({"stage": f"RECOVERY_{attempt}", "holes": len(holes), **recovery})

        with session_scope() as session:
            snapshot = session.get(models.MarketSnapshot, creation.snapshot_id)
            snapshot.recovery_finished_at = now_utc()
            snapshot.status = SnapshotStatus.CALCULATING

        with session_scope() as session:
            calculation = calculate_snapshot(
                session, creation.snapshot_id, profile_version=profile_version
            )
        stages.append(
            {
                "stage": "CALCULATION",
                "metrics": calculation.metrics,
                "offers_included": calculation.offers_included,
                "trip_rows": calculation.trip_rows,
                "no_market_metrics": calculation.no_market_metrics,
            }
        )

        with session_scope() as session:
            publication = finalize_snapshot(
                session,
                creation.snapshot_id,
                profile_version=profile_version,
                golden_result=golden_result,
            )
            coverage = compute_coverage(session, creation.snapshot_id)
        stages.append(
            {
                "stage": "PUBLICATION",
                "status": publication.status,
                "coverage_total": publication.coverage_total,
                "notes": [note["code"] for note in publication.notes],
            }
        )

    return PipelineReport(
        snapshot_id=creation.snapshot_id,
        snapshot_date=creation.snapshot_date,
        planned=creation.planned,
        collected_attempts=primary["attempts"],
        offers=primary["offers"],
        recovery_rounds=rounds_done,
        recovered_jobs=recovered,
        status=publication.status,
        coverage_total=coverage.total.completion,
        within_sla=publication.within_sla,
        metrics=calculation.metrics,
        trip_rows=calculation.trip_rows,
        stages=stages,
    )


def recalculate(
    snapshot_id: int,
    *,
    profile_version: str | None = None,
    make_active: bool = True,
) -> dict[str, Any]:
    """Пересчёт снимка другой методикой.

    Создаёт новый ``CalculationRun``. Прежний остаётся неизменным — на него
    ссылаются уже показанные цифры, и переписать его значило бы переписать
    историю.
    """
    with session_scope() as session:
        report = calculate_snapshot(
            session, snapshot_id, profile_version=profile_version, make_active=make_active
        )
    if make_active:
        with session_scope() as session:
            publication = finalize_snapshot(
                session, snapshot_id, profile_version=profile_version
            )
        return {"calculation": asdict(report), "publication": asdict(publication)}
    return {"calculation": asdict(report)}
