"""Суточный цикл: диспетчер, настойчивость досбора, регулятор одновременности.

Прежняя модель раскладывала цикл по часам, и проверять в ней надо было
арифметику окон: успевает ли авиа до старта проживания, влезает ли досбор между
сбором и расчётом. Окна ушли — шаги выдаются по состоянию, — и проверять надо
другое: тот ли шаг выдаётся, останавливается ли настойчивость там, где должна,
и находит ли регулятор рабочую точку, вместо того чтобы упираться в потолок.

Числа берутся из тех же мест, что и продакшен: стоимость наблюдения из
``pipeline.OBSERVATION_COST``, размер матрицы из планировщика, пороги из профиля
методики. Тест, повторяющий константу у себя, проверял бы сам себя.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from tmo.catalog.registry import methodology_profile
from tmo.core.enums import CollectionFamily, JobStatus, SnapshotStatus
from tmo.core.timeutil import MSK
from tmo.db import models
from tmo.planner.matrix import expected_size
from tmo.services import cycle
from tmo.services.pipeline import seconds_per_observation

DAY = date(2026, 8, 9)


# --------------------------------------------------------------------------- #
# Обвязка
# --------------------------------------------------------------------------- #


def _snapshot(session, *, status=SnapshotStatus.COLLECTING, day: date = DAY):
    from tmo.core.timeutil import now_utc

    snapshot = models.MarketSnapshot(
        snapshot_date=day,
        attempt_no=1,
        status=status,
        horizon_days=30,
        created_at=now_utc(),
    )
    session.add(snapshot)
    session.flush()
    return snapshot


def _job(session, snapshot, family, *, status=JobStatus.PLANNED, touched=False, retries=0):
    from tmo.core.timeutil import now_utc

    index = session.query(models.CollectionJob).count()
    job = models.CollectionJob(
        snapshot_id=snapshot.id,
        job_key=f"{family}-{index}",
        series_key=f"{family}-series",
        family=family,
        status=status,
        service_date=DAY,
        retry_count=retries,
        first_dispatched_at=now_utc() if touched else None,
    )
    session.add(job)
    session.flush()
    return job


# --------------------------------------------------------------------------- #
# Диспетчер выдаёт ровно один шаг, и именно тот
# --------------------------------------------------------------------------- #


def test_missing_snapshot_is_opened(session) -> None:
    step = cycle.next_step(session, DAY)
    assert step.step == cycle.STEP_OPEN


def test_air_is_collected_before_the_cheap_families(session) -> None:
    """Авиа идёт первым: оно единственное, чья цена зависит от часа.

    22 секунды на наблюдение ночью против 147 днём. ЖД и проживание день
    переносят, авиа — нет, поэтому ночное окно принадлежит ему.
    """
    snapshot = _snapshot(session)
    for family in ("RAIL", "HOTEL", "AIR"):
        _job(session, snapshot, family)

    step = cycle.next_step(session, DAY)
    assert step.step == cycle.STEP_COLLECT
    assert step.family == CollectionFamily.AIR.value


def test_families_follow_one_another_without_overlap(session) -> None:
    """Следующее семейство выдаётся, только когда предыдущее обойдено.

    Раньше их разносили по часам, и затянувшееся семейство не задерживало
    следующее, а шло рядом: каждая задача заводила свой пул, и источник получал
    кратную одновременность.
    """
    snapshot = _snapshot(session)
    air = _job(session, snapshot, "AIR")
    _job(session, snapshot, "RAIL")

    assert cycle.next_step(session, DAY).family == "AIR"

    air.status = JobStatus.SUCCESS
    air.first_dispatched_at = datetime.now(MSK)
    session.flush()

    assert cycle.next_step(session, DAY).family == "RAIL"


def test_touched_family_moves_to_recovery_not_to_a_second_pass(session) -> None:
    """Обойдённое семейство добирается досбором, а не собирается заново.

    Разница не косметическая: первичный сбор идёт по всему семейству, досбор —
    по дырам. Второй первичный проход потратил бы обращения на наблюдения,
    которые уже закрыты.
    """
    snapshot = _snapshot(session)
    _job(session, snapshot, "AIR", status=JobStatus.SUCCESS, touched=True)
    _job(session, snapshot, "AIR", status=JobStatus.FAILED, touched=True)

    step = cycle.next_step(session, DAY)
    assert step.step == cycle.STEP_RECOVER
    assert step.details["holes"] == 1


def test_recovery_repeats_while_holes_remain(session) -> None:
    """Настойчивость обеспечивает диспетчер, а не цикл внутри задачи.

    Задача, крутящаяся до победы, держит состояние в памяти процесса: перезапуск
    воркера теряет его целиком, а брокер возвращает потерянную задачу только по
    истечении своего окна видимости — через двадцать часов.
    """
    snapshot = _snapshot(session)
    hole = _job(session, snapshot, "AIR", status=JobStatus.FAILED, touched=True)

    assert cycle.next_step(session, DAY).step == cycle.STEP_RECOVER
    assert cycle.next_step(session, DAY).step == cycle.STEP_RECOVER

    hole.status = JobStatus.SUCCESS
    session.flush()
    assert cycle.next_step(session, DAY).step == cycle.STEP_CLOSE


def test_hole_that_exhausted_its_attempts_stops_being_retried(session) -> None:
    """«До результата» и «бесконечно» — разные вещи.

    Наблюдение, на котором источник спотыкается систематически, при
    неограниченном повторе съедает обращения, нужные остальным, и держит снимок
    открытым до полуночи ради дыры, которая не закроется.
    """
    snapshot = _snapshot(session)
    _job(session, snapshot, "AIR", status=JobStatus.FAILED, touched=True, retries=99)

    step = cycle.next_step(session, DAY, max_job_attempts=12)
    assert step.step == cycle.STEP_CLOSE
    assert "исчерпали лимит попыток" in step.reason


def test_snapshot_of_pure_failures_is_not_ready(session) -> None:
    """Отказ — это не собранное наблюдение, сколько бы их ни было.

    В покрытии ``FAILED`` считается завершённым, и для публикации это верно:
    судьба наблюдения известна и видна в отчёте. Для решения «собирать дальше
    или хватит» — неверно ровно наоборот. Снимок, где провалилось всё до
    единого наблюдения, имеет стопроцентное покрытие и нулевую ценность, и
    остановиться на нём значило бы объявить готовым сбор, не собравший ничего.
    """
    snapshot = _snapshot(session)
    for _ in range(10):
        _job(session, snapshot, "AIR", status=JobStatus.FAILED, touched=True)

    assert not cycle.coverage_meets_ready(session, snapshot.id)
    assert cycle.next_step(session, DAY).step == cycle.STEP_RECOVER


def test_empty_market_is_an_answer_not_a_hole(session) -> None:
    """«Предложений нет» закрывает наблюдение.

    Добирать Самару — Казань, где прямого сообщения не существует, значило бы
    тратить обращения впустую каждые сутки.
    """
    snapshot = _snapshot(session)
    for family in ("AIR", "RAIL", "HOTEL"):
        _job(session, snapshot, family, status=JobStatus.NO_MARKET, touched=True)

    assert cycle.coverage_meets_ready(session, snapshot.id)


def test_full_coverage_closes_the_snapshot_without_waiting_for_the_deadline(session) -> None:
    snapshot = _snapshot(session)
    for family in ("AIR", "RAIL", "HOTEL"):
        _job(session, snapshot, family, status=JobStatus.SUCCESS, touched=True)

    step = cycle.next_step(session, DAY)
    assert step.step == cycle.STEP_CLOSE
    assert "READY" in step.reason


def test_deadline_closes_the_snapshot_with_what_was_collected(session) -> None:
    """После рубежа суток снимок закрывается тем, что есть.

    Незакрытый снимок витрине не мешает — она показывает последний полностью
    собранный день, — но и висеть он не должен: дата снимка календарная, и цикл,
    переехавший за полночь, развёл бы семейства по разным снимкам.
    """
    snapshot = _snapshot(session)
    _job(session, snapshot, "AIR")  # нетронутое: в обычный час пошёл бы сбор

    after = cycle.day_deadline(DAY) + timedelta(minutes=1)
    step = cycle.next_step(session, DAY, now=after)
    assert step.step == cycle.STEP_CLOSE
    assert "истекли" in step.reason


def test_closed_snapshot_asks_for_nothing(session) -> None:
    for status in (SnapshotStatus.READY, SnapshotStatus.DEGRADED, SnapshotStatus.FAILED):
        session.query(models.MarketSnapshot).delete()
        _snapshot(session, status=status)
        assert cycle.next_step(session, DAY).step == cycle.STEP_IDLE


def test_cycle_closes_inside_its_own_calendar_day() -> None:
    """Рубеж суток обязан лежать внутри суток снимка.

    Дата снимка календарная и одна на все шаги цикла. Рубеж, уехавший за
    полночь, развёл бы семейства по разным снимкам — каждый оказался бы неполным
    при полностью собранных данных.
    """
    deadline = cycle.day_deadline(DAY)
    assert deadline.astimezone(MSK).date() == DAY
    assert deadline < datetime.combine(
        DAY + timedelta(days=1), datetime.min.time(), tzinfo=MSK
    )


# --------------------------------------------------------------------------- #
# Витрина под новую модель
# --------------------------------------------------------------------------- #


def test_progress_tells_collection_apart_from_failure(session) -> None:
    """«Идёт сбор» и «провалилось» обязаны различаться в ответе API.

    Пока цикл шёл по часам и заканчивался к 10:00, разницы не требовалось: к
    моменту, когда на витрину смотрели, всё было решено. Теперь незакрытый
    снимок — нормальное состояние двадцати двух часов в сутки, и читать его как
    провал значит пугать пользователя штатной работой.
    """
    snapshot = _snapshot(session, status=SnapshotStatus.COLLECTING)
    _job(session, snapshot, "AIR", status=JobStatus.SUCCESS, touched=True)
    _job(session, snapshot, "AIR", status=JobStatus.FAILED, touched=True)

    state = cycle.progress(session, DAY)
    assert state is not None
    assert state["is_closed"] is False
    assert state["step"] == cycle.STEP_RECOVER
    assert state["answered"]["AIR"] == 0.5
    assert state["holes"] == 1

    snapshot.status = SnapshotStatus.FAILED
    session.flush()
    assert cycle.progress(session, DAY)["is_closed"] is True


def test_progress_is_absent_before_the_day_opens(session) -> None:
    """Снимка за сегодня ещё нет — это состояние ночи до 00:30, а не ошибка."""
    assert cycle.progress(session, DAY) is None


def test_versions_of_one_date_are_listed_separately(session) -> None:
    """Попытки одной даты обязаны быть различимы в витрине.

    За одной датой может стоять и собранный здесь снимок, и импортированный со
    стороннего стенда. Прежний список отдавал одну строку на дату, и выбрать
    между ними было нельзя — показывалась молча последняя.
    """
    from tmo.core.timeutil import now_utc
    from tmo.services.snapshot import available_snapshot_dates

    for attempt in (1, 2):
        session.add(
            models.MarketSnapshot(
                snapshot_date=DAY,
                attempt_no=attempt,
                status=SnapshotStatus.READY,
                horizon_days=30,
                coverage_total=0.9 + attempt / 100,
                created_at=now_utc(),
            )
        )
    session.flush()

    listed = available_snapshot_dates(session)
    assert len(listed) == 1, "дата обязана быть одной строкой"
    assert [v["label"] for v in listed[0]["versions"]] == ["v2", "v1"]
    assert listed[0]["attempt_no"] == 2, "по умолчанию — последняя попытка"


def test_version_can_be_selected_explicitly(session) -> None:
    from tmo.core.timeutil import now_utc
    from tmo.services.snapshot import snapshot_for_date

    for attempt in (1, 2):
        session.add(
            models.MarketSnapshot(
                snapshot_date=DAY,
                attempt_no=attempt,
                status=SnapshotStatus.READY,
                horizon_days=30,
                created_at=now_utc(),
            )
        )
    session.flush()

    assert snapshot_for_date(session, DAY).attempt_no == 2
    assert snapshot_for_date(session, DAY, attempt_no=1).attempt_no == 1
    assert snapshot_for_date(session, DAY, attempt_no=7) is None


# --------------------------------------------------------------------------- #
# Регулятор одновременности
# --------------------------------------------------------------------------- #


def _governor(**kwargs):
    from tmo.connectors.transport import AdaptiveConcurrency

    return AdaptiveConcurrency(**{"ceiling": 12, "floor": 2, "growth_after": 4, **kwargs})


def test_governor_starts_at_the_ceiling() -> None:
    """Потолок — рабочая точка по умолчанию, а не цель для подъёма.

    Ночь начинается с неё, и если источник её держит, регулятор не вмешивается.
    """
    assert _governor().current == 12


def test_failure_halves_concurrency() -> None:
    """Падение вдвое, а рост по единице: вернуть скорость дешевле, чем ронять."""
    governor = _governor()
    governor.record_failure()
    assert governor.current == 6


def test_simultaneous_failures_count_once() -> None:
    """Двенадцать потоков, узнавших об одном 503, — это один отказ.

    Без этого залп параллельных отказов уронил бы одновременность с потолка до
    пола за один приём, хотя источнику плохо один раз.
    """
    governor = _governor()
    for _ in range(12):
        governor.record_failure()
    assert governor.current == 6


def test_governor_never_falls_below_the_floor() -> None:
    """Перестать спрашивать вовсе — работа размыкателя, а не регулятора."""
    governor = _governor(floor=2)
    for _ in range(10):
        governor.record_failure()
        governor._last_decrease = None  # следующий отказ — новая серия
    assert governor.current == 2


def test_clean_series_returns_concurrency_towards_the_ceiling() -> None:
    governor = _governor(growth_after=4)
    governor.record_failure()
    assert governor.current == 6

    for _ in range(4):
        governor.record_success()
    assert governor.current == 7


def test_growth_stops_at_the_ceiling() -> None:
    """Реестр источников задаёт потолок: регулятор ищет точку под ним."""
    governor = _governor()
    for _ in range(1000):
        governor.record_success()
    assert governor.current == 12


def test_batch_size_follows_the_working_point() -> None:
    """Пачка обязана считаться по фактической одновременности, а не по потолку.

    Пачка на 80 авианаблюдений при потолке 12 требует 229 секунд из бюджета в
    240. Та же пачка при осевшей до двух рабочей точке требует 1370 — её хвост
    целиком уходит в ``BUDGET_EXHAUSTED``, то есть в дыры.
    """
    from tmo.connectors.transport import TRANSPORT_POOL
    from tmo.services.pipeline import batch_size_for_family

    TRANSPORT_POOL.reset()
    try:
        at_ceiling = batch_size_for_family("AIR")
        TRANSPORT_POOL.governor("tutu_mcp", 12, floor=1)
        for _ in range(4):
            TRANSPORT_POOL.governor("tutu_mcp", 12, floor=1).record_failure()
            TRANSPORT_POOL.governor("tutu_mcp", 12, floor=1)._last_decrease = None
        degraded = batch_size_for_family("AIR")
    finally:
        TRANSPORT_POOL.reset()

    assert degraded < at_ceiling, (
        f"при осевшей рабочей точке пачка осталась прежней: {degraded} против {at_ceiling}"
    )


# --------------------------------------------------------------------------- #
# Арифметика суток
# --------------------------------------------------------------------------- #


def _family_hours(family: str) -> float:
    return expected_size()[family] * seconds_per_observation(family) / 3600.0


def test_matrix_fits_the_operational_day() -> None:
    """Вся матрица обязана помещаться в сутки при исправном источнике.

    Это и есть смысл снятого срока в 10:00. В прежнем окне 01:00–09:00 матрица
    не помещалась: 9,3 часа последовательной работы против восьми часов окна, —
    и никакая настройка этого не исправляла.
    """
    needed = sum(_family_hours(f.value) for f in CollectionFamily)
    window = (cycle.day_deadline(DAY) - datetime.combine(
        DAY, datetime.min.time(), tzinfo=MSK
    ).replace(hour=1)).total_seconds() / 3600.0

    assert needed <= window, (
        f"матрице нужно {needed:.1f} ч, а от начала сбора до рубежа суток {window:.1f} ч"
    )


def test_hard_limit_of_a_family_covers_its_expected_run() -> None:
    """Жёсткий лимит задачи обязан быть больше ожидаемой работы.

    Лимит уничтожает несохранённое: он последняя страховка, а не расписание.
    """
    from tmo.tasks.collection import COLLECT_FAMILY_TIME_LIMIT

    longest = max(_family_hours(f.value) for f in CollectionFamily)
    assert longest * 3600 <= COLLECT_FAMILY_TIME_LIMIT, (
        f"самое долгое семейство идёт {longest:.1f} ч при лимите "
        f"{COLLECT_FAMILY_TIME_LIMIT / 3600:.1f} ч"
    )


@pytest.mark.parametrize("level", ["degraded", "ready"])
def test_publication_threshold_is_reachable_within_the_day(level: str) -> None:
    """Порог публикации обязан быть достижим за операционные сутки.

    Проверяется не «уложились ли вчера», а разрешает ли конструкция уложиться в
    принципе. Порог, недостижимый при полностью исправной системе, означал бы,
    что снимок не опубликуется никогда.
    """
    profile = methodology_profile("baseline_v1")
    share = (
        profile.degraded_completion(CollectionFamily.AIR)
        if level == "degraded"
        else profile.ready_completion(CollectionFamily.AIR)
    )
    needed = (
        _family_hours("AIR") * share + _family_hours("HOTEL") + _family_hours("RAIL")
    )
    window = 22.0  # 01:00 → 23:00 МСК

    assert needed <= window, (
        f"порог {level} требует {needed:.1f} ч сбора при сутках в {window:.0f} ч"
    )


@pytest.mark.parametrize("family", list(CollectionFamily))
def test_family_threshold_is_not_decorative(family: CollectionFamily) -> None:
    """Порог семейства обязан быть достижимой границей, а не украшением.

    Если просело **одно** семейство, ровно до своего порога, а остальные собраны
    полностью — снимок обязан пройти общий порог. Иначе семейный порог никогда не
    станет ограничителем и будет вводить в заблуждение.
    """
    profile = methodology_profile("baseline_v1")
    sizes = expected_size()

    collected = sum(
        sizes[member.value] * (profile.degraded_completion(member) if member is family else 1.0)
        for member in CollectionFamily
    )
    share = collected / sizes["TOTAL"]

    assert share >= profile.degraded_completion(), (
        f"{family.value} на своём пороге {profile.degraded_completion(family):.0%} "
        f"при полных остальных даёт {share:.1%} против общего "
        f"{profile.degraded_completion():.0%} — семейный порог недостижим"
    )
