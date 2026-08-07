"""Коннектор Туту.ру через публичный MCP-эндпоинт.

Источник покрывает все три вертикали и потому является опорным. Всё
нетривиальное здесь — следствие проверенного поведения сервиса, а не
предположений о нём (см. SOURCES_PLAYBOOK).

Четыре решения, каждое из которых однажды стоило неверной цифры:

1. **Имена и границы аргументов читаются из ``tools/list``.** Схема менялась
   между версиями сервера, и жёстко зашитое имя перестаёт работать без ошибки:
   параметр игнорируется, выдача приходит по другому запросу.
2. **Категория вагона берётся из выдачи поиска, а не из карты мест.** Сервер
   версии 0.32 отдаёт ``variants[].seat_category`` и фильтр
   ``seat_categories``; карта мест больше не нужна — а её цены предкорзинные и
   систематически ниже корзины на 6–8 %.
3. **Город, а не аэропорт.** Для многоаэропортовых городов передаётся название
   города: запрос в конкретный аэропорт отсекал 76 % рынка.
4. **Выдача дочитывается постранично.** Одна страница — не рынок; при
   ``sort=price_asc`` обрыв на первой странице смещает медиану вниз
   систематически, а не случайно.
"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime
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
from tmo.connectors.mcp_client import ArgumentBuilder, McpClient
from tmo.connectors.transport import TimeBudget
from tmo.core.config import Settings
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

TOOL_RAIL = "search_rail"
TOOL_AIR = "search_avia"
TOOL_HOTELS = "search_hotels"

#: Категории вагонов в терминах источника совпадают с нашими: сопоставление
#: один в один, отдельная таблица не нужна. Выводить класс из кода
#: обслуживания по первому символу нельзя: `2В` и `2С` — сидячие, а не купе.
SEAT_CATEGORY_MAP = {
    "SEDENTARY": CarType.SEDENTARY,
    "RESERVED_SEAT": CarType.RESERVED_SEAT,
    "COMPARTMENT": CarType.COMPARTMENT,
    "LUX": CarType.LUX,
    "SOFT": CarType.SOFT,
    "SHARED": CarType.SHARED,
}

#: Признаки размещения, не являющегося гостиницей. Проверка вторичная: тип
#: объекта источник в выдаче не возвращает вовсе, поэтому основной отсев —
#: серверный фильтр, а это защита на своей стороне.
NON_HOTEL_MARKERS = (
    "апартамент",
    "апарт-",
    "апартотел",
    "хостел",
    "hostel",
    "гостевой дом",
    "гостевой комплекс",
    "guest house",
    "guesthouse",
    "квартир",
    "apartment",
    "студия в ",
)

@dataclass(slots=True)
class _PageResult:
    """Итог постраничного обхода.

    ``page_of_item`` нужен, чтобы связать разобранное предложение с той
    страницей выдачи, из которой оно пришло: у обрезанной выборки это
    единственный способ понять, что именно мы успели прочитать.
    """

    items: list[dict[str, Any]]
    page_of_item: list[int]
    artifacts: list[RawArtifact]
    meta: dict[str, Any]
    is_partial: bool
    partial_reason: str | None
    pages_read: int

    def attach_pages(self, offers: list[ProviderOffer]) -> list[ProviderOffer]:
        for offer in offers:
            if 0 <= offer.raw_index < len(self.page_of_item):
                offer.raw_page = self.page_of_item[offer.raw_index]
        return offers


_PLACE_RE = re.compile(r"^(?P<city>[^—,]+?)(?:\s*—\s*(?P<point>.+?))?(?:[,\s]*\((?P<code>[^)]+)\))?$")


def _parse_place(value: Any) -> dict[str, str | None]:
    """Разбирает пункт маршрута.

    Источник отдаёт строку, а не объект: «Москва — Ленинградский вокзал
    (2006004)» или «Сочи, 2064130». Обе формы встречаются в одном ответе.
    """
    if not isinstance(value, str) or not value.strip():
        return {"raw": None, "city": None, "point": None, "code": None}
    text = value.strip()
    code: str | None = None
    bracket = re.search(r"\(([^)]+)\)\s*$", text)
    if bracket:
        code = bracket.group(1).strip()
        text = text[: bracket.start()].strip()
    else:
        tail = re.search(r",\s*([A-Z0-9]{3,10})\s*$", text)
        if tail:
            code = tail.group(1).strip()
            text = text[: tail.start()].strip()
    parts = [p.strip() for p in text.split("—")]
    city = parts[0] if parts else None
    point = parts[1] if len(parts) > 1 else None
    return {"raw": value, "city": city or None, "point": point, "code": code}


def _dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _leg(offer: dict[str, Any], label: str) -> dict[str, Any] | None:
    for leg in offer.get("legs") or []:
        if isinstance(leg, dict) and str(leg.get("label", "")).lower() == label:
            return leg
    return None


def _segments(leg: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not leg:
        return []
    return [item for item in (leg.get("segments") or []) if isinstance(item, dict)]


class TutuConnector(BaseConnector):
    """Туту.ру: авиа, ЖД и проживание одним MCP-эндпоинтом."""

    code = "tutu_mcp"
    version = "2.0.0"

    def __init__(self, source: Source, settings: Settings | None = None) -> None:
        super().__init__(source, settings)
        self._mcp: McpClient | None = None
        self._mcp_lock = threading.Lock()

    # -- инфраструктура ------------------------------------------------------

    def mcp(self, budget: TimeBudget) -> McpClient:
        """Клиент MCP, общий на процесс. Инициализация — под блокировкой.

        Без блокировки восемь потоков пачки одновременно видят ``None`` и
        делают по два сетевых вызова каждый: залп из шестнадцати запросов в
        первую же секунду сбора. Именно такой залп открывал размыкатель цепи
        на восьми отказах подряд и уносил всю пачку.
        """
        if self._mcp is not None:
            return self._mcp
        with self._mcp_lock:
            # Проверка повторяется внутри блокировки: пока поток ждал, клиента
            # мог создать кто-то другой.
            if self._mcp is None:
                client = McpClient(
                    self.transport(),
                    self.source.endpoint,
                    client_name="travel-monitoring-observatory",
                )
                client.initialize(budget=budget)
                client.tool_schemas(budget=budget)
                self._mcp = client
        return self._mcp

    def close(self) -> None:
        self._mcp = None
        super().close()

    def health_check(self) -> dict[str, Any]:
        budget = TimeBudget(total_seconds=30)
        client = self.mcp(budget)
        tools = client.tool_schemas(budget=budget)
        return {
            "source": self.source.code,
            "status": "ok",
            "server_version": client.server_version,
            "tools": sorted(tools),
        }

    def _schema(self, client: McpClient, tool: str, budget: TimeBudget) -> dict[str, Any]:
        schemas = client.tool_schemas(budget=budget)
        if tool not in schemas:
            raise ConnectorSchemaError(
                f"MCP-сервер не предоставляет инструмент {tool}; доступны {sorted(schemas)}",
                source_code=self.source.code,
            )
        return schemas[tool]

    def _page_size(self, builder: ArgumentBuilder) -> int:
        """Размер страницы, зажатый в объявленные схемой границы."""
        return int(self.source.page_size or 30)

    # -- постраничный обход --------------------------------------------------

    def _paginate(
        self,
        client: McpClient,
        tool: str,
        base_args: dict[str, Any],
        budget: TimeBudget,
        *,
        items_key: str,
        endpoint_label: str,
    ) -> _PageResult:
        """Дочитывает выдачу до конца или до границы, которую поставили мы сами.

        Возвращает объекты, сырые артефакты, ``meta`` последней страницы,
        признак обрезанной выборки, причину обрезки и число прочитанных страниц.
        Обрезка — это не ошибка: обращение состоялось, предложения настоящие,
        но выборка неполна и медиана смещена неизвестно куда.
        """
        items: list[dict[str, Any]] = []
        page_of_item: list[int] = []
        artifacts: list[RawArtifact] = []
        meta: dict[str, Any] = {}
        is_partial = False
        partial_reason: str | None = None
        page = 1
        max_pages = max(1, int(self.source.max_pages or 1))

        while page <= max_pages:
            if budget.exhausted:
                is_partial = True
                partial_reason = "TIME_BUDGET"
                break
            args = {**base_args, "page": page}
            requested_at = now_utc()
            try:
                payload = client.call_tool(tool, args, budget=budget)
            except BudgetExhausted:
                is_partial = True
                partial_reason = "TIME_BUDGET"
                break
            fetched_at = now_utc()
            page_meta = payload.get("meta") if isinstance(payload, dict) else {}
            page_meta = page_meta if isinstance(page_meta, dict) else {}
            page_items = payload.get(items_key) if isinstance(payload, dict) else None
            page_items = [x for x in (page_items or []) if isinstance(x, dict)]

            artifacts.append(
                RawArtifact(
                    payload=payload,
                    endpoint=f"{self.source.endpoint}#{endpoint_label}",
                    request_params=args,
                    requested_at=requested_at,
                    fetched_at=fetched_at,
                    http_status=200,
                    page_number=page,
                    pagination={
                        "page": page_meta.get("page", page),
                        "page_size": page_meta.get("page_size"),
                        "total_returned": page_meta.get("total_returned", len(page_items)),
                        "total_matched": page_meta.get("total_matched"),
                        "total_matched_exact": page_meta.get("total_matched_exact"),
                        "has_more": page_meta.get("has_more"),
                    },
                )
            )
            items.extend(page_items)
            page_of_item.extend([page] * len(page_items))
            meta = page_meta

            if not page_meta.get("has_more"):
                break
            if page >= max_pages:
                # Потолок выдачи источника: страница 10 × 30 объектов. Дальше
                # он не отдаёт вовсе — это ограничение источника, а не рынка.
                is_partial = True
                partial_reason = "SOURCE_PAGE_CAP"
                break
            page += 1

        return _PageResult(
            items=items,
            page_of_item=page_of_item,
            artifacts=artifacts,
            meta=meta,
            is_partial=is_partial,
            partial_reason=partial_reason,
            pages_read=len(artifacts),
        )

    # ------------------------------------------------------------------ #
    # ЖД
    # ------------------------------------------------------------------ #

    def collect_rail(self, query: RailQuery, budget: TimeBudget) -> ConnectorResult:
        started = time.perf_counter()
        client = self.mcp(budget)
        schema = self._schema(client, TOOL_RAIL, budget)
        builder = ArgumentBuilder(TOOL_RAIL, schema, source_code=self.source.code)

        # Города передаются названиями: узловое название означает все вокзалы,
        # а код конкретного вокзала отсёк бы поезда с остальных.
        builder.set(query.origin_name, "origin", "from_city", required=True)
        builder.set(query.destination_name, "destination", "to_city", required=True)
        builder.set(query.service_date, "departure_date", "date", required=True)
        builder.set(query.passengers, "passengers", "adults")
        builder.set(True, "direct_only")
        # Купе отбирается серверным фильтром: страница ограничена, и каждый
        # плацкартный поезд вытесняет купейный, которого мы уже не увидим.
        builder.set(["COMPARTMENT"], "seat_categories")
        # `full` разворачивает тарифные строки: без них не видно ни сервисного
        # класса, ни возвратности, и схлопнуть сетку до поезда нечем.
        builder.set("full", "view")
        builder.set("price_asc", "sort")
        builder.set(self._page_size(builder), "page_size", "per_page", "limit")

        page = self._paginate(
            client, TOOL_RAIL, builder.args, budget,
            items_key="offers", endpoint_label=TOOL_RAIL,
        )
        offers_raw, artifacts, meta = page.items, page.artifacts, page.meta
        is_partial, partial_reason, pages = page.is_partial, page.partial_reason, page.pages_read

        offers = page.attach_pages(self._parse_rail(offers_raw, query))
        no_market_reason = None
        if not offers:
            no_market_reason = self._rail_no_market_reason(meta, offers_raw)

        return ConnectorResult(
            source_code=self.source.code,
            family=CollectionFamily.RAIL,
            outcome=self._outcome(offers, is_partial),
            offers=offers,
            raw_artifacts=artifacts,
            latency_ms=int((time.perf_counter() - started) * 1000),
            pages_read=pages,
            total_matched=_int(meta.get("total_matched")),
            is_partial=is_partial,
            partial_reason=partial_reason,
            no_market_reason=no_market_reason,
            source_tool_version=client.server_version,
            diagnostics={
                "tool": TOOL_RAIL,
                "args": builder.args,
                "schema_adjustments": builder.adjustments,
                "dropped_not_direct": meta.get("post_filter_dropped_not_direct"),
                "dropped_wrong_seat_category": meta.get("post_filter_dropped_wrong_seat_category"),
                "unverified_seat_category": meta.get("post_filter_unverified_seat_category"),
                "carriers_available": meta.get("carriers_available"),
                "resolved_from": meta.get("from"),
                "resolved_to": meta.get("to"),
            },
        )

    def _rail_no_market_reason(
        self, meta: dict[str, Any], offers_raw: list[dict[str, Any]]
    ) -> NoMarketReason:
        """Отличает «сообщения нет» от «всё отфильтровано».

        Пустая выдача при ненулевых счётчиках отброса означает, что поезда
        есть, но не проходят методику. Это разные факты о рынке, и смешивать
        их нельзя: первый закрывает наблюдение, второй требует объяснения.
        """
        if offers_raw:
            return NoMarketReason.ALL_FILTERED_OUT
        dropped = sum(
            _int(meta.get(key)) or 0
            for key in (
                "post_filter_dropped_not_direct",
                "post_filter_dropped_wrong_seat_category",
                "post_filter_dropped_wrong_carrier",
                "post_filter_dropped_over_cap",
            )
        )
        if dropped > 0:
            return NoMarketReason.ALL_FILTERED_OUT
        return NoMarketReason.NO_DIRECT_SERVICE

    def _parse_rail(self, raw_offers: list[dict[str, Any]], query: RailQuery) -> list[ProviderOffer]:
        """Одна тарифная строка — одно предложение.

        Схлопывание сетки до поезда делает методика, а не коннектор: здесь
        сохраняются все купейные строки, чтобы исключённые были видны в
        детализации цены с причиной исключения.
        """
        offers: list[ProviderOffer] = []
        for index, raw in enumerate(raw_offers):
            outbound = _leg(raw, "outbound") or (raw.get("legs") or [{}])[0]
            segments = _segments(outbound if isinstance(outbound, dict) else None)
            first = segments[0] if segments else {}
            last = segments[-1] if segments else {}
            origin_place = _parse_place(first.get("from") or (outbound or {}).get("from"))
            destination_place = _parse_place(last.get("to") or (outbound or {}).get("to"))
            train_number = first.get("voyage_no")
            departure_at = _dt(raw.get("departure_at") or first.get("departure_at"))
            arrival_at = _dt(raw.get("arrival_at") or last.get("arrival_at"))
            base_price = to_decimal((raw.get("price") or {}).get("amount"))
            currency = str((raw.get("price") or {}).get("currency") or "RUB")
            deeplink = raw.get("checkout_url") or raw.get("search_results_url")
            variants = [v for v in (raw.get("variants") or []) if isinstance(v, dict)]

            if not variants:
                # Компактная выдача без тарифных строк: класс неизвестен.
                # Подставлять его из запроса нельзя — цена сидячего места
                # встала бы в одну строку с купе.
                variants = [{"price": raw.get("price"), "seat_category": None, "__no_variants__": True}]

            for variant in variants:
                price = to_decimal((variant.get("price") or {}).get("amount")) or base_price
                category_raw = variant.get("seat_category")
                car_type = SEAT_CATEGORY_MAP.get(str(category_raw or "").upper(), CarType.UNKNOWN)
                conditions = variant.get("conditions") or {}
                offers.append(
                    ProviderOffer(
                        kind="RAIL",
                        source_offer_id=str(
                            variant.get("variant_id") or raw.get("offer_id") or f"idx{index}"
                        ),
                        currency=str((variant.get("price") or {}).get("currency") or currency),
                        price=price,
                        # Туту отдаёт цену за одно место в одну сторону:
                        # проверено сравнением passengers=1 и passengers=2.
                        price_basis=PriceBasis.PER_PASSENGER_LEG,
                        origin_code=query.origin_code,
                        destination_code=query.destination_code,
                        departure_at=departure_at,
                        arrival_at=arrival_at,
                        transport={
                            "mode": "RAIL",
                            "train_number": train_number,
                            "carriers": [str(c) for c in (raw.get("carriers") or []) if c],
                            "car_type": car_type.value,
                            "car_type_raw": category_raw,
                            "service_class": variant.get("service_class"),
                            "segments_count": _int(raw.get("segments_count")) or len(segments),
                            "is_direct": (_int(raw.get("segments_count")) or len(segments)) == 1,
                            "duration_minutes": _int(raw.get("duration_min")),
                            "seats_left": _int(variant.get("seats_left")),
                            "refundable": conditions.get("refundable"),
                            "changeable": conditions.get("changeable"),
                            "origin_station": origin_place,
                            "destination_station": destination_place,
                        },
                        metadata={
                            "offer_id": raw.get("offer_id"),
                            "search_price_from": float(base_price) if base_price else None,
                            "price_source": "search_listing",
                            "uncategorized_fares": raw.get("uncategorized_fares"),
                            "no_variants": bool(variant.get("__no_variants__")),
                        },
                        deeplink=deeplink,
                        raw_index=index,
                    )
                )
        return offers

    # ------------------------------------------------------------------ #
    # Авиа
    # ------------------------------------------------------------------ #

    def collect_air(self, query: AirQuery, budget: TimeBudget) -> ConnectorResult:
        started = time.perf_counter()
        client = self.mcp(budget)
        schema = self._schema(client, TOOL_AIR, budget)
        builder = ArgumentBuilder(TOOL_AIR, schema, source_code=self.source.code)

        # Название города, а не код аэропорта: код понимается буквально и
        # отбрасывает рейсы во все остальные аэропорты города.
        builder.set(query.origin_name, "origin", "from_city", required=True)
        builder.set(query.destination_name, "destination", "to_city", required=True)
        builder.set(query.departure_date, "departure_date", required=True)
        # Настоящий круговой тариф: он продаётся на конкретную пару дат, и
        # сумма двух односторонних им не является.
        builder.set(query.return_date, "return_date", required=True)
        builder.set(query.adults, "adults")
        builder.set("Y", "service_class")
        builder.set(True, "direct_only")
        builder.set("price_asc", "sort")
        builder.set(self._page_size(builder), "page_size", "per_page", "limit")

        page = self._paginate(
            client, TOOL_AIR, builder.args, budget,
            items_key="offers", endpoint_label=TOOL_AIR,
        )
        offers_raw, artifacts, meta = page.items, page.artifacts, page.meta
        is_partial, partial_reason, pages = page.is_partial, page.partial_reason, page.pages_read

        offers = page.attach_pages(self._parse_air(offers_raw, query))
        no_market_reason = None
        if not offers:
            dropped = _int(meta.get("post_filter_dropped_not_direct")) or 0
            no_market_reason = (
                NoMarketReason.ALL_FILTERED_OUT
                if dropped or offers_raw
                else NoMarketReason.NO_DIRECT_SERVICE
            )

        return ConnectorResult(
            source_code=self.source.code,
            family=CollectionFamily.AIR,
            outcome=self._outcome(offers, is_partial),
            offers=offers,
            raw_artifacts=artifacts,
            latency_ms=int((time.perf_counter() - started) * 1000),
            pages_read=pages,
            total_matched=_int(meta.get("total_matched")),
            is_partial=is_partial,
            partial_reason=partial_reason,
            no_market_reason=no_market_reason,
            source_tool_version=client.server_version,
            diagnostics={
                "tool": TOOL_AIR,
                "args": builder.args,
                "schema_adjustments": builder.adjustments,
                "dropped_not_direct": meta.get("post_filter_dropped_not_direct"),
                "dropped_wrong_airport": meta.get("post_filter_dropped_wrong_airport"),
                "airport_note": meta.get("airport_note"),
                "total_matched_exact": meta.get("total_matched_exact"),
                "resolved_from": meta.get("from"),
                "resolved_to": meta.get("to"),
                "round_trip": meta.get("round_trip"),
            },
        )

    def _parse_air(self, raw_offers: list[dict[str, Any]], query: AirQuery) -> list[ProviderOffer]:
        """Каждый тарифный вариант — отдельная строка.

        Схлопывание в один рейс делает методика: цена «Лайт» и «Оптимум» на
        одном рейсе различаются вдвое, и считать их двумя предложениями рынка
        значит описывать тарифную сетку вместо выбора покупателя.
        """
        offers: list[ProviderOffer] = []
        for index, raw in enumerate(raw_offers):
            outbound = _leg(raw, "outbound")
            inbound = _leg(raw, "return") or _leg(raw, "inbound")
            out_segments = _segments(outbound)
            in_segments = _segments(inbound)
            itinerary = [
                {
                    "voyage_no": seg.get("voyage_no"),
                    "departure_at": seg.get("departure_at"),
                    "from": _parse_place(seg.get("from")),
                    "to": _parse_place(seg.get("to")),
                    "carrier": seg.get("carrier"),
                }
                for seg in (*out_segments, *in_segments)
            ]
            currency = str((raw.get("price") or {}).get("currency") or "RUB")
            base_price = to_decimal((raw.get("price") or {}).get("amount"))
            variants = [v for v in (raw.get("variants") or []) if isinstance(v, dict)] or [
                {"price": raw.get("price")}
            ]

            for variant in variants:
                conditions = variant.get("conditions") or {}
                price = to_decimal((variant.get("price") or {}).get("amount")) or base_price
                offers.append(
                    ProviderOffer(
                        kind="AIR",
                        source_offer_id=str(
                            variant.get("variant_id") or raw.get("offer_id") or f"idx{index}"
                        ),
                        currency=str((variant.get("price") or {}).get("currency") or currency),
                        price=price,
                        # Цена авиа — за всех пассажиров запроса и оба плеча.
                        price_basis=PriceBasis.ALL_PASSENGERS_ROUND_TRIP,
                        origin_code=query.origin_code,
                        destination_code=query.destination_code,
                        departure_at=_dt(raw.get("departure_at")),
                        arrival_at=_dt(raw.get("arrival_at")),
                        return_departure_at=_dt(raw.get("return_departure_at")),
                        return_arrival_at=_dt(raw.get("return_arrival_at")),
                        transport={
                            "mode": "AIR",
                            "carriers": [str(c) for c in (raw.get("carriers") or []) if c],
                            "cabin": variant.get("service_class") or "ECONOMIC",
                            "fare_family": conditions.get("fare_family"),
                            "refundable": conditions.get("refundable"),
                            "changeable": conditions.get("changeable"),
                            "baggage": conditions.get("baggage"),
                            "cabin_baggage": conditions.get("cabin_baggage"),
                            "segments_count": _int(raw.get("segments_count")),
                            "outbound_segments": len(out_segments),
                            "inbound_segments": len(in_segments),
                            # Прямой — это по одному сегменту в каждом плече,
                            # а не «мало сегментов всего».
                            "is_direct": len(out_segments) == 1 and len(in_segments) == 1,
                            "is_round_trip": bool(raw.get("is_round_trip")) and bool(in_segments),
                            "duration_minutes": _int(raw.get("duration_min")),
                            "itinerary": itinerary,
                            "passenger_count": query.adults,
                        },
                        metadata={
                            "offer_id": raw.get("offer_id"),
                            "search_results_url": raw.get("search_results_url"),
                        },
                        deeplink=raw.get("search_results_url"),
                        raw_index=index,
                    )
                )
        return offers

    # ------------------------------------------------------------------ #
    # Проживание
    # ------------------------------------------------------------------ #

    def collect_hotel(self, query: HotelQuery, budget: TimeBudget) -> ConnectorResult:
        started = time.perf_counter()
        client = self.mcp(budget)
        schema = self._schema(client, TOOL_HOTELS, budget)
        builder = ArgumentBuilder(TOOL_HOTELS, schema, source_code=self.source.code)

        builder.set(query.city_name, "city_name", "city", required=True)
        builder.set(query.check_in, "check_in", "checkin_date", required=True)
        builder.set(query.check_out, "check_out", "checkout_date", required=True)
        builder.set(query.adults, "adults")
        # Звёздность — мультивыбор массивом: скаляр отклоняется валидацией.
        builder.set([query.stars], "stars")
        # Тип объекта фильтруется на стороне источника: страница ограничена
        # тридцатью объектами, и каждый апартамент вытесняет отель, которого мы
        # уже не увидим. Сам тип в выдаче не приходит — отсюда вторая проверка
        # на своей стороне.
        builder.set(["hotel"], "hotel_types")
        builder.set(self._page_size(builder), "page_size", "per_page", "limit")

        page = self._paginate(
            client, TOOL_HOTELS, builder.args, budget,
            items_key="hotels", endpoint_label=TOOL_HOTELS,
        )
        items, artifacts, meta = page.items, page.artifacts, page.meta
        is_partial, partial_reason, pages = page.is_partial, page.partial_reason, page.pages_read

        stay = {}
        if artifacts:
            payload = artifacts[0].payload
            stay = payload.get("stay") if isinstance(payload, dict) else {}
            stay = stay if isinstance(stay, dict) else {}

        offers = page.attach_pages(self._parse_hotels(items, query, stay))
        filter_confirmed = _hotel_filter_confirmed(meta)

        return ConnectorResult(
            source_code=self.source.code,
            family=CollectionFamily.HOTEL,
            outcome=self._outcome(offers, is_partial),
            offers=offers,
            raw_artifacts=artifacts,
            latency_ms=int((time.perf_counter() - started) * 1000),
            pages_read=pages,
            total_matched=_int(meta.get("total_matched")) or (len(items) if items else None),
            is_partial=is_partial,
            partial_reason=partial_reason,
            no_market_reason=None if offers else NoMarketReason.EMPTY_RESPONSE,
            source_tool_version=client.server_version,
            diagnostics={
                "tool": TOOL_HOTELS,
                "args": builder.args,
                "schema_adjustments": builder.adjustments,
                "resolved_geo": meta.get("resolved_geo"),
                "filters_applied": meta.get("filters_applied"),
                # Подтверждение того, что серверный фильтр действительно
                # применён. Без него выборка не считается гостиничной молча.
                "server_filters_confirmed": filter_confirmed,
                "stay": stay,
            },
        )

    def _parse_hotels(
        self, items: list[dict[str, Any]], query: HotelQuery, stay: dict[str, Any]
    ) -> list[ProviderOffer]:
        nights = _int(stay.get("nights")) or query.nights or 1
        check_in = _parse_iso_date(stay.get("check_in")) or query.check_in
        check_out = _parse_iso_date(stay.get("check_out")) or query.check_out

        offers: list[ProviderOffer] = []
        for index, hotel in enumerate(items):
            best = hotel.get("best_offer") if isinstance(hotel.get("best_offer"), dict) else {}
            price = to_decimal((best.get("price") or {}).get("amount"))
            if price is None:
                continue
            # Источник помечает базу цены явно. Единственная корректная
            # трактовка `stay_total` — итог за весь период: домножать его на
            # число ночей значит удвоить цену.
            basis_raw = str(best.get("price_basis") or "stay_total")
            total = price if basis_raw == "stay_total" else price * nights
            name = str(hotel.get("name") or "")
            room_name = str(best.get("room_name") or "")
            stars = _int(hotel.get("stars"))
            location = hotel.get("location") if isinstance(hotel.get("location"), dict) else {}

            offers.append(
                ProviderOffer(
                    kind="HOTEL",
                    source_offer_id=str(hotel.get("tutu_offer_id") or hotel.get("hotel_id") or index),
                    currency=str((best.get("price") or {}).get("currency") or "RUB"),
                    price=total,
                    price_basis=PriceBasis.STAY_TOTAL,
                    city_code=query.city_code,
                    check_in=check_in,
                    check_out=check_out,
                    nights=nights,
                    property_info={
                        "property_id": str(hotel.get("hotel_id") or ""),
                        "name": name,
                        "stars": stars,
                        "stars_unrated": stars == 0,
                        # Источник не возвращает тип объекта: поле пустое у всех
                        # объектов. Классификация делается нормализацией по
                        # признакам, которые до нас доходят.
                        "property_type_raw": hotel.get("type"),
                        "property_type_hint": _classify_property(name, room_name),
                        "address": hotel.get("address"),
                        "room_name": room_name,
                        "rating": hotel.get("rating"),
                        "review_count": _int(hotel.get("review_count")),
                        "latitude": location.get("lat"),
                        "longitude": location.get("lng"),
                        "meal_name": best.get("meal_name"),
                        "breakfast_included": best.get("breakfast_included"),
                        "free_cancellation": best.get("free_cancellation"),
                        "adults": query.adults,
                        "rooms": query.rooms,
                    },
                    metadata={
                        "alias": hotel.get("alias"),
                        "offerpack_hash": best.get("offerpack_hash"),
                        "price_basis_raw": basis_raw,
                        "source_price_amount": float(price),
                    },
                    deeplink=best.get("checkout_url") or hotel.get("checkout_url"),
                    raw_index=index,
                )
            )
        return offers

    # -- общее ---------------------------------------------------------------

    @staticmethod
    def _outcome(offers: list[ProviderOffer], is_partial: bool) -> AttemptOutcome:
        if not offers:
            return AttemptOutcome.NO_MARKET
        return AttemptOutcome.PARTIAL if is_partial else AttemptOutcome.SUCCESS


def _parse_iso_date(value: Any) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        return None


def _classify_property(name: str, room_name: str) -> str:
    """Тип размещения по признакам, которые доходят до нас.

    Тип объекта источник не возвращает, поэтому единственные наблюдаемые
    признаки — название объекта и категория номера. Это подсказка для
    нормализации, а не приговор: смешивать апартаменты с гостиницами нельзя,
    но и выбрасывать гостиницу по неудачному названию номера тоже.
    """
    haystack = f"{name} {room_name}".lower()
    for marker in NON_HOTEL_MARKERS:
        if marker in haystack:
            return "NON_HOTEL_SUSPECT"
    return "HOTEL_LIKELY"


def _hotel_filter_confirmed(meta: dict[str, Any]) -> dict[str, bool]:
    """Проверяет, что источник подтвердил применение серверных фильтров.

    Отправленный фильтр и применённый фильтр — разные вещи. Источник эхом
    возвращает применённые фильтры, и это единственный способ убедиться, что
    выборка действительно гостиничная и действительно нужной звёздности.
    """
    applied = (meta.get("filters_applied") or {}).get("option_filters") or []
    ids = {str(item.get("id")) for item in applied if isinstance(item, dict)}
    return {
        "stars": "stars" in ids,
        "hotel_type": "hotel_type" in ids or "hotel_types" in ids,
    }
