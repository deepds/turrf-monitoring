"""Суточный цикл как состояние, а не как расписание.

Прежняя модель раскладывала цикл по часам: авиа в 01:00, ЖД в 06:15, проживание
в 06:30, досбор в 08:00, расчёт в 09:00. Часы задавали намерение, но ничего не
гарантировали, и ночь 08.08.2026 показала, чем это кончается:

* затянувшийся сбор авиа не задерживал сбор проживания, а шёл **рядом** с ним:
  воркер держит восемь потоков, и каждая задача заводила свой пул из двенадцати;
* досбор в 08:00 уходил на второй процесс со своим лимитером темпа и своим
  размыкателем — источник получал двойную норму;
* расчёт в 09:00 стартовал независимо от того, закончен ли сбор, и считал по
  данным, которые в этот момент ещё дописывались.

Здесь цикл описан состоянием снимка, а часы остались только у его начала.
Диспетчер спрашивает «что сделано» и выдаёт **ровно один** следующий шаг:

.. code-block:: text

    нет снимка                   → OPEN
    семейство не обошли ни разу  → COLLECT(семейство)
    остались дыры                → RECOVER
    покрытие набрано / вышло время → CLOSE (расчёт и финализация)
    снимок закрыт                → IDLE

Из этого следуют три свойства, которых у расписания не было.

**Шаги не накладываются.** Следующий выдаётся, когда предыдущий отпустил
аренду, — а не когда наступил его час.

**Отказ воркера перестал стоить ночи.** Аренда истекает за минуты, диспетчер
выдаёт шаг заново, и сбор продолжается с того наблюдения, где остановился.

**Снимок закрывается по готовности.** Пока он не закрыт, витрина показывает
предыдущий полностью собранный день — это поведение ``latest_published`` и
менять его не потребовалось. Преждевременная финализация была бы хуже
задержки: она подменила бы вчерашний полный снимок сегодняшним неполным.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from tmo.catalog.registry import methodology_profile
from tmo.core.enums import CollectionFamily, JobStatus, SnapshotStatus
from tmo.core.timeutil import MSK, now_utc
from tmo.db import models

#: Порядок обхода семейств. Авиа первым — не потому, что оно дороже, а потому
#: что оно единственное, чья цена измеримо зависит от часа: 22 секунды на
#: наблюдение ночью против 147 днём (замеры 08.08.2026). ЖД и проживание день
#: переносят, авиа — нет, поэтому ночное окно отдаётся ему целиком.
FAMILY_ORDER: tuple[str, ...] = (
    CollectionFamily.AIR.value,
    CollectionFamily.RAIL.value,
    CollectionFamily.HOTEL.value,
)

#: Шаги цикла.
STEP_OPEN = "OPEN"
STEP_COLLECT = "COLLECT"
STEP_RECOVER = "RECOVER"
STEP_CLOSE = "CLOSE"
STEP_IDLE = "IDLE"


@dataclass(frozen=True, slots=True)
class CycleStep:
    """Один следующий шаг и причина, по которой он именно такой.

    Причина хранится вместе с шагом намеренно: диспетчер срабатывает раз в
    несколько минут, и без неё разбор ночи сводится к угадыванию, почему в
    04:00 шёл досбор, а не сбор.
    """

    step: str
    reason: str
    snapshot_id: int | None = None
    family: str | None = None
    details: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "step": self.step,
            "reason": self.reason,
            "snapshot_id": self.snapshot_id,
            "family": self.family,
            **(self.details or {}),
        }


def day_deadline(snapshot_date: date, *, hour: int = 23) -> datetime:
    """Момент, после которого снимок закрывается тем, что есть.

    Не 10:00: SLA публикации снят сознательно. Витрина показывает последний
    **полностью собранный** день, поэтому незакрытый сегодняшний снимок никому
    не мешает, а лишний час сбора закрывает дыры, которых иначе не закрыть.

    Рубеж всё же нужен, и не ради красоты: снимок обязан закрыться внутри своих
    календарных суток. Дата снимка календарная, и цикл, переехавший за полночь,
    развёл бы семейства по разным снимкам.
    """
    return datetime.combine(snapshot_date, time(hour=hour), tzinfo=MSK)


def _untouched_by_family(session: Session, snapshot_id: int) -> dict[str, int]:
    """Наблюдения, к которым сбор не подходил ни разу.

    Признак — пустой ``first_dispatched_at``. Он и отделяет первичный обход от
    досбора: после первичного обхода нетронутых не остаётся, а остаются дыры.
    """
    rows = session.execute(
        select(models.CollectionJob.family, func.count(models.CollectionJob.id))
        .where(
            models.CollectionJob.snapshot_id == snapshot_id,
            models.CollectionJob.first_dispatched_at.is_(None),
        )
        .group_by(models.CollectionJob.family)
    ).all()
    return {str(family): int(count) for family, count in rows}


def _holes_count(
    session: Session,
    snapshot_id: int,
    *,
    max_attempts: int | None = None,
    attempted_only: bool = False,
) -> int:
    """Наблюдения, подлежащие сбору.

    ``attempted_only`` отделяет два разных состояния, которые в статусе
    выглядят одинаково. Наблюдение, к которому сбор не подходил ни разу, и
    наблюдение, которое пробовали и не получилось, оба числятся ``PLANNED`` —
    но первое означает «очередь ещё не дошла», а второе «источник не ответил».

    Для решения диспетчера разницы нет: собрать надо и то и другое. Для
    человека разница вся: в начале суток несобранных ровно столько, сколько в
    плане, и показывать это число как пропуски значит объявлять аварией
    нормальное начало работы.
    """
    query = select(func.count(models.CollectionJob.id)).where(
        models.CollectionJob.snapshot_id == snapshot_id,
        models.CollectionJob.status.in_([status.value for status in JobStatus if status.is_hole]),
    )
    if attempted_only:
        query = query.where(models.CollectionJob.first_dispatched_at.is_not(None))
    if max_attempts is not None:
        query = query.where(models.CollectionJob.retry_count < max_attempts)
    return int(session.scalar(query) or 0)


def answered_share(session: Session, snapshot_id: int) -> dict[str, float]:
    """Доля наблюдений, получивших ответ о рынке, по семействам и в целом.

    **Не то же самое, что ``coverage.completion``.** Покрытие считает
    завершённым и ``FAILED``: для публикации это верно — наблюдение обработано,
    его судьба известна и видна в отчёте. Для решения «собирать дальше или
    хватит» это неверно ровно наоборот: отказ — это то, ради чего досбор и
    существует. Снимок, где всё до единого наблюдения провалилось, имеет
    стопроцентное покрытие и нулевую ценность, и остановиться на нём значило бы
    объявить готовым сбор, не собравший ничего.

    ``NO_MARKET`` сюда входит: «предложений нет» — это ответ о рынке, а не сбой,
    и добирать Самару — Казань, где прямого сообщения не существует, значило бы
    тратить обращения впустую каждые сутки.
    """
    rows = session.execute(
        select(
            models.CollectionJob.family,
            models.CollectionJob.status,
            func.count(models.CollectionJob.id),
        )
        .where(models.CollectionJob.snapshot_id == snapshot_id)
        .group_by(models.CollectionJob.family, models.CollectionJob.status)
    ).all()

    answered: dict[str, int] = {}
    planned: dict[str, int] = {}
    for family, status, count in rows:
        key = str(family)
        planned[key] = planned.get(key, 0) + count
        planned["TOTAL"] = planned.get("TOTAL", 0) + count
        if JobStatus(str(status)).is_hole:
            continue
        answered[key] = answered.get(key, 0) + count
        answered["TOTAL"] = answered.get("TOTAL", 0) + count

    return {
        key: (answered.get(key, 0) / total if total else 0.0) for key, total in planned.items()
    }


def coverage_meets_ready(
    session: Session, snapshot_id: int, *, profile_version: str | None = None
) -> bool:
    """Набрано ли покрытие, при котором дальше собирать незачем.

    Пороги берутся из профиля методики — те же, по которым потом судят ворота.
    Иметь здесь собственную константу значило бы собирать до порога, которого
    публикация не спросит, или остановиться раньше, чем она потребует.

    Считается по доле **отвеченных**, а не завершённых: см. ``answered_share``.
    Планка от этого строже той, что спросят ворота, и это намеренно — цикл
    целится выше порога, а судит порог.
    """
    profile = methodology_profile(profile_version)
    shares = answered_share(session, snapshot_id)
    if not shares:
        return False
    if shares.get("TOTAL", 0.0) < profile.ready_completion():
        return False
    for family in CollectionFamily:
        if family.value not in shares:
            continue
        if shares[family.value] < profile.ready_completion(family):
            return False
    return True


def next_step(
    session: Session,
    snapshot_date: date,
    *,
    now: datetime | None = None,
    max_job_attempts: int | None = None,
    profile_version: str | None = None,
) -> CycleStep:
    """Что делать прямо сейчас. Ровно один шаг, без побочных действий."""
    now = now or now_utc()

    snapshot = session.scalars(
        select(models.MarketSnapshot)
        .where(models.MarketSnapshot.snapshot_date == snapshot_date)
        .order_by(models.MarketSnapshot.attempt_no.desc())
        .limit(1)
    ).first()

    if snapshot is None:
        return CycleStep(step=STEP_OPEN, reason="Снимка за операционные сутки ещё нет")

    if SnapshotStatus(snapshot.status).is_terminal:
        return CycleStep(
            step=STEP_IDLE,
            reason=f"Снимок закрыт со статусом {snapshot.status}",
            snapshot_id=snapshot.id,
        )

    deadline = day_deadline(snapshot_date)
    out_of_time = now >= deadline

    untouched = _untouched_by_family(session, snapshot.id)
    if not out_of_time:
        for family in FAMILY_ORDER:
            pending = untouched.get(family, 0)
            if pending:
                return CycleStep(
                    step=STEP_COLLECT,
                    reason=f"Семейство {family} ещё не обходили: {pending} наблюдений",
                    snapshot_id=snapshot.id,
                    family=family,
                    details={"untouched": pending},
                )

    if coverage_meets_ready(session, snapshot.id, profile_version=profile_version):
        return CycleStep(
            step=STEP_CLOSE,
            reason="Покрытие набрано до порогов READY",
            snapshot_id=snapshot.id,
        )

    if out_of_time:
        return CycleStep(
            step=STEP_CLOSE,
            reason=f"Операционные сутки истекли в {deadline.isoformat()}",
            snapshot_id=snapshot.id,
            details={"holes": _holes_count(session, snapshot.id)},
        )

    holes = _holes_count(session, snapshot.id, max_attempts=max_job_attempts)
    if holes:
        return CycleStep(
            step=STEP_RECOVER,
            reason=f"Осталось дыр: {holes}",
            snapshot_id=snapshot.id,
            details={"holes": holes},
        )

    # Дыры есть, но все исчерпали лимит попыток: добирать больше нечего, и
    # держать снимок открытым до ночи незачем. Он закроется тем, что собрано, и
    # ворота скажут, годится ли это.
    exhausted = _holes_count(session, snapshot.id)
    return CycleStep(
        step=STEP_CLOSE,
        reason=(
            "Дыр, подлежащих досбору, не осталось"
            if not exhausted
            else f"Оставшиеся {exhausted} дыр исчерпали лимит попыток"
        ),
        snapshot_id=snapshot.id,
        details={"holes": exhausted},
    )


def time_until_deadline(snapshot_date: date, *, now: datetime | None = None) -> timedelta:
    return day_deadline(snapshot_date) - (now or now_utc())


def progress(
    session: Session, snapshot_date: date | None = None, *, now: datetime | None = None
) -> dict[str, Any] | None:
    """Что происходит со снимком за текущие сутки. ``None``, если его нет.

    Нужно витрине, и именно из-за новой модели. Пока цикл шёл по часам и
    заканчивался к 10:00, вопрос «что там сейчас» не возникал: к моменту, когда
    на витрину смотрели, всё было либо опубликовано, либо провалено. Теперь
    незакрытый снимок — нормальное состояние двадцати двух часов в сутки, и
    отличать «идёт сбор» от «провалилось» обязана витрина, а не журнал воркера.
    """
    now = now or now_utc()
    snapshot_date = snapshot_date or now.astimezone(MSK).date()

    snapshot = session.scalars(
        select(models.MarketSnapshot)
        .where(models.MarketSnapshot.snapshot_date == snapshot_date)
        .order_by(models.MarketSnapshot.attempt_no.desc())
        .limit(1)
    ).first()
    if snapshot is None:
        return None

    shares = answered_share(session, snapshot.id)
    step = next_step(session, snapshot_date, now=now)
    deadline = day_deadline(snapshot_date)

    planned = int(
        session.scalar(
            select(func.count(models.CollectionJob.id)).where(
                models.CollectionJob.snapshot_id == snapshot.id
            )
        )
        or 0
    )
    remaining = _holes_count(session, snapshot.id)

    return {
        "snapshot_id": snapshot.id,
        "snapshot_date": snapshot_date.isoformat(),
        "attempt_no": snapshot.attempt_no,
        "status": str(snapshot.status),
        "is_closed": SnapshotStatus(snapshot.status).is_terminal,
        "step": step.step,
        "step_reason": step.reason,
        "step_family": step.family,
        "answered": {key: round(value, 4) for key, value in shares.items()},
        "planned": planned,
        #: Получили ответ. То же, что доля `answered`, но числом.
        "answered_count": planned - remaining,
        #: Осталось собрать — вместе с теми, до кого очередь не дошла.
        "remaining": remaining,
        #: Пробовали и не получилось. Только это и есть пропуски.
        "holes": _holes_count(session, snapshot.id, attempted_only=True),
        "deadline": deadline.isoformat(),
        "minutes_left": max(0, int((deadline - now).total_seconds() // 60)),
    }
