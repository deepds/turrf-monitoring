"""Задачи сбора и расчёта.

Шаги цикла выдаёт диспетчер ``advance_snapshot`` — по состоянию снимка, а не по
часам. Расписание знает только один момент: когда открывать новые операционные
сутки. Всё остальное решает ``tmo.services.cycle``.

Каждый шаг работает под арендой (``tmo.tasks.lease``). Аренда делает две вещи,
которых расписание не делало: не даёт двум шагам идти одновременно и возвращает
шаг в работу, если его исполнитель умер.

У каждого шага свой мягкий бюджет и свой жёсткий таймаут, причём мягкий строго
меньше жёсткого: при исчерпании мягкого обход прекращается сам, собранное
сохраняется и помечается частичным. Жёсткий остаётся последней страховкой и в
норме не срабатывает — он уничтожает уже собранное.
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
from tmo.services import cycle
from tmo.services.calculation import calculate_snapshot as calculate_service
from tmo.services.coverage import find_holes
from tmo.services.pipeline import collect_jobs
from tmo.services.publication import finalize_snapshot as finalize_service
from tmo.services.snapshot import create_snapshot
from tmo.tasks import lease
from tmo.tasks.celery_app import celery_app

logger = get_logger(__name__)

#: Жёсткий предел одного захода досбора. Три часа: заход короткий, но упереться
#: он может в ту же стену, что и первичный сбор, — пятиминутные паузы остывания
#: цепи на дорогом семействе.
RECOVER_TIME_LIMIT = 3 * 3600

#: Статусы наблюдений, закрытых ответом о рынке. Повторный сбор их не касается.
COLLECTED_JOB_STATUSES = [status.value for status in JobStatus if status.is_collected]


def _settings():
    return get_settings()


def reclaim_stale_jobs(session, snapshot_id: int) -> int:
    """Возвращает в план наблюдения, брошенные умершим шагом.

    Наблюдение помечается ``RUNNING`` на время пачки и снимается с этой отметки
    при записи. Воркер, убитый посреди пачки, оставляет отметку навсегда: пачка
    не запишется никогда, а наблюдение числится выполняющимся.

    Дальше это ломает не сбор, а **лечение** — и ломает круговым образом.
    Проверка живости считает нездоровьем «в работе есть, завершений нет»;
    осиротевшие отметки дают ей ровно эту картину, autoheal перезапускает
    воркер, перезапуск роняет текущую пачку и добавляет новых сирот. Стенд
    meduza 08.08.2026 вошёл в этот круг и за полчаса не записал ни одной
    попытки: каждый перезапуск приходился на момент, когда пачка собиралась
    записаться.

    Вызывается под арендой шага. Аренда и делает операцию безопасной: пока она
    наша, другого сбора по этому снимку нет, и всё, что числится выполняющимся,
    — заведомо чужое наследство.
    """
    from sqlalchemy import update as sa_update

    result = session.execute(
        sa_update(models.CollectionJob)
        .where(
            models.CollectionJob.snapshot_id == snapshot_id,
            models.CollectionJob.status.in_(
                [JobStatus.RUNNING.value, JobStatus.DISPATCHED.value]
            ),
        )
        .values(status=JobStatus.PLANNED.value)
    )
    return int(result.rowcount or 0)


def _current_snapshot_id(session, snapshot_date: date | None = None) -> int | None:
    snapshot_date = snapshot_date or snapshot_date_for()
    return session.scalar(
        select(models.MarketSnapshot.id)
        .where(models.MarketSnapshot.snapshot_date == snapshot_date)
        .order_by(models.MarketSnapshot.attempt_no.desc())
        .limit(1)
    )


@celery_app.task(name="tmo.advance_snapshot", bind=True, time_limit=120)
def advance_snapshot(self, snapshot_date: str | None = None) -> dict[str, Any]:
    """Диспетчер цикла: выдаёт ровно один следующий шаг.

    Сам не собирает и не считает — только смотрит на состояние снимка и ставит
    в очередь то, что должно идти сейчас. Поэтому он короткий, живёт на
    свободном воркере обслуживания и не может застрять за тем, чем управляет.

    Повторный запуск безвреден: занятый шаг держит аренду и второй раз не
    возьмётся. Именно на этом держится восстановление после отказа — диспетчер
    срабатывает каждые несколько минут и подхватывает шаг, чей исполнитель умер,
    не дожидаясь, пока брокер вернёт потерянную задачу.
    """
    settings = _settings()
    target = date.fromisoformat(snapshot_date) if snapshot_date else snapshot_date_for()

    with session_scope() as session:
        step = cycle.next_step(
            session, target, max_job_attempts=settings.max_job_attempts
        )

    if step.step == cycle.STEP_IDLE:
        return step.as_dict()

    lease_name = {
        cycle.STEP_OPEN: f"open:{target.isoformat()}",
        cycle.STEP_COLLECT: f"collect:{target.isoformat()}:{step.family}",
        cycle.STEP_RECOVER: f"recover:{step.snapshot_id}",
        cycle.STEP_CLOSE: f"close:{step.snapshot_id}",
    }[step.step]

    if lease.is_held(lease_name):
        return {**step.as_dict(), "dispatched": False, "note": "Шаг уже выполняется"}

    dispatch = {
        cycle.STEP_OPEN: lambda: open_snapshot.delay(snapshot_date=target.isoformat()),
        cycle.STEP_COLLECT: lambda: collect_family.delay(
            family=step.family, snapshot_date=target.isoformat()
        ),
        cycle.STEP_RECOVER: lambda: recover_snapshot.delay(snapshot_id=step.snapshot_id),
        cycle.STEP_CLOSE: lambda: close_snapshot.delay(snapshot_id=step.snapshot_id),
    }[step.step]

    dispatch()
    logger.info("Шаг цикла поставлен в очередь", **step.as_dict())
    return {**step.as_dict(), "dispatched": True}


@celery_app.task(name="tmo.open_snapshot", bind=True, time_limit=900, soft_time_limit=600)
def open_snapshot(self, snapshot_date: str | None = None) -> dict[str, Any]:
    """Создаёт снимок и план на операционные сутки.

    Под арендой: открытие приходит из двух мест — из расписания в 00:30 и от
    диспетчера, обнаружившего, что снимка нет. Два одновременных открытия дали
    бы второй снимок с ``attempt_no=2`` и осиротевшим планом первого, а сбор
    пошёл бы по второму: ``_current_snapshot_id`` берёт наибольшую попытку.
    """
    target = date.fromisoformat(snapshot_date) if snapshot_date else snapshot_date_for()
    with lease.acquire(f"open:{target.isoformat()}") as held:
        if held is None:
            logger.info("Снимок уже открывается: шаг пропущен", snapshot_date=target.isoformat())
            return {"status": "ALREADY_RUNNING", "snapshot_date": target.isoformat()}
        return _open_snapshot(target)


def _open_snapshot(target: date) -> dict[str, Any]:
    with session_scope() as session:
        existing = _current_snapshot_id(session, target)
        if existing is not None:
            logger.info("Снимок за эти сутки уже открыт", snapshot_id=existing)
            return {"status": "ALREADY_OPEN", "snapshot_id": existing}
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
    """Собирает одно семейство наблюдений текущего снимка.

    Идёт под арендой: два сбора одного семейства — это удвоенная одновременность
    на источнике, а не удвоенная скорость.
    """
    target = date.fromisoformat(snapshot_date) if snapshot_date else snapshot_date_for()
    with lease.acquire(f"collect:{target.isoformat()}:{family}") as held:
        if held is None:
            logger.info("Сбор семейства уже идёт: шаг пропущен", family=family)
            return {"status": "ALREADY_RUNNING", "family": family}
        return _collect_family(family, target, held)


def _collect_family(family: str, target: date, held: lease.Lease) -> dict[str, Any]:
    with session_scope() as session:
        snapshot_id = _current_snapshot_id(session, target)
        if snapshot_id is None:
            creation = create_snapshot(session, snapshot_date=target)
            snapshot_id = creation.snapshot_id
        reclaimed = reclaim_stale_jobs(session, snapshot_id)
        if reclaimed:
            logger.warning(
                "Наблюдения, брошенные умершим шагом, возвращены в план",
                reclaimed=reclaimed,
                family=family,
            )
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
        totals = collect_jobs(
            job_ids,
            execution_scope="PRIMARY",
            family=family,
            deadline=cycle.day_deadline(target),
            on_progress=held.renew,
        )

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


@celery_app.task(name="tmo.recover_snapshot", bind=True, time_limit=RECOVER_TIME_LIMIT)
def recover_snapshot(self, snapshot_id: int | None = None, rounds: int = 1) -> dict[str, Any]:
    """Досбор технических дыр — один заход.

    Настойчивость обеспечивает не эта задача, а диспетчер: он выдаёт досбор
    снова и снова, пока дыры есть. Заход короткий намеренно. Задача, которая
    сама крутится до победы, — это состояние в памяти процесса: перезапуск
    воркера теряет его целиком, а брокер возвращает такую задачу только по
    истечении своего окна видимости, то есть через двадцать часов. Состояние в
    базе переживает и перезапуск, и потерю задачи.

    Идёт с солью в ключе идемпотентности: без неё повторное исполнение той же
    области молча вернуло бы прежний результат вместо новой попытки. Соль
    включает номер захода в пределах суток, поэтому каждый досбор — новая
    попытка, а не повтор прежней.
    """
    settings = _settings()
    with session_scope() as session:
        snapshot_id = snapshot_id or _current_snapshot_id(session)
        if snapshot_id is None:
            return {"status": "NO_SNAPSHOT"}
        snapshot = session.get(models.MarketSnapshot, snapshot_id)
        target = snapshot.snapshot_date

    with lease.acquire(f"recover:{snapshot_id}") as held:
        if held is None:
            logger.info("Досбор уже идёт: шаг пропущен", snapshot_id=snapshot_id)
            return {"status": "ALREADY_RUNNING", "snapshot_id": snapshot_id}

        with session_scope() as session:
            snapshot = session.get(models.MarketSnapshot, snapshot_id)
            snapshot.status = SnapshotStatus.RECOVERING
            reclaimed = reclaim_stale_jobs(session, snapshot_id)
            if reclaimed:
                logger.warning(
                    "Наблюдения, брошенные умершим шагом, возвращены в план",
                    reclaimed=reclaimed,
                )

        totals = {"rounds": 0, "attempts": 0, "offers": 0}
        with log_context(snapshot_id=snapshot_id):
            for attempt in range(1, rounds + 1):
                with session_scope() as session:
                    holes = find_holes(
                        session, snapshot_id, max_attempts=settings.max_job_attempts
                    )
                if not holes:
                    break
                logger.info("Досбор", round=attempt, holes=len(holes))
                result = collect_jobs(
                    holes,
                    execution_scope="RECOVERY",
                    attempt_salt=f"recovery-{now_utc().strftime('%H%M%S')}-{attempt}",
                    deadline=cycle.day_deadline(target),
                    on_progress=held.renew,
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


@celery_app.task(name="tmo.close_snapshot", bind=True, time_limit=3 * 3600)
def close_snapshot(self, snapshot_id: int | None = None,
                   profile_version: str | None = None) -> dict[str, Any]:
    """Расчёт и финализация одним шагом.

    Раздельные записи расписания на 09:00 и 09:30 разошлись с реальностью в
    первую же ночь: расчёт стартовал, когда сбор ещё шёл, а финализация — когда
    шёл уже расчёт. Между этими двумя действиями нет ничего, ради чего стоило бы
    рисковать промежутком: расчёт без финализации оставляет снимок в
    ``CALCULATING`` навсегда, а финализация без расчёта проваливает ворота
    ``NO_CALCULATION_RUN``.
    """
    with session_scope() as session:
        snapshot_id = snapshot_id or _current_snapshot_id(session)
        if snapshot_id is None:
            return {"status": "NO_SNAPSHOT"}

    with lease.acquire(f"close:{snapshot_id}") as held:
        if held is None:
            logger.info("Закрытие снимка уже идёт: шаг пропущен", snapshot_id=snapshot_id)
            return {"status": "ALREADY_RUNNING", "snapshot_id": snapshot_id}

        with log_context(snapshot_id=snapshot_id):
            with session_scope() as session:
                snapshot = session.get(models.MarketSnapshot, snapshot_id)
                snapshot.status = SnapshotStatus.CALCULATING
            with session_scope() as session:
                calculation = calculate_service(
                    session, snapshot_id, profile_version=profile_version
                )
            held.renew()
            with session_scope() as session:
                publication = finalize_service(session, snapshot_id)
        logger.info(
            "Снимок закрыт",
            snapshot_id=snapshot_id,
            status=publication.status,
            coverage_total=publication.coverage_total,
        )
        return {
            "snapshot_id": snapshot_id,
            "metrics": calculation.metrics,
            "trip_rows": calculation.trip_rows,
            "status": publication.status,
            "coverage_total": publication.coverage_total,
            "notes": [note["code"] for note in publication.notes],
        }


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
