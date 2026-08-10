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

#: Момент внутри операционных суток снимка. Без него тесты, дошедшие до
#: проверки рубежа, зеленеют до 23:00 и краснеют после — а падение это
#: описывает не дефект, а время запуска.
MIDDAY = datetime(2026, 8, 9, 12, 0, tzinfo=MSK)


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
    step = cycle.next_step(session, DAY, now=MIDDAY)
    assert step.step == cycle.STEP_OPEN


def test_air_is_collected_before_the_cheap_families(session) -> None:
    """Авиа идёт первым: оно единственное, чья цена зависит от часа.

    22 секунды на наблюдение ночью против 147 днём. ЖД и проживание день
    переносят, авиа — нет, поэтому ночное окно принадлежит ему.
    """
    snapshot = _snapshot(session)
    for family in ("RAIL", "HOTEL", "AIR"):
        _job(session, snapshot, family)

    step = cycle.next_step(session, DAY, now=MIDDAY)
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

    assert cycle.next_step(session, DAY, now=MIDDAY).family == "AIR"

    air.status = JobStatus.SUCCESS
    air.first_dispatched_at = datetime.now(MSK)
    session.flush()

    assert cycle.next_step(session, DAY, now=MIDDAY).family == "RAIL"


def test_touched_family_moves_to_recovery_not_to_a_second_pass(session) -> None:
    """Обойдённое семейство добирается досбором, а не собирается заново.

    Разница не косметическая: первичный сбор идёт по всему семейству, досбор —
    по дырам. Второй первичный проход потратил бы обращения на наблюдения,
    которые уже закрыты.
    """
    snapshot = _snapshot(session)
    _job(session, snapshot, "AIR", status=JobStatus.SUCCESS, touched=True)
    _job(session, snapshot, "AIR", status=JobStatus.FAILED, touched=True)

    step = cycle.next_step(session, DAY, now=MIDDAY)
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

    assert cycle.next_step(session, DAY, now=MIDDAY).step == cycle.STEP_RECOVER
    assert cycle.next_step(session, DAY, now=MIDDAY).step == cycle.STEP_RECOVER

    hole.status = JobStatus.SUCCESS
    session.flush()
    assert cycle.next_step(session, DAY, now=MIDDAY).step == cycle.STEP_CLOSE


def test_hole_that_exhausted_its_attempts_stops_being_retried(session) -> None:
    """«До результата» и «бесконечно» — разные вещи.

    Наблюдение, на котором источник спотыкается систематически, при
    неограниченном повторе съедает обращения, нужные остальным, и держит снимок
    открытым до полуночи ради дыры, которая не закроется.
    """
    snapshot = _snapshot(session)
    _job(session, snapshot, "AIR", status=JobStatus.FAILED, touched=True, retries=99)

    step = cycle.next_step(session, DAY, now=MIDDAY, max_job_attempts=12)
    assert step.step == cycle.STEP_CLOSE
    assert "исчерпали лимит попыток" in step.reason


def test_readiness_waits_for_every_observation_to_reach_a_terminal_outcome(session) -> None:
    """Готовность обязана требовать того же, что и ворота полноты.

    В снимке 09.08.2026 отвеченных было 98,35 % при пороге 98 %, диспетчер
    закрыл сутки — а 31 наблюдение оставалось в работе, и ворота его не
    пропустили. Проверка, допускающая до ворот то, что они заведомо отвергнут,
    хуже отсутствующей: она тратит расчёт и закрывает сутки отказом.
    """
    snapshot = _snapshot(session)
    for _ in range(99):
        _job(session, snapshot, "AIR", status=JobStatus.SUCCESS, touched=True)
    in_flight = _job(session, snapshot, "AIR", status=JobStatus.RUNNING, touched=True)

    assert not cycle.coverage_meets_ready(session, snapshot.id), (
        "снимок с наблюдением в работе не может считаться готовым"
    )

    in_flight.status = JobStatus.SUCCESS
    session.flush()
    assert cycle.coverage_meets_ready(session, snapshot.id)


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
    assert cycle.next_step(session, DAY, now=MIDDAY).step == cycle.STEP_RECOVER


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

    step = cycle.next_step(session, DAY, now=MIDDAY)
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
        assert cycle.next_step(session, DAY, now=MIDDAY).step == cycle.STEP_IDLE


@pytest.mark.parametrize("status", [SnapshotStatus.READY, SnapshotStatus.DEGRADED])
def test_late_step_does_not_reopen_a_published_snapshot(database: str, status) -> None:
    """Шаг, пришедший мимо диспетчера, обязан отказаться сам.

    Проверка выше закрывает эту дыру со стороны того, кто раздаёт работу. Но
    шаг приходит не только оттуда: задача, не подтверждённая умершим воркером,
    возвращается брокером часами позже — когда снимок уже опубликован.

    Так 10.08.2026 запоздалый досбор пришёл на опубликованный снимок 09.08. Он
    перевёл его в ``RECOVERING``, упёрся в истёкший рубеж тех суток, собрал ноль
    и оставил статус нетерминальным. Снимок исчез с витрины, и его место заняла
    импортированная копия того же дня — молча, без единой ошибки в логе.
    """
    from tmo.db.session import session_scope
    from tmo.tasks.collection import calculate_snapshot, close_snapshot, recover_snapshot

    with session_scope() as session:
        snapshot_id = _snapshot(session, status=status).id

    for task in (recover_snapshot, close_snapshot, calculate_snapshot):
        assert task(snapshot_id=snapshot_id)["status"] == "ALREADY_CLOSED", task.name
        with session_scope() as session:
            snapshot = session.get(models.MarketSnapshot, snapshot_id)
            assert SnapshotStatus(snapshot.status) is status, task.name


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
# Восстановление после смерти шага
# --------------------------------------------------------------------------- #


def test_orphaned_jobs_return_to_plan_when_a_step_takes_over(session) -> None:
    """Наблюдения убитого шага возвращаются в план, а не числятся в работе.

    Воркер, убитый посреди пачки, оставляет отметку ``RUNNING`` навсегда. Дальше
    это ломает не сбор, а лечение, и ломает круговым образом: проверка живости
    видит «в работе есть, завершений нет», autoheal перезапускает воркер,
    перезапуск роняет текущую пачку и добавляет новых сирот. На meduza
    08.08.2026 стенд провёл в этом круге полчаса, не записав ни одной попытки.
    """
    from tmo.tasks.collection import reclaim_stale_jobs

    snapshot = _snapshot(session)
    _job(session, snapshot, "AIR", status=JobStatus.RUNNING, touched=True)
    _job(session, snapshot, "AIR", status=JobStatus.DISPATCHED, touched=True)
    done = _job(session, snapshot, "AIR", status=JobStatus.SUCCESS, touched=True)

    assert reclaim_stale_jobs(session, snapshot.id) == 2
    session.flush()

    statuses = [job.status for job in session.query(models.CollectionJob).all()]
    assert statuses.count(JobStatus.PLANNED) == 2
    assert done.status == JobStatus.SUCCESS, "закрытое наблюдение трогать нельзя"


def test_stall_threshold_outlives_a_step_lease() -> None:
    """Порог застоя обязан пережить срок аренды.

    Пока аренда умершего шага не истекла, новый шаг не начнётся и завершений не
    будет. Порог, равный сроку аренды, объявляет застоем штатное восстановление
    — и лечение начинает драться с ним.
    """
    from tmo.core.config import get_settings
    from tmo.tasks.lease import LEASE_TTL_SECONDS

    threshold = get_settings().stall_threshold_minutes * 60
    batch = get_settings().batch_soft_budget_seconds

    assert threshold > LEASE_TTL_SECONDS + batch, (
        f"порог застоя {threshold} с не переживает аренду {LEASE_TTL_SECONDS} с "
        f"плюс пачку {batch} с"
    )


# --------------------------------------------------------------------------- #
# Досбор: собственная рабочая точка и растущая пауза
# --------------------------------------------------------------------------- #


def test_close_lease_outlives_the_calculation_it_protects() -> None:
    """Аренда закрытия обязана пережить расчёт, который она защищает.

    Расчёт полной матрицы — единственный вызов длиной в полчаса, продлевать
    аренду посреди него некому. Прежний срок в 15 минут истекал на середине,
    диспетчер видел аренду свободной и запускал закрытие заново: снимок
    09.08.2026 получил пять расчётов подряд, два одновременно, и не закрылся
    вовсе — каждый новый заход сбрасывал статус и начинал с нуля.
    """
    from tmo.tasks.collection import CLOSE_LEASE_TTL, close_snapshot

    measured_calculation_seconds = 37 * 60
    assert measured_calculation_seconds < CLOSE_LEASE_TTL, (
        f"аренда {CLOSE_LEASE_TTL} с не переживает измеренный расчёт "
        f"{measured_calculation_seconds} с"
    )
    assert close_snapshot.time_limit > CLOSE_LEASE_TTL, (
        "аренда обязана истечь раньше жёсткого лимита задачи, иначе умерший шаг "
        "заблокирует закрытие насовсем"
    )


def test_one_lease_covers_every_step_that_touches_the_source() -> None:
    """Аренда одна на сбор, а не на семейство.

    Прежде ключ включал семейство, и авиа с проживанием держали разные аренды —
    то есть могли идти одновременно, ради чего аренда и вводилась. Окно
    настоящее: отметка «наблюдение тронуто» ставится в начале пачки, а задача
    завершается спустя ещё несколько минут.
    """
    from tmo.tasks import lease

    day = DAY.isoformat()
    assert lease.collection_lease(day) == f"collect:{day}"
    # Досбор ходит в тот же источник и обязан делить ту же аренду.
    assert lease.collection_lease(day) == lease.collection_lease(day)


def test_recovery_runs_at_its_own_working_point(session) -> None:
    """Досбор идёт с пониженной одновременностью, а не с найденной сбором.

    Он работает по подвыборке, отобранной по признаку «источник её не отдал»:
    дешёвое закрылось сразу, в пропусках осели самые тяжёлые наблюдения. Замер
    09.08.2026 — 1,4 страницы на авианаблюдение в конце первичного прохода
    против 7,5 в досборе тех же наблюдений.
    """
    from tmo.core.config import get_settings

    settings = get_settings()
    assert settings.recovery_concurrency < source_ceiling(), (
        "рабочая точка досбора обязана быть ниже потолка первичного сбора"
    )
    assert settings.recovery_concurrency >= 1


def source_ceiling() -> int:
    from tmo.catalog.registry import source_registry

    return source_registry().get("tutu_mcp").concurrency


def test_circuit_cooldown_grows_with_repeated_openings() -> None:
    """Повторное размыкание означает, что прошлой паузы не хватило.

    Фиксированные пять минут исходили из того, что размыкание редко. В досборе
    09.08.2026 цикл «размыкание → пауза → размыкание» повторялся раз за разом и
    съедал четверть времени.
    """
    from tmo.connectors.transport import CircuitBreaker

    circuit = CircuitBreaker(2, 300, max_reset_seconds=1800)
    assert circuit.current_reset_seconds == 300

    for _ in range(2):
        circuit.record_failure(RuntimeError("503"))
    assert circuit.current_reset_seconds == 300, "первое размыкание не удлиняет паузу"

    circuit._opened_at = None  # цепь остыла, успеха не было
    for _ in range(2):
        circuit.record_failure(RuntimeError("503"))
    assert circuit.current_reset_seconds == 600

    circuit._opened_at = None
    for _ in range(2):
        circuit.record_failure(RuntimeError("503"))
    assert circuit.current_reset_seconds == 1200


def test_cooldown_has_a_ceiling_and_a_success_resets_it() -> None:
    """Пауза не уходит в часы, а успех обнуляет счётчик: источник ожил."""
    from tmo.connectors.transport import CircuitBreaker

    circuit = CircuitBreaker(1, 300, max_reset_seconds=1800)
    for _ in range(10):
        circuit._opened_at = None
        circuit.record_failure(RuntimeError("503"))
    assert circuit.current_reset_seconds == 1800, "потолок остывания не удержан"

    circuit.record_success()
    assert circuit.current_reset_seconds == 300, "успех обязан обнулить наказание"


def test_recovery_holes_are_grouped_by_family_expensive_first(session) -> None:
    """Пропуски досбираются по семействам, дорогое первым.

    Общим списком размер пачки считать не от чего: у смешанного набора нет
    осмысленной средней стоимости, и досбор брал плоское умолчание в 40
    наблюдений — при стоимости авианаблюдения это кратно больше бюджета.

    Порядок нужен потому, что досбор упирается в рубеж суток: семейство, до
    которого не дошли, остаётся непереспрошенным, и дешёвое успеет в любом
    случае.
    """
    from tmo.tasks.collection import _holes_by_family

    snapshot = _snapshot(session)
    for family in ("HOTEL", "AIR", "RAIL"):
        _job(session, snapshot, family, status=JobStatus.FAILED, touched=True)
    _job(session, snapshot, "AIR", status=JobStatus.SUCCESS, touched=True)

    grouped = _holes_by_family(session, snapshot.id)
    assert list(grouped) == ["AIR", "HOTEL", "RAIL"], "дорогое семейство обязано идти первым"
    assert len(grouped["AIR"]) == 1, "закрытое наблюдение в досбор не попадает"


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

    state = cycle.progress(session, DAY, now=MIDDAY)
    assert state is not None
    assert state["is_closed"] is False
    assert state["step"] == cycle.STEP_RECOVER
    assert state["answered"]["AIR"] == 0.5
    assert state["holes"] == 1

    snapshot.status = SnapshotStatus.FAILED
    session.flush()
    assert cycle.progress(session, DAY, now=MIDDAY)["is_closed"] is True


def test_untouched_observations_are_not_reported_as_holes(session) -> None:
    """Пропуск — это «пробовали и не вышло», а не «очередь не дошла».

    Оба состояния выглядят в статусе одинаково: наблюдение числится `PLANNED` и
    когда к нему не подходили ни разу, и когда источник не ответил. Для
    диспетчера разницы нет — собрать надо и то и другое. Для человека разница
    вся: в начале суток несобранных ровно столько, сколько в плане, и показ
    этого числа как пропусков объявляет аварией нормальное начало работы.
    """
    snapshot = _snapshot(session)
    for _ in range(10):
        _job(session, snapshot, "AIR")  # очередь не дошла
    _job(session, snapshot, "AIR", status=JobStatus.FAILED, touched=True)  # не ответил
    _job(session, snapshot, "AIR", status=JobStatus.SUCCESS, touched=True)

    state = cycle.progress(session, DAY, now=MIDDAY)
    assert state["planned"] == 12
    assert state["answered_count"] == 1
    assert state["remaining"] == 11, "осталось собрать всё, кроме отвеченного"
    assert state["holes"] == 1, "пропуск ровно один — тот, к которому подходили"


def test_progress_is_absent_before_the_day_opens(session) -> None:
    """Снимка за сегодня ещё нет — это состояние ночи до 00:30, а не ошибка."""
    assert cycle.progress(session, DAY, now=MIDDAY) is None


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


def test_batch_never_asks_for_more_than_its_budget_allows() -> None:
    """Пачка не может требовать больше времени, чем ей отведено.

    Нижняя граница в 20 наблюдений делала ровно это на дорогом семействе: в ночь
    09.08.2026 авианаблюдение стоило 72 секунды при одновременности 2, в бюджет
    помещалось пять, а граница требовала двадцати — и две трети пачки уходили в
    `BUDGET_EXHAUSTED` до всякого обращения к источнику.
    """
    from tmo.connectors.transport import TRANSPORT_POOL
    from tmo.core.config import get_settings
    from tmo.services.pipeline import batch_size_for_family, seconds_per_observation

    budget = get_settings().batch_soft_budget_seconds
    TRANSPORT_POOL.reset()
    try:
        for concurrency in (12, 6, 3, 2):
            governor = TRANSPORT_POOL.governor("tutu_mcp", 12, floor=2)
            governor._current = float(concurrency)
            for family in ("AIR", "HOTEL", "RAIL"):
                needed = batch_size_for_family(family) * seconds_per_observation(family)
                assert needed <= budget, (
                    f"{family} при одновременности {concurrency}: пачке нужно "
                    f"{needed:.0f} с при бюджете {budget} с"
                )
    finally:
        TRANSPORT_POOL.reset()


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
