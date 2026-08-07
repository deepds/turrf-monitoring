"""Матрица наблюдений.

Матрица — это обещание системы: столько наблюдений она обязана сделать за
сутки. Если размер поплывёт, покрытие станет измеряться относительно другого
знаменателя, и «98 % собрано» перестанет что-либо значить.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from tmo.core.enums import CollectionFamily
from tmo.core.timeutil import date_pairs, horizon_dates
from tmo.planner.matrix import STAR_CATEGORIES, build_matrix, expected_size

SNAPSHOT = date(2026, 8, 7)


def test_matrix_size_matches_specification() -> None:
    """15 840 наблюдений: 600 ЖД + 8 700 авиа + 6 540 проживания."""
    matrix = build_matrix(SNAPSHOT)
    counts = matrix.counts_by_family()
    assert counts == {"RAIL": 600, "AIR": 8700, "HOTEL": 6540}
    assert len(matrix) == 15_840
    assert expected_size(30)["TOTAL"] == 15_840


def test_matrix_is_deterministic() -> None:
    """Одинаковый вход обязан давать побайтово одинаковый набор наблюдений."""
    first = build_matrix(SNAPSHOT)
    second = build_matrix(SNAPSHOT)
    assert first.digest == second.digest
    assert [job.job_key for job in first.jobs] == [job.job_key for job in second.jobs]


def test_matrix_changes_with_snapshot_date() -> None:
    assert build_matrix(SNAPSHOT).digest != build_matrix(SNAPSHOT + timedelta(days=1)).digest


def test_job_keys_are_unique() -> None:
    """Совпадение ключей означало бы, что одно наблюдение теряется молча."""
    keys = [job.job_key for job in build_matrix(SNAPSHOT).jobs]
    assert len(keys) == len(set(keys))


def test_today_is_not_observed() -> None:
    """Сегодняшний день в горизонт не входит (SCOPE-R O3)."""
    matrix = build_matrix(SNAPSHOT)
    dates = {job.service_date for job in matrix.jobs if job.service_date}
    dates |= {job.check_in for job in matrix.jobs if job.check_in}
    assert min(dates) == SNAPSHOT + timedelta(days=1)


def test_rail_is_observed_per_leg() -> None:
    """ЖД наблюдается плечом, а не круговой поездкой.

    Круговое наблюдение ЖД давало бы сочетания «поезд туда × поезд обратно» —
    набор комбинаций вместо выбора пассажира.
    """
    rail = [job for job in build_matrix(SNAPSHOT).jobs if job.family is CollectionFamily.RAIL]
    assert all(job.return_date is None for job in rail)
    assert all(job.params["car_type"] == "COMPARTMENT" for job in rail)
    assert all(job.params["direct_only"] is True for job in rail)
    assert all(job.params["passengers"] == 1 for job in rail)
    # Обе стороны каждого направления наблюдаются отдельно.
    directions = {(job.origin_code, job.destination_code) for job in rail}
    assert ("MOW", "AER") in directions
    assert ("AER", "MOW") in directions


def test_air_is_a_real_round_trip_on_a_date_pair() -> None:
    air = [job for job in build_matrix(SNAPSHOT).jobs if job.family is CollectionFamily.AIR]
    assert len(air) == 20 * len(date_pairs(SNAPSHOT))
    assert all(job.return_date and job.return_date > job.service_date for job in air)
    assert all(job.params["trip_type"] == "ROUND_TRIP" for job in air)
    horizon_end = SNAPSHOT + timedelta(days=30)
    assert max(job.return_date for job in air) == horizon_end


def test_hotel_graph_tail_extends_beyond_horizon() -> None:
    """Последняя точка графика проживания: заезд D+30, выезд D+31."""
    hotel = [job for job in build_matrix(SNAPSHOT).jobs if job.family is CollectionFamily.HOTEL]
    horizon_end = SNAPSHOT + timedelta(days=30)
    tail = [job for job in hotel if job.check_in == horizon_end]
    assert len(tail) == len(STAR_CATEGORIES) * 5
    assert all(job.check_out == horizon_end + timedelta(days=1) for job in tail)


def test_one_night_points_are_part_of_the_pairs() -> None:
    """Точка «одна ночь» не планируется отдельно: пара (d, d+1) уже есть."""
    hotel = [job for job in build_matrix(SNAPSHOT).jobs if job.family is CollectionFamily.HOTEL]
    single = [job for job in hotel if job.nights == 1]
    # 29 пар внутри горизонта + 1 хвостовая, на каждый город и категорию.
    assert len(single) == 5 * len(STAR_CATEGORIES) * 30


def test_series_key_is_stable_across_snapshots() -> None:
    """Один и тот же логический ряд между днями сопоставим по series_key."""
    today = build_matrix(SNAPSHOT)
    tomorrow = build_matrix(SNAPSHOT + timedelta(days=1))
    pick = next(
        job
        for job in today.jobs
        if job.family is CollectionFamily.RAIL
        and job.origin_code == "MOW"
        and job.destination_code == "AER"
        and job.day_offset == 14
    )
    twin = next(
        job
        for job in tomorrow.jobs
        if job.family is CollectionFamily.RAIL
        and job.origin_code == "MOW"
        and job.destination_code == "AER"
        and job.day_offset == 14
    )
    assert pick.series_key == twin.series_key
    assert pick.job_key != twin.job_key


@pytest.mark.parametrize("horizon", [7, 14, 30])
def test_expected_size_matches_built_matrix(horizon: int) -> None:
    matrix = build_matrix(SNAPSHOT, horizon_days=horizon)
    assert matrix.counts_by_family() == {
        key: value for key, value in expected_size(horizon).items() if key != "TOTAL"
    }
    assert len(horizon_dates(SNAPSHOT, days=horizon)) == horizon
