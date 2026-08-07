"""Отбор: какие из наблюдённых предложений являются рынком.

Отбор ничего не удаляет. Каждое предложение получает решение — включено или
исключено, и у исключения обязательно есть причина. Это позволяет открыть
любую цену и увидеть не только то, что вошло в расчёт, но и то, что не вошло,
и почему.

Порядок правил важен. Сначала — жёсткие несоответствия наблюдения запросу
(маршрут, даты, цена), затем — методика (класс, прямой, тип объекта), и лишь
в конце — схлопывание тарифной сетки. Иначе «не самый дешёвый тариф» стало бы
причиной исключения там, где настоящая причина — бизнес-класс.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from tmo.core.enums import CarType, CollectionFamily, ExclusionReason, PropertyType
from tmo.engine.statistics import OutlierDecision, detect_outliers


@dataclass(slots=True)
class Candidate:
    """Предложение на входе отбора. ``ref`` — что угодно, чем адресуется Offer."""

    ref: Any
    source_code: str
    price: Decimal
    currency: str
    equivalence_key: str
    transport: dict[str, Any] = field(default_factory=dict)
    property_info: dict[str, Any] = field(default_factory=dict)
    validation_flags: list[str] = field(default_factory=list)


@dataclass(slots=True)
class Decision:
    candidate: Candidate
    included: bool
    reason: ExclusionReason | None = None
    detail: str | None = None


@dataclass(slots=True)
class SelectionResult:
    decisions: list[Decision]
    outlier: OutlierDecision | None = None

    @property
    def included(self) -> list[Decision]:
        return [d for d in self.decisions if d.included]

    @property
    def excluded(self) -> list[Decision]:
        return [d for d in self.decisions if not d.included]

    @property
    def prices(self) -> list[Decimal]:
        return [d.candidate.price for d in self.decisions if d.included]

    @property
    def sources(self) -> set[str]:
        return {d.candidate.source_code for d in self.decisions if d.included}


#: Отметки нормализации, которые сами по себе исключают предложение.
FLAG_TO_REASON: dict[str, ExclusionReason] = {
    "ROUTE_MISMATCH": ExclusionReason.WRONG_ROUTE,
    "DEPARTURE_DATE_MISMATCH": ExclusionReason.WRONG_DATES,
    "RETURN_DATE_MISMATCH": ExclusionReason.WRONG_DATES,
    "CHECK_IN_MISMATCH": ExclusionReason.WRONG_DATES,
    "CHECK_OUT_MISMATCH": ExclusionReason.WRONG_DATES,
    "NIGHTS_MISMATCH": ExclusionReason.WRONG_DATES,
    "PRICE_DERIVED_FROM_PER_NIGHT": ExclusionReason.NON_POSITIVE_PRICE,
}


def select(
    candidates: list[Candidate],
    *,
    family: CollectionFamily,
    rules: dict[str, Any],
    outlier_rules: dict[str, Any],
    currency: str = "RUB",
) -> SelectionResult:
    """Применяет методику к выборке одного наблюдения."""
    decisions: list[Decision] = []
    survivors: list[Decision] = []

    for candidate in candidates:
        decision = _apply_rules(candidate, family=family, rules=rules, currency=currency)
        decisions.append(decision)
        if decision.included:
            survivors.append(decision)

    _collapse_fares(survivors, rules)
    outlier = _remove_outliers(
        [d for d in survivors if d.included], outlier_rules=outlier_rules
    )
    return SelectionResult(decisions=decisions, outlier=outlier)


def _apply_rules(
    candidate: Candidate,
    *,
    family: CollectionFamily,
    rules: dict[str, Any],
    currency: str,
) -> Decision:
    if candidate.price is None or candidate.price <= 0:
        return Decision(candidate, False, ExclusionReason.NON_POSITIVE_PRICE)
    if candidate.currency != currency:
        return Decision(
            candidate, False, ExclusionReason.WRONG_CURRENCY, f"{candidate.currency} ≠ {currency}"
        )

    for flag in candidate.validation_flags:
        reason = FLAG_TO_REASON.get(flag)
        if reason is not None:
            return Decision(candidate, False, reason, flag)

    if family is CollectionFamily.RAIL:
        return _apply_rail(candidate, rules)
    if family is CollectionFamily.AIR:
        return _apply_air(candidate, rules)
    return _apply_hotel(candidate, rules)


def _apply_rail(candidate: Candidate, rules: dict[str, Any]) -> Decision:
    transport = candidate.transport
    car_type = str(transport.get("car_type") or CarType.UNKNOWN.value)

    if car_type == CarType.UNKNOWN.value:
        # Неклассифицированный вагон нельзя ни включить, ни выбросить молча:
        # цена сидячего места, вставшая в один ряд с купе, ошибку не покажет.
        return Decision(
            candidate, False, ExclusionReason.UNCLASSIFIED_CAR_TYPE,
            "источник не сообщил тип вагона",
        )
    required = str(rules.get("car_type") or CarType.COMPARTMENT.value)
    if car_type != required:
        return Decision(candidate, False, ExclusionReason.WRONG_CAR_TYPE, car_type)

    # Сервисный класс намеренно не фильтруется: `2К`/`2Д` — это тарифная
    # маркировка внутри купе, а не отдельная бизнес-категория.
    if rules.get("direct_only", True) and transport.get("is_direct") is False:
        return Decision(candidate, False, ExclusionReason.NOT_DIRECT)
    if rules.get("exclude_disabled_places_groups", True) and transport.get(
        "has_places_for_disabled"
    ):
        # Два места по льготной цене рынком не являются, но занижают
        # наблюдаемый минимум — а минимум показывается на витрине как «от».
        return Decision(candidate, False, ExclusionReason.DISABLED_PLACES_GROUP)
    if rules.get("exclude_sale_forbidden", True) and transport.get("sale_forbidden"):
        return Decision(candidate, False, ExclusionReason.SALE_FORBIDDEN)
    if rules.get("require_places_available", True):
        seats = transport.get("seats_left")
        if seats is not None and int(seats) <= 0:
            return Decision(candidate, False, ExclusionReason.NO_PLACES)
    return Decision(candidate, True)


def _apply_air(candidate: Candidate, rules: dict[str, Any]) -> Decision:
    transport = candidate.transport
    if rules.get("direct_only", True) and not transport.get("is_direct"):
        return Decision(candidate, False, ExclusionReason.NOT_DIRECT)
    if rules.get("trip_type") == "ROUND_TRIP" and not transport.get("is_round_trip"):
        return Decision(candidate, False, ExclusionReason.NOT_ROUND_TRIP)

    cabin = str(transport.get("cabin") or "").upper()
    if rules.get("cabin") == "ECONOMY" and cabin and not cabin.startswith("ECONOM"):
        return Decision(candidate, False, ExclusionReason.WRONG_CABIN, cabin)

    if rules.get("refundable") is False:
        refundable = transport.get("refundable")
        if refundable is True:
            return Decision(candidate, False, ExclusionReason.REFUNDABLE_FARE)
        if refundable is None:
            # Невозвратность не подтверждена. Считать её выполненной значило бы
            # поставить в выборку тариф с неизвестными условиями.
            return Decision(
                candidate, False, ExclusionReason.REFUNDABLE_FARE, "возвратность не подтверждена"
            )
    return Decision(candidate, True)


def _apply_hotel(candidate: Candidate, rules: dict[str, Any]) -> Decision:
    info = candidate.property_info
    allowed_types = {str(item) for item in (rules.get("property_types") or ["HOTEL"])}
    property_type = str(info.get("property_type") or PropertyType.UNKNOWN.value)
    if property_type not in allowed_types:
        return Decision(candidate, False, ExclusionReason.WRONG_PROPERTY_TYPE, property_type)

    allowed_stars = {int(item) for item in (rules.get("stars") or [3, 4, 5])}
    stars = info.get("stars")
    if stars is None or int(stars) not in allowed_stars:
        return Decision(candidate, False, ExclusionReason.WRONG_STARS, str(stars))
    return Decision(candidate, True)


def _collapse_fares(survivors: list[Decision], rules: dict[str, Any]) -> None:
    """Схлопывает тарифную сетку до физического объекта рынка.

    На один рейс источник возвращает 4–6 тарифов, различающихся вчетверо; сайт
    показывает по рейсу одну цену — самую дешёвую. Считать каждый тариф
    предложением значит посадить медиану на третий тариф — вдвое выше того,
    что платит покупатель.

    Отброшенные строки остаются с причиной ``FARE_COLLAPSED_NOT_CHEAPEST``:
    на них работает анализ надбавок за багаж и возвратность.
    """
    if not rules.get("fare_collapse"):
        return
    cheapest: dict[str, Decision] = {}
    for decision in survivors:
        key = decision.candidate.equivalence_key
        current = cheapest.get(key)
        if current is None or decision.candidate.price < current.candidate.price:
            cheapest[key] = decision

    keep = {id(decision) for decision in cheapest.values()}
    for decision in survivors:
        if id(decision) not in keep:
            decision.included = False
            decision.reason = ExclusionReason.FARE_COLLAPSED_NOT_CHEAPEST
            winner = cheapest[decision.candidate.equivalence_key]
            decision.detail = f"дешевле: {winner.candidate.price}"


def _remove_outliers(
    survivors: list[Decision], *, outlier_rules: dict[str, Any]
) -> OutlierDecision:
    prices = [decision.candidate.price for decision in survivors]
    outcome = detect_outliers(
        prices,
        min_sample=int(outlier_rules.get("min_sample_for_removal", 8)),
        multiplier=float(outlier_rules.get("iqr_multiplier", 3.0)),
        max_removed_share=float(outlier_rules.get("max_removed_share", 0.25)),
    )
    if not outcome.applied or outcome.removed == 0:
        return outcome
    for decision in survivors:
        price = decision.candidate.price
        if outcome.low is not None and price < outcome.low:
            decision.included = False
            decision.reason = ExclusionReason.STATISTICAL_OUTLIER
            decision.detail = f"< {outcome.low}"
        elif outcome.high is not None and price > outcome.high:
            decision.included = False
            decision.reason = ExclusionReason.STATISTICAL_OUTLIER
            decision.detail = f"> {outcome.high}"
    return outcome
