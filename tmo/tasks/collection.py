"""Задачи сбора и расчёта.

У каждого семейства свой мягкий бюджет и свой жёсткий таймаут, причём мягкий
строго меньше жёсткого: при исчерпании мягкого обход прекращается сам,
собранное сохраняется и помечается частичным. Жёсткий остаётся последней
страховкой и в норме не срабатывает — он уничтожает уже собранное.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import date
from typing import Any

from sqlalchemy import func, select

from tmo.core.config import get_settings
from tmo.core.enums import JobStatus, SnapshotStatus
from tmo.core.logging import get_logger, log_context
from tmo.core.timeutil import now_utc, snapshot_date_for
from tmo.db import models
from tmo.db.session import session_scope
from tmo.services.calculation import calculate_snapshot as calculate_service
from tmo.services.coverage import find_holes
from tmo.services.pipeline import collect_jobs
from tmo.services.publication import finalize_snapshot as finalize_service
from tmo.services.snapshot import create_snapshot
from tmo.tasks.celery_app import celery_app

logger = get_logger(__name__)

#: Статусы наблюдений, закрытых ответом о рынке. Повторный сбор их не касается.
COLLECTED_JOB_STATUSES = [status.value for status in JobStatus if status.is_collected]


def _settings():
    return get_settings()


def _current_snapshot_id(session, snapshot_date: date | None = None) -> int | None:
    snapshot_date = snapshot_date or snapshot_date_for()
    return session.scalar(
        select(models.MarketSnapshot.id)
        .where(models.MarketSnapshot.snapshot_date == snapshot_date)
        .order_by(models.MarketSnapshot.attempt_no.desc())
        .limit(1)
    )


@celery_app.task(name="tmo.open_snapshot", bind=True, time_limit=900, soft_time_limit=600)
def open_snapshot(self, snapshot_date: str | None = None) -> dict[str, Any]:
    """Создаёт снимок и план на операционные сутки."""
    target = date.fromisoformat(snapshot_date) if snapshot_date else snapshot_date_for()
    with session_scope() as session:
        creation = create_snapshot(session, snapshot_date=target)
    logger.info("Снимок открыт", snapshot_id=creation.snapshot_id, planned=creation.planned)
    return {
        "snapshot_id": creation.snapshot_id,
        "snapshot_date": creation.snapshot_date.isoformat(),
        "planned": creation.planned,
        "by_family": creation.by_family,
        "plan_digest": creation.plan_digest,
    }


#: Жёсткий предел сбора семейства. Десять часов, а не шесть: авиа при
#: одновременности 6 требует 8,2 часа, и прежний лимит убил бы задачу на
#: середине — с потерей того, что не успело записаться пачкой.
COLLECT_FAMILY_TIME_LIMIT = 10 * 3600


@celery_app.task(
    name="tmo.collect_family",
    bind=True,
    time_limit=COLLECT_FAMILY_TIME_LIMIT,
    soft_time_limit=COLLECT_FAMILY_TIME_LIMIT - 300,
)
def collect_family(self, family: str, snapshot_date: str | None = None) -> dict[str, Any]:
    """Собирает одно семейство наблюдений текущего снимка."""
    target = date.fromisoformat(snapshot_date) if snapshot_date else snapshot_date_for()
    with session_scope() as session:
        snapshot_id = _current_snapshot_id(session, target)
        if snapshot_id is None:
            creation = create_snapshot(session, snapshot_date=target)
            snapshot_id = creation.snapshot_id
        # Уже закрытые наблюдения повторному сбору не подлежат: обращения к
        # источнику потрачены бы впустую, а их результат всё равно отсекается
        # по ключу идемпотентности при записи. Дыры (FAILED) остаются в выборке
        # намеренно — их и должен закрыть повтор.
        job_ids = list(
            session.scalars(
                select(models.CollectionJob.id).where(
                    models.CollectionJob.snapshot_id == snapshot_id,
                    models.CollectionJob.family == family,
                    models.CollectionJob.status.notin_(COLLECTED_JOB_STATUSES),
                )
            )
        )
        planned_total = session.scalar(
            select(func.count(models.CollectionJob.id)).where(
                models.CollectionJob.snapshot_id == snapshot_id,
                models.CollectionJob.family == family,
            )
        )
        snapshot = session.get(models.MarketSnapshot, snapshot_id)
        snapshot.status = SnapshotStatus.COLLECTING

    with log_context(snapshot_id=snapshot_id):
        logger.info(
            "Сбор семейства",
            family=family,
            jobs=len(job_ids),
            already_collected=planned_total - len(job_ids),
        )
        totals = collect_jobs(job_ids, execution_scope="PRIMARY", family=family)

    # Ноль обращений при непустой выборке — это не успех. Так выглядит проход
    # по разомкнутой цепи: все пачки пропущены, окно семейства израсходовано,
    # задача рапортует успех. Разница обязана быть видимой и в логе, и в
    # результате задачи.
    collected_nothing = bool(job_ids) and not totals.get("attempts")
    if collected_nothing:
        logger.error(
            "Сбор семейства не сделал ни одного обращения",
            family=family,
            jobs=len(job_ids),
        )
    return {
        "snapshot_id": snapshot_id,
        "family": family,
        "status": "NOTHING_COLLECTED" if collected_nothing else "OK",
        **totals,
    }


@celery_app.task(name="tmo.collect_batch", bind=True, time_limit=None, soft_time_limit=None)
def collect_batch(self, job_ids: list[int], execution_scope: str = "PRIMARY",
                  attempt_salt: str = "") -> dict[str, Any]:
    """Одна пачка наблюдений. Жёсткий таймаут задаётся из настроек."""
    settings = _settings()
    self.request.timelimit = (settings.batch_hard_timeout_seconds, None)
    from tmo.execution.runner import run_batch

    report = run_batch(
        job_ids,
        execution_scope=execution_scope,
        attempt_salt=attempt_salt,
        soft_budget_seconds=settings.batch_soft_budget_seconds,
    )
    return asdict(report)


@celery_app.task(name="tmo.recover_snapshot", bind=True, time_limit=3 * 3600)
def recover_snapshot(self, snapshot_id: int | None = None, rounds: int = 2) -> dict[str, Any]:
    """Досбор технических дыр.

    Идёт с солью в ключе идемпотентности: без неё повторное исполнение той же
    области молча вернуло бы прежний результат вместо новой попытки.
    """
    with session_scope() as session:
        snapshot_id = snapshot_id or _current_snapshot_id(session)
        if snapshot_id is None:
            return {"status": "NO_SNAPSHOT"}
        snapshot = session.get(models.MarketSnapshot, snapshot_id)
        snapshot.status = SnapshotStatus.RECOVERING

    totals = {"rounds": 0, "attempts": 0, "offers": 0}
    with log_context(snapshot_id=snapshot_id):
        for attempt in range(1, rounds + 1):
            with session_scope() as session:
                holes = find_holes(session, snapshot_id)
            if not holes:
                break
            logger.info("Досбор", round=attempt, holes=len(holes))
            result = collect_jobs(
                holes, execution_scope="RECOVERY", attempt_salt=f"recovery-{attempt}"
            )
            totals["rounds"] = attempt
            totals["attempts"] += result["attempts"]
            totals["offers"] += result["offers"]

    with session_scope() as session:
        snapshot = session.get(models.MarketSnapshot, snapshot_id)
        snapshot.recovery_finished_at = now_utc()
    return {"snapshot_id": snapshot_id, **totals}


@celery_app.task(name="tmo.calculate_snapshot", bind=True, time_limit=2 * 3600)
def calculate_snapshot(self, snapshot_id: int | None = None,
                       profile_version: str | None = None) -> dict[str, Any]:
    with session_scope() as session:
        snapshot_id = snapshot_id or _current_snapshot_id(session)
        if snapshot_id is None:
            return {"status": "NO_SNAPSHOT"}
        snapshot = session.get(models.MarketSnapshot, snapshot_id)
        snapshot.status = SnapshotStatus.CALCULATING

    with session_scope() as session:
        report = calculate_service(session, snapshot_id, profile_version=profile_version)
    return asdict(report)


@celery_app.task(name="tmo.recalculate_snapshot", bind=True, time_limit=2 * 3600)
def recalculate_snapshot(self, snapshot_id: int,
                         profile_version: str | None = None,
                         make_active: bool = True) -> dict[str, Any]:
    """Пересчёт другой методикой. Прежний расчёт остаётся неизменным."""
    from tmo.services.pipeline import recalculate

    return recalculate(snapshot_id, profile_version=profile_version, make_active=make_active)


@celery_app.task(name="tmo.finalize_snapshot", bind=True, time_limit=1800)
def finalize_snapshot(self, snapshot_id: int | None = None) -> dict[str, Any]:
    with session_scope() as session:
        snapshot_id = snapshot_id or _current_snapshot_id(session)
        if snapshot_id is None:
            return {"status": "NO_SNAPSHOT"}
        result = finalize_service(session, snapshot_id)
    return {
        "snapshot_id": result.snapshot_id,
        "status": result.status,
        "coverage_total": result.coverage_total,
        "within_sla": result.within_sla,
        "notes": result.notes,
    }
