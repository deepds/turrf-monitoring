"""Регрессии ночи 08.08.2026.

Пять отказов, из-за которых первый автоматический прогон собрал данные и
объявил себя несостоявшимся. Каждый прошёл через 190 существующих тестов
незамеченным, потому что все они смотрят на удачный путь: сбор идёт, цепь
замкнута, задача выполняется один раз. Здесь проверяется обратное.
"""

from __future__ import annotations

import threading
from datetime import date, timedelta

import pytest
from sqlalchemy import select

from tmo.core.enums import JobStatus
from tmo.db import models
from tmo.db.session import session_scope

SNAPSHOT = date(2026, 8, 8)


@pytest.fixture()
def open_circuit():
    """Разомкнутая цепь у всех источников — состояние ночного отказа.

    Пачка в этом состоянии не доходит ни до одного наблюдения и возвращает их
    все как ``untouched``. Именно здесь затирались статусы.
    """
    from tmo.catalog.registry import source_registry
    from tmo.connectors.transport import TRANSPORT_POOL
    from tmo.core.config import get_settings

    settings = get_settings()
    for source in source_registry().sources:
        circuit = TRANSPORT_POOL.circuit(
            source.code, settings.circuit_failure_threshold, settings.circuit_reset_seconds
        )
        for _ in range(settings.circuit_failure_threshold):
            circuit.record_failure(RuntimeError("tutu_mcp ответил 503"))
        assert circuit.is_open
    yield
    TRANSPORT_POOL.reset()


def _loaded_celery_app():
    """Приложение Celery с импортированными модулями задач.

    Простой импорт ``celery_app`` регистрирует только расписание: модули из
    ``include`` подтягивает загрузчик при старте воркера. Тест обязан
    воспроизвести именно этот шаг — иначе он проверяет не то, что видит воркер.
    """
    from tmo.tasks.celery_app import celery_app

    celery_app.loader.import_default_modules()
    return celery_app


# --------------------------------------------------------------------------- #
# Сторож застоя: пауза расписания — не застой
# --------------------------------------------------------------------------- #


def _snapshot_with_jobs(session, statuses: list[str]) -> int:
    """Снимок с наблюдениями заданных статусов и одним завершением в прошлом."""
    from tmo.core.timeutil import now_utc

    snapshot = models.MarketSnapshot(
        snapshot_date=SNAPSHOT,
        attempt_no=1,
        status="COLLECTING",
        is_synthetic=False,
        created_at=now_utc(),
    )
    session.add(snapshot)
    session.flush()

    long_ago = now_utc() - timedelta(hours=2)
    for index, status in enumerate(statuses):
        session.add(
            models.CollectionJob(
                snapshot_id=snapshot.id,
                job_key=f"job-{index}",
                series_key=f"series-{index}",
                family="AIR",
                origin_code="MOW",
                destination_code="AER",
                service_date=SNAPSHOT + timedelta(days=1),
                return_date=SNAPSHOT + timedelta(days=2),
                day_offset=1,
                status=status,
                completed_at=long_ago if JobStatus(status).is_collected else None,
                params={},
            )
        )
    session.flush()
    return snapshot.id


def test_planned_jobs_awaiting_their_hour_are_not_a_stall(database: str) -> None:
    """Авиа ждёт 02:00, проживание — 05:00. Это план, а не повисшая работа.

    Прежняя проверка считала незавершённой работой всё, включая ``PLANNED``,
    объявляла застоем паузу расписания и через autoheal перезапускала воркеры
    ровно тогда, когда следующему семейству пора было начинать.
    """
    from tmo.tasks.health import check

    with session_scope() as session:
        _snapshot_with_jobs(session, ["SUCCESS"] * 3 + ["PLANNED"] * 100)

    healthy, message = check()
    assert healthy, message


def test_dispatched_work_without_progress_is_a_stall(database: str) -> None:
    """А вот отданное в очередь и не движущееся — застой, и его видно."""
    from tmo.tasks.health import check

    with session_scope() as session:
        _snapshot_with_jobs(session, ["SUCCESS"] + ["RUNNING"] * 10)

    healthy, message = check()
    assert not healthy
    assert "завершений нет" in message


# --------------------------------------------------------------------------- #
# Статус собранного наблюдения не стирается
# --------------------------------------------------------------------------- #


def test_collected_job_survives_a_batch_that_never_reached_it(
    database: str, open_circuit
) -> None:
    """Повторный проход по разомкнутой цепи не возвращает собранное в план.

    Так была потеряна ночь: пять копий задачи проживания ходили по одним и тем
    же пачкам, упирались в разомкнутую цепь и возвращали в ``PLANNED`` уже
    собранные наблюдения. Данные остались в базе, покрытие упало с 98 % до
    45 %, ворота отказали в публикации.
    """
    from tmo.execution.runner import run_batch

    with session_scope() as session:
        snapshot_id = _snapshot_with_jobs(session, ["SUCCESS", "PARTIAL", "NO_MARKET"])
        job_ids = list(
            session.scalars(
                select(models.CollectionJob.id).where(
                    models.CollectionJob.snapshot_id == snapshot_id
                )
            )
        )

    run_batch(job_ids, execution_scope="PRIMARY")

    with session_scope() as session:
        statuses = sorted(
            session.scalars(
                select(models.CollectionJob.status).where(
                    models.CollectionJob.id.in_(job_ids)
                )
            )
        )
    assert statuses == ["NO_MARKET", "PARTIAL", "SUCCESS"]


def test_unreached_planned_job_returns_to_plan(database: str, open_circuit) -> None:
    """Обратная сторона: несобранное обязано вернуться в план, а не зависнуть."""
    from tmo.execution.runner import run_batch

    with session_scope() as session:
        snapshot_id = _snapshot_with_jobs(session, ["PLANNED", "FAILED"])
        job_ids = list(
            session.scalars(
                select(models.CollectionJob.id).where(
                    models.CollectionJob.snapshot_id == snapshot_id
                )
            )
        )

    run_batch(job_ids, execution_scope="PRIMARY")

    with session_scope() as session:
        statuses = set(
            session.scalars(
                select(models.CollectionJob.status).where(
                    models.CollectionJob.id.in_(job_ids)
                )
            )
        )
    assert statuses <= {JobStatus.PLANNED.value, JobStatus.FAILED.value}
    assert JobStatus.RUNNING.value not in statuses


# --------------------------------------------------------------------------- #
# Учёт обращений: счётчик наблюдения не считает чужие
# --------------------------------------------------------------------------- #


def test_connector_hands_out_one_transport_to_all_threads() -> None:
    """Клиент источника создаётся один раз, сколько бы потоков ни просило.

    Без блокировки потоки пачки одновременно видят ``None`` и создают каждый
    свой ``SourceTransport``. Побеждает последний записавший, остальные работают
    через собственные объекты — и счётчик обращений наблюдения считается на
    одном клиенте, пока запросы уходят через другой. В базу попадает ноль.

    Та же гонка была устранена в ``TutuConnector.mcp()``; уровнем ниже она
    осталась и проявилась только на живом многопоточном прогоне 08.08.2026.
    """
    from tmo.catalog.registry import source_registry
    from tmo.connectors.base import BaseConnector

    class _Probe(BaseConnector):
        code = "tutu_mcp"

        def collect_rail(self, query, budget): ...
        def collect_air(self, query, budget): ...
        def collect_hotel(self, query, budget): ...

    connector = _Probe(source_registry().get("tutu_mcp"))
    barrier = threading.Barrier(8)
    seen: list[int] = []
    lock = threading.Lock()

    def grab() -> None:
        barrier.wait()
        transport = connector.transport()
        with lock:
            seen.append(id(transport))

    threads = [threading.Thread(target=grab) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    connector.close()

    assert len(set(seen)) == 1, f"создано {len(set(seen))} клиентов вместо одного"


def test_call_counter_does_not_leak_between_threads() -> None:
    """Клиент общий на источник, счётчик наблюдения — нет.

    Из-за общего счётчика у Туту в базе стояло 0,01 обращения на наблюдение
    при фактических один-восемь: дельта, снятая вокруг одного наблюдения,
    включала обращения соседних потоков пачки и вырождалась.
    """
    from tmo.connectors.transport import CircuitBreaker, RateLimiter, SourceTransport

    transport = SourceTransport(
        source_code="probe",
        allowed_hosts=("example.invalid",),
        rate_limiter=RateLimiter(0),
        circuit=CircuitBreaker(8, 300),
    )
    seen: dict[str, int] = {}
    barrier = threading.Barrier(3)

    def worker(name: str, calls: int) -> None:
        barrier.wait()
        for _ in range(calls):
            transport._local.calls = transport.thread_call_count + 1
        seen[name] = transport.thread_call_count

    threads = [
        threading.Thread(target=worker, args=("a", 3)),
        threading.Thread(target=worker, args=("b", 7)),
        threading.Thread(target=worker, args=("c", 5)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    transport.close()

    assert seen == {"a": 3, "b": 7, "c": 5}


# --------------------------------------------------------------------------- #
# Настройки, из-за которых ночь не состоялась
# --------------------------------------------------------------------------- #


def test_broker_holds_a_task_longer_than_it_can_run() -> None:
    """Брокер обязан ждать дольше самой длинной задачи.

    Иначе Redis объявляет занятого воркера мёртвым и выдаёт задачу второму:
    сбор проживания был получен пять раз и выполнялся в пять копий.
    """
    celery_app = _loaded_celery_app()

    visibility = celery_app.conf.broker_transport_options["visibility_timeout"]
    longest = max(
        task.time_limit
        for task in celery_app.tasks.values()
        if getattr(task, "time_limit", None)
    )
    assert visibility > longest
    # И объявленный самый длинный лимит не должен разъехаться с фактическим:
    # значение продублировано в двух модулях, чтобы не было цикла импортов.
    from tmo.tasks.celery_app import LONGEST_TASK_SECONDS

    assert longest <= LONGEST_TASK_SECONDS, (
        f"объявлено {LONGEST_TASK_SECONDS} с, а самая длинная задача идёт {longest} с"
    )


def test_every_scheduled_task_is_registered() -> None:
    """Расписание не должно ссылаться на задачу, которой у воркера нет.

    Пустой ``tmo/tasks/__init__.py`` и ``Celery(...)`` без ``include`` давали
    воркер, знающий расписание и не знающий ни одной задачи: `beat` слал
    запуски, воркер отвечал `KeyError`, ночь молчала.
    """
    celery_app = _loaded_celery_app()

    scheduled = {entry["task"] for entry in celery_app.conf.beat_schedule.values()}
    missing = scheduled - set(celery_app.tasks)
    assert not missing, f"в расписании есть незарегистрированные задачи: {sorted(missing)}"


def test_whole_cycle_stays_inside_one_calendar_day() -> None:
    """Расписание обязано укладываться в одни календарные сутки МСК.

    На этом держится единственность даты снимка: она календарная, и все задачи
    цикла получают её одинаково. Вынос любого сбора за полночь назад развёл бы
    семейства по разным снимкам — каждый оказался бы неполным при полностью
    собранных данных.

    Тест не запрещает такой перенос, а требует вернуть вместе с ним вечерний
    рубеж дат: он здесь был, пока сбор авиа стоял на 21:00, и снят вместе с ним.
    """
    from datetime import datetime

    from tmo.core.timeutil import MSK, snapshot_date_for

    celery_app = _loaded_celery_app()
    day = date(2026, 8, 8)

    dates = set()
    for name, entry in celery_app.conf.beat_schedule.items():
        if name == "watch-progress":
            continue
        hour, minute = min(entry["schedule"].hour), min(entry["schedule"].minute)
        dates.add(
            snapshot_date_for(datetime(day.year, day.month, day.day, hour, minute, tzinfo=MSK))
        )

    assert dates == {day}, f"цикл разъехался по датам: {sorted(dates)}"


def test_batch_size_fits_the_batch_budget(family: str = "AIR") -> None:
    """Пачка обязана укладываться в свой бюджет — иначе она рвётся всегда.

    40 авианаблюдений при одновременности 3 требовали 271 секунду против
    бюджета в 240: каждая пачка закрывалась `BUDGET_EXHAUSTED`, не дойдя до
    трети своих наблюдений, и за ночь так потерялось 219 наблюдений.
    """
    from tmo.core.config import get_settings
    from tmo.services.pipeline import (
        BATCH_BUDGET_FILL,
        OBSERVATION_COST,
        batch_size_for_family,
        seconds_per_observation,
    )

    settings = get_settings()

    for name in OBSERVATION_COST:
        size = batch_size_for_family(name)
        expected = size * seconds_per_observation(name)
        assert expected <= settings.batch_soft_budget_seconds, (
            f"{name}: пачка из {size} наблюдений требует {expected:.0f} с "
            f"при бюджете {settings.batch_soft_budget_seconds} с"
        )
        # И не вырождается в пачку из одного наблюдения на дешёвом семействе.
        assert expected >= settings.batch_soft_budget_seconds * BATCH_BUDGET_FILL * 0.5 or size >= 100


def test_unknown_family_falls_back_to_configured_batch_size() -> None:
    """Досбор идёт по дырам всех семейств сразу: стоимость смешанная."""
    from tmo.core.config import get_settings
    from tmo.services.pipeline import batch_size_for_family

    assert batch_size_for_family(None) == get_settings().batch_size


def test_schedule_starts_the_most_expensive_family_first() -> None:
    """Авиа стоит шестнадцать часов и обязано начинать первым.

    Проверяется не порядок ради порядка: при старте в 02:00 авиа получало три
    часа до расчёта и собирало 3,5 % семейства.
    """
    celery_app = _loaded_celery_app()
    schedule = celery_app.conf.beat_schedule

    def at(entry_name: str) -> int:
        """Минуты от полуночи: цикл целиком лежит внутри одних суток."""
        entry = schedule[entry_name]["schedule"]
        return min(entry.hour) * 60 + min(entry.minute)

    air = at("collect-air")
    for cheap in ("collect-rail", "collect-hotel"):
        assert at(cheap) > air, f"{cheap} дешевле авиа и обязано идти после него"
        assert at(cheap) < at("recover-holes"), f"{cheap} обязано успеть до досбора"
    assert at("open-snapshot") <= air, "снимок открывается не позже первого сбора"
    assert at("recover-holes") < at("calculate") < at("finalize")


@pytest.mark.parametrize("status", ["SUCCESS", "PARTIAL", "NO_MARKET"])
def test_collected_statuses_are_excluded_from_repeat_collection(status: str) -> None:
    """Граница «собрано / дыра» одна на все места, где сбор повторяется."""
    from tmo.tasks.collection import COLLECTED_JOB_STATUSES

    assert status in COLLECTED_JOB_STATUSES
    assert JobStatus.FAILED.value not in COLLECTED_JOB_STATUSES
