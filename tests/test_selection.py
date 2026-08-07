"""Отбор: что является рынком, а что нет.

Каждый тест здесь соответствует дефекту, который однажды дал неверную цифру.
Формулировки причин исключения проверяются наравне с числами: цифра без
объяснения непроверяема.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from tmo.catalog.registry import methodology_profile
from tmo.core.enums import CollectionFamily, ExclusionReason
from tmo.engine.selection import Candidate, select


@pytest.fixture()
def profile():
    return methodology_profile("baseline_v1")


def rail_candidate(
    ref: int,
    price: str,
    *,
    train: str = "001A",
    car_type: str = "COMPARTMENT",
    service_class: str = "2К",
    source: str = "tutu_mcp",
    **transport: object,
) -> Candidate:
    return Candidate(
        ref=ref,
        source_code=source,
        price=Decimal(price),
        currency="RUB",
        equivalence_key=f"{source}|{train}|{car_type}",
        transport={
            "train_number": train,
            "car_type": car_type,
            "service_class": service_class,
            "is_direct": True,
            "seats_left": 10,
            **transport,
        },
    )


def air_candidate(
    ref: int,
    price: str,
    *,
    itinerary: str = "SU100",
    refundable: bool = False,
    direct: bool = True,
    round_trip: bool = True,
    cabin: str = "ECONOMIC",
) -> Candidate:
    return Candidate(
        ref=ref,
        source_code="tutu_mcp",
        price=Decimal(price),
        currency="RUB",
        equivalence_key=itinerary,
        transport={
            "is_direct": direct,
            "is_round_trip": round_trip,
            "cabin": cabin,
            "refundable": refundable,
        },
    )


def hotel_candidate(
    ref: int, price: str, *, stars: int = 4, property_type: str = "HOTEL", name: str = "Отель"
) -> Candidate:
    return Candidate(
        ref=ref,
        source_code="tutu_mcp",
        price=Decimal(price),
        currency="RUB",
        equivalence_key=f"hotel-{ref}",
        property_info={"stars": stars, "property_type": property_type, "name": name},
    )


def run(candidates: list[Candidate], family: CollectionFamily, profile) -> object:
    return select(
        candidates,
        family=family,
        rules=profile.selection_for(family),
        outlier_rules=profile.outliers,
    )


# --------------------------------------------------------------------------- #
# Схлопывание тарифной сетки
# --------------------------------------------------------------------------- #


def test_fare_collapse_keeps_one_row_per_itinerary(profile) -> None:
    """Один рейс участвует в расчёте один раз, по самому дешёвому тарифу.

    Тарифная сетка, посчитанная как набор предложений, сажает медиану на
    третий тариф — вдвое выше того, что платит покупатель.
    """
    candidates = [
        air_candidate(1, "31825", itinerary="A"),
        air_candidate(2, "36213", itinerary="A"),
        air_candidate(3, "38045", itinerary="A", refundable=True),
        air_candidate(4, "40000", itinerary="B"),
    ]
    result = run(candidates, CollectionFamily.AIR, profile)

    assert [d.candidate.ref for d in result.included] == [1, 4]
    collapsed = [d for d in result.excluded if d.reason is ExclusionReason.FARE_COLLAPSED_NOT_CHEAPEST]
    assert [d.candidate.ref for d in collapsed] == [2]
    # Возвратный тариф отбраковывается раньше схлопывания: настоящая причина
    # его исключения — условия, а не «не самый дешёвый».
    refundable = [d for d in result.excluded if d.reason is ExclusionReason.REFUNDABLE_FARE]
    assert [d.candidate.ref for d in refundable] == [3]


def test_rail_fare_collapse_is_per_train_and_car_type(profile) -> None:
    """Пять купейных групп одного поезда дают одно предложение рынка."""
    candidates = [
        rail_candidate(1, "5735.68", train="587М", service_class="2У"),
        rail_candidate(2, "7232.92", train="587М", service_class="2К"),
        rail_candidate(3, "7232.92", train="587М", service_class="2Э"),
        rail_candidate(4, "6100.00", train="102В", service_class="2К"),
    ]
    result = run(candidates, CollectionFamily.RAIL, profile)
    assert sorted(d.candidate.ref for d in result.included) == [1, 4]
    assert result.prices == [Decimal("5735.68"), Decimal("6100.00")]


def test_same_train_from_two_sources_is_not_a_duplicate(profile) -> None:
    """Цена агента и тариф перевозчика — два наблюдения, а не дубликат."""
    candidates = [
        rail_candidate(1, "18951", train="472С", source="tutu_mcp"),
        rail_candidate(2, "16998", train="472С", source="rzd"),
    ]
    result = run(candidates, CollectionFamily.RAIL, profile)
    assert len(result.included) == 2
    assert result.sources == {"tutu_mcp", "rzd"}


# --------------------------------------------------------------------------- #
# Правила методики
# --------------------------------------------------------------------------- #


def test_only_compartment_survives(profile) -> None:
    candidates = [
        rail_candidate(1, "6000", train="1", car_type="COMPARTMENT"),
        rail_candidate(2, "3200", train="2", car_type="RESERVED_SEAT"),
        rail_candidate(3, "1200", train="3", car_type="SEDENTARY"),
        rail_candidate(4, "14000", train="4", car_type="LUX"),
    ]
    result = run(candidates, CollectionFamily.RAIL, profile)
    assert [d.candidate.ref for d in result.included] == [1]
    assert {d.reason for d in result.excluded} == {ExclusionReason.WRONG_CAR_TYPE}


def test_service_class_is_not_filtered(profile) -> None:
    """`2К` и `2Д` не являются обязательным фильтром.

    Обязательный фильтр по сервисному классу отбрасывал 24 315 купейных
    предложений РЖД из 45 420 — больше половины рынка.
    """
    candidates = [
        rail_candidate(1, "6000", train="1", service_class="2К"),
        rail_candidate(2, "6400", train="2", service_class="2Д"),
        rail_candidate(3, "6800", train="3", service_class="2У"),
        rail_candidate(4, "7100", train="4", service_class="2Э"),
    ]
    result = run(candidates, CollectionFamily.RAIL, profile)
    assert len(result.included) == 4


def test_unclassified_car_type_is_excluded_with_its_own_reason(profile) -> None:
    """Неизвестный класс нельзя ни включить, ни выбросить молча.

    Цена сидячего места, вставшая в один ряд с купе, ошибку не покажет.
    """
    result = run(
        [rail_candidate(1, "3000", car_type="UNKNOWN")], CollectionFamily.RAIL, profile
    )
    assert not result.included
    assert result.excluded[0].reason is ExclusionReason.UNCLASSIFIED_CAR_TYPE


def test_disabled_places_group_is_excluded(profile) -> None:
    """Два места по льготной цене занижали наблюдаемый минимум."""
    candidates = [
        rail_candidate(1, "6213", train="1"),
        rail_candidate(2, "4169", train="2", has_places_for_disabled=True),
    ]
    result = run(candidates, CollectionFamily.RAIL, profile)
    assert [d.candidate.ref for d in result.included] == [1]
    assert result.excluded[0].reason is ExclusionReason.DISABLED_PLACES_GROUP
    assert result.prices == [Decimal("6213")]


def test_sold_out_car_is_excluded(profile) -> None:
    """Вагон без мест: цена справочная, купить по ней нельзя."""
    result = run(
        [rail_candidate(1, "6000", seats_left=0)], CollectionFamily.RAIL, profile
    )
    assert result.excluded[0].reason is ExclusionReason.NO_PLACES


def test_connecting_flight_never_enters_direct_sample(profile) -> None:
    result = run(
        [air_candidate(1, "20000", direct=False)], CollectionFamily.AIR, profile
    )
    assert not result.included
    assert result.excluded[0].reason is ExclusionReason.NOT_DIRECT


def test_one_way_offer_is_not_a_round_trip(profile) -> None:
    result = run(
        [air_candidate(1, "20000", round_trip=False)], CollectionFamily.AIR, profile
    )
    assert result.excluded[0].reason is ExclusionReason.NOT_ROUND_TRIP


def test_unconfirmed_refundability_is_excluded(profile) -> None:
    """Невозвратность обязана быть подтверждена, а не предположена."""
    candidate = air_candidate(1, "20000")
    candidate.transport["refundable"] = None
    result = run([candidate], CollectionFamily.AIR, profile)
    assert result.excluded[0].reason is ExclusionReason.REFUNDABLE_FARE
    assert "не подтверждена" in (result.excluded[0].detail or "")


def test_apartment_never_enters_hotel_sample(profile) -> None:
    """Доля апартаментов различается по городам в разы: смешанная выборка
    делает города несопоставимыми."""
    candidates = [
        hotel_candidate(1, "8600"),
        hotel_candidate(2, "13000", property_type="APARTMENT"),
        hotel_candidate(3, "2100", property_type="HOSTEL"),
    ]
    result = run(candidates, CollectionFamily.HOTEL, profile)
    assert [d.candidate.ref for d in result.included] == [1]
    assert {d.reason for d in result.excluded} == {ExclusionReason.WRONG_PROPERTY_TYPE}


def test_wrong_stars_are_excluded(profile) -> None:
    result = run([hotel_candidate(1, "3000", stars=2)], CollectionFamily.HOTEL, profile)
    assert result.excluded[0].reason is ExclusionReason.WRONG_STARS


# --------------------------------------------------------------------------- #
# Провенанс отбора
# --------------------------------------------------------------------------- #


def test_every_exclusion_carries_a_reason(profile) -> None:
    """Исключение без причины делает цифру необъяснимой."""
    candidates = [
        rail_candidate(1, "6000", train="1"),
        rail_candidate(2, "6500", train="1"),
        rail_candidate(3, "3000", train="2", car_type="RESERVED_SEAT"),
        rail_candidate(4, "5000", train="3", seats_left=0),
    ]
    result = run(candidates, CollectionFamily.RAIL, profile)
    assert result.excluded
    assert all(decision.reason is not None for decision in result.excluded)


def test_nothing_is_dropped_silently(profile) -> None:
    """Каждое поданное предложение получает решение."""
    candidates = [rail_candidate(i, str(5000 + i * 100), train=f"T{i}") for i in range(1, 12)]
    result = run(candidates, CollectionFamily.RAIL, profile)
    assert len(result.decisions) == len(candidates)
    assert len(result.included) + len(result.excluded) == len(candidates)
