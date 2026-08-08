"""Celery: очереди, расписание, таймауты.

Очереди разделены не для красоты. Один воркер, обслуживающий всё сразу, не
выполняет собственного лечения: забитый сбором пул не запускает ни сторожа, ни
детектор застоя. Поэтому:

* ``collect`` и ``compute`` — всё длинное и сетевое, отдельный воркер;
* ``maintenance`` и ``ondemand`` — обслуживание и запросы пользователя,
  второй воркер, свободный по построению.

Расписание разнесено по компонентам: одновременный залп источник не держит,
именно он открывал размыкатель цепи. К 10:00 MSK всё закончено, дальше
четырнадцать часов запаса на повтор.
"""

from __future__ import annotations

from celery import Celery
from celery.schedules import crontab

from tmo.core.config import get_settings
from tmo.core.logging import configure_logging

settings = get_settings()
configure_logging(settings.log_level, settings.log_format)

#: Модули с задачами перечисляются явно. Воркер запускается как
#: `celery -A tmo.tasks.celery_app:celery_app` и импортирует только этот
#: модуль: без `include` он знает расписание и не знает ни одной задачи, а
#: `beat` при этом исправно шлёт запуски. Отказ выглядит как
#: `KeyError: 'tmo.open_snapshot'` в логе воркера и как молчащая ночь на
#: витрине. Импорт отложен до старта воркера, поэтому цикла не возникает:
#: модули задач сами импортируют `celery_app`.
TASK_MODULES = [
    "tmo.tasks.collection",
    "tmo.tasks.maintenance",
]

celery_app = Celery(
    "tmo",
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=TASK_MODULES,
)

#: Самый длинный жёсткий лимит среди задач — десять часов у ``collect_family``.
#: Всё, что связано с «пока задача считается живой», обязано быть больше него.
#: Значение продублировано, а не импортировано: модуль задач импортирует этот,
#: и обратная связь дала бы цикл. Расхождение ловит тест.
LONGEST_TASK_SECONDS = 10 * 3600

celery_app.conf.update(
    timezone="Europe/Moscow",
    enable_utc=True,
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    result_expires=7 * 24 * 3600,
    # Подтверждение после выполнения: при перезапуске пула задача возвращается
    # в очередь, а не теряется.
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    broker_transport_options={
        # Redis считает задачу потерянной по истечении этого срока и отдаёт её
        # второму потребителю. Значение по умолчанию — час, а сбор семейства
        # идёт до шести: в ночь 08.08.2026 задача проживания была выдана пять
        # раз и выполнялась в пять копий. Они ходили по одним и тем же пачкам,
        # впятеро подняли нагрузку на источник, получили 503, разомкнули цепь —
        # и вернули в план 6 116 уже собранных наблюдений, обрушив покрытие с
        # 98 % до 45 %. Запас вдвое: перевыдача допустима только тогда, когда
        # воркер действительно мёртв, а не когда он занят делом.
        "visibility_timeout": 2 * LONGEST_TASK_SECONDS,
    },
    # Один префетч: длинные задачи не должны застревать за чужой очередью.
    worker_prefetch_multiplier=1,
    worker_max_tasks_per_child=200,
    task_default_queue="collect",
    task_routes={
        "tmo.collect_batch": {"queue": "collect"},
        "tmo.collect_family": {"queue": "collect"},
        "tmo.open_snapshot": {"queue": "collect"},
        "tmo.recover_snapshot": {"queue": "maintenance"},
        "tmo.calculate_snapshot": {"queue": "compute"},
        "tmo.recalculate_snapshot": {"queue": "compute"},
        "tmo.finalize_snapshot": {"queue": "compute"},
        "tmo.watch_collection_progress": {"queue": "maintenance"},
        "tmo.purge_raw": {"queue": "maintenance"},
        "tmo.daily_quality_digest": {"queue": "maintenance"},
    },
    # Порядок семейств задан их стоимостью, а не удобством чтения. Замер ночи
    # 08.08.2026: авиа — 5 страниц и 20,3 с на наблюдение, 8 700 наблюдений;
    # проживание — 3,6 страницы и 7,2 с, 6 540 наблюдений; ЖД — 1 страница и
    # 1-1,5 с, 600 наблюдений. При одновременности 6 это 8,2 часа, 2,2 часа и
    # шесть минут — вместе больше ночи. Поэтому самое дорогое начинает сразу
    # после открытия снимка и забирает всё свободное окно, а дешёвые, которые
    # заведомо успевают, встают в конец.
    #
    # Авиа начинается в 21:00 предыдущих суток и получает восемь с половиной
    # часов — ровно столько, сколько ему нужно. Дешёвые семейства встают под
    # утро, когда авиа закончило: три пачки по шесть потоков дали бы источнику
    # восемнадцать одновременных обращений — та зона, где ночью ломалось.
    #
    # Цикл переходит через полночь, поэтому задачи берут дату из
    # `operational_date_for`, а не из календаря: с рубежом в 20:00 и авиа, и
    # утренняя публикация получают одну и ту же дату — завтрашнюю от момента
    # старта. Без переноса авиа ушло бы во вчерашний снимок, а утренний
    # недосчитался бы 8 700 наблюдений при полностью собранных данных.
    beat_schedule={
        "open-snapshot": {
            "task": "tmo.open_snapshot",
            "schedule": crontab(hour=20, minute=30),
        },
        "collect-air": {
            "task": "tmo.collect_family",
            "schedule": crontab(hour=21, minute=0),
            "kwargs": {"family": "AIR"},
        },
        "collect-rail": {
            "task": "tmo.collect_family",
            "schedule": crontab(hour=5, minute=15),
            "kwargs": {"family": "RAIL"},
        },
        "collect-hotel": {
            "task": "tmo.collect_family",
            "schedule": crontab(hour=5, minute=30),
            "kwargs": {"family": "HOTEL"},
        },
        "recover-holes": {
            "task": "tmo.recover_snapshot",
            "schedule": crontab(hour=8, minute=0),
        },
        "calculate": {
            "task": "tmo.calculate_snapshot",
            "schedule": crontab(hour=9, minute=0),
        },
        "finalize": {
            "task": "tmo.finalize_snapshot",
            "schedule": crontab(hour=9, minute=30),
        },
        "quality-digest": {
            "task": "tmo.daily_quality_digest",
            "schedule": crontab(hour=10, minute=0),
        },
        "watch-progress": {
            "task": "tmo.watch_collection_progress",
            "schedule": crontab(minute="*/5"),
        },
        "purge-raw": {
            "task": "tmo.purge_raw",
            "schedule": crontab(hour=11, minute=0),
        },
    },
)
