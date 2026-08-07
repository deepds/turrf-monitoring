"""Статистика выборки.

Считается на ``Decimal``, чтобы медиана не зависела от двоичного округления:
две одинаковые цены не должны давать разный результат от порядка сложения.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from tmo.core.money import quantize


@dataclass(frozen=True, slots=True)
class Summary:
    count: int
    minimum: Decimal | None
    maximum: Decimal | None
    median: Decimal | None
    p25: Decimal | None
    p75: Decimal | None


def median(values: list[Decimal]) -> Decimal | None:
    """Медиана. Для чётной выборки — среднее двух центральных значений."""
    if not values:
        return None
    ordered = sorted(values)
    size = len(ordered)
    middle = size // 2
    if size % 2 == 1:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / Decimal(2)


def percentile(values: list[Decimal], fraction: float) -> Decimal | None:
    """Перцентиль линейной интерполяцией между соседними значениями."""
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = fraction * (len(ordered) - 1)
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = Decimal(str(position - lower))
    return ordered[lower] + (ordered[upper] - ordered[lower]) * weight


def summarize(values: list[Decimal]) -> Summary:
    if not values:
        return Summary(0, None, None, None, None, None)
    return Summary(
        count=len(values),
        minimum=quantize(min(values)),
        maximum=quantize(max(values)),
        median=quantize(median(values)),
        p25=quantize(percentile(values, 0.25)),
        p75=quantize(percentile(values, 0.75)),
    )


def iqr_bounds(values: list[Decimal], multiplier: float) -> tuple[Decimal, Decimal] | None:
    """Границы Тьюки. ``None``, если выборка вырождена и границы бессмысленны."""
    if len(values) < 4:
        return None
    q1 = percentile(values, 0.25)
    q3 = percentile(values, 0.75)
    if q1 is None or q3 is None:
        return None
    spread = q3 - q1
    if spread <= 0:
        return None
    factor = Decimal(str(multiplier))
    return q1 - spread * factor, q3 + spread * factor


@dataclass(frozen=True, slots=True)
class OutlierDecision:
    """Что сделала политика выбросов и почему."""

    applied: bool
    reason: str
    low: Decimal | None = None
    high: Decimal | None = None
    removed: int = 0


def detect_outliers(
    values: list[Decimal],
    *,
    min_sample: int,
    multiplier: float,
    max_removed_share: float,
) -> OutlierDecision:
    """Решает, чистить ли выборку.

    Три случая, и в каждом система обязана объяснить себя:

    * выборка мала — не чистим, понижаем уверенность. На пяти поездах
      «выброс» — это половина рынка направления;
    * границы не строятся (все цены равны) — чистить нечего;
    * правило хочет убрать больше допустимой доли — выборка неоднородна,
      и удаление четверти наблюдений скрыло бы этот факт вместо того, чтобы
      его показать.
    """
    if len(values) < min_sample:
        return OutlierDecision(applied=False, reason="SAMPLE_TOO_SMALL")
    bounds = iqr_bounds(values, multiplier)
    if bounds is None:
        return OutlierDecision(applied=False, reason="NO_SPREAD")
    low, high = bounds
    removed = sum(1 for value in values if value < low or value > high)
    if removed == 0:
        return OutlierDecision(applied=True, reason="NO_OUTLIERS", low=low, high=high, removed=0)
    if removed / len(values) > max_removed_share:
        return OutlierDecision(
            applied=False, reason="TOO_MANY_OUTLIERS", low=low, high=high, removed=removed
        )
    return OutlierDecision(applied=True, reason="APPLIED", low=low, high=high, removed=removed)
