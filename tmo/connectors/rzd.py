"""Коннектор РЖД (ticket.rzd.ru).

Второй источник железной дороги и единственное место, где два источника
пересекаются: только здесь расхождение цен осмысленно и проверяемо.

Что здесь неочевидно и проверено на живом сервисе:

* публичный путь ``/api/v1/railway-service/prices/train-pricing`` устойчиво
  отвечает ``500`` на любой формат тела — используется B2B-путь;
* ответ может прийти двухфазным: ``result: "RID"`` и идентификатор, данные —
  только при повторном опросе. Разобрать промежуточный ответ значит получить
  **пустую выдачу без признака ошибки**;
* форма ответа плавает: поезда либо в ``Trains``, либо в ``Tp[].List``, вагоны
  либо в ``CarGroups``, либо в ``Cars``;
* в выдаче есть вагонные группы для инвалидов — два места по льготной цене.
  Они занижают наблюдаемый минимум, а именно минимум показывается как «от».

И одна ловушка со счётчиком мест: ``PlaceQuantity`` бывает нулём при
непустых ``LowerPlaceQuantity`` / ``UpperPlaceQuantity``. Читать его как
«мест нет» значит выбросить продающийся вагон.
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any

from tmo.catalog.registry import Source
from tmo.connectors.base import BaseConnector
from tmo.connectors.contracts import (
    AirQuery,
    ConnectorResult,
    HotelQuery,
    ProviderOffer,
    RailQuery,
    RawArtifact,
)
from tmo.connectors.transport import TimeBudget
from tmo.core.enums import (
    AttemptOutcome,
    CarType,
    CollectionFamily,
    NoMarketReason,
    PriceBasis,
)
from tmo.core.errors import BudgetExhausted, ConnectorSchemaError
from tmo.core.money import to_decimal
from tmo.core.timeutil import now_utc

PRICING_PATH = "/apib2b/p/Railway/V1/Search/TrainPricing"
SUGGEST_PATH = "/api/v1/suggests"

#: Браузерный User-Agent обязателен: сервис отклоняет запросы с умолчанием
#: клиентов вроде curl.
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)

#: Типы вагонов источника → типы методики. Люкс, СВ и сидячие в MVP не входят,
#: но распознаются явно: неизвестный тип и «известный, но вне методики» — это
#: разные факты, и второй не должен выглядеть schema drift.
CAR_TYPE_MAP: dict[str, CarType] = {
    "compartment": CarType.COMPARTMENT,
    "купе": CarType.COMPARTMENT,
    "куп": CarType.COMPARTMENT,
    "reservedseat": CarType.RESERVED_SEAT,
    "плац": CarType.RESERVED_SEAT,
    "плацкартный": CarType.RESERVED_SEAT,
    "luxury": CarType.LUX,
    "св": CarType.LUX,
    "soft": CarType.SOFT,
    "мягкий": CarType.SOFT,
    "sitting": CarType.SEDENTARY,
    "sedentary": CarType.SEDENTARY,
    "сид": CarType.SEDENTARY,
    "common": CarType.SHARED,
    "общий": CarType.SHARED,
}

MAX_RID_POLLS = 6


def _norm(value: Any) -> str:
    return str(value or "").strip().lower().replace(" ", "").replace("ё", "е")


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _naive_msk(value: Any) -> datetime | None:
    """Метки РЖД приходят локальным временем без смещения.

    Считать их UTC значило бы сдвинуть каждый поезд на три часа; приведение к
    зоне делается в общем месте (``to_utc``), а здесь метка остаётся наивной.
    """
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


class RzdConnector(BaseConnector):
    """Железнодорожные предложения РЖД: тариф перевозчика без агентского сбора."""

    code = "rzd"
    version = "2.0.0"

    def __init__(self, source: Source, settings: Any = None) -> None:
        super().__init__(source, settings)
        self.base_url = source.endpoint.rstrip("/")
        self._station_cache: dict[str, str] = {}

    def default_headers(self) -> dict[str, str]:
        return {
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "Referer": f"{self.base_url}/",
            "User-Agent": USER_AGENT,
        }

    # -- справочник станций --------------------------------------------------

    def resolve_city_code(self, city_name: str, budget: TimeBudget) -> str | None:
        """Узловой код города: он означает все вокзалы, а не один из них."""
        key = _norm(city_name)
        if key in self._station_cache:
            return self._station_cache[key]
        response = self.transport().get(
            f"{self.base_url}{SUGGEST_PATH}",
            params={
                "GroupResults": "true",
                "RailwaySortPriority": "true",
                "MergeSuburban": "true",
                "Query": city_name,
                "Language": "ru",
                "TransportType": "rail",
            },
            budget=budget,
        )
        payload = response.json()
        for entry in payload.get("city") or []:
            if not isinstance(entry, dict):
                continue
            code = entry.get("expressCode")
            if code and _norm(entry.get("name")) == key:
                self._station_cache[key] = str(code)
                return str(code)
        for entry in payload.get("city") or []:
            if isinstance(entry, dict) and entry.get("expressCode"):
                return str(entry["expressCode"])
        return None

    # -- поиск ---------------------------------------------------------------

    def collect_rail(self, query: RailQuery, budget: TimeBudget) -> ConnectorResult:
        started = time.perf_counter()
        origin = query.origin_rzd_code or self.resolve_city_code(query.origin_name, budget)
        destination = query.destination_rzd_code or self.resolve_city_code(
            query.destination_name, budget
        )
        if not origin or not destination:
            raise ConnectorSchemaError(
                f"Не удалось определить коды станций {query.origin_name} → {query.destination_name}",
                source_code=self.source.code,
            )

        body = {
            "Origin": origin,
            "Destination": destination,
            # Имя поля выяснено по тексту ошибки самого сервиса: он называет
            # некорректные поля прямо в сообщении.
            "DepartureDate": f"{query.service_date.isoformat()}T00:00:00",
            "TimeFrom": 0,
            "TimeTo": 24,
            "CarGrouping": "DontGroup",
            "GetByLocalTime": True,
            # `StandardPlaces` источник принимает, но возвращает на него ноль
            # поездов. Льготные группы отбираются на нашей стороне по признаку.
            "SpecialPlacesDemand": "StandardPlacesAndForDisabledPersons",
            "GetTrainsFromSchedule": True,
            "CarIssuingType": "Passenger",
        }

        payload, artifacts, polls = self._search(body, budget)
        trains = _train_items(payload)
        offers, diagnostics = self._parse(trains, query)

        no_market_reason: NoMarketReason | None = None
        if not offers:
            if not trains:
                no_market_reason = NoMarketReason.NO_DIRECT_SERVICE
            else:
                no_market_reason = NoMarketReason.ALL_FILTERED_OUT

        return ConnectorResult(
            source_code=self.source.code,
            family=CollectionFamily.RAIL,
            outcome=AttemptOutcome.NO_MARKET if not offers else AttemptOutcome.SUCCESS,
            offers=offers,
            raw_artifacts=artifacts,
            latency_ms=int((time.perf_counter() - started) * 1000),
            pages_read=1,
            total_matched=len(trains) or None,
            # Пагинации у источника нет: он отдаёт весь список сразу.
            is_partial=False,
            no_market_reason=no_market_reason,
            diagnostics={
                "origin_code": origin,
                "destination_code": destination,
                "rid_polls": polls,
                "trains_returned": len(trains),
                "not_all_trains_returned": payload.get("NotAllTrainsReturned")
                if isinstance(payload, dict)
                else None,
                **diagnostics,
            },
        )

    def _search(
        self, body: dict[str, Any], budget: TimeBudget
    ) -> tuple[dict[str, Any], list[RawArtifact], int]:
        """Один поиск с обработкой двухфазного протокола RID."""
        url = f"{self.base_url}{PRICING_PATH}"
        params = {"service_provider": "B2B_RZD"}
        artifacts: list[RawArtifact] = []

        requested_at = now_utc()
        response = self.transport().post_json(url, body, params=params, budget=budget)
        payload = _json_or_raise(response, self.source.code)
        artifacts.append(
            RawArtifact(
                payload=payload,
                endpoint=f"{url}?service_provider=B2B_RZD",
                request_params=body,
                requested_at=requested_at,
                fetched_at=now_utc(),
                http_status=response.status_code,
                page_number=1,
            )
        )

        polls = 0
        while isinstance(payload, dict) and str(payload.get("result", "")).upper() == "RID":
            rid = payload.get("RID") or payload.get("rid")
            if not rid or polls >= MAX_RID_POLLS:
                # Разобрать промежуточный ответ значит объявить пустой рынок
                # там, где данные просто ещё не пришли.
                raise ConnectorSchemaError(
                    f"РЖД не отдал данные за {polls} опросов (RID={rid})",
                    source_code=self.source.code,
                )
            delay = min(0.6 * (polls + 1), 2.5)
            if not budget.can_afford(delay + 2.0):
                raise BudgetExhausted(
                    "Бюджет времени не позволяет дождаться ответа РЖД",
                    source_code=self.source.code,
                )
            time.sleep(delay)
            polls += 1
            requested_at = now_utc()
            response = self.transport().post_json(
                url, body, params={**params, "rid": rid, "RID": rid}, budget=budget
            )
            payload = _json_or_raise(response, self.source.code)
            artifacts.append(
                RawArtifact(
                    payload=payload,
                    endpoint=f"{url}?service_provider=B2B_RZD&rid={rid}",
                    request_params={**body, "RID": rid},
                    requested_at=requested_at,
                    fetched_at=now_utc(),
                    http_status=response.status_code,
                    page_number=polls + 1,
                )
            )

        result = str(payload.get("result", "")).upper() if isinstance(payload, dict) else ""
        if result not in ("", "OK", "NONE"):
            raise ConnectorSchemaError(
                f"РЖД вернул result={result}: {str(payload)[:200]}",
                source_code=self.source.code,
            )
        return payload if isinstance(payload, dict) else {}, artifacts, polls

    # -- разбор --------------------------------------------------------------

    def _parse(
        self, trains: list[dict[str, Any]], query: RailQuery
    ) -> tuple[list[ProviderOffer], dict[str, Any]]:
        """Разворачивает поезда в предложения «поезд × вагонная группа».

        Отбрасывается только то, что предложением рынка не является: запрет
        продажи, льготные группы, группы без цены и без мест. Всё остальное,
        включая вагоны вне методики, передаётся дальше с честным типом —
        решение о включении принимает методика, а не коннектор.
        """
        offers: list[ProviderOffer] = []
        dropped = {
            "sale_forbidden": 0,
            "disabled_places": 0,
            "no_price": 0,
            "no_places": 0,
            "unknown_car_type": 0,
        }

        for index, train in enumerate(trains):
            if not isinstance(train, dict):
                continue
            if train.get("IsSaleForbidden") is True:
                dropped["sale_forbidden"] += 1
                continue

            train_number = train.get("TrainNumber") or train.get("DisplayTrainNumber")
            departure_at = _naive_msk(
                train.get("DepartureDateTime") or train.get("LocalDepartureDateTime")
            )
            arrival_at = _naive_msk(
                train.get("ArrivalDateTime") or train.get("LocalArrivalDateTime")
            )
            duration = train.get("TripDuration")
            carriers = _carriers(train)

            for group in _car_groups(train):
                price = to_decimal(group.get("MinPrice"))
                if price is None:
                    price = to_decimal(group.get("Price"))
                if price is None or price <= 0:
                    # Без цены записывать нечего: это не предложение.
                    dropped["no_price"] += 1
                    continue

                places = _available_places(group)
                # Признаки, по которым методика исключит группу, передаются
                # дальше, а не гасятся здесь. Исключённое предложение обязано
                # оставаться видимым в детализации цены вместе с причиной:
                # льготная группа занижала минимум, и это надо показывать, а
                # не просто не показывать.
                if group.get("HasPlacesForDisabledPersons") is True:
                    dropped["disabled_places"] += 1
                if group.get("IsSaleForbidden") is True:
                    dropped["sale_forbidden"] += 1
                if places is not None and places <= 0:
                    dropped["no_places"] += 1

                car_type = _map_car_type(group)
                if car_type is CarType.UNKNOWN:
                    dropped["unknown_car_type"] += 1

                service_classes = group.get("ServiceClasses") or group.get("ServiceClass") or []
                if isinstance(service_classes, str):
                    service_classes = [service_classes]

                offers.append(
                    ProviderOffer(
                        kind="RAIL",
                        source_offer_id=(
                            f"{train_number}:{car_type.value}:"
                            f"{'-'.join(str(c) for c in service_classes) or 'na'}:"
                            f"{group.get('MinPrice')}"
                        ),
                        currency="RUB",
                        price=price,
                        # Тариф перевозчика за одно место в одну сторону.
                        price_basis=PriceBasis.PER_PASSENGER_LEG,
                        origin_code=query.origin_code,
                        destination_code=query.destination_code,
                        departure_at=departure_at,
                        arrival_at=arrival_at,
                        transport={
                            "mode": "RAIL",
                            "train_number": train_number,
                            "train_name": train.get("TrainName") or train.get("TrainDescription"),
                            "carriers": carriers,
                            "car_type": car_type.value,
                            "car_type_raw": group.get("CarTypeName") or group.get("CarType"),
                            "service_class": (
                                str(service_classes[0]) if service_classes else None
                            ),
                            "service_classes": [str(item) for item in service_classes if item],
                            # Поиск по паре узловых кодов возвращает поезда,
                            # идущие маршрут целиком: это прямое сообщение.
                            "segments_count": 1,
                            "is_direct": True,
                            "duration_minutes": _duration_minutes(duration),
                            "seats_left": places,
                            # Места целевого назначения: два места по цене на
                            # треть ниже обычного купе того же поезда.
                            "has_places_for_disabled": bool(
                                group.get("HasPlacesForDisabledPersons")
                            ),
                            "sale_forbidden": bool(group.get("IsSaleForbidden")),
                            "is_two_storey": bool(train.get("HasTwoStoreyCars")),
                            "origin_station": {
                                "code": str(train.get("OriginStationCode") or ""),
                                "name": train.get("OriginStationName") or train.get("OriginName"),
                            },
                            "destination_station": {
                                "code": str(train.get("DestinationStationCode") or ""),
                                "name": train.get("DestinationStationName")
                                or train.get("DestinationName"),
                            },
                            # Возвратность отдельной строкой источник не даёт;
                            # признак группы сообщает лишь наличие таких мест.
                            "refundable": None,
                            "has_non_refundable_tariff": group.get("HasNonRefundableTariff"),
                        },
                        metadata={
                            "max_price": float(to_decimal(group.get("MaxPrice")) or 0) or None,
                            "total_place_quantity": group.get("TotalPlaceQuantity"),
                            "place_quantity": group.get("PlaceQuantity"),
                            "lower_place_quantity": group.get("LowerPlaceQuantity"),
                            "upper_place_quantity": group.get("UpperPlaceQuantity"),
                            "is_from_schedule": train.get("IsFromSchedule"),
                            "price_source": "train_pricing",
                        },
                        raw_index=index,
                    )
                )

        return offers, {"dropped": dropped}

    # -- прочее --------------------------------------------------------------

    def collect_air(self, query: AirQuery, budget: TimeBudget) -> ConnectorResult:
        return self.unsupported(CollectionFamily.AIR)

    def collect_hotel(self, query: HotelQuery, budget: TimeBudget) -> ConnectorResult:
        return self.unsupported(CollectionFamily.HOTEL)

    def health_check(self) -> dict[str, Any]:
        code = self.resolve_city_code("Москва", TimeBudget(total_seconds=30))
        return {
            "source": self.source.code,
            "status": "ok" if code else "degraded",
            "moscow_code": code,
        }


# --------------------------------------------------------------------------- #
# Разбор ответа: форма плавает, каждое поле читается по списку кандидатов
# --------------------------------------------------------------------------- #


def _json_or_raise(response: Any, source_code: str) -> Any:
    try:
        return response.json()
    except ValueError as exc:
        raise ConnectorSchemaError(
            f"РЖД вернул неразбираемое тело: {response.text[:200]}", source_code=source_code
        ) from exc


def _train_items(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    direct = payload.get("Trains")
    if isinstance(direct, list):
        return [item for item in direct if isinstance(item, dict)]
    containers = payload.get("Tp")
    if isinstance(containers, list):
        items: list[dict[str, Any]] = []
        for container in containers:
            nested = container.get("List") if isinstance(container, dict) else None
            if isinstance(nested, list):
                items.extend(item for item in nested if isinstance(item, dict))
        return items
    return []


def _car_groups(train: dict[str, Any]) -> list[dict[str, Any]]:
    for key in ("CarGroups", "Cars"):
        value = train.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def _map_car_type(group: dict[str, Any]) -> CarType:
    for key in ("CarType", "CarTypeName", "Type"):
        mapped = CAR_TYPE_MAP.get(_norm(group.get(key)))
        if mapped:
            return mapped
    return CarType.UNKNOWN


def _available_places(group: dict[str, Any]) -> int | None:
    """Сколько мест в группе.

    Три источника числа, и порядок важен. ``TotalPlaceQuantity`` читается с
    явной проверкой на ``None``: ноль мест — осмысленное значение, а через
    ``or`` он проваливается в запасное поле и оттуда в ``None``.

    ``PlaceQuantity`` ставится последним намеренно: он бывает нулём при
    непустых ``LowerPlaceQuantity`` и ``UpperPlaceQuantity`` — проверено на
    живом ответе, где 0 стояло рядом со 182 нижними и 195 верхними местами.
    """
    total = _int(group.get("TotalPlaceQuantity"))
    if total is not None:
        return total
    parts = [
        _int(group.get(key))
        for key in (
            "LowerPlaceQuantity",
            "UpperPlaceQuantity",
            "LowerSidePlaceQuantity",
            "UpperSidePlaceQuantity",
        )
    ]
    known = [value for value in parts if value is not None]
    if known and sum(known) > 0:
        return sum(known)
    return _int(group.get("PlaceQuantity"))


def _carriers(train: dict[str, Any]) -> list[str]:
    for key in ("CarrierDisplayNames", "Carriers", "Carrier"):
        value = train.get(key)
        if isinstance(value, list) and value:
            return [str(item) for item in value if item]
        if isinstance(value, str) and value:
            return [value]
    return []


def _duration_minutes(value: Any) -> int | None:
    """``TripDuration`` приходит числом минут (``2481.0``), а не строкой."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip()
    if ":" in text:
        try:
            hours, minutes = text.split(":")[:2]
            return int(hours) * 60 + int(minutes)
        except ValueError:
            return None
    try:
        return int(float(text))
    except ValueError:
        return None
