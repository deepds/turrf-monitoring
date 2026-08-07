"""Идентификаторы: ключи наблюдений, идемпотентность, отпечатки предложений.

Три разных ключа, которые легко перепутать:

``job_key``
    Что именно наблюдается. Один и тот же логический факт рынка в разные дни
    даёт один и тот же ``job_key`` — по нему строятся исторические ряды.

``idempotency_key``
    Что именно исполняется. Включает снимок, источник и область исполнения
    (плановый сбор / досбор / принудительный повтор). Без области исполнения
    досбор молча возвращал бы результат планового прогона (SCOPE-R P3.3).

``fingerprint`` / ``equivalence_key``
    Что именно предложено. Отпечаток различает строки источника, ключ
    эквивалентности — физические объекты рынка (рейс, поезд, отель).
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from decimal import Decimal
from typing import Any


def _canonical(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _canonical(value[k]) for k in sorted(value)}
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        # Нормализуем экспоненту: Decimal("100") и Decimal("1E+2") равны, но
        # их строковые формы различаются, и отпечатки разошлись бы.
        return format(value.normalize(), "f")
    if isinstance(value, float):
        return format(Decimal(str(value)).normalize(), "f")
    return value


def digest(*parts: Any, length: int = 32) -> str:
    """Устойчивый хеш от произвольной структуры."""
    payload = json.dumps(_canonical(list(parts)), ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:length]


def job_key(family: str, params: dict[str, Any]) -> str:
    """Ключ логического наблюдения внутри одного снимка.

    Даты входят в ключ: наблюдение «Москва → Сочи на 20 августа» и «на 21
    августа» — разные наблюдения одного снимка.
    """
    return f"{family}:{digest(family, params, length=24)}"


def series_key(family: str, params: dict[str, Any], *, offset_fields: tuple[str, ...]) -> str:
    """Ключ исторического ряда: то же наблюдение в разных снимках.

    Абсолютные даты заменяются на смещение от даты снимка, поэтому «плечо
    Москва → Сочи на D+14» сопоставимо между днями.
    """
    stable = {k: v for k, v in params.items() if k not in offset_fields}
    return f"{family}:{digest(family, stable, length=24)}"


def idempotency_key(
    *,
    snapshot_date: date,
    family: str,
    job_key_value: str,
    source_code: str,
    execution_scope: str,
    attempt_salt: str = "",
) -> str:
    """Ключ исполнения одной попытки источника.

    ``execution_scope`` различает плановый сбор, досбор и ручной повтор.
    ``attempt_salt`` делает принудительный повтор действительно новым: без него
    источник вернул бы прежний результат как ``SKIPPED_IDEMPOTENT``.
    """
    return digest(
        snapshot_date, family, job_key_value, source_code, execution_scope, attempt_salt, length=40
    )


def offer_fingerprint(source_code: str, payload: dict[str, Any]) -> str:
    """Отпечаток строки источника: различает тарифные варианты одного объекта."""
    return digest("offer", source_code, payload, length=40)


def equivalence_key(kind: str, payload: dict[str, Any]) -> str:
    """Ключ физического объекта рынка.

    Для авиа это конкретная связка рейсов, для ЖД — поезд и тип вагона, для
    проживания — объект размещения. По нему схлопывается тарифная сетка: из
    нескольких строк одного объекта в расчёт идёт одна.
    """
    return digest("equiv", kind, payload, length=40)
