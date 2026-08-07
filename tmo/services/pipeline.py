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
) -> dict[str, int]:
    """Прогоняет наблюдения пачками. Каждая пачка — свои три фазы."""
    settings = get_settings()
    batch_size = batch_size or settings.batch_size
    totals = {"attempts": 0, "offers": 0, "raw": 0, "batches": 0, "budget_exhausted": 0}

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
