"""Нормализация: приведение единицы цены и классификация признаков.

Ошибка здесь — ошибка на порядок: цена «за всех пассажиров», принятая за цену
одного, удваивает витрину; цена периода, домноженная на число ночей, удваивает
проживание.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

import pytest

from tmo.connectors.contracts import AirQuery, HotelQuery, ProviderOffer, RailQuery
from tmo.core.enums import PriceBasis, PropertyType
from tmo.normalization.normalizer import normalize

RAIL_QUERY = RailQuery(
    origin_code="MOW", origin_name="Москва", origin_rzd_code="2000000",
    destination_code="AER", destination_name="Сочи", destination_rzd_code="2064130",
    service_date=date(2026, 8, 21),
)
AIR_QUERY = AirQuery(
    origin_code="MOW", origin_name="Москва", origin_metro_code="MOW",
    destination_code="AER", destination_name="Сочи", destination_metro_code="AER",
    departure_date=date(2026, 8, 21), return_date=date(2026, 8, 28),
)
HOTEL_QUERY = HotelQuery(
    city_code="AER", city_name="Сочи",
    check_in=date(2026, 8, 21), check_out=date(2026, 8, 24), stars=4,
)


def rail_offer(**kwargs: object) -> ProviderOffer:
    base = {
        "kind": "RAIL",
        "source_offer_id": "x",
        "currency": "RUB",
        "price": Decimal("5735.68"),
        "price_basis": PriceBasis.PER_PASSENGER_LEG,
        "origin_code": "MOW",
        "destination_code": "AER",
        "departure_at": datetime(2026, 8, 21, 15, 54),
        "transport": {"train_number": "587М", "car_type": "COMPARTMENT", "segments_count": 1},
    }
    base.update(kwargs)
    return ProviderOffer(**base)  # type: ignore[arg-type]


def air_offer(**kwargs: object) -> ProviderOffer:
    base = {
        "kind": "AIR",
        "source_offer_id": "x",
        "currency": "RUB",
        "price": Decimal("31825"),
        "price_basis": PriceBasis.ALL_PASSENGERS_ROUND_TRIP,
        "origin_code": "MOW",
        "destination_code": "AER",
        "departure_at": datetime(2026, 8, 21, 19, 40),
        "return_departure_at": datetime(2026, 8, 28, 7, 5),
        "transport": {
            "is_direct": True,
            "is_round_trip": True,
            "cabin": "ECONOMIC",
            "passenger_count": 1,
            "itinerary": [{"voyage_no": "N4-6503", "departure_at": "2026-08-21T19:40:00"}],
        },
    }
    base.update(kwargs)
    return ProviderOffer(**base)  # type: ignore[arg-type]


def hotel_offer(**kwargs: object) -> ProviderOffer:
    base = {
        "kind": "HOTEL",
        "source_offer_id": "x",
        "currency": "RUB",
        "price": Decimal("24000"),
        "price_basis": PriceBasis.STAY_TOTAL,
        "city_code": "AER",
        "check_in": date(2026, 8, 21),
        "check_out": date(2026, 8, 24),
        "nights": 3,
        "property_info": {"property_id": "1", "name": "Отель", "stars": 4},
    }
    base.update(kwargs)
    return ProviderOffer(**base)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Единицы цены
# --------------------------------------------------------------------------- #


def test_air_price_for_all_passengers_is_reduced_to_one() -> None:
    """Цена авиа — за всех пассажиров: проверено сравнением adults=1 и adults=2."""
    offer = air_offer(price=Decimal("63650"))
    offer.transport["passenger_count"] = 2
    result = normalize(offer, AIR_QUERY, source_code="tutu_mcp")
    assert result.price == Decimal("31825.00")
    assert result.price_basis == PriceBasis.PER_PASSENGER_ROUND_TRIP.value
    assert result.source_price == Decimal("63650")


def test_hotel_stay_total_is_not_multiplied_by_nights() -> None:
    """`stay_total` — итог за период. Домножение удваивает цену."""
    result = normalize(hotel_offer(), HOTEL_QUERY, source_code="tutu_mcp")
    assert result.price == Decimal("24000")
    assert result.nights == 3


def test_per_night_price_is_marked_when_derived() -> None:
    """Приведение цены ночи к периоду допустимо, но обязано быть помечено."""
    offer = hotel_offer(price=Decimal("8000"), price_basis=PriceBasis.PER_NIGHT)
    result = normalize(offer, HOTEL_QUERY, source_code="tutu_mcp")
    assert result.price == Decimal("24000")
    assert "PRICE_DERIVED_FROM_PER_NIGHT" in result.validation_flags


def test_rail_price_stays_per_leg() -> None:
    result = normalize(rail_offer(), RAIL_QUERY, source_code="rzd")
    assert result.price == Decimal("5735.68")
    assert result.price_basis == PriceBasis.PER_PASSENGER_LEG.value


def test_offer_without_price_is_not_stored() -> None:
    assert normalize(rail_offer(price=None), RAIL_QUERY, source_code="rzd") is None
    assert normalize(rail_offer(price=Decimal("0")), RAIL_QUERY, source_code="rzd") is None


# --------------------------------------------------------------------------- #
# Отметки нарушений
# --------------------------------------------------------------------------- #


def test_wrong_departure_date_is_flagged() -> None:
    """Источник ответил про другой день — цифру нельзя ставить в этот ряд."""
    offer = rail_offer(departure_at=datetime(2026, 8, 22, 10, 0))
    result = normalize(offer, RAIL_QUERY, source_code="rzd")
    assert "DEPARTURE_DATE_MISMATCH" in result.validation_flags


def test_one_way_air_is_flagged() -> None:
    offer = air_offer()
    offer.transport["is_round_trip"] = False
    result = normalize(offer, AIR_QUERY, source_code="tutu_mcp")
    assert "NOT_ROUND_TRIP" in result.validation_flags


def test_unknown_car_type_is_flagged() -> None:
    offer = rail_offer()
    offer.transport["car_type"] = "UNKNOWN"
    result = normalize(offer, RAIL_QUERY, source_code="tutu_mcp")
    assert "CAR_TYPE_UNKNOWN" in result.validation_flags


def test_flags_do_not_delete_the_offer() -> None:
    """Предложение с нарушением сохраняется: решает методика, а не нормализация."""
    offer = rail_offer(departure_at=datetime(2026, 9, 1, 10, 0))
    result = normalize(offer, RAIL_QUERY, source_code="rzd")
    assert result is not None
    assert result.price == Decimal("5735.68")


# --------------------------------------------------------------------------- #
# Классификация типа размещения
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("hint", "raw", "expected"),
    [
        ("HOTEL_LIKELY", None, PropertyType.HOTEL.value),
        ("NON_HOTEL_SUSPECT", None, PropertyType.APARTMENT.value),
        ("HOTEL_LIKELY", "apartments", PropertyType.APARTMENT.value),
        ("NON_HOTEL_SUSPECT", "hotel", PropertyType.HOTEL.value),
    ],
)
def test_property_type_uses_source_value_first(hint: str, raw: str | None, expected: str) -> None:
    """Явный тип от источника важнее подсказки по названию."""
    offer = hotel_offer()
    offer.property_info["property_type_hint"] = hint
    offer.property_info["property_type_raw"] = raw
    result = normalize(offer, HOTEL_QUERY, source_code="tutu_mcp")
    assert result.property_attributes["property_type"] == expected


# --------------------------------------------------------------------------- #
# Ключи
# --------------------------------------------------------------------------- #


def test_equivalence_key_separates_car_types() -> None:
    """Плацкарт и купе одного поезда — разные объекты рынка."""
    compartment = normalize(rail_offer(), RAIL_QUERY, source_code="rzd")
    reserved = rail_offer()
    reserved.transport["car_type"] = "RESERVED_SEAT"
    reserved_norm = normalize(reserved, RAIL_QUERY, source_code="rzd")
    assert compartment.equivalence_key != reserved_norm.equivalence_key


def test_equivalence_key_separates_sources() -> None:
    """Один поезд у двух продавцов — два наблюдения, а не дубликат."""
    tutu = normalize(rail_offer(), RAIL_QUERY, source_code="tutu_mcp")
    rzd = normalize(rail_offer(), RAIL_QUERY, source_code="rzd")
    assert tutu.equivalence_key != rzd.equivalence_key


def test_fingerprint_separates_fare_rows() -> None:
    """Отпечаток различает тарифные строки одного поезда."""
    first = normalize(rail_offer(source_offer_id="a"), RAIL_QUERY, source_code="rzd")
    second = normalize(
        rail_offer(source_offer_id="b", price=Decimal("7232.92")), RAIL_QUERY, source_code="rzd"
    )
    assert first.fingerprint != second.fingerprint
    assert first.equivalence_key == second.equivalence_key
