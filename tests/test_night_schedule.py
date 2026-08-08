"""Ночной цикл 00:30 → 10:00 как набор проверяемых утверждений.

Расписание, рабочая точка, размер матрицы и пороги публикации связаны
арифметикой, которую нигде не видно: она возникает при чтении четырёх файлов
сразу. Здесь она записана исполняемым кодом, чтобы правка любого из четырёх
показывала, что ночь перестала складываться, — а не чтобы зафиксировать
конкретные значения.

Числа берутся из тех же мест, что и продакшен: расписание из ``celery_app``,
стоимость наблюдения из ``pipeline.OBSERVATION_COST``, одновременность и темп
из реестра источников, пороги из профиля методики. Тест, повторяющий константу
у себя, проверял бы сам себя.

Время наблюдения считается по худшему из двух ограничителей — одновременности и
лимиту темпа. Расчёт по одной лишь одновременности однажды уже сказал, что ночь
укладывается в окно, тогда как связывал её лимит темпа.

Замер, на котором всё держится: снимок 6, ночь 08.08.2026. Его оговорки — в
docs/LOAD_PROFILE.md, раздел «Чего этот замер не показывает».
"""

from __future__ import annotations

import pytest

from tmo.catalog.registry import methodology_profile
from tmo.core.enums import CollectionFamily
from tmo.planner.matrix import expected_size
from tmo.services.pipeline import seconds_per_observation

#: Задачи цикла в порядке их выполнения. Досбор, расчёт и публикация в сборе не
#: участвуют, но задают границу, до которой сбор обязан закончиться.
COLLECT_ENTRIES = ("collect-air", "collect-rail", "collect-hotel")


def _app():
    from tmo.tasks.celery_app import celery_app

    celery_app.loader.import_default_modules()
    return celery_app


def _minutes_into_cycle(entry) -> int:
    """Минуты от полуночи.

    Сравнение по календарному времени корректно ровно потому, что цикл целиком
    лежит внутри одних суток. Границу стережёт
    ``test_whole_cycle_stays_inside_one_calendar_day``: если сбор уедет за
    полночь, сначала упадёт он, а не эти сравнения.
    """
    return min(entry.hour) * 60 + min(entry.minute)


def _schedule() -> dict[str, int]:
    return {
        name: _minutes_into_cycle(entry["schedule"])
        for name, entry in _app().conf.beat_schedule.items()
        if name != "watch-progress"
    }


def _family_minutes(family: str) -> float:
    """Сколько минут занимает сбор семейства целиком при текущих настройках."""
    jobs = expected_size()[family]
    return jobs * seconds_per_observation(family) / 60.0


# --------------------------------------------------------------------------- #
# Ночь складывается арифметически
# --------------------------------------------------------------------------- #


def test_everything_is_collected_before_the_calculation() -> None:
    """Вся матрица обязана быть собрана до начала расчёта.

    Считается всё окно от первого семейства до расчёта, включая досбор: он тоже
    собирает, и его наблюдения попадают в тот же снимок. Что не успело до
    расчёта — не попадёт в цифру вовсе.
    """
    schedule = _schedule()
    window = schedule["calculate"] - schedule["collect-air"]
    needed = sum(_family_minutes(f.value) for f in CollectionFamily)

    assert needed <= window, (
        f"матрице нужно {needed / 60:.1f} ч, а от старта авиа до расчёта "
        f"{window / 60:.1f} ч — ночь не складывается"
    )


def test_snapshot_is_publishable_without_relying_on_recovery() -> None:
    """Нижний порог публикации обязан браться первичным сбором.

    Досбор — часть плана, а не героическая мера: он рассчитан на дыры, а не на
    остаток семейства. Снимок, публикуемый только при удачном досборе, зависит
    от того, что к утру пройдут причины отказов, — а они могут и не пройти.
    """
    profile = methodology_profile("baseline_v1")
    schedule = _schedule()
    window = schedule["recover-holes"] - schedule["collect-air"]
    needed = sum(
        _minutes_for_share(f.value, profile.degraded_completion(f)) for f in CollectionFamily
    )

    assert needed <= window, (
        f"нижний порог требует {needed / 60:.1f} ч первичного сбора при окне "
        f"{window / 60:.1f} ч до досбора"
    )


def test_air_gets_its_window_before_the_cheap_families_start() -> None:
    """Авиа обязано закончить до старта следующего семейства.

    Иначе два семейства идут одновременно, каждое со своим пулом, и источник
    получает удвоенную одновременность — ту зону, где ночью 08.08.2026 он
    отвечал 503.
    """
    schedule = _schedule()
    next_family = min(schedule[name] for name in COLLECT_ENTRIES if name != "collect-air")
    available = next_family - schedule["collect-air"]

    assert _family_minutes("AIR") <= available, (
        f"авиа идёт {_family_minutes('AIR') / 60:.1f} ч, а следующее семейство "
        f"стартует через {available / 60:.1f} ч"
    )


def test_hard_limit_of_a_family_covers_its_expected_run() -> None:
    """Жёсткий лимит задачи обязан быть больше ожидаемой работы.

    Лимит уничтожает несохранённое: он последняя страховка, а не расписание.
    """
    from tmo.tasks.collection import COLLECT_FAMILY_TIME_LIMIT

    longest = max(_family_minutes(f.value) for f in CollectionFamily)
    assert longest * 60 <= COLLECT_FAMILY_TIME_LIMIT, (
        f"самое долгое семейство идёт {longest / 60:.1f} ч при лимите "
        f"{COLLECT_FAMILY_TIME_LIMIT / 3600:.1f} ч"
    )


def test_snapshot_opens_before_collection_and_in_time() -> None:
    """План обязан быть готов к старту сбора, с запасом на свой лимит."""
    from tmo.tasks.collection import open_snapshot

    schedule = _schedule()
    gap_minutes = schedule["collect-air"] - schedule["open-snapshot"]
    assert gap_minutes > 0, "снимок открывается после начала сбора"
    assert open_snapshot.time_limit / 60 <= gap_minutes, (
        f"открытие снимка может идти {open_snapshot.time_limit / 60:.0f} мин "
        f"при зазоре до сбора в {gap_minutes} мин"
    )


def test_morning_pipeline_finishes_before_the_sla() -> None:
    """Досбор, расчёт и публикация обязаны уложиться до 10:00 MSK."""
    from datetime import date

    from tmo.core.timeutil import sla_deadline

    schedule = _schedule()
    deadline = sla_deadline(date(2026, 8, 8)).hour * 60

    for name in ("recover-holes", "calculate", "finalize"):
        assert schedule[name] < deadline, f"{name} назначен после SLA"
    assert schedule["recover-holes"] < schedule["calculate"] < schedule["finalize"]


# --------------------------------------------------------------------------- #
# Пороги публикации достижимы в этом окне
# --------------------------------------------------------------------------- #


def _minutes_for_share(family: str, share: float) -> float:
    return expected_size()[family] * share * seconds_per_observation(family) / 60.0


@pytest.mark.parametrize("level", ["degraded", "ready"])
def test_publication_threshold_is_reachable_within_the_window(level: str) -> None:
    """Порог публикации обязан быть достижим за отведённое сбору время.

    Проверяется не «уложились ли вчера», а разрешает ли конструкция уложиться в
    принципе. Порог, недостижимый при полностью исправной системе, означал бы,
    что снимок не опубликуется никогда, — и узнать об этом лучше здесь, чем
    через месяц пустой витрины.

    Считается по самому дешёвому способу дотянуть до порога: дешёвые семейства
    собираются полностью, авиа — ровно настолько, насколько требует его
    собственный порог по семейству.
    """
    profile = methodology_profile("baseline_v1")
    schedule = _schedule()
    # Окно до расчёта, а не до досбора: досбор тоже собирает, и его наблюдения
    # попадают в тот же снимок.
    window = schedule["calculate"] - schedule["collect-air"]

    if level == "degraded":
        air_share = profile.degraded_completion(CollectionFamily.AIR)
    else:
        air_share = profile.ready_completion(CollectionFamily.AIR)

    needed = (
        _minutes_for_share("AIR", air_share)
        + _minutes_for_share("HOTEL", 1.0)
        + _minutes_for_share("RAIL", 1.0)
    )
    assert needed <= window, (
        f"порог {level} требует {needed / 60:.1f} ч сбора при окне "
        f"{window / 60:.1f} ч — недостижим"
    )


@pytest.mark.parametrize("family", list(CollectionFamily))
def test_family_threshold_is_not_decorative(family: CollectionFamily) -> None:
    """Порог семейства обязан быть достижимой границей, а не украшением.

    Семейный и общий пороги действуют вместе, и одновременное проседание всех
    семейств до их порогов общий порог, разумеется, не пропустит — это
    нормальная работа конъюнкции, а не противоречие.

    Проверяется другое: если просело **одно** семейство, ровно до своего порога,
    а остальные собраны полностью — снимок обязан пройти общий порог. Иначе
    семейный порог никогда не станет ограничителем и будет вводить в
    заблуждение: профиль объявляет «авиа на 75 % допустимо», а фактически
    отсечка всегда выше и задана в другом месте.
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
