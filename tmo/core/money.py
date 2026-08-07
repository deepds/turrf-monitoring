"""Деньги.

Цены хранятся и считаются в ``Decimal``. Округление до копеек делается один
раз — на выходе агрегации, а не в промежуточных шагах.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

CENTS = Decimal("0.01")
BASE_CURRENCY = "RUB"


def to_decimal(value: Any) -> Decimal | None:
    """Безопасное приведение к Decimal. Мусор превращается в None, не в ноль."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        return Decimal(str(value))
    text = str(value).strip().replace(" ", "").replace(" ", "").replace(",", ".")
    if not text:
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def quantize(value: Decimal | None) -> Decimal | None:
    if value is None:
        return None
    return value.quantize(CENTS, rounding=ROUND_HALF_UP)


def add(*values: Decimal | None) -> Decimal | None:
    """Сумма, в которой отсутствующее слагаемое делает результат отсутствующим.

    Складывать плечо с ``None`` и получать цену одного плеча значило бы
    показать половину поездки как всю поездку.
    """
    total = Decimal(0)
    for value in values:
        if value is None:
            return None
        total += value
    return total


def is_positive(value: Decimal | None) -> bool:
    return value is not None and value > 0
