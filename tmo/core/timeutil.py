"""Время и горизонт наблюдения.

Два разных времени, которые нельзя путать (SCOPE-R C4.7):

* ``snapshot_date`` / ``observed_at`` — бизнес-идентификатор наблюдения;
* ``fetched_at`` — фактический момент получения данных, от него считается
  свежесть.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta, timezone

#: Московское время: SLA публикации и граница операционных суток заданы в нём.
MSK = timezone(timedelta(hours=3), name="MSK")

#: Горизонт наблюдения: D+1 .. D+HORIZON_DAYS. Сегодня не входит (SCOPE-R O3).
HORIZON_DAYS = 30


def now_utc() -> datetime:
    return datetime.now(UTC)


def now_msk() -> datetime:
    return datetime.now(MSK)


def to_utc(value: datetime) -> datetime:
    """Приводит к UTC, считая наивное время московским.

    Наивные метки приходят от источников (РЖД отдаёт локальное время без
    смещения). Молча считать их UTC значило бы сдвинуть все поезда на три часа.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=MSK).astimezone(UTC)
    return value.astimezone(UTC)


def snapshot_date_for(moment: datetime | None = None) -> date:
    """Операционная дата снимка для момента времени.

    Дата календарная и одна на все задачи цикла — и это свойство расписания, а
    не совпадение. Цикл идёт с 00:30 до 10:00 МСК, целиком внутри одних суток.

    **Вынос любого сбора за полночь назад ломает это допущение**: семейства
    разойдутся по разным снимкам, и каждый окажется неполным при полностью
    собранных данных. Тогда понадобится вечерний рубеж — момент, начиная с
    которого цикл относится к следующим суткам. Такой рубеж здесь был, пока сбор
    авиа стоял на 21:00, и снят вместе с ним: неиспользуемое ветвление дат в
    системе, где расхождение дат уже стоило снимка, опаснее своего отсутствия.

    Границу стережёт ``tests/test_night_regressions.py``: расписание обязано
    укладываться в одни календарные сутки.
    """
    moment = moment or now_msk()
    return moment.astimezone(MSK).date()


def sla_deadline(snapshot_date: date, *, hour: int = 10) -> datetime:
    """Момент, к которому снимок должен быть опубликован (10:00 MSK)."""
    return datetime.combine(snapshot_date, time(hour=hour), tzinfo=MSK)


def horizon_dates(snapshot_date: date, *, days: int = HORIZON_DAYS) -> list[date]:
    """Служебные даты наблюдения: D+1 .. D+days."""
    return [snapshot_date + timedelta(days=offset) for offset in range(1, days + 1)]


def date_pairs(snapshot_date: date, *, days: int = HORIZON_DAYS) -> list[tuple[date, date]]:
    """Все пары (отправление, возвращение) внутри горизонта.

    ``return_date > departure_date``, обе внутри ``[D+1, D+days]``.
    Для 30 дней это C(30,2) = 435 пар.
    """
    dates = horizon_dates(snapshot_date, days=days)
    return [(dates[i], dates[j]) for i in range(len(dates)) for j in range(i + 1, len(dates))]


def nights_between(check_in: date, check_out: date) -> int:
    return (check_out - check_in).days


def age_minutes(fetched_at: datetime, *, reference: datetime | None = None) -> float:
    """Свежесть в минутах, считается только от ``fetched_at``."""
    reference = reference or now_utc()
    return (to_utc(reference) - to_utc(fetched_at)).total_seconds() / 60.0
