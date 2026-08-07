"""Контрактные тесты коннекторов.

Проверяют разбор записанных ответов и устойчивость к тому, чем источники
ломали расчёт молча: переименованию аргументов, плавающей форме ответа,
отсутствию поля, нулю вместо отсутствия.

Сети здесь нет: тест, зависящий от чужого сервиса, ломается не по нашей вине.
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from tmo.connectors.contracts import AirQuery, HotelQuery, RailQuery
from tmo.connectors.mcp_client import ArgumentBuilder, bounds, item_type, property_type
from tmo.connectors.rzd import RzdConnector, _available_places, _train_items
from tmo.connectors.tutu import (
    TutuConnector,
    _classify_property,
    _hotel_filter_confirmed,
    _parse_place,
)
from tmo.core.enums import CarType
from tmo.core.errors import ConnectorSchemaError

GOLDEN = Path(__file__).resolve().parents[1] / "golden" / "recorded_raw"


def load(case: str) -> dict:
    return json.loads((GOLDEN / f"{case}.json").read_text(encoding="utf-8"))


@pytest.fixture()
def tutu() -> TutuConnector:
    return TutuConnector.__new__(TutuConnector)


@pytest.fixture()
def rzd() -> RzdConnector:
    return RzdConnector.__new__(RzdConnector)


@pytest.fixture()
def rail_query() -> RailQuery:
    return RailQuery(
        origin_code="MOW",
        origin_name="Москва",
        origin_rzd_code="2000000",
        destination_code="AER",
        destination_name="Сочи",
        destination_rzd_code="2064130",
        service_date=date(2026, 8, 21),
    )


# --------------------------------------------------------------------------- #
# Схема инструмента MCP
# --------------------------------------------------------------------------- #

SCHEMA = {
    "properties": {
        "origin": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        "page_size": {"type": "integer", "minimum": 1, "maximum": 30},
        "stars": {"anyOf": [{"items": {"type": "integer"}, "type": "array"}, {"type": "null"}]},
        "direct_only": {"type": "boolean"},
    }
}


def test_argument_names_come_from_the_schema() -> None:
    """Жёстко зашитое имя однажды перестаёт работать без всякой ошибки."""
    builder = ArgumentBuilder("t", SCHEMA, source_code="tutu_mcp")
    assert builder.set("Москва", "from_city", "origin") == "origin"
    assert builder.args == {"origin": "Москва"}


def test_missing_required_argument_raises_instead_of_silence(tutu: TutuConnector) -> None:
    """Молчаливый пропуск параметра даёт выдачу по другому запросу."""
    builder = ArgumentBuilder("t", SCHEMA, source_code="tutu_mcp")
    with pytest.raises(ConnectorSchemaError):
        builder.set("Москва", "city_name", required=True)


def test_value_is_clamped_to_schema_bounds() -> None:
    """`page_size=50` при максимуме 30 отклоняет запрос целиком."""
    builder = ArgumentBuilder("t", SCHEMA, source_code="tutu_mcp")
    builder.set(50, "page_size")
    assert builder.args["page_size"] == 30
    assert builder.adjustments


def test_scalar_is_coerced_to_declared_array() -> None:
    """`stars` объявлен массивом: скаляр отклоняется валидацией источника."""
    builder = ArgumentBuilder("t", SCHEMA, source_code="tutu_mcp")
    builder.set(3, "stars")
    assert builder.args["stars"] == [3]


def test_schema_introspection_helpers() -> None:
    assert property_type(SCHEMA, "origin") == "string"
    assert property_type(SCHEMA, "page_size") == "integer"
    assert item_type(SCHEMA, "stars") == "integer"
    assert bounds(SCHEMA, "page_size") == (1.0, 30.0)


# --------------------------------------------------------------------------- #
# Разбор пунктов маршрута
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("value", "city", "code"),
    [
        ("Москва — Павелецкий вокзал (2000005)", "Москва", "2000005"),
        ("Сочи, 2064130", "Сочи", "2064130"),
        ("Москва — Шереметьево (SVO)", "Москва", "SVO"),
        ("Сочи, AER", "Сочи", "AER"),
    ],
)
def test_places_arrive_as_strings_not_objects(value: str, city: str, code: str) -> None:
    parsed = _parse_place(value)
    assert parsed["city"] == city
    assert parsed["code"] == code


def test_empty_place_does_not_crash() -> None:
    assert _parse_place(None)["city"] is None


# --------------------------------------------------------------------------- #
# ЖД: класс вагона
# --------------------------------------------------------------------------- #


def test_rail_car_type_comes_from_seat_category(tutu: TutuConnector, rail_query: RailQuery) -> None:
    """Класс берётся из поля категории, а не выводится из кода обслуживания.

    `2В` и `2С` — сидячие, а не купе: вывод по первому символу дал бы цену
    сидячего места в одном ряду с купе.
    """
    case = load("rail_tutu_compartment")
    offers = tutu._parse_rail(case["payload"]["offers"], rail_query)
    assert offers
    assert all(offer.transport["car_type"] == CarType.COMPARTMENT.value for offer in offers)
    # Сервисные классы разные — и это не мешает им быть купе.
    classes = {offer.transport["service_class"] for offer in offers}
    assert len(classes) > 1


def test_rail_offer_without_variants_is_unclassified(
    tutu: TutuConnector, rail_query: RailQuery
) -> None:
    """Компактная выдача без тарифных строк не даёт подставить класс из запроса."""
    payload = [{"offer_id": "x", "price": {"amount": 5000, "currency": "RUB"}, "legs": []}]
    offers = tutu._parse_rail(payload, rail_query)
    assert offers[0].transport["car_type"] == CarType.UNKNOWN.value


def test_empty_response_yields_no_offers(tutu: TutuConnector, rail_query: RailQuery) -> None:
    assert tutu._parse_rail([], rail_query) == []


# --------------------------------------------------------------------------- #
# Авиа
# --------------------------------------------------------------------------- #


def test_air_direct_means_one_segment_per_leg(tutu: TutuConnector) -> None:
    """Прямой — по одному сегменту в каждом плече, а не «мало сегментов всего»."""
    case = load("air_round_trip_fare_grid")
    query = AirQuery(
        origin_code="MOW",
        origin_name="Москва",
        origin_metro_code="MOW",
        destination_code="AER",
        destination_name="Сочи",
        destination_metro_code="AER",
        departure_date=date(2026, 8, 21),
        return_date=date(2026, 8, 28),
    )
    offers = tutu._parse_air(case["payload"]["offers"], query)
    assert offers
    assert all(offer.transport["is_direct"] for offer in offers)
    assert all(offer.transport["is_round_trip"] for offer in offers)
    assert all(offer.transport["outbound_segments"] == 1 for offer in offers)
    assert all(offer.transport["inbound_segments"] == 1 for offer in offers)


def test_air_itinerary_is_the_equivalence_basis(tutu: TutuConnector) -> None:
    """Тарифные строки одного рейса обязаны иметь один маршрутный ключ."""
    case = load("air_round_trip_fare_grid")
    query = AirQuery(
        origin_code="MOW", origin_name="Москва", origin_metro_code="MOW",
        destination_code="AER", destination_name="Сочи", destination_metro_code="AER",
        departure_date=date(2026, 8, 21), return_date=date(2026, 8, 28),
    )
    offers = tutu._parse_air(case["payload"]["offers"], query)
    itineraries = {
        tuple((s["voyage_no"], s["departure_at"]) for s in offer.transport["itinerary"])
        for offer in offers
    }
    assert len(itineraries) < len(offers), "тарифные строки должны схлопываться в рейсы"


# --------------------------------------------------------------------------- #
# Проживание
# --------------------------------------------------------------------------- #


def test_hotel_price_basis_is_respected(tutu: TutuConnector) -> None:
    """`stay_total` — цена за весь период. Домножать её на ночи нельзя."""
    case = load("hotel_sochi_3star_one_night")
    query = HotelQuery(
        city_code="AER", city_name="Сочи",
        check_in=date(2026, 8, 21), check_out=date(2026, 8, 22), stars=3,
    )
    offers = tutu._parse_hotels(
        case["payload"]["hotels"], query, case["payload"]["stay"]
    )
    assert offers
    first = case["payload"]["hotels"][0]["best_offer"]["price"]["amount"]
    assert offers[0].price == Decimal(str(first))
    assert offers[0].metadata["price_basis_raw"] == "stay_total"


def test_server_filter_confirmation_is_read_from_meta() -> None:
    """Отправленный фильтр и применённый фильтр — разные вещи."""
    case = load("hotel_sochi_3star_one_night")
    confirmed = _hotel_filter_confirmed(case["payload"]["meta"])
    assert confirmed == {"stars": True, "hotel_type": True}
    assert _hotel_filter_confirmed({}) == {"stars": False, "hotel_type": False}


@pytest.mark.parametrize(
    ("name", "room", "expected"),
    [
        ("Отель «Приморье»", "Стандарт", "HOTEL_LIKELY"),
        ("Апарт-отель \"Парк Горького\"", "Студия", "NON_HOTEL_SUSPECT"),
        ("Гостевой дом Грейс Эдем", "Двухместный", "NON_HOTEL_SUSPECT"),
        ("Хостел на Морской", "Койко-место", "NON_HOTEL_SUSPECT"),
    ],
)
def test_property_classification(name: str, room: str, expected: str) -> None:
    assert _classify_property(name, room) == expected


# --------------------------------------------------------------------------- #
# РЖД: плавающая форма ответа
# --------------------------------------------------------------------------- #


def test_rzd_reads_both_response_shapes() -> None:
    """Поезда приходят либо в `Trains`, либо вложенно в `Tp[].List`."""
    assert len(_train_items({"Trains": [{"a": 1}]})) == 1
    assert len(_train_items({"Tp": [{"List": [{"a": 1}, {"b": 2}]}]})) == 2
    assert _train_items({"unknown": []}) == []


def test_zero_places_is_meaningful_not_missing() -> None:
    """`TotalPlaceQuantity` читается с явной проверкой на None.

    Через `or` ноль проваливается в запасное поле и оттуда в None: распроданный
    вагон выглядит вагоном с неизвестным числом мест.
    """
    assert _available_places({"TotalPlaceQuantity": 0}) == 0


def test_place_quantity_zero_does_not_hide_real_seats() -> None:
    """Живой ответ РЖД: `PlaceQuantity: 0` при 182 нижних и 195 верхних местах.

    Чтение `PlaceQuantity` раньше компонентов выбросило бы продающийся вагон.
    """
    group = {
        "PlaceQuantity": 0,
        "LowerPlaceQuantity": 182,
        "UpperPlaceQuantity": 195,
        "LowerSidePlaceQuantity": 0,
        "UpperSidePlaceQuantity": 0,
    }
    assert _available_places(group) == 377


def test_rzd_parses_live_response(rzd: RzdConnector, rail_query: RailQuery) -> None:
    case = load("rail_rzd_compartment")
    offers, diagnostics = rzd._parse(case["payload"]["Trains"], rail_query)
    assert offers
    car_types = {offer.transport["car_type"] for offer in offers}
    # Коннектор не отбраковывает классы: это делает методика.
    assert CarType.COMPARTMENT.value in car_types
    assert CarType.RESERVED_SEAT.value in car_types
    assert "dropped" in diagnostics


def test_rzd_keeps_disabled_group_visible(rzd: RzdConnector, rail_query: RailQuery) -> None:
    """Льготная группа передаётся дальше с признаком, а не гасится молча.

    Исключённое предложение обязано оставаться видимым в детализации цены.
    """
    payload = [
        {
            "TrainNumber": "001А",
            "DepartureDateTime": "2026-08-21T10:00:00",
            "CarGroups": [
                {
                    "CarType": "Compartment",
                    "MinPrice": 4169,
                    "TotalPlaceQuantity": 2,
                    "HasPlacesForDisabledPersons": True,
                }
            ],
        }
    ]
    offers, diagnostics = rzd._parse(payload, rail_query)
    assert len(offers) == 1
    assert offers[0].transport["has_places_for_disabled"] is True
    assert diagnostics["dropped"]["disabled_places"] == 1
