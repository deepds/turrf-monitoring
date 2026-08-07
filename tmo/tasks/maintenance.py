"""Обслуживание: сторож прогресса, ретеншен, суточная сводка.

Живёт в отдельной очереди и на отдельном воркере. Тот, кто чинит, не должен
стоять в очереди, которую он чинит.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from sqlalchemy import func, select

from tmo.core.config import get_settings
from tmo.core.enums import JobStatus
from tmo.core.logging import get_logger
from tmo.core.timeutil import now_utc, snapshot_date_for
from tmo.db import models
from tmo.db.session import session_scope
from tmo.storage.raw_store import RawStore
from tmo.tasks.celery_app import celery_app

logger = get_logger(__name__)

#: Сколько суток хранятся тела сырых ответов. Метаданные, Offers и метрики
#: не удаляются никогда: без них цифру нельзя объяснить.
RAW_RETENTION_DAYS = 45


@celery_app.task(name="tmo.watch_collection_progress", time_limit=300)
def watch_collection_progress() -> dict[str, Any]:
    """Сторож застоя.

    Проверяет не «отвечает ли воркер», а **разбирается ли очередь**. Пинг
    приходит из главного процесса и проходит исправно тогда, когда все
    процессы пула заняты намертво.
    """
    settings = get_settings()
    threshold = timedelta(minutes=settings.stall_threshold_minutes)
    now = now_utc()

    with session_scope() as session:
        snapshot_id = session.scalar(
            select(models.MarketSnapshot.id)
            .where(models.MarketSnapshot.snapshot_date == snapshot_date_for())
            .order_by(models.MarketSnapshot.attempt_no.desc())
            .limit(1)
        )
        if snapshot_id is None:
            return {"status": "NO_SNAPSHOT"}

        pending = session.scalar(
            select(func.count(models.CollectionJob.id)).where(
                models.CollectionJob.snapshot_id == snapshot_id,
                models.CollectionJob.status.in_(
                    [JobStatus.PLANNED.value, JobStatus.DISPATCHED.value, JobStatus.RUNNING.value]
                ),
            )
        )
        if not pending:
            # Пустая очередь — это здоровье: разбирать нечего, молчание законно.
            return {"status": "IDLE", "snapshot_id": snapshot_id, "pending": 0}

        last_completion = session.scalar(
            select(func.max(models.CollectionJob.completed_at)).where(
                models.CollectionJob.snapshot_id == snapshot_id
            )
        )

    if last_completion is None:
        return {"status": "NO_PROGRESS_YET", "snapshot_id": snapshot_id, "pending": pending}

    idle_for = now - last_completion
    stalled = idle_for > threshold
    if stalled:
        logger.error(
            "Сбор стоит: очередь не пуста, завершений нет",
            snapshot_id=snapshot_id,
            pending=pending,
            idle_minutes=round(idle_for.total_seconds() / 60, 1),
        )
        celery_app.control.pool_restart(reload=False, destination=None)
    return {
        "status": "STALLED" if stalled else "PROGRESSING",
        "snapshot_id": snapshot_id,
        "pending": pending,
        "idle_minutes": round(idle_for.total_seconds() / 60, 1),
    }


@celery_app.task(name="tmo.purge_raw", time_limit=3600)
def purge_raw(retention_days: int = RAW_RETENTION_DAYS) -> dict[str, Any]:
    """Удаляет тела сырых ответов старше горизонта хранения.

    Удаляются только тела. Метаданные ``raw_responses``, ``offers`` и метрики
    остаются: исторические наблюдения не удаляются.
    """
    cutoff = date.today() - timedelta(days=retention_days)
    removed = RawStore().purge_before(cutoff)
    logger.info("Ретеншен сырых ответов", cutoff=cutoff.isoformat(), removed_files=removed)
    return {"cutoff": cutoff.isoformat(), "removed_files": removed}


@celery_app.task(name="tmo.daily_quality_digest", time_limit=900)
def daily_quality_digest(snapshot_date: str | None = None) -> dict[str, Any]:
    """Сводка за сутки.

    Прогон, потерявший больше пяти процентов наблюдений, обязан быть виден без
    чтения логов.
    """
    from tmo.services.coverage import compute_coverage
    from tmo.services.showcase import snapshot_overview

    target = date.fromisoformat(snapshot_date) if snapshot_date else snapshot_date_for()
    with session_scope() as session:
        snapshot = session.scalars(
            select(models.MarketSnapshot)
            .where(models.MarketSnapshot.snapshot_date == target)
            .order_by(models.MarketSnapshot.attempt_no.desc())
            .limit(1)
        ).first()
        if snapshot is None:
            return {"status": "NO_SNAPSHOT", "snapshot_date": target.isoformat()}
        coverage = compute_coverage(session, snapshot.id)
        overview = snapshot_overview(session, snapshot)

    digest = {
        "snapshot_date": target.isoformat(),
        "status": str(snapshot.status),
        "coverage": coverage.as_dict(),
        "sources": overview["sources"],
        "publication_notes": overview["snapshot"]["publication_notes"],
    }
    loss = 1.0 - coverage.total.completion
    if loss > 0.05:
        logger.error("Прогон потерял более 5 % наблюдений", loss=round(loss, 4), **{
            "snapshot_date": target.isoformat()
        })
    else:
        logger.info("Суточная сводка", loss=round(loss, 4), snapshot_date=target.isoformat())
    return digest
