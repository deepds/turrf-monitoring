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


@pytest.fixture()
def profile_v2():
    return methodology_profile("baseline_v2")


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
    ref: int,
    price: str,
    *,
    stars: int = 4,
    property_type: str = "HOTEL",
    name: str = "Отель",
    room: str = "Номер стандарт",
) -> Candidate:
    return Candidate(
        ref=ref,
        source_code="tutu_mcp",
        price=Decimal(price),
        currency="RUB",
        equivalence_key=f"hotel-{ref}",
        property_info={
            "stars": stars,
            "property_type": property_type,
            "name": name,
            "room_name": room,
        },
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


def test_high_speed_seated_enters_the_sample_but_ordinary_seated_does_not(
    profile_v2,
) -> None:
    """Сидячее место допускается по имени поезда, а не по типу вагона.

    ``SEDENTARY`` объединяет «Сапсан» со средней ценой 17 323 ₽ и пригородный
    сидячий вагон за 292 ₽ — разброс внутри типа в пятьдесят раз. Разрешить
    тип целиком значило бы описать медианой не рынок, а состав выдачи.
    """
    candidates = [
        rail_candidate(1, "6000", train="1", car_type="COMPARTMENT"),
        rail_candidate(2, "17300", train="751", car_type="SEDENTARY", train_name="САПСАН"),
        rail_candidate(3, "2800", train="723", car_type="SEDENTARY", train_name="ЛАСТОЧКА"),
        rail_candidate(4, "292", train="6001", car_type="SEDENTARY"),
        rail_candidate(5, "3200", train="2", car_type="RESERVED_SEAT"),
    ]
    result = run(candidates, CollectionFamily.RAIL, profile_v2)

    assert [d.candidate.ref for d in result.included] == [1, 2]
    assert {d.reason for d in result.excluded} == {ExclusionReason.WRONG_CAR_TYPE}


def test_high_speed_allowance_covers_seated_only(profile_v2) -> None:
    """Купе и СВ «Сапсана» остаются за методикой.

    Бизнес-класс за 103 000 ₽ — не тот рынок, о котором показатель. Имя поезда
    открывает дверь сидячему месту, а не поезду целиком.
    """
    candidates = [
        rail_candidate(1, "103000", train="751", car_type="LUX", train_name="САПСАН"),
        rail_candidate(2, "60000", train="751", car_type="SOFT", train_name="САПСАН"),
    ]
    result = run(candidates, CollectionFamily.RAIL, profile_v2)

    assert not result.included
    assert {d.reason for d in result.excluded} == {ExclusionReason.WRONG_CAR_TYPE}


def test_baseline_v1_still_takes_compartment_only(profile) -> None:
    """Прежняя версия не меняет поведения от появления новой.

    На baseline_v1 ссылаются уже показанные цифры. Версия, поехавшая вслед за
    кодом, переписала бы историю задним числом.
    """
    candidates = [
        rail_candidate(1, "6000", train="1", car_type="COMPARTMENT"),
        rail_candidate(2, "17300", train="751", car_type="SEDENTARY", train_name="САПСАН"),
    ]
    result = run(candidates, CollectionFamily.RAIL, profile)

    assert [d.candidate.ref for d in result.included] == [1]


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


def test_premium_room_categories_are_excluded_by_name(profile_v2) -> None:
    """Категория номера берётся из названия — другого признака источник не даёт.

    Ни выдача поиска, ни детали предложения не содержат ни ``category``, ни
    ``class``: класс закодирован словом внутри названия. Проверено на живом
    ответе Туту 12.08.2026.
    """
    candidates = [
        hotel_candidate(1, "5000", room="Двухместный номер Standard"),
        hotel_candidate(2, "5200", room="Бюджетный двухместный номер без окна"),
        hotel_candidate(3, "5400", room="Номер комфорт с 1 двуспальной кроватью"),
        hotel_candidate(4, "12000", room="Двухместный номер Deluxe"),
        hotel_candidate(5, "24000", room="Двухместный люкс c 1 комнатой"),
        hotel_candidate(6, "9000", room="Апартаменты c 1 комнатой"),
        hotel_candidate(7, "7000", room="Студия"),
    ]
    result = run(candidates, CollectionFamily.HOTEL, profile_v2)

    assert [d.candidate.ref for d in result.included] == [1, 2, 3]
    assert {d.reason for d in result.excluded} == {ExclusionReason.WRONG_ROOM_CATEGORY}


def test_premium_marker_wins_over_basic_one(profile_v2) -> None:
    """«Номер делюкс» содержит оба признака, и решает старший."""
    result = run(
        [hotel_candidate(1, "15000", room="Номер делюкс с 1 двуспальной кроватью")],
        CollectionFamily.HOTEL,
        profile_v2,
    )
    assert not result.included
    assert result.excluded[0].reason is ExclusionReason.WRONG_ROOM_CATEGORY


def test_unrecognised_room_category_is_excluded_with_its_name(profile_v2) -> None:
    """Не опознали — не берём, и в детализации видно что именно.

    В эту долю попадают «Панорама Роял» и «SKY VIEW», но также «Однокомнатная
    квартира на улице…» и «1-местная капсула» — то есть не номера гостиницы
    вовсе. Люкс, попавший в медиану молча, дороже потерянного наблюдения.
    """
    result = run(
        [hotel_candidate(1, "26000", room="Панорама Роял")],
        CollectionFamily.HOTEL,
        profile_v2,
    )
    assert not result.included
    decision = result.excluded[0]
    assert decision.reason is ExclusionReason.WRONG_ROOM_CATEGORY
    assert "Панорама Роял" in (decision.detail or "")


def test_baseline_v1_does_not_look_at_room_category(profile) -> None:
    """Прежняя версия про категорию номера ничего не знает и знать не должна."""
    result = run(
        [hotel_candidate(1, "24000", room="Двухместный люкс c 1 комнатой")],
        CollectionFamily.HOTEL,
        profile,
    )
    assert [d.candidate.ref for d in result.included] == [1]


def test_metric_takes_only_its_own_star_category() -> None:
    """В метрику «четыре звезды» идут только четырёхзвёздочные предложения.

    Матрица проживания — город × звёздность × даты, и у каждой метрики своя
    категория. Профиль разрешает 3, 4 и 5 звёзд, и фильтр по этому объединению
    пропускал в четырёхзвёздочную метрику предложения трёх и пяти звёзд: 55 253
    нарушения `HOTEL_STARS_ALLOWED` в снимке 09.08.2026, снимок не опубликован.

    Медиана «четырёхзвёздочного проживания», посчитанная по трём и пяти
    звёздам, описывает не ту величину, что заявлена в её названии, — поэтому
    правило и стоит среди критических.
    """
    loaded = methodology_profile("baseline_v1")
    base = loaded.selection_for(CollectionFamily.HOTEL)
    allowed = {int(item) for item in base["stars"]}
    assert allowed == {3, 4, 5}, "профиль изменился — тест описывает другую ситуацию"

    # Так правила сужаются расчётом под звёздность конкретного наблюдения.
    rules = {**base, "stars": sorted(allowed & {4})}
    result = select(
        [
            hotel_candidate(1, "3000", stars=3),
            hotel_candidate(2, "4000", stars=4),
            hotel_candidate(3, "5000", stars=5),
        ],
        family=CollectionFamily.HOTEL,
        rules=rules,
        outlier_rules={"min_sample_for_removal": 8},
        currency="RUB",
    )
    assert [d.candidate.ref for d in result.included] == [2]
    assert {d.reason for d in result.excluded} == {ExclusionReason.WRONG_STARS}


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
