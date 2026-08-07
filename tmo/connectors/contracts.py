"""Контракт между коннекторами и остальной системой.

Коннектор не знает бизнес-методику. Он обязан вернуть объекты описанных здесь
типов; всё, что источник прислал сверх контракта, живёт в ``source_metadata`` и
в движок не попадает.

Обратное правило столь же важно: коннектор не применяет методику, но обязан
передать наблюдаемые признаки, по которым методика решает — тип вагона,
число сегментов, звёздность, возвратность тарифа. Отбросить признак в
коннекторе значит лишить движок возможности отбраковать предложение.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from tmo.core.enums import AttemptOutcome, CollectionFamily, NoMarketReason, PriceBasis
from tmo.version import CONNECTOR_CONTRACT_VERSION

# --------------------------------------------------------------------------- #
# Запросы
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class RailQuery:
    """Плечо ЖД: один пассажир, одна дата, одно направление."""

    origin_code: str
    origin_name: str
    origin_rzd_code: str
    destination_code: str
    destination_name: str
    destination_rzd_code: str
    service_date: date
    passengers: int = 1


@dataclass(frozen=True, slots=True)
class AirQuery:
    """Круговой авиатариф на конкретную пару дат."""

    origin_code: str
    origin_name: str
    origin_metro_code: str
    destination_code: str
    destination_name: str
    destination_metro_code: str
    departure_date: date
    return_date: date
    adults: int = 1
    cabin: str = "ECONOMY"


@dataclass(frozen=True, slots=True)
class HotelQuery:
    """Бронь на пару дат: настоящий multi-night запрос, а не цена ночи × N."""

    city_code: str
    city_name: str
    check_in: date
    check_out: date
    stars: int
    adults: int = 1
    rooms: int = 1

    @property
    def nights(self) -> int:
        return (self.check_out - self.check_in).days


Query = RailQuery | AirQuery | HotelQuery


# --------------------------------------------------------------------------- #
# Предложения источника
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class ProviderOffer:
    """Предложение в терминах источника, до применения методики."""

    kind: str
    source_offer_id: str | None
    currency: str
    #: Цена ровно в той единице, которую объявляет ``price_basis``.
    price: Decimal | None
    price_basis: PriceBasis
    contract_version: str = CONNECTOR_CONTRACT_VERSION

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

    transport: dict[str, Any] = field(default_factory=dict)
    property_info: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    deeplink: str | None = None
    #: Порядковый номер объекта в разобранной выдаче.
    raw_index: int = 0
    #: Страница выдачи, из которой разобрано предложение. Нужна, чтобы связать
    #: Offer с конкретным сырым ответом: у обрезанной выборки важно, какая
    #: именно страница дала строку.
    raw_page: int = 1


# --------------------------------------------------------------------------- #
# Сырые артефакты и результат
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class RawArtifact:
    """Сырой ответ источника. Неизменяем, хранится целиком."""

    payload: Any
    endpoint: str
    request_params: dict[str, Any]
    requested_at: datetime
    fetched_at: datetime
    http_status: int | None = None
    content_type: str = "application/json"
    page_number: int = 1
    pagination: dict[str, Any] = field(default_factory=dict)
    is_partial: bool = False
    error_metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ConnectorResult:
    """Единый ответ коннектора.

    Отказ источника — это результат с ``outcome != SUCCESS``, а не исключение,
    поднимающееся до расчёта. Исключения ловит исполнитель и превращает в
    такой же результат: конечная запись обязана существовать всегда.
    """

    source_code: str
    family: CollectionFamily
    outcome: AttemptOutcome
    offers: list[ProviderOffer] = field(default_factory=list)
    raw_artifacts: list[RawArtifact] = field(default_factory=list)
    requested_at: datetime | None = None
    fetched_at: datetime | None = None
    latency_ms: int | None = None
    http_calls: int = 0
    pages_read: int = 0
    total_matched: int | None = None
    is_partial: bool = False
    partial_reason: str | None = None
    no_market_reason: NoMarketReason | None = None
    error_code: str | None = None
    error_message: str | None = None
    connector_version: str = "2.0.0"
    source_tool_version: str | None = None
    diagnostics: dict[str, Any] = field(default_factory=dict)

    @property
    def offer_count(self) -> int:
        return len(self.offers)
