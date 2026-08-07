"""Нормализация: предложение источника → Offer.

Здесь происходят ровно три вещи, и ни одна из них не является методикой:

1. **Приведение единицы цены.** Источники отдают цену за разное: Туту по авиа —
   за всех пассажиров и оба плеча, по ЖД — за одно место в одну сторону, по
   отелям — за весь период. ``price_basis`` фиксирует, что пришло; ``price``
   приведена к единице методики. Ошибиться здесь — ошибиться на порядок.

2. **Классификация наблюдаемых признаков.** Тип вагона, прямой ли рейс, тип
   объекта размещения. Классификация не отбраковывает: она превращает сырой
   признак в проверяемое значение.

3. **Отметка нарушений.** Несовпадение маршрута, дат, отрицательная цена
   попадают в ``validation_flags``. Предложение при этом сохраняется: решение,
   что с ним делать, принимает методика, и при её смене оно может измениться.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from tmo.connectors.contracts import AirQuery, HotelQuery, ProviderOffer, Query, RailQuery
from tmo.core.enums import CarType, CollectionFamily, PriceBasis, PropertyType
from tmo.core.ids import equivalence_key, offer_fingerprint
from tmo.core.money import is_positive
from tmo.core.timeutil import to_utc
from tmo.version import OFFER_SCHEMA_VERSION


@dataclass(slots=True)
class NormalizedOffer:
    """Offer до записи в базу. Поля повторяют модель ``offers``."""

    source_code: str
    kind: str
    currency: str
    price: Decimal
    source_price: Decimal | None
    price_basis: str
    fingerprint: str
    equivalence_key: str
    schema_version: str = OFFER_SCHEMA_VERSION
    origin_code: str | None = None
    destination_code: str | None = None
    city_code: str | None = None
    departure_at: datetime | None = None
    arrival_at: datetime | None = None
    return_departure_at: datetime | None = None
    return_arrival_at: datetime | None = None
    check_in: date | None = None
    check_out: date | None = None
    nights: int | None = None
    transport_attributes: dict[str, Any] = field(default_factory=dict)
    property_attributes: dict[str, Any] = field(default_factory=dict)
    source_metadata: dict[str, Any] = field(default_factory=dict)
    deeplink: str | None = None
    validation_flags: list[str] = field(default_factory=list)
    raw_index: int = 0
    raw_page: int = 1


class NormalizationError(Exception):
    """Предложение невозможно нормализовать: нет цены или неизвестен вид."""


def normalize(
    offer: ProviderOffer,
    query: Query,
    *,
    source_code: str,
) -> NormalizedOffer | None:
    """Возвращает Offer или ``None``, если предложение непригодно даже к записи.

    ``None`` — только для случаев, когда записывать нечего: нет цены вообще.
    Всё, что имеет цену, сохраняется, пусть и с отметками нарушений.
    """
    if not is_positive(offer.price):
        return None

    if isinstance(query, RailQuery):
        return _normalize_rail(offer, query, source_code)
    if isinstance(query, AirQuery):
        return _normalize_air(offer, query, source_code)
    if isinstance(query, HotelQuery):
        return _normalize_hotel(offer, query, source_code)
    raise NormalizationError(f"Неизвестный тип запроса: {type(query).__name__}")


# --------------------------------------------------------------------------- #
# ЖД
# --------------------------------------------------------------------------- #


def _normalize_rail(offer: ProviderOffer, query: RailQuery, source_code: str) -> NormalizedOffer:
    transport = dict(offer.transport)
    flags: list[str] = []

    # Оба источника отдают цену за одно место в одну сторону. Пассажир один,
    # поэтому приведение тождественно — но зафиксировано явно, чтобы смена
    # состава пассажиров не потребовала догадок.
    price = offer.price
    if offer.price_basis is not PriceBasis.PER_PASSENGER_LEG:
        flags.append("UNEXPECTED_PRICE_BASIS")

    car_type = CarType(transport.get("car_type") or CarType.UNKNOWN.value)
    if car_type is CarType.UNKNOWN:
        flags.append("CAR_TYPE_UNKNOWN")

    segments = transport.get("segments_count")
    if segments is not None and int(segments) != 1:
        flags.append("NOT_DIRECT")

    departure_at = _as_utc(offer.departure_at)
    if departure_at is not None and offer.departure_at is not None:
        # Дата отправления обязана совпадать с датой наблюдения. Расхождение
        # означает, что источник ответил про другой день, — и такую цифру
        # нельзя ставить в ряд «медиана на 20 августа».
        local_date = offer.departure_at.date()
        if local_date != query.service_date:
            flags.append("DEPARTURE_DATE_MISMATCH")

    if offer.origin_code != query.origin_code or offer.destination_code != query.destination_code:
        flags.append("ROUTE_MISMATCH")

    equivalence = equivalence_key(
        "RAIL",
        {
            "source": source_code,
            "train": str(transport.get("train_number") or "").upper(),
            "departure": offer.departure_at.isoformat() if offer.departure_at else None,
            "car_type": car_type.value,
        },
    )
    return NormalizedOffer(
        source_code=source_code,
        kind="RAIL",
        currency=offer.currency,
        price=price,
        source_price=offer.price,
        price_basis=PriceBasis.PER_PASSENGER_LEG.value,
        fingerprint=_fingerprint(source_code, offer),
        equivalence_key=equivalence,
        origin_code=query.origin_code,
        destination_code=query.destination_code,
        departure_at=departure_at,
        arrival_at=_as_utc(offer.arrival_at),
        transport_attributes=transport,
        source_metadata=dict(offer.metadata),
        deeplink=offer.deeplink,
        validation_flags=flags,
        raw_index=offer.raw_index,
        raw_page=offer.raw_page,
    )


# --------------------------------------------------------------------------- #
# Авиа
# --------------------------------------------------------------------------- #


def _normalize_air(offer: ProviderOffer, query: AirQuery, source_code: str) -> NormalizedOffer:
    transport = dict(offer.transport)
    flags: list[str] = []

    passengers = int(transport.get("passenger_count") or query.adults or 1)
    price = offer.price
    if offer.price_basis is PriceBasis.ALL_PASSENGERS_ROUND_TRIP:
        # Цена за всех пассажиров: приводим к одному. Проверено сравнением
        # adults=1 и adults=2 — отношение 1,97.
        if passengers > 1:
            price = (offer.price / passengers).quantize(Decimal("0.01"))
    elif offer.price_basis is not PriceBasis.PER_PASSENGER_ROUND_TRIP:
        flags.append("UNEXPECTED_PRICE_BASIS")

    if not transport.get("is_round_trip"):
        # Одностороннее предложение не является круговым тарифом. Складывать
        # два таких в «круговой» запрещено методикой.
        flags.append("NOT_ROUND_TRIP")
    if not transport.get("is_direct"):
        flags.append("NOT_DIRECT")
    cabin = str(transport.get("cabin") or "").upper()
    if cabin and not cabin.startswith("ECONOM"):
        flags.append("NOT_ECONOMY")

    if offer.departure_at and offer.departure_at.date() != query.departure_date:
        flags.append("DEPARTURE_DATE_MISMATCH")
    if offer.return_departure_at and offer.return_departure_at.date() != query.return_date:
        flags.append("RETURN_DATE_MISMATCH")

    itinerary = transport.get("itinerary") or []
    equivalence = equivalence_key(
        "AIR",
        {
            "source": source_code,
            "legs": [
                {"voyage": str(seg.get("voyage_no") or ""), "departure": seg.get("departure_at")}
                for seg in itinerary
            ],
        },
    )
    return NormalizedOffer(
        source_code=source_code,
        kind="AIR",
        currency=offer.currency,
        price=price,
        source_price=offer.price,
        price_basis=PriceBasis.PER_PASSENGER_ROUND_TRIP.value,
        fingerprint=_fingerprint(source_code, offer),
        equivalence_key=equivalence,
        origin_code=query.origin_code,
        destination_code=query.destination_code,
        departure_at=_as_utc(offer.departure_at),
        arrival_at=_as_utc(offer.arrival_at),
        return_departure_at=_as_utc(offer.return_departure_at),
        return_arrival_at=_as_utc(offer.return_arrival_at),
        nights=(query.return_date - query.departure_date).days,
        transport_attributes=transport,
        source_metadata=dict(offer.metadata),
        deeplink=offer.deeplink,
        validation_flags=flags,
        raw_index=offer.raw_index,
        raw_page=offer.raw_page,
    )


# --------------------------------------------------------------------------- #
# Проживание
# --------------------------------------------------------------------------- #


def _normalize_hotel(offer: ProviderOffer, query: HotelQuery, source_code: str) -> NormalizedOffer:
    info = dict(offer.property_info)
    flags: list[str] = []

    nights = int(offer.nights or query.nights or 1)
    price = offer.price
    if offer.price_basis is PriceBasis.PER_NIGHT:
        # Приведение цены ночи к периоду допустимо только когда источник
        # объявил базу «за ночь». Обратное — умножение периода на ночи —
        # удваивает цену и методикой запрещено.
        price = offer.price * nights
        flags.append("PRICE_DERIVED_FROM_PER_NIGHT")
    elif offer.price_basis is not PriceBasis.STAY_TOTAL:
        flags.append("UNEXPECTED_PRICE_BASIS")

    property_type = _classify_property_type(info)
    info["property_type"] = property_type.value
    if property_type is not PropertyType.HOTEL:
        flags.append("NOT_HOTEL")

    stars = info.get("stars")
    if stars is None or int(stars) != query.stars:
        flags.append("STARS_MISMATCH")

    if offer.check_in and offer.check_in != query.check_in:
        flags.append("CHECK_IN_MISMATCH")
    if offer.check_out and offer.check_out != query.check_out:
        flags.append("CHECK_OUT_MISMATCH")
    if nights != query.nights:
        flags.append("NIGHTS_MISMATCH")

    equivalence = equivalence_key(
        "HOTEL",
        {
            "source": source_code,
            "property": str(info.get("property_id") or info.get("name") or ""),
            "check_in": query.check_in.isoformat(),
            "check_out": query.check_out.isoformat(),
        },
    )
    return NormalizedOffer(
        source_code=source_code,
        kind="HOTEL",
        currency=offer.currency,
        price=price,
        source_price=offer.price,
        price_basis=PriceBasis.STAY_TOTAL.value,
        fingerprint=_fingerprint(source_code, offer),
        equivalence_key=equivalence,
        city_code=query.city_code,
        check_in=offer.check_in or query.check_in,
        check_out=offer.check_out or query.check_out,
        nights=nights,
        property_attributes=info,
        source_metadata=dict(offer.metadata),
        deeplink=offer.deeplink,
        validation_flags=flags,
        raw_index=offer.raw_index,
        raw_page=offer.raw_page,
    )


def _classify_property_type(info: dict[str, Any]) -> PropertyType:
    """Тип объекта размещения.

    Источник тип не возвращает: поле пустое у всех объектов. Поэтому
    классификация опирается на серверный фильтр (он подтверждается в
    ``meta.filters_applied``) и на подсказку по названию. Подсказка может
    ошибиться в обе стороны, поэтому она понижает до APARTMENT только при
    явном признаке, а не при его отсутствии.
    """
    raw = str(info.get("property_type_raw") or "").strip().lower()
    if raw:
        mapping = {
            "hotel": PropertyType.HOTEL,
            "отель": PropertyType.HOTEL,
            "гостиница": PropertyType.HOTEL,
            "apartments": PropertyType.APARTMENT,
            "apartment": PropertyType.APARTMENT,
            "апартаменты": PropertyType.APARTMENT,
            "hostel": PropertyType.HOSTEL,
            "хостел": PropertyType.HOSTEL,
            "aparthotel": PropertyType.APARTHOTEL,
            "guesthouse": PropertyType.GUESTHOUSE,
        }
        if raw in mapping:
            return mapping[raw]
    if info.get("property_type_hint") == "NON_HOTEL_SUSPECT":
        return PropertyType.APARTMENT
    return PropertyType.HOTEL


# --------------------------------------------------------------------------- #
# Общее
# --------------------------------------------------------------------------- #


def _fingerprint(source_code: str, offer: ProviderOffer) -> str:
    return offer_fingerprint(
        source_code,
        {
            "kind": offer.kind,
            "id": offer.source_offer_id,
            "price": offer.price,
            "currency": offer.currency,
            "departure": offer.departure_at.isoformat() if offer.departure_at else None,
            "return": offer.return_departure_at.isoformat() if offer.return_departure_at else None,
            "check_in": offer.check_in.isoformat() if offer.check_in else None,
            "transport": {
                k: v
                for k, v in offer.transport.items()
                if k in ("train_number", "car_type", "service_class", "fare_family", "cabin")
            },
            "property": {
                k: v for k, v in offer.property_info.items() if k in ("property_id", "room_name")
            },
        },
    )


def _as_utc(value: datetime | None) -> datetime | None:
    return to_utc(value) if value is not None else None


def normalize_batch(
    offers: list[ProviderOffer], query: Query, *, source_code: str
) -> tuple[list[NormalizedOffer], int]:
    """Нормализует пачку, возвращая также число отброшенных без цены."""
    result: list[NormalizedOffer] = []
    dropped = 0
    for offer in offers:
        normalized = normalize(offer, query, source_code=source_code)
        if normalized is None:
            dropped += 1
            continue
        result.append(normalized)
    return result, dropped


def family_of(query: Query) -> CollectionFamily:
    if isinstance(query, RailQuery):
        return CollectionFamily.RAIL
    if isinstance(query, AirQuery):
        return CollectionFamily.AIR
    return CollectionFamily.HOTEL
