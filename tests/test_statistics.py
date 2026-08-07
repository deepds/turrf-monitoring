"""Статистика и политика выбросов.

Главный тест здесь — про малую выборку: на пяти поездах «статистический
выброс» есть половина рынка направления, и агрессивная чистка удаляет рынок,
а не шум.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from tmo.engine.statistics import detect_outliers, median, percentile, summarize


def D(*values: str) -> list[Decimal]:
    return [Decimal(value) for value in values]


def test_median_of_odd_sample() -> None:
    assert median(D("1", "5", "3")) == Decimal("3")


def test_median_of_even_sample_is_average_of_middle_two() -> None:
    assert median(D("10", "20", "30", "40")) == Decimal("25")


def test_median_is_order_independent() -> None:
    """Одинаковая выборка обязана давать одинаковую медиану при любом порядке."""
    assert median(D("7232.92", "5735.68", "6100.00")) == median(
        D("6100.00", "7232.92", "5735.68")
    )


def test_median_of_empty_sample_is_none_not_zero() -> None:
    """Ноль означал бы бесплатную поездку, а не отсутствие данных."""
    assert median([]) is None
    assert summarize([]).median is None


def test_percentiles_interpolate() -> None:
    values = D("10", "20", "30", "40")
    assert percentile(values, 0.25) == Decimal("17.5")
    assert percentile(values, 0.75) == Decimal("32.5")


def test_summary_rounds_to_kopecks() -> None:
    summary = summarize(D("100.005", "200.004"))
    assert summary.median == Decimal("150.00")
    assert summary.count == 2


# --------------------------------------------------------------------------- #
# Политика выбросов
# --------------------------------------------------------------------------- #


def test_small_sample_is_not_cleaned() -> None:
    """При выборке меньше порога чистка не применяется вовсе."""
    decision = detect_outliers(
        D("5000", "5200", "5400", "99000"),
        min_sample=8,
        multiplier=3.0,
        max_removed_share=0.25,
    )
    assert decision.applied is False
    assert decision.reason == "SAMPLE_TOO_SMALL"
    assert decision.removed == 0


def test_large_sample_removes_extreme_value() -> None:
    values = D(*([str(5000 + i * 50) for i in range(12)] + ["250000"]))
    decision = detect_outliers(values, min_sample=8, multiplier=3.0, max_removed_share=0.25)
    assert decision.applied is True
    assert decision.removed == 1


def test_flat_sample_has_nothing_to_clean() -> None:
    decision = detect_outliers(
        D(*(["6000"] * 10)), min_sample=8, multiplier=3.0, max_removed_share=0.25
    )
    assert decision.applied is False
    assert decision.reason == "NO_SPREAD"


def test_heterogeneous_sample_is_marked_not_trimmed() -> None:
    """Если правило хочет убрать больше допустимой доли, выборка неоднородна.

    Удаление четверти наблюдений скрыло бы этот факт вместо того, чтобы его
    показать.
    """
    # Равномерный разброс от 1 000 до 12 000: узкий забор Тьюки хочет срезать
    # половину выборки. Такая выборка не «грязная», она широкая.
    values = D(*[str(1000 * i) for i in range(1, 13)])
    decision = detect_outliers(values, min_sample=8, multiplier=0.1, max_removed_share=0.25)
    assert decision.applied is False
    assert decision.reason == "TOO_MANY_OUTLIERS"
    assert decision.removed / len(values) > 0.25


@pytest.mark.parametrize("size", [7, 8])
def test_threshold_boundary_is_inclusive(size: int) -> None:
    """Порог включающий: ровно ``min_sample`` наблюдений уже чистятся."""
    values = D(*([str(5000 + i) for i in range(size - 1)] + ["500000"]))
    decision = detect_outliers(values, min_sample=8, multiplier=3.0, max_removed_share=0.5)
    assert decision.applied is (size >= 8)
