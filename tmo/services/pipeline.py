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
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
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

@dataclass(frozen=True, slots=True)
class SourceCost:
    """Во что обходится одно наблюдение у одного источника.

    Две величины, а не одна, потому что ограничителей тоже два: задержка
    упирается в одновременность, число обращений — в лимит темпа. Какой из них
    сработает, зависит от настроек, и считать надо по худшему.
    """

    seconds: float
    calls: float


#: Измеренная стоимость наблюдения. Ночь 08.08.2026, снимок 6.
#:
#: Это не настройка, а результат замера. От него считается размер пачки и по нему
#: проверяется, укладывается ли ночь в окно. Пачка фиксированного размера для
#: семейств, различающихся в двадцать раз, гарантированно рвётся на дорогом:
#: 40 авианаблюдений при одновременности 3 требуют 271 секунду против бюджета в
#: 240, и за ночь так потерялось 219 наблюдений.
#:
#: Среднее берётся по ``SUCCESS`` **и** ``PARTIAL``. Первая версия этой таблицы
#: считала только по ``SUCCESS`` и давала для авиа 20,3 с — среднее ста
#: восьмидесяти наблюдений, собранных за первые пятьдесят две минуты ночи, пока
#: источник был свеж. Ещё 134 частичных наблюдения той же ночи стоили по 53 с и
#: в среднее не входили. Пропускная способность пула определяется средним по
#: всему, что он обслуживает, а не по удачной его части: 34,3 против 20,3
#: превращают авиа из укладывающейся в окно в не укладывающуюся, и разница эта
#: была не в источнике, а в способе счёта.
#:
#: Пересматривать после каждого изменения рабочей точки или поведения источника.
OBSERVATION_COST: dict[str, dict[str, SourceCost]] = {
    "AIR": {"tutu_mcp": SourceCost(seconds=34.3, calls=6.26)},
    "HOTEL": {"tutu_mcp": SourceCost(seconds=7.2, calls=3.62)},
    "RAIL": {
        "tutu_mcp": SourceCost(seconds=1.0, calls=1.03),
        "rzd": SourceCost(seconds=1.5, calls=1.00),
    },
}

#: Какую долю бюджета пачке позволено занять по расчёту. Остаток — на разброс
#: задержек, повторы и запись: замер даёт среднее, а рвётся пачка по хвосту.
BATCH_BUDGET_FILL = 0.7

#: Границы размера пачки. Снизу — чтобы накладные расходы транзакций не съели
#: выигрыш, сверху — чтобы отказ одной пачки не уносил слишком много работы.
MIN_BATCH_SIZE = 20
MAX_BATCH_SIZE = 400


def seconds_per_observation(family: str | None) -> float:
    """Ожидаемое время одного наблюдения семейства при текущей рабочей точке.

    Источники внутри пачки обходятся последовательно (`runner.execute_batch`),
    поэтому стоимость — сумма по источникам. У каждого берётся **худший из двух
    ограничителей**: задержка, делённая на одновременность, и число обращений,
    делённое на разрешённый темп.

    Учитывать оба обязательно. При одновременности 6 связывает задержка, при 12
    — уже лимит темпа: авиа просит около 215 обращений в минуту при разрешённых
    180, а проживание упирается в лимит уже на семи потоках. Расчёт по одной
    лишь одновременности показал бы, что ночь укладывается в окно, тогда как на
    деле не укладывается.

    Возвращает 0 для семейства без замера: досбор идёт по дырам всех семейств
    сразу, и осмысленной средней стоимости у него нет.

    Одновременность берётся **фактическая**, а не из реестра. Регулятор снижает
    её при отказах источника, и пачка, посчитанная по потолку, при рабочей точке
    вдвое ниже требует вдвое больше времени, чем ей отведено: её хвост целиком
    уходит в ``BUDGET_EXHAUSTED``.
    """
    from tmo.catalog.registry import source_registry
    from tmo.connectors.transport import TRANSPORT_POOL

    costs = OBSERVATION_COST.get(str(family or ""))
    if not costs:
        return 0.0

    registry = source_registry()
    total = 0.0
    for code, cost in costs.items():
        source = registry.get(code)
        concurrency = TRANSPORT_POOL.current_concurrency(code, source.concurrency)
        by_concurrency = cost.seconds / max(1, concurrency)
        by_rate = (
            cost.calls / (source.rate_limit_per_minute / 60.0)
            if source.rate_limit_per_minute
            else 0.0
        )
        total += max(by_concurrency, by_rate)
    return total


def batch_size_for_family(family: str | None, *, settings: Any = None) -> int:
    """Сколько наблюдений семейства укладывается в бюджет одной пачки."""
    settings = settings or get_settings()
    seconds = seconds_per_observation(family)
    if seconds <= 0:
        return settings.batch_size

    budget = settings.batch_soft_budget_seconds * BATCH_BUDGET_FILL
    return max(MIN_BATCH_SIZE, min(MAX_BATCH_SIZE, int(budget / seconds)))


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
    deadline: datetime | None = None,
    on_progress: Callable[[], bool] | None = None,
) -> dict[str, int]:
    """Прогоняет наблюдения пачками. Каждая пачка — свои три фазы.

    Размер пачки пересчитывается **перед каждой пачкой**, а не один раз на
    вызов: регулятор одновременности меняет рабочую точку по ходу сбора, и
    пачка, посчитанная под двенадцать потоков, при осевших до двух не успевает
    и трети своих наблюдений.

    Пачка, целиком пропущенная разомкнутой цепью, останавливает обход до её
    остывания. Без паузы окно семейства расходуется впустую: в ночь
    08.08.2026 сбор авиа прошёл все 218 пачек за шесть секунд, не сделав ни
    одного обращения, и вернул успех. Цепь остывает за пять минут — дешевле
    подождать их один раз, чем потерять восьмичасовое окно.

    ``deadline`` останавливает обход по времени, оставляя несобранное в плане.
    Это не отказ: наблюдение, до которого не дошли, — дыра, а дыры добираются
    следующим проходом. Право решать, когда прекратить, принадлежит диспетчеру,
    и он выражает его этим параметром.

    ``on_progress`` вызывается после каждой пачки и продлевает аренду шага.
    Вернув ``False``, он останавливает обход: аренда перехвачена, шаг ведёт
    кто-то другой, и продолжать — значит удваивать нагрузку на источник.
    """
    settings = get_settings()
    totals = {
        "attempts": 0, "offers": 0, "raw": 0, "batches": 0,
        "budget_exhausted": 0, "circuit_waits": 0, "batch_size": 0,
        "deadline_reached": 0, "skipped": 0, "lease_lost": 0,
    }

    start = 0
    while start < len(job_ids):
        if deadline is not None and now_utc() >= deadline:
            logger.info(
                "Обход остановлен по дедлайну: остаток остаётся дырами",
                collected=start,
                remaining=len(job_ids) - start,
            )
            totals["deadline_reached"] = 1
            break

        size = batch_size or batch_size_for_family(family, settings=settings)
        chunk = job_ids[start : start + size]
        totals["batch_size"] = size
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
        totals["skipped"] += report.skipped_untouched
        start += len(chunk)

        if on_progress is not None and not on_progress():
            logger.warning(
                "Аренда шага потеряна: обход остановлен",
                collected=start,
                remaining=len(job_ids) - start,
            )
            totals["lease_lost"] = 1
            break

        skipped_whole_batch = report.attempts == 0 and report.skipped_untouched >= len(chunk)
        has_more = start < len(job_ids)
        if skipped_whole_batch and has_more:
            wait = settings.circuit_reset_seconds
            if deadline is not None and now_utc() + timedelta(seconds=wait) >= deadline:
                totals["deadline_reached"] = 1
                break
            logger.warning(
                "Пачка пропущена целиком: пауза до остывания размыкателя",
                skipped=report.skipped_untouched,
                wait_seconds=wait,
            )
            totals["circuit_waits"] += 1
            time.sleep(wait)
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
    origins: tuple[str, ...] | None = None,
) -> PipelineReport:
    """Полный суточный цикл от плана до опубликованного снимка.

    ``origins`` ограничивает матрицу поездками из перечисленных городов —
    нагрузочный прогон конвейера на доле объёма. Такой снимок на витрину не
    попадает.
    """
    stages: list[dict[str, Any]] = []

    with session_scope() as session:
        creation = create_snapshot(
            session,
            snapshot_date=snapshot_date,
            horizon_days=horizon_days,
            families=families,
            is_synthetic=is_synthetic,
            profile_version=profile_version,
            origins=origins,
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
