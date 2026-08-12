"""Перечисления домена.

Значения перечислений попадают в базу как строки и в API как строки, поэтому
переименование значения — это миграция данных, а не рефакторинг.
"""

from __future__ import annotations

from enum import StrEnum


class CollectionFamily(StrEnum):
    """Семейство наблюдений: определяет и планирование, и методику."""

    RAIL = "RAIL"
    AIR = "AIR"
    HOTEL = "HOTEL"


class SnapshotStatus(StrEnum):
    """Жизненный цикл Market Snapshot (SCOPE-R P1.1)."""

    PLANNING = "PLANNING"
    COLLECTING = "COLLECTING"
    RECOVERING = "RECOVERING"
    CALCULATING = "CALCULATING"
    READY = "READY"
    DEGRADED = "DEGRADED"
    FAILED = "FAILED"

    @property
    def is_published(self) -> bool:
        """Пригоден ли снимок как витрина."""
        return self in (SnapshotStatus.READY, SnapshotStatus.DEGRADED)

    @property
    def is_terminal(self) -> bool:
        return self in (
            SnapshotStatus.READY,
            SnapshotStatus.DEGRADED,
            SnapshotStatus.FAILED,
        )


class JobStatus(StrEnum):
    """Состояние логического наблюдения рынка."""

    PLANNED = "PLANNED"
    DISPATCHED = "DISPATCHED"
    RUNNING = "RUNNING"
    #: Все обязательные источники ответили полной выдачей.
    SUCCESS = "SUCCESS"
    #: Данные получены, но выборка заведомо неполна (truncation / бюджет).
    PARTIAL = "PARTIAL"
    #: Источник ответил, предложений нет. Валидное наблюдение рынка.
    NO_MARKET = "NO_MARKET"
    #: Ни один источник не дал пригодного результата.
    FAILED = "FAILED"

    @property
    def is_completed(self) -> bool:
        """`NO_MARKET` считается завершённым наблюдением (SCOPE-R P16)."""
        return self in (
            JobStatus.SUCCESS,
            JobStatus.PARTIAL,
            JobStatus.NO_MARKET,
            JobStatus.FAILED,
        )

    @property
    def has_data(self) -> bool:
        return self in (JobStatus.SUCCESS, JobStatus.PARTIAL)

    @property
    def is_hole(self) -> bool:
        """Техническая дыра, подлежащая досбору (SCOPE-R P5.1)."""
        return self in (JobStatus.FAILED, JobStatus.PLANNED, JobStatus.DISPATCHED, JobStatus.RUNNING)

    @property
    def is_collected(self) -> bool:
        """Наблюдение закрыто ответом о рынке и повторному сбору не подлежит.

        Точное дополнение к ``is_hole``: ``FAILED`` сюда не входит намеренно —
        это дыра, которую досбор обязан закрыть. Отличается от
        ``is_completed`` ровно на ``FAILED``.

        Граница нужна там, где наблюдение отправляют в работу повторно:
        собранное наблюдение нельзя переводить в ``RUNNING``, иначе пачка,
        упёршаяся в разомкнутую цепь, вернёт его в план как несобранное.
        """
        return self in (JobStatus.SUCCESS, JobStatus.PARTIAL, JobStatus.NO_MARKET)


class AttemptOutcome(StrEnum):
    """Исход одного обращения к источнику (SCOPE-R P1.4).

    Silent skip запрещён: у каждой попытки есть конечная запись с одним из
    этих исходов.
    """

    SUCCESS = "SUCCESS"
    PARTIAL = "PARTIAL"
    NO_MARKET = "NO_MARKET"
    TIMEOUT = "TIMEOUT"
    RATE_LIMITED = "RATE_LIMITED"
    SCHEMA_ERROR = "SCHEMA_ERROR"
    TRANSPORT_ERROR = "TRANSPORT_ERROR"
    CIRCUIT_OPEN = "CIRCUIT_OPEN"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    FAILED = "FAILED"

    @property
    def is_ok(self) -> bool:
        return self in (
            AttemptOutcome.SUCCESS,
            AttemptOutcome.PARTIAL,
            AttemptOutcome.NO_MARKET,
        )

    @property
    def is_technical_failure(self) -> bool:
        """Дыра — только техническая. `NO_MARKET` сюда не входит."""
        return self in (
            AttemptOutcome.TIMEOUT,
            AttemptOutcome.RATE_LIMITED,
            AttemptOutcome.SCHEMA_ERROR,
            AttemptOutcome.TRANSPORT_ERROR,
            AttemptOutcome.CIRCUIT_OPEN,
            AttemptOutcome.BUDGET_EXHAUSTED,
            AttemptOutcome.FAILED,
        )


class NoMarketReason(StrEnum):
    """Почему рынка нет. Отличает «нет сообщения» от «всё отфильтровано»."""

    NO_DIRECT_SERVICE = "NO_DIRECT_SERVICE"
    ALL_FILTERED_OUT = "ALL_FILTERED_OUT"
    EMPTY_RESPONSE = "EMPTY_RESPONSE"


class OfferKind(StrEnum):
    RAIL = "RAIL"
    AIR = "AIR"
    HOTEL = "HOTEL"


class PriceBasis(StrEnum):
    """За что именно цена у источника. Приведение к методике — в нормализации."""

    PER_PASSENGER_LEG = "PER_PASSENGER_LEG"
    PER_PASSENGER_ROUND_TRIP = "PER_PASSENGER_ROUND_TRIP"
    ALL_PASSENGERS_ROUND_TRIP = "ALL_PASSENGERS_ROUND_TRIP"
    STAY_TOTAL = "STAY_TOTAL"
    PER_NIGHT = "PER_NIGHT"


class CarType(StrEnum):
    """Тип вагона в терминах методики. `2К`/`2Д` — это ServiceClass, не тип."""

    SEDENTARY = "SEDENTARY"
    RESERVED_SEAT = "RESERVED_SEAT"
    COMPARTMENT = "COMPARTMENT"
    LUX = "LUX"
    SOFT = "SOFT"
    SHARED = "SHARED"
    UNKNOWN = "UNKNOWN"


class PropertyType(StrEnum):
    HOTEL = "HOTEL"
    APARTMENT = "APARTMENT"
    HOSTEL = "HOSTEL"
    GUESTHOUSE = "GUESTHOUSE"
    APARTHOTEL = "APARTHOTEL"
    UNKNOWN = "UNKNOWN"


class MetricType(StrEnum):
    """Тип опубликованной метрики. Определяет, что означает её цена."""

    #: Одно плечо ЖД, купе, один пассажир, конкретная дата.
    RAIL_LEG = "RAIL_LEG"
    #: Настоящий круговой авиатариф на конкретную пару дат, эконом, прямой.
    AIR_ROUND_TRIP = "AIR_ROUND_TRIP"
    #: Настоящая multi-night бронь на конкретную пару дат.
    HOTEL_STAY = "HOTEL_STAY"
    #: Одна ночь — точка графика проживания.
    HOTEL_NIGHT = "HOTEL_NIGHT"


class ConfidenceLevel(StrEnum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class ExclusionReason(StrEnum):
    """Почему предложение не попало в расчёт. Обязательно для каждого исключения."""

    NOT_DIRECT = "NOT_DIRECT"
    WRONG_CAR_TYPE = "WRONG_CAR_TYPE"
    WRONG_CABIN = "WRONG_CABIN"
    REFUNDABLE_FARE = "REFUNDABLE_FARE"
    NOT_ROUND_TRIP = "NOT_ROUND_TRIP"
    WRONG_PROPERTY_TYPE = "WRONG_PROPERTY_TYPE"
    WRONG_STARS = "WRONG_STARS"
    #: Категория номера вне методики: люкс, апартаменты, студия и подобные.
    #: Сюда же попадает нераспознанная категория — см. `on_unknown_room_category`.
    WRONG_ROOM_CATEGORY = "WRONG_ROOM_CATEGORY"
    WRONG_ROUTE = "WRONG_ROUTE"
    WRONG_DATES = "WRONG_DATES"
    NON_POSITIVE_PRICE = "NON_POSITIVE_PRICE"
    WRONG_CURRENCY = "WRONG_CURRENCY"
    DISABLED_PLACES_GROUP = "DISABLED_PLACES_GROUP"
    SALE_FORBIDDEN = "SALE_FORBIDDEN"
    NO_PLACES = "NO_PLACES"
    #: Тарифная строка того же физического рейса/поезда, но не самая дешёвая.
    FARE_COLLAPSED_NOT_CHEAPEST = "FARE_COLLAPSED_NOT_CHEAPEST"
    #: Дубликат по equivalence_key внутри одного источника.
    DUPLICATE = "DUPLICATE"
    STATISTICAL_OUTLIER = "STATISTICAL_OUTLIER"
    UNCLASSIFIED_CAR_TYPE = "UNCLASSIFIED_CAR_TYPE"


class WarningCode(StrEnum):
    """Предупреждения метрики. Пользователь видит их расшифровку."""

    PARTIAL_SAMPLE = "PARTIAL_SAMPLE"
    SMALL_SAMPLE = "SMALL_SAMPLE"
    SINGLE_SOURCE = "SINGLE_SOURCE"
    SOURCE_DISAGREEMENT = "SOURCE_DISAGREEMENT"
    OUTLIERS_NOT_REMOVED = "OUTLIERS_NOT_REMOVED"
    SOURCE_FAILURE_IN_SAMPLE = "SOURCE_FAILURE_IN_SAMPLE"
    STALE_FETCH = "STALE_FETCH"
    SERVER_FILTER_UNCONFIRMED = "SERVER_FILTER_UNCONFIRMED"
    UNVERIFIED_CATEGORY_DROPPED = "UNVERIFIED_CATEGORY_DROPPED"


class PublicationStatus(StrEnum):
    READY = "READY"
    DEGRADED = "DEGRADED"
    FAILED = "FAILED"


class TransportMode(StrEnum):
    RAIL = "RAIL"
    AIR = "AIR"
