"""Источник воспроизведения: записанные ответы и детерминированный рынок.

Два режима, и оба существуют ради одного — прогнать **настоящий** конвейер без
сети:

``RECORDED``
    отдаёт заранее записанные ответы источников. На нём работает Golden
    Dataset: разбор, отбор и агрегация выполняются тем же кодом, что и в бою,
    поэтому тест ловит изменение методики, а не свою собственную заглушку.

``SIMULATED``
    генерирует ответы **в форме источника** по детерминированному правилу.
    Нужен для демонстрации полной витрины и для capacity-прогонов, где важна
    механика, а не рыночные цифры.

Снимок, собранный этим источником, помечается ``is_synthetic=true`` и никогда
не публикуется как наблюдение рынка: расчётная величина, выданная за
наблюдаемую цену, — ровно то, что запрещено.
"""

from __future__ import annotations

import hashlib
import json
import math
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from tmo.catalog.registry import Source, city_registry
from tmo.connectors.base import BaseConnector
from tmo.connectors.contracts import (
    AirQuery,
    ConnectorResult,
    HotelQuery,
    ProviderOffer,
    Query,
    RailQuery,
    RawArtifact,
)
from tmo.connectors.rzd import RzdConnector
from tmo.connectors.transport import TimeBudget
from tmo.connectors.tutu import TutuConnector
from tmo.core.config import Settings, get_settings
from tmo.core.enums import AttemptOutcome, CollectionFamily, NoMarketReason
from tmo.core.timeutil import now_utc

#: Каталог записанных ответов по умолчанию.
DEFAULT_FIXTURES = Path(__file__).resolve().parents[2] / "golden" / "recorded_raw"

CARRIERS_AIR = ("Аэрофлот", "Победа", "S7 Airlines", "Utair", "Уральские авиалинии", "Азимут")
CARRIERS_RAIL = ("ФПК", "ГРАНД", "ТВЕРСК")
SERVICE_CLASSES = ("2К", "2У", "2Э", "2Б")
HOTEL_SUFFIXES = ("Плаза", "Гранд", "Централь", "Парк", "Атриум", "Резиденс", "Панорама", "Форум")


def _seed(*parts: Any) -> int:
    payload = "|".join(str(p) for p in parts)
    return int(hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12], 16)


def _jitter(seed: int, spread: float) -> float:
    """Детерминированное отклонение в диапазоне ``[-spread, +spread]``."""
    return ((seed % 10_000) / 10_000.0 * 2 - 1) * spread


def _weekend_factor(value: date) -> float:
    return 1.18 if value.weekday() >= 4 else 1.0


def _lead_factor(days_ahead: int) -> float:
    """Чем ближе дата, тем дороже. Кривая пологая, без обрывов."""
    return 1.0 + 0.55 * math.exp(-days_ahead / 9.0)


class ReplayConnector(BaseConnector):
    """Коннектор без сети. Использует парсеры боевых коннекторов."""

    code = "replay"
    version = "2.0.0"
    uses_network = False

    def __init__(
        self,
        source: Source,
        settings: Settings | None = None,
        *,
        mode: str = "SIMULATED",
        fixtures_dir: Path | None = None,
    ) -> None:
        super().__init__(source, settings or get_settings())
        self.mode = mode.upper()
        self.fixtures_dir = fixtures_dir or DEFAULT_FIXTURES
        # Парсеры боевых коннекторов: разбор в тесте обязан быть тем же, что и
        # в бою, иначе тест защищает заглушку.
        self._tutu_parser = TutuConnector.__new__(TutuConnector)
        self._rzd_parser = RzdConnector.__new__(RzdConnector)

    def transport(self):
        raise RuntimeError("ReplayConnector не выполняет сетевых обращений")

    def close(self) -> None:
        return None

    def health_check(self) -> dict[str, Any]:
        return {"source": self.source.code, "status": "ok", "mode": self.mode}

    # -- маршрутизация -------------------------------------------------------

    def collect_rail(self, query: RailQuery, budget: TimeBudget) -> ConnectorResult:
        return self._collect(CollectionFamily.RAIL, query)

    def collect_air(self, query: AirQuery, budget: TimeBudget) -> ConnectorResult:
        return self._collect(CollectionFamily.AIR, query)

    def collect_hotel(self, query: HotelQuery, budget: TimeBudget) -> ConnectorResult:
        return self._collect(CollectionFamily.HOTEL, query)

    def _collect(self, family: CollectionFamily, query: Query) -> ConnectorResult:
        started = time.perf_counter()
        requested_at = now_utc()
        payload, origin_source, fixture_name = self._payload(family, query)
        fetched_at = now_utc()

        offers, diagnostics = self._parse(family, query, payload, origin_source)
        raw = RawArtifact(
            payload=payload,
            endpoint=f"replay://{origin_source}/{family.value.lower()}",
            request_params=_query_params(query),
            requested_at=requested_at,
            fetched_at=fetched_at,
            http_status=200,
            page_number=1,
            pagination={"has_more": False, "total_returned": len(offers)},
        )
        return ConnectorResult(
            source_code=self.source.code,
            family=family,
            outcome=AttemptOutcome.SUCCESS if offers else AttemptOutcome.NO_MARKET,
            offers=offers,
            raw_artifacts=[raw],
            latency_ms=int((time.perf_counter() - started) * 1000),
            http_calls=0,
            pages_read=1,
            total_matched=len(offers) or None,
            no_market_reason=None if offers else self._no_market_reason(family, query),
            connector_version=self.version,
            source_tool_version=f"replay/{self.mode}",
            diagnostics={
                "mode": self.mode,
                "emulated_source": origin_source,
                "fixture": fixture_name,
                "is_synthetic": True,
                **diagnostics,
            },
        )

    def _no_market_reason(self, family: CollectionFamily, query: Query) -> NoMarketReason:
        if isinstance(query, RailQuery):
            gap = city_registry().expected_gap(query.origin_code, query.destination_code, family)
            if gap == "NO_DIRECT_SERVICE":
                return NoMarketReason.NO_DIRECT_SERVICE
        return NoMarketReason.EMPTY_RESPONSE

    def _parse(
        self,
        family: CollectionFamily,
        query: Query,
        payload: Any,
        origin_source: str,
    ) -> tuple[list[ProviderOffer], dict[str, Any]]:
        if family is CollectionFamily.RAIL and origin_source == "rzd":
            offers, diagnostics = self._rzd_parser._parse(
                payload.get("Trains") or [], query
            )
            return offers, diagnostics
        if family is CollectionFamily.RAIL:
            return self._tutu_parser._parse_rail(payload.get("offers") or [], query), {}
        if family is CollectionFamily.AIR:
            return self._tutu_parser._parse_air(payload.get("offers") or [], query), {}
        stay = payload.get("stay") or {}
        return self._tutu_parser._parse_hotels(payload.get("hotels") or [], query, stay), {}

    # -- источники данных ----------------------------------------------------

    @property
    def emulated_source(self) -> str:
        """Чью выдачу воспроизводим. Зависит от источника, которым подменён."""
        return "rzd" if self.source.code == "rzd" else "tutu_mcp"

    def _payload(self, family: CollectionFamily, query: Query) -> tuple[Any, str, str | None]:
        if self.mode == "RECORDED":
            payload, origin_source, name = self._recorded(family, query)
            if payload is not None:
                return payload, origin_source, name
            raise FileNotFoundError(
                f"Нет записанного ответа для {family.value} {_query_params(query)}"
            )
        return (*self._simulate(family, query), None)

    def _recorded(self, family: CollectionFamily, query: Query) -> tuple[Any, str, str | None]:
        params = _query_params(query)
        key = hashlib.sha256(
            json.dumps([family.value, params], sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()[:16]
        for candidate in sorted(self.fixtures_dir.glob("*.json")):
            data = json.loads(candidate.read_text(encoding="utf-8"))
            if data.get("family") != family.value:
                continue
            if data.get("key") == key or data.get("params") == params:
                return data["payload"], data.get("emulated_source", "tutu_mcp"), candidate.name
        return None, "tutu_mcp", None

    # -- детерминированный рынок ---------------------------------------------

    def _simulate(self, family: CollectionFamily, query: Query) -> tuple[Any, str]:
        source = self.emulated_source
        if family is CollectionFamily.RAIL:
            if source == "rzd":
                return self._simulate_rail_rzd(query), "rzd"
            return self._simulate_rail(query), "tutu_mcp"
        if family is CollectionFamily.AIR:
            return self._simulate_air(query), "tutu_mcp"
        return self._simulate_hotel(query), "tutu_mcp"

    def _simulate_rail_rzd(self, query: RailQuery) -> dict[str, Any]:
        """Выдача РЖД в её собственной форме.

        Цена ниже цены Туту на постоянную долю: перевозчик продаёт тариф, агент
        добавляет свой сбор. Разрыв воспроизводится намеренно — на нём
        проверяется предупреждение о расхождении источников.
        """
        registry = city_registry()
        if registry.expected_gap(query.origin_code, query.destination_code, CollectionFamily.RAIL):
            return {"Trains": [], "result": "OK"}

        base = _rail_base_price(query.origin_code, query.destination_code) * 0.885
        days_ahead = max(1, (query.service_date - date.today()).days)
        seed = _seed("rail-rzd", query.origin_code, query.destination_code, query.service_date)
        trains = []
        for index in range(2 + seed % 7):
            train_seed = _seed(seed, index)
            price = base * _lead_factor(days_ahead) * _weekend_factor(query.service_date)
            price *= 1 + _jitter(train_seed, 0.2)
            departure = datetime.combine(query.service_date, datetime.min.time()) + timedelta(
                hours=(train_seed % 20)
            )
            groups = []
            for group_index in range(1 + train_seed % 3):
                groups.append(
                    {
                        "CarType": "Compartment",
                        "CarTypeName": "КУПЕ",
                        "ServiceClasses": [SERVICE_CLASSES[(train_seed + group_index) % 4]],
                        "MinPrice": round(price * (1 + 0.08 * group_index), 2),
                        "MaxPrice": round(price * (1 + 0.08 * group_index) * 1.2, 2),
                        "TotalPlaceQuantity": 4 + (train_seed + group_index) % 60,
                        "HasPlacesForDisabledPersons": False,
                        "IsSaleForbidden": False,
                        "Carriers": [CARRIERS_RAIL[train_seed % len(CARRIERS_RAIL)]],
                    }
                )
            # Одна льготная группа на часть поездов: её исключение с причиной
            # должно быть видно в детализации цены.
            if train_seed % 3 == 0:
                groups.append(
                    {
                        "CarType": "Compartment",
                        "CarTypeName": "КУПЕ",
                        "ServiceClasses": ["2К"],
                        "MinPrice": round(price * 0.67, 2),
                        "TotalPlaceQuantity": 2,
                        "HasPlacesForDisabledPersons": True,
                        "IsSaleForbidden": False,
                    }
                )
            # И один плацкартный вагон: он обязан отбраковаться методикой.
            groups.append(
                {
                    "CarType": "ReservedSeat",
                    "CarTypeName": "ПЛАЦ",
                    "ServiceClasses": ["3Э"],
                    "MinPrice": round(price * 0.55, 2),
                    "TotalPlaceQuantity": 20,
                    "HasPlacesForDisabledPersons": False,
                    "IsSaleForbidden": False,
                }
            )
            trains.append(
                {
                    "TrainNumber": f"{100 + train_seed % 800}{'МСЩЭ'[train_seed % 4]}",
                    "DisplayTrainNumber": f"{100 + train_seed % 800}",
                    "TrainName": "",
                    "OriginStationCode": query.origin_rzd_code,
                    "DestinationStationCode": query.destination_rzd_code,
                    "OriginStationName": _city_name(query.origin_code),
                    "DestinationStationName": _city_name(query.destination_code),
                    "DepartureDateTime": departure.isoformat(),
                    "ArrivalDateTime": (
                        departure + timedelta(minutes=480 + train_seed % 1800)
                    ).isoformat(),
                    "TripDuration": float(480 + train_seed % 1800),
                    "Carriers": [CARRIERS_RAIL[train_seed % len(CARRIERS_RAIL)]],
                    "CarrierDisplayNames": [CARRIERS_RAIL[train_seed % len(CARRIERS_RAIL)]],
                    "IsSaleForbidden": False,
                    "HasTwoStoreyCars": train_seed % 5 == 0,
                    "CarGroups": groups,
                }
            )
        return {"Trains": trains, "result": "OK"}

    def _simulate_rail(self, query: RailQuery) -> dict[str, Any]:
        registry = city_registry()
        if registry.expected_gap(query.origin_code, query.destination_code, CollectionFamily.RAIL):
            # Отсутствие прямого сообщения воспроизводится честно: пустая
            # выдача с ненулевым счётчиком отброшенных пересадок.
            return {"offers": [], "meta": {"total_returned": 0, "has_more": False,
                                           "post_filter_dropped_not_direct": 0}}

        base = _rail_base_price(query.origin_code, query.destination_code)
        days_ahead = max(1, (query.service_date - date.today()).days)
        seed = _seed("rail", query.origin_code, query.destination_code, query.service_date)
        train_count = 2 + seed % 7
        offers = []
        for index in range(train_count):
            train_seed = _seed(seed, index)
            price = base * _lead_factor(days_ahead) * _weekend_factor(query.service_date)
            price *= 1 + _jitter(train_seed, 0.22)
            departure = datetime.combine(query.service_date, datetime.min.time()) + timedelta(
                hours=(train_seed % 20), minutes=(train_seed % 6) * 10
            )
            duration = 480 + (train_seed % 1800)
            variants = []
            for fare_index in range(1 + train_seed % 4):
                fare_price = round(price * (1 + 0.11 * fare_index), 2)
                variants.append(
                    {
                        "variant_id": f"sim-{train_seed}-{fare_index}",
                        "price": {"amount": fare_price, "currency": "RUB"},
                        "conditions": {"refundable": fare_index > 0, "changeable": False},
                        "service_class": SERVICE_CLASSES[(train_seed + fare_index) % 4],
                        "seat_category": "COMPARTMENT",
                        "seats_left": 1 + (train_seed + fare_index) % 40,
                    }
                )
            offers.append(
                {
                    "offer_id": f"sim-rail-{train_seed}",
                    "transport": "railway",
                    "price": {"amount": variants[0]["price"]["amount"], "currency": "RUB"},
                    "duration_min": duration,
                    "carriers": [CARRIERS_RAIL[train_seed % len(CARRIERS_RAIL)]],
                    "segments_count": 1,
                    "departure_at": departure.isoformat(),
                    "arrival_at": (departure + timedelta(minutes=duration)).isoformat(),
                    "search_results_url": "https://www.tutu.ru/poezda/",
                    "legs": [
                        {
                            "label": "outbound",
                            "from": f"{_city_name(query.origin_code)} — вокзал (200000{index})",
                            "to": f"{_city_name(query.destination_code)}, 206413{index}",
                            "departure_at": departure.isoformat(),
                            "arrival_at": (departure + timedelta(minutes=duration)).isoformat(),
                            "segments": [
                                {
                                    "from": f"{_city_name(query.origin_code)} — вокзал (200000{index})",
                                    "to": f"{_city_name(query.destination_code)}, 206413{index}",
                                    "departure_at": departure.isoformat(),
                                    "arrival_at": (
                                        departure + timedelta(minutes=duration)
                                    ).isoformat(),
                                    "carrier": CARRIERS_RAIL[train_seed % len(CARRIERS_RAIL)],
                                    "voyage_no": f"{100 + train_seed % 800}{'МСЩЭ'[train_seed % 4]}",
                                }
                            ],
                        }
                    ],
                    "variants": variants,
                }
            )
        return {
            "offers": offers,
            "meta": {
                "total_returned": len(offers),
                "total_matched": len(offers),
                "has_more": False,
                "post_filter_dropped_not_direct": seed % 3,
                "post_filter_dropped_wrong_seat_category": seed % 4,
                "post_filter_unverified_seat_category": 0,
            },
        }

    def _simulate_air(self, query: AirQuery) -> dict[str, Any]:
        base = _air_base_price(query.origin_code, query.destination_code)
        days_ahead = max(1, (query.departure_date - date.today()).days)
        nights = (query.return_date - query.departure_date).days
        seed = _seed("air", query.origin_code, query.destination_code,
                     query.departure_date, query.return_date)
        flight_count = 4 + seed % 14
        offers = []
        for index in range(flight_count):
            flight_seed = _seed(seed, index)
            price = base * _lead_factor(days_ahead) * _weekend_factor(query.departure_date)
            # Короткая поездка дороже в пересчёте, длинная — дешевле за счёт
            # правил применения тарифа.
            price *= 1.0 + max(0.0, (7 - nights)) * 0.012
            price *= 1 + _jitter(flight_seed, 0.28)
            carrier = CARRIERS_AIR[flight_seed % len(CARRIERS_AIR)]
            out_dep = datetime.combine(query.departure_date, datetime.min.time()) + timedelta(
                hours=6 + flight_seed % 15
            )
            back_dep = datetime.combine(query.return_date, datetime.min.time()) + timedelta(
                hours=7 + (flight_seed // 3) % 14
            )
            duration = 100 + flight_seed % 200
            variants = []
            for fare_index, family_name in enumerate(("Лайт", "Оптимум", "Максимум")):
                variants.append(
                    {
                        "variant_id": f"sim-{flight_seed}-{fare_index}",
                        "price": {
                            "amount": round(price * (1 + 0.19 * fare_index), 2),
                            "currency": "RUB",
                        },
                        "conditions": {
                            "fare_family": family_name,
                            "baggage": {"kg": 0 if fare_index == 0 else 20, "pieces": fare_index},
                            "refundable": fare_index >= 2,
                            "changeable": fare_index >= 1,
                        },
                        "service_class": "ECONOMIC",
                    }
                )
            offers.append(
                {
                    "offer_id": f"sim-air-{flight_seed}",
                    "transport": "avia",
                    "price": {"amount": variants[0]["price"]["amount"], "currency": "RUB"},
                    "duration_min": duration * 2,
                    "carriers": [carrier],
                    "segments_count": 2,
                    "is_round_trip": True,
                    "departure_at": out_dep.isoformat(),
                    "arrival_at": (out_dep + timedelta(minutes=duration)).isoformat(),
                    "return_departure_at": back_dep.isoformat(),
                    "return_arrival_at": (back_dep + timedelta(minutes=duration)).isoformat(),
                    "search_results_url": "https://avia.tutu.ru/",
                    "legs": [
                        _sim_leg("outbound", query.origin_code, query.destination_code,
                                 out_dep, duration, carrier, flight_seed),
                        _sim_leg("return", query.destination_code, query.origin_code,
                                 back_dep, duration, carrier, flight_seed + 1),
                    ],
                    "variants": variants,
                }
            )
        return {
            "offers": offers,
            "meta": {
                "total_returned": len(offers),
                "total_matched": len(offers),
                "total_matched_exact": True,
                "has_more": False,
                "round_trip": True,
                "post_filter_dropped_not_direct": seed % 9,
            },
        }

    def _simulate_hotel(self, query: HotelQuery) -> dict[str, Any]:
        base = _hotel_base_price(query.city_code, query.stars)
        days_ahead = max(1, (query.check_in - date.today()).days)
        seed = _seed("hotel", query.city_code, query.stars, query.check_in, query.check_out)
        count = 12 + seed % 24
        nights = query.nights
        hotels = []
        for index in range(count):
            hotel_seed = _seed(seed, index)
            per_night = base * _lead_factor(days_ahead) * _weekend_factor(query.check_in)
            per_night *= 1 + _jitter(hotel_seed, 0.35)
            hotels.append(
                {
                    "hotel_id": f"sim-{hotel_seed}",
                    "hotel_geo_id": f"sim-{hotel_seed}",
                    "tutu_offer_id": f"sim-offer-{hotel_seed}",
                    "name": (
                        f"Отель «{HOTEL_SUFFIXES[hotel_seed % len(HOTEL_SUFFIXES)]} "
                        f"{_city_name(query.city_code)}»"
                    ),
                    "stars": query.stars,
                    "rating": round(6.5 + (hotel_seed % 300) / 100.0, 2),
                    "review_count": hotel_seed % 900,
                    "address": f"{1 + hotel_seed % 90} км от центра",
                    "location": {"lat": 55.0 + (hotel_seed % 700) / 100.0,
                                 "lng": 37.0 + (hotel_seed % 500) / 100.0},
                    "alias": f"sim_hotel_{hotel_seed}",
                    "best_offer": {
                        "offerpack_hash": f"pack-{hotel_seed}",
                        "room_name": "Стандартный двухместный номер",
                        "price": {"amount": round(per_night * nights, 2), "currency": "RUB"},
                        "price_basis": "stay_total",
                        "checkout_url": "https://hotel.tutu.ru/offers/details",
                        "free_cancellation": hotel_seed % 2 == 0,
                        "breakfast_included": None,
                        "meal_name": None,
                    },
                }
            )
        return {
            "hotels": hotels,
            "stay": {
                "check_in": query.check_in.isoformat(),
                "check_out": query.check_out.isoformat(),
                "nights": nights,
            },
            "meta": {
                "total_returned": len(hotels),
                "has_more": False,
                "resolved_geo": {"name": _city_name(query.city_code), "geo_type": "locality"},
                "filters_applied": {
                    "option_filters": [
                        {"id": "stars", "selected_items": [str(query.stars)]},
                        {"id": "hotel_type", "selected_items": ["hotel"]},
                    ]
                },
            },
        }


def _sim_leg(
    label: str, origin: str, destination: str, departure: datetime,
    duration: int, carrier: str, seed: int,
) -> dict[str, Any]:
    return {
        "label": label,
        "from": f"{_city_name(origin)} — аэропорт ({origin})",
        "to": f"{_city_name(destination)}, {destination}",
        "departure_at": departure.isoformat(),
        "arrival_at": (departure + timedelta(minutes=duration)).isoformat(),
        "duration_min": duration,
        "segments": [
            {
                "from": f"{_city_name(origin)} — аэропорт ({origin})",
                "to": f"{_city_name(destination)}, {destination}",
                "departure_at": departure.isoformat(),
                "arrival_at": (departure + timedelta(minutes=duration)).isoformat(),
                "duration_min": duration,
                "carrier": carrier,
                "voyage_no": f"{carrier[:2].upper()}-{1000 + seed % 8000}",
            }
        ],
    }


def _city_name(code: str) -> str:
    try:
        return city_registry().get(code).name
    except Exception:
        return code


#: Опорные цены подобраны по порядку величины реальных наблюдений 07.08.2026.
#: Это не прогноз и не оценка рынка: числа существуют, чтобы конвейер работал.
_RAIL_BASE = {
    ("MOW", "LED"): 4200, ("MOW", "AER"): 6100, ("MOW", "KUF"): 4600, ("MOW", "KZN"): 4300,
    ("LED", "AER"): 8200, ("LED", "KUF"): 6900, ("LED", "KZN"): 6400,
    ("AER", "KUF"): 7300, ("AER", "KZN"): 7100, ("KUF", "KZN"): 3100,
}
_AIR_BASE = {
    ("MOW", "LED"): 11000, ("MOW", "AER"): 17500, ("MOW", "KUF"): 13500, ("MOW", "KZN"): 12000,
    ("LED", "AER"): 21000, ("LED", "KUF"): 18000, ("LED", "KZN"): 16000,
    ("AER", "KUF"): 22000, ("AER", "KZN"): 20500, ("KUF", "KZN"): 14500,
}
_HOTEL_BASE = {
    "MOW": {3: 5100, 4: 8600, 5: 17500},
    "LED": {3: 5600, 4: 9100, 5: 18500},
    "AER": {3: 6000, 4: 10500, 5: 21000},
    "KUF": {3: 4300, 4: 6900, 5: 12500},
    "KZN": {3: 4700, 4: 7600, 5: 14500},
}


def _rail_base_price(origin: str, destination: str) -> float:
    return float(_RAIL_BASE.get((origin, destination)) or _RAIL_BASE.get((destination, origin)) or 5000)


def _air_base_price(origin: str, destination: str) -> float:
    return float(_AIR_BASE.get((origin, destination)) or _AIR_BASE.get((destination, origin)) or 15000)


def _hotel_base_price(city: str, stars: int) -> float:
    return float((_HOTEL_BASE.get(city) or _HOTEL_BASE["MOW"]).get(stars, 6000))


def _query_params(query: Query) -> dict[str, Any]:
    if isinstance(query, RailQuery):
        return {
            "origin": query.origin_code,
            "destination": query.destination_code,
            "service_date": query.service_date.isoformat(),
            "passengers": query.passengers,
        }
    if isinstance(query, AirQuery):
        return {
            "origin": query.origin_code,
            "destination": query.destination_code,
            "departure_date": query.departure_date.isoformat(),
            "return_date": query.return_date.isoformat(),
            "adults": query.adults,
        }
    return {
        "city": query.city_code,
        "check_in": query.check_in.isoformat(),
        "check_out": query.check_out.isoformat(),
        "stars": query.stars,
        "adults": query.adults,
    }
