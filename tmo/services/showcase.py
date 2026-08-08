"""Запросы витрины.

Вся бизнес-логика заканчивается здесь: API отдаёт готовые DTO, фронтенд ничего
не досчитывает. Ни один запрос этого модуля не обращается к внешним
источникам — дашборд работает только по заранее собранным данным.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from tmo.catalog.registry import city_registry
from tmo.core.enums import MetricType, SnapshotStatus, TransportMode
from tmo.db import models
from tmo.services.calculation import active_run
from tmo.services.snapshot import latest_published, snapshot_for_date


@dataclass(slots=True)
class SnapshotContext:
    """Какой снимок и какой расчёт обслуживают запрос."""

    snapshot: models.MarketSnapshot
    run: models.CalculationRun
    #: Состояние снимка за текущие сутки, если он ещё не закрыт. Витрина
    #: показывает не его, а последний готовый, — но обязана сказать, почему.
    today: dict[str, Any] | None = None

    @property
    def is_fallback(self) -> bool:
        """Показывается не сегодняшний снимок."""
        from tmo.core.timeutil import snapshot_date_for

        return self.snapshot.snapshot_date != snapshot_date_for()

    @property
    def fallback_reason(self) -> str | None:
        """Почему показан не сегодняшний снимок.

        Различение появилось вместе с моделью сбора по готовности. Прежний текст
        «не опубликован либо не прошёл ворота» был верен, пока цикл заканчивался
        к 10:00: к моменту, когда на витрину смотрели, всё было решено. Теперь
        незакрытый снимок — нормальное состояние двадцати двух часов в сутки, и
        читать его как провал значит пугать пользователя штатной работой.
        """
        if not self.is_fallback:
            return None
        if self.today is None:
            return "NOT_STARTED"
        if not self.today.get("is_closed"):
            return "IN_PROGRESS"
        return "FAILED"

    def as_dict(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot.id,
            "snapshot_date": self.snapshot.snapshot_date.isoformat(),
            "status": str(self.snapshot.status),
            "attempt_no": self.snapshot.attempt_no,
            "version_label": f"v{self.snapshot.attempt_no}",
            "fallback_reason": self.fallback_reason,
            "today": self.today,
            "is_synthetic": bool(self.snapshot.is_synthetic),
            "is_fallback": self.is_fallback,
            "published_at": self.snapshot.published_at.isoformat()
            if self.snapshot.published_at
            else None,
            "coverage_total": round(float(self.snapshot.coverage_total), 4),
            "coverage_rail": round(float(self.snapshot.coverage_rail), 4),
            "coverage_air": round(float(self.snapshot.coverage_air), 4),
            "coverage_hotel": round(float(self.snapshot.coverage_hotel), 4),
            "publication_notes": list(self.snapshot.publication_notes or []),
            "calculation_run_id": self.run.id,
            "methodology_version": self.run.methodology_version,
        }


class NoPublishedSnapshot(Exception):
    """Ни одного пригодного снимка нет: витрина пуста и обязана это сказать."""


def resolve_context(
    session: Session,
    *,
    snapshot_date: date | None = None,
    attempt_no: int | None = None,
    allow_synthetic: bool = True,
) -> SnapshotContext:
    """Находит снимок и его активный расчёт.

    Витрина показывает **последний полностью собранный** день. Сегодняшний
    снимок, пока он собирается, сюда не попадает и попасть не должен: показать
    вчерашние данные — нормально, показать сегодняшние недособранные как
    готовые — нет.

    ``attempt_no`` выбирает версию среди попыток одной даты. Без него берётся
    последняя.
    """
    from tmo.services import cycle

    if snapshot_date is not None:
        snapshot = snapshot_for_date(session, snapshot_date, attempt_no=attempt_no)
    else:
        snapshot = latest_published(session)
        if snapshot is None and allow_synthetic:
            from tmo.services.snapshot import latest_any

            snapshot = latest_any(session)
    if snapshot is None:
        raise NoPublishedSnapshot("Нет ни одного опубликованного снимка")
    run = active_run(session, snapshot.id)
    if run is None:
        raise NoPublishedSnapshot(f"У снимка {snapshot.snapshot_date} нет активного расчёта")
    return SnapshotContext(snapshot=snapshot, run=run, today=cycle.progress(session))


# --------------------------------------------------------------------------- #
# Блок A — «Куда ехать»
# --------------------------------------------------------------------------- #


def trips(
    session: Session,
    context: SnapshotContext,
    *,
    origin: str,
    departure_date: date,
    return_date: date,
    transport_mode: str,
    stars: int,
) -> list[dict[str, Any]]:
    """Направления из выбранного города на выбранные даты."""
    rows = session.scalars(
        select(models.TripCostRow)
        .where(
            models.TripCostRow.calculation_run_id == context.run.id,
            models.TripCostRow.origin_code == origin,
            models.TripCostRow.departure_date == departure_date,
            models.TripCostRow.return_date == return_date,
            models.TripCostRow.transport_mode == transport_mode,
            models.TripCostRow.stars == stars,
        )
        .order_by(models.TripCostRow.total_median.is_(None), models.TripCostRow.total_median)
    ).all()

    registry = city_registry()
    return [
        {
            "origin": {"code": row.origin_code, "name": registry.get(row.origin_code).name},
            "destination": {
                "code": row.destination_code,
                "name": registry.get(row.destination_code).name,
            },
            "departure_date": row.departure_date.isoformat(),
            "return_date": row.return_date.isoformat(),
            "nights": row.nights,
            "transport_mode": row.transport_mode,
            "stars": row.stars,
            "currency": row.currency,
            "transport_median": _num(row.transport_median),
            "transport_min": _num(row.transport_min),
            "accommodation_median": _num(row.accommodation_median),
            "accommodation_min": _num(row.accommodation_min),
            "total_median": _num(row.total_median),
            "total_min": _num(row.total_min),
            "offers_count": row.offers_count,
            "sources_count": row.sources_count,
            "quality_score": row.quality_score,
            "confidence_level": str(row.confidence_level),
            "is_partial": row.is_partial,
            "is_complete": row.is_complete,
            "warning_codes": list(row.warning_codes or []),
            "missing_components": list(row.missing_components or []),
            "transport_metric_ids": list(row.transport_metric_ids or []),
            "accommodation_metric_id": row.accommodation_metric_id,
            # ЖД показывается как сумма двух отдельно наблюдавшихся плеч, и
            # витрина обязана это сказать словами, а не подразумевать.
            "transport_composition": (
                "Сумма двух отдельно наблюдавшихся плеч"
                if row.transport_mode == TransportMode.RAIL.value
                else "Настоящий круговой тариф на эту пару дат"
            ),
        }
        for row in rows
    ]


# --------------------------------------------------------------------------- #
# Блок B — график ЖД
# --------------------------------------------------------------------------- #


def rail_chart(
    session: Session,
    context: SnapshotContext,
    *,
    origin: str,
    destination: str | None = None,
) -> dict[str, Any]:
    """Ряды стоимости ЖД по датам отправления. Авиа здесь не показывается."""
    query = _metric_query(context, MetricType.RAIL_LEG).where(
        models.CalculatedMetric.origin_code == origin
    )
    if destination:
        query = query.where(models.CalculatedMetric.destination_code == destination)
    metrics = session.scalars(
        query.order_by(models.CalculatedMetric.destination_code, models.CalculatedMetric.service_date)
    ).all()

    registry = city_registry()
    series: dict[str, dict[str, Any]] = {}
    for metric in metrics:
        entry = series.setdefault(
            metric.destination_code,
            {
                "destination": {
                    "code": metric.destination_code,
                    "name": registry.get(metric.destination_code).name,
                },
                "points": [],
            },
        )
        entry["points"].append(_point(metric))
    return {
        "origin": {"code": origin, "name": registry.get(origin).name},
        "parameters": {
            "transport": "RAIL",
            "car_type": "COMPARTMENT",
            "direct_only": True,
            "passengers": 1,
            "leg": "ONE_WAY",
        },
        "series": list(series.values()),
    }


def air_chart(
    session: Session,
    context: SnapshotContext,
    *,
    origin: str,
    nights: int,
    destination: str | None = None,
) -> dict[str, Any]:
    """Ряды стоимости авиа по датам вылета при фиксированной длительности.

    Авиа наблюдается парой дат, а не одной: на каждую дату вылета приходится
    29 разных цен по длительности поездки. Линия по дате вылета существует
    только как **срез** этой сетки — поэтому длительность задаётся явно, а не
    выбирается за пользователя.

    Каждая точка остаётся настоящим круговым тарифом на конкретную пару дат.
    Ни одна не является суммой двух односторонних: такой величины на рынке
    нет.
    """
    query = _metric_query(context, MetricType.AIR_ROUND_TRIP).where(
        models.CalculatedMetric.origin_code == origin,
        models.CalculatedMetric.nights == nights,
    )
    if destination:
        query = query.where(models.CalculatedMetric.destination_code == destination)
    metrics = session.scalars(
        query.order_by(
            models.CalculatedMetric.destination_code, models.CalculatedMetric.service_date
        )
    ).all()

    registry = city_registry()
    series: dict[str, dict[str, Any]] = {}
    for metric in metrics:
        entry = series.setdefault(
            metric.destination_code,
            {
                "destination": {
                    "code": metric.destination_code,
                    "name": registry.get(metric.destination_code).name,
                },
                "points": [],
            },
        )
        point = _point(metric)
        # Обратная дата — часть наблюдения, а не производная: без неё точка
        # неотличима от односторонней.
        point["return_date"] = metric.return_date.isoformat() if metric.return_date else None
        entry["points"].append(point)

    return {
        "origin": {"code": origin, "name": registry.get(origin).name},
        "parameters": {
            "transport": "AIR",
            "cabin": "ECONOMY",
            "direct_only": True,
            "refundable": False,
            "passengers": 1,
            "trip_type": "ROUND_TRIP",
            "nights": nights,
        },
        "available_nights": available_air_nights(session, context),
        "series": list(series.values()),
    }


def air_grid(
    session: Session,
    context: SnapshotContext,
    *,
    origin: str,
    destination: str,
) -> dict[str, Any]:
    """Полная сетка наблюдений авиа по одному маршруту.

    Авиа наблюдается парой дат, и линия по дате вылета — лишь срез. Сетка
    показывает всё, что наблюдалось: 30 дат вылета × длительность поездки.

    Шкала цвета считается **здесь**, а не на фронтенде, и строится по одному
    маршруту: цена Москва → Сочи и Москва → Петербург несравнимы, и общая
    шкала покрасила бы один маршрут сплошь «дорогим».

    Пустая клетка и клетка без рынка — разные вещи, и в шкалу не входит ни
    одна: серый цвет «дёшево» был бы прямым враньём.
    """
    metrics = session.scalars(
        _metric_query(context, MetricType.AIR_ROUND_TRIP)
        .where(
            models.CalculatedMetric.origin_code == origin,
            models.CalculatedMetric.destination_code == destination,
        )
        .order_by(models.CalculatedMetric.service_date, models.CalculatedMetric.nights)
    ).all()

    cells: list[dict[str, Any]] = []
    priced: list[float] = []
    for metric in metrics:
        value = _num(metric.median_price)
        if value is not None:
            priced.append(value)
        cells.append(
            {
                "metric_id": metric.id,
                "departure_date": metric.service_date.isoformat() if metric.service_date else None,
                "return_date": metric.return_date.isoformat() if metric.return_date else None,
                "nights": metric.nights,
                "day_offset": metric.day_offset,
                "median": value,
                "min": _num(metric.min_price),
                "offers_count": metric.offers_count,
                "sources_count": metric.sources_count,
                "confidence_level": str(metric.confidence_level),
                "is_partial": metric.is_partial,
                "is_no_market": metric.is_no_market,
                "no_market_reason": metric.no_market_reason,
                "warning_codes": list(metric.warning_codes or []),
            }
        )

    registry = city_registry()
    return {
        "origin": {"code": origin, "name": registry.get(origin).name},
        "destination": {"code": destination, "name": registry.get(destination).name},
        "parameters": {
            "transport": "AIR",
            "cabin": "ECONOMY",
            "direct_only": True,
            "refundable": False,
            "passengers": 1,
            "trip_type": "ROUND_TRIP",
        },
        "departure_dates": sorted(
            {cell["departure_date"] for cell in cells if cell["departure_date"]}
        ),
        "nights_options": sorted({cell["nights"] for cell in cells if cell["nights"]}),
        # Шкала строится только по клеткам с ценой. Клетки без рынка и
        # несобранные в неё не входят.
        "scale": {
            "min": round(min(priced), 2) if priced else None,
            "max": round(max(priced), 2) if priced else None,
            "priced_cells": len(priced),
            "no_market_cells": sum(1 for cell in cells if cell["is_no_market"]),
            "total_cells": len(cells),
        },
        "cells": cells,
    }


def available_air_nights(session: Session, context: SnapshotContext) -> list[int]:
    """Длительности поездок, наблюдавшиеся в этом расчёте."""
    return sorted(
        value
        for value in session.scalars(
            select(models.CalculatedMetric.nights)
            .where(
                models.CalculatedMetric.calculation_run_id == context.run.id,
                models.CalculatedMetric.metric_type == MetricType.AIR_ROUND_TRIP.value,
                models.CalculatedMetric.nights.is_not(None),
            )
            .distinct()
        )
        if value is not None
    )


# --------------------------------------------------------------------------- #
# Блок C — график проживания
# --------------------------------------------------------------------------- #


def hotel_chart(
    session: Session, context: SnapshotContext, *, stars: int
) -> dict[str, Any]:
    """Одна ночь, один взрослый, один номер — по всем пяти городам сразу."""
    metrics = session.scalars(
        _metric_query(context, MetricType.HOTEL_NIGHT)
        .where(models.CalculatedMetric.stars == stars)
        .order_by(models.CalculatedMetric.city_code, models.CalculatedMetric.check_in)
    ).all()

    registry = city_registry()
    series: dict[str, dict[str, Any]] = {}
    for metric in metrics:
        entry = series.setdefault(
            metric.city_code,
            {
                "city": {"code": metric.city_code, "name": registry.get(metric.city_code).name},
                "points": [],
            },
        )
        entry["points"].append(_point(metric, date_field="check_in"))
    return {
        "parameters": {
            "stars": stars,
            "nights": 1,
            "adults": 1,
            "rooms": 1,
            "property_type": "HOTEL",
        },
        "series": [series[code] for code in registry.codes if code in series],
    }


def _metric_query(context: SnapshotContext, metric_type: MetricType) -> Select:
    return select(models.CalculatedMetric).where(
        models.CalculatedMetric.calculation_run_id == context.run.id,
        models.CalculatedMetric.metric_type == metric_type.value,
    )


def _point(metric: models.CalculatedMetric, *, date_field: str = "service_date") -> dict[str, Any]:
    value = getattr(metric, date_field)
    return {
        "metric_id": metric.id,
        "date": value.isoformat() if value else None,
        "day_offset": metric.day_offset,
        "median": _num(metric.median_price),
        "min": _num(metric.min_price),
        "offers_count": metric.offers_count,
        "sources_count": metric.sources_count,
        "confidence_level": str(metric.confidence_level),
        "quality_score": metric.quality_score,
        "is_partial": metric.is_partial,
        "is_no_market": metric.is_no_market,
        "no_market_reason": metric.no_market_reason,
        "warning_codes": list(metric.warning_codes or []),
    }


def _num(value: Any) -> float | None:
    return float(value) if value is not None else None


# --------------------------------------------------------------------------- #
# Детализация цены
# --------------------------------------------------------------------------- #


def metric_details(session: Session, metric_id: int) -> dict[str, Any]:
    """Всё, что нужно, чтобы проверить опубликованную цифру."""
    metric = session.get(models.CalculatedMetric, metric_id)
    if metric is None:
        raise LookupError(f"Метрика {metric_id} не найдена")
    run = session.get(models.CalculationRun, metric.calculation_run_id)
    snapshot = session.get(models.MarketSnapshot, metric.snapshot_id)
    job = session.get(models.CollectionJob, metric.collection_job_id)

    attempts = session.scalars(
        select(models.SourceAttempt)
        .where(models.SourceAttempt.collection_job_id == metric.collection_job_id)
        .order_by(models.SourceAttempt.id)
    ).all()

    registry = city_registry()

    def city_name(code: str | None) -> str | None:
        return registry.get(code).name if code else None

    return {
        "metric_id": metric.id,
        "metric_type": str(metric.metric_type),
        "snapshot_id": snapshot.id,
        "snapshot_date": snapshot.snapshot_date.isoformat(),
        "snapshot_status": str(snapshot.status),
        "is_synthetic": bool(snapshot.is_synthetic),
        "calculation_run_id": run.id,
        "methodology_version": run.methodology_version,
        "computed_at": metric.computed_at.isoformat(),
        # Свежесть считается от момента фактического получения данных, а не от
        # даты снимка.
        "fetched_at": metric.fetched_at.isoformat() if metric.fetched_at else None,
        "currency": metric.currency,
        "median_price": _num(metric.median_price),
        "min_price": _num(metric.min_price),
        "max_price": _num(metric.max_price),
        "p25_price": _num(metric.p25_price),
        "p75_price": _num(metric.p75_price),
        "offers_count": metric.offers_count,
        "offers_excluded": metric.offers_excluded,
        "sources_count": metric.sources_count,
        "source_coverage": metric.source_coverage,
        "quality_score": metric.quality_score,
        "confidence_level": str(metric.confidence_level),
        "is_partial": metric.is_partial,
        "is_no_market": metric.is_no_market,
        "no_market_reason": metric.no_market_reason,
        "warning_codes": list(metric.warning_codes or []),
        "per_source": dict(metric.per_source or {}),
        "observation": {
            "job_key": job.job_key if job else None,
            "family": str(job.family) if job else None,
            "origin": {"code": metric.origin_code, "name": city_name(metric.origin_code)}
            if metric.origin_code
            else None,
            "destination": {
                "code": metric.destination_code,
                "name": city_name(metric.destination_code),
            }
            if metric.destination_code
            else None,
            "city": {"code": metric.city_code, "name": city_name(metric.city_code)}
            if metric.city_code
            else None,
            "service_date": metric.service_date.isoformat() if metric.service_date else None,
            "return_date": metric.return_date.isoformat() if metric.return_date else None,
            "check_in": metric.check_in.isoformat() if metric.check_in else None,
            "check_out": metric.check_out.isoformat() if metric.check_out else None,
            "stars": metric.stars,
            "nights": metric.nights,
            "params": dict(job.params) if job else {},
        },
        "source_attempts": [
            {
                "source_attempt_id": attempt.id,
                "source_code": attempt.source_code,
                "execution_scope": attempt.execution_scope,
                "outcome": str(attempt.outcome),
                "no_market_reason": attempt.no_market_reason,
                "requested_at": attempt.requested_at.isoformat(),
                "fetched_at": attempt.fetched_at.isoformat() if attempt.fetched_at else None,
                "latency_ms": attempt.latency_ms,
                "http_calls": attempt.http_calls,
                "pages_read": attempt.pages_read,
                "total_matched": attempt.total_matched,
                "is_partial": attempt.is_partial,
                "partial_reason": attempt.partial_reason,
                "offers_parsed": attempt.offers_parsed,
                "error_code": attempt.error_code,
                "error_message": attempt.error_message,
                "connector_version": attempt.connector_version,
                "source_tool_version": attempt.source_tool_version,
                "diagnostics": dict(attempt.diagnostics or {}),
            }
            for attempt in attempts
        ],
    }


def metric_offers(
    session: Session, metric_id: int, *, included: bool | None = None, limit: int = 1000
) -> list[dict[str, Any]]:
    """Предложения метрики: включённые и исключённые, с причиной исключения."""
    query = (
        select(models.MetricOfferLink, models.Offer, models.RawResponse)
        .join(models.Offer, models.Offer.id == models.MetricOfferLink.offer_id)
        .outerjoin(models.RawResponse, models.RawResponse.id == models.Offer.raw_response_id)
        .where(models.MetricOfferLink.metric_id == metric_id)
    )
    if included is not None:
        query = query.where(models.MetricOfferLink.is_included.is_(included))
    rows = session.execute(
        query.order_by(
            models.MetricOfferLink.is_included.desc(), models.Offer.price
        ).limit(limit)
    ).all()

    result = []
    for link, offer, raw in rows:
        transport = offer.transport_attributes or {}
        property_info = offer.property_attributes or {}
        result.append(
            {
                "offer_id": offer.id,
                "is_included": link.is_included,
                "exclusion_reason": link.exclusion_reason,
                "exclusion_detail": link.exclusion_detail,
                "source_code": offer.source_code,
                "kind": offer.kind,
                "price": float(offer.price),
                "source_price": _num(offer.source_price),
                "price_basis": offer.price_basis,
                "currency": offer.currency,
                "fetched_at": offer.fetched_at.isoformat(),
                "departure_at": offer.departure_at.isoformat() if offer.departure_at else None,
                "arrival_at": offer.arrival_at.isoformat() if offer.arrival_at else None,
                "return_departure_at": offer.return_departure_at.isoformat()
                if offer.return_departure_at
                else None,
                "check_in": offer.check_in.isoformat() if offer.check_in else None,
                "check_out": offer.check_out.isoformat() if offer.check_out else None,
                "nights": offer.nights,
                "route": _route_label(offer, transport, property_info),
                "carrier": ", ".join(transport.get("carriers") or []) or None,
                "vehicle": transport.get("train_number")
                or _flight_numbers(transport),
                "car_type": transport.get("car_type"),
                "service_class": transport.get("service_class"),
                "fare_family": transport.get("fare_family"),
                "refundable": transport.get("refundable"),
                "property_name": property_info.get("name"),
                "stars": property_info.get("stars"),
                "property_type": property_info.get("property_type"),
                "room_name": property_info.get("room_name"),
                "validation_flags": list(offer.validation_flags or []),
                "fingerprint": offer.fingerprint,
                "equivalence_key": offer.equivalence_key,
                "deeplink": offer.deeplink,
                "provenance": {
                    "source_attempt_id": offer.source_attempt_id,
                    "raw_response_id": offer.raw_response_id,
                    "raw_storage_ref": raw.storage_ref if raw else None,
                    "raw_endpoint": raw.endpoint if raw else None,
                    "raw_sha256": raw.payload_sha256 if raw else None,
                    "raw_page": raw.page_number if raw else None,
                    "requested_at": raw.requested_at.isoformat() if raw else None,
                },
            }
        )
    return result


def _route_label(
    offer: models.Offer, transport: dict[str, Any], property_info: dict[str, Any]
) -> str | None:
    if offer.kind == "HOTEL":
        return property_info.get("name")
    origin = (transport.get("origin_station") or {}).get("raw") or offer.origin_code
    destination = (transport.get("destination_station") or {}).get("raw") or offer.destination_code
    return f"{origin} → {destination}"


def _flight_numbers(transport: dict[str, Any]) -> str | None:
    numbers = [
        str(segment.get("voyage_no"))
        for segment in (transport.get("itinerary") or [])
        if segment.get("voyage_no")
    ]
    return " / ".join(numbers) or None


# --------------------------------------------------------------------------- #
# Покрытие
# --------------------------------------------------------------------------- #


def coverage_matrix(session: Session, snapshot_id: int) -> dict[str, Any]:
    """Матрица покрытия: дыры должны быть видны глазом, а не вычитываться."""
    rows = session.execute(
        select(
            models.CollectionJob.family,
            models.CollectionJob.origin_code,
            models.CollectionJob.destination_code,
            models.CollectionJob.city_code,
            models.CollectionJob.day_offset,
            models.CollectionJob.stars,
            models.CollectionJob.status,
            func.count(models.CollectionJob.id),
        )
        .where(models.CollectionJob.snapshot_id == snapshot_id)
        .group_by(
            models.CollectionJob.family,
            models.CollectionJob.origin_code,
            models.CollectionJob.destination_code,
            models.CollectionJob.city_code,
            models.CollectionJob.day_offset,
            models.CollectionJob.stars,
            models.CollectionJob.status,
        )
    ).all()

    cells: dict[str, dict[str, dict[str, int]]] = {}
    for family, origin, destination, city, offset, stars, status, count in rows:
        family_bucket = cells.setdefault(str(family), {})
        key = f"{city}|{stars}" if str(family) == "HOTEL" else f"{origin}→{destination}"
        row_bucket = family_bucket.setdefault(key, {})
        column = f"D+{offset}" if offset is not None else "?"
        row_bucket[column] = row_bucket.get(column, 0)
        # Клетка описывается худшим статусом среди наблюдений: одна дыра в
        # клетке — это дыра.
        row_bucket[column] = max(row_bucket[column], _status_weight(str(status)) * int(count > 0))
    return {"snapshot_id": snapshot_id, "cells": cells, "legend": _STATUS_WEIGHTS}


_STATUS_WEIGHTS = {
    "SUCCESS": 1,
    "PARTIAL": 2,
    "NO_MARKET": 3,
    "FAILED": 4,
    "PLANNED": 5,
    "DISPATCHED": 5,
    "RUNNING": 5,
}


def _status_weight(status: str) -> int:
    return _STATUS_WEIGHTS.get(status, 0)


def snapshot_overview(session: Session, snapshot: models.MarketSnapshot) -> dict[str, Any]:
    """Сводка снимка для экрана «Покрытие / качество»."""
    run = active_run(session, snapshot.id)
    confidence_rows = session.execute(
        select(
            models.CalculatedMetric.metric_type,
            models.CalculatedMetric.confidence_level,
            func.count(models.CalculatedMetric.id),
        )
        .where(models.CalculatedMetric.calculation_run_id == (run.id if run else -1))
        .group_by(models.CalculatedMetric.metric_type, models.CalculatedMetric.confidence_level)
    ).all()

    distribution: dict[str, dict[str, int]] = {}
    for metric_type, level, count in confidence_rows:
        distribution.setdefault(str(metric_type), {})[str(level)] = int(count)

    sources = session.scalars(
        select(models.SnapshotSourceResult).where(
            models.SnapshotSourceResult.snapshot_id == snapshot.id
        )
    ).all()

    return {
        "snapshot": {
            "snapshot_id": snapshot.id,
            "snapshot_date": snapshot.snapshot_date.isoformat(),
            "attempt_no": snapshot.attempt_no,
            "status": str(snapshot.status),
            "is_synthetic": bool(snapshot.is_synthetic),
            "started_at": snapshot.started_at.isoformat() if snapshot.started_at else None,
            "primary_collection_finished_at": (
                snapshot.primary_collection_finished_at.isoformat()
                if snapshot.primary_collection_finished_at
                else None
            ),
            "recovery_finished_at": (
                snapshot.recovery_finished_at.isoformat()
                if snapshot.recovery_finished_at
                else None
            ),
            "published_at": snapshot.published_at.isoformat() if snapshot.published_at else None,
            "coverage_total": round(float(snapshot.coverage_total), 4),
            "coverage_rail": round(float(snapshot.coverage_rail), 4),
            "coverage_air": round(float(snapshot.coverage_air), 4),
            "coverage_hotel": round(float(snapshot.coverage_hotel), 4),
            "publication_notes": list(snapshot.publication_notes or []),
        },
        "quality_summary": dict(snapshot.quality_summary or {}),
        "confidence_distribution": distribution,
        "sources": [
            {
                "source_code": row.source_code,
                "family": str(row.family),
                "attempts": row.attempts,
                "success": row.success,
                "partial": row.partial,
                "no_market": row.no_market,
                "failures_by_outcome": dict(row.failures_by_outcome or {}),
                "offers_parsed": row.offers_parsed,
                "http_calls": row.http_calls,
                "p50_latency_ms": row.p50_latency_ms,
                "p95_latency_ms": row.p95_latency_ms,
                "first_fetched_at": row.first_fetched_at.isoformat()
                if row.first_fetched_at
                else None,
                "last_fetched_at": row.last_fetched_at.isoformat()
                if row.last_fetched_at
                else None,
            }
            for row in sources
        ],
    }


def available_origins() -> list[dict[str, str]]:
    return [{"code": city.code, "name": city.name} for city in city_registry().ordered]


def snapshot_is_published(snapshot: models.MarketSnapshot) -> bool:
    return SnapshotStatus(snapshot.status).is_published
