"""Справочники и состояние сервиса."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from tmo.api.deps import db_session
from tmo.catalog.registry import (
    available_profiles,
    city_registry,
    methodology_profile,
    source_registry,
)
from tmo.core.config import get_settings
from tmo.core.enums import ExclusionReason, WarningCode
from tmo.core.timeutil import HORIZON_DAYS, now_msk, sla_deadline, snapshot_date_for
from tmo.db import models
from tmo.planner.matrix import STAR_CATEGORIES, expected_size
from tmo.version import APP_VERSION

router = APIRouter(tags=["Справочники"])


@router.get("/health", summary="Состояние сервиса")
def health(session: Session = Depends(db_session)) -> dict[str, Any]:
    database_ok = True
    try:
        session.execute(text("SELECT 1"))
    except Exception:
        database_ok = False
    latest = session.scalars(
        select(models.MarketSnapshot)
        .order_by(models.MarketSnapshot.snapshot_date.desc(), models.MarketSnapshot.id.desc())
        .limit(1)
    ).first()
    return {
        "status": "ok" if database_ok else "degraded",
        "version": APP_VERSION,
        "database": "ok" if database_ok else "unavailable",
        "now_msk": now_msk().isoformat(),
        "sla_deadline": sla_deadline(snapshot_date_for()).isoformat(),
        "latest_snapshot": {
            "snapshot_date": latest.snapshot_date.isoformat(),
            "status": str(latest.status),
            "coverage_total": round(float(latest.coverage_total), 4),
        }
        if latest
        else None,
    }


@router.get("/reference/cities", summary="Города MVP")
def cities() -> dict[str, Any]:
    registry = city_registry()
    return {
        "cities": [
            {
                "code": city.code,
                "name": city.name,
                "name_en": city.name_en,
                "timezone": city.timezone,
                "multi_airport": city.multi_airport,
            }
            for city in registry.ordered
        ],
        "known_market_gaps": [
            {
                "origin": gap.origin,
                "destination": gap.destination,
                "family": str(gap.family),
                "expectation": gap.expectation,
                "note": gap.note,
            }
            for gap in registry.known_market_gaps
        ],
        "star_categories": list(STAR_CATEGORIES),
        "horizon_days": get_settings().horizon_days or HORIZON_DAYS,
    }


@router.get("/reference/methodology", summary="Активная версия методики")
def methodology(version: str | None = None) -> dict[str, Any]:
    profile = methodology_profile(version)
    return {
        "version": profile.version,
        "title": profile.title,
        "effective_from": profile.effective_from.isoformat(),
        "description": profile.description,
        "selection": profile.selection,
        "aggregation": profile.aggregation,
        "outliers": profile.outliers,
        "quality": profile.quality,
        "confidence": profile.confidence,
        "publication": profile.publication,
        "trip_cost": profile.trip_cost,
        "available_versions": available_profiles(),
    }


@router.get("/reference/sources", summary="Источники и семантика их цен")
def sources() -> dict[str, Any]:
    return {
        "sources": [
            {
                "code": source.code,
                "name": source.name,
                "protocol": source.protocol,
                "families": [str(family) for family in source.families],
                "price_semantics": source.price_semantics,
                "rate_limit_per_minute": source.rate_limit_per_minute,
                "is_enabled": source.is_enabled,
                "is_synthetic": source.is_synthetic,
                "notes": source.notes.strip(),
            }
            for source in source_registry().sources
        ]
    }


@router.get("/reference/dictionary", summary="Расшифровка кодов витрины")
def dictionary() -> dict[str, Any]:
    """Коды исключений и предупреждений человеческим языком.

    Код без расшифровки на экране бесполезен: пользователь видит, что что-то
    не так, но не знает что.
    """
    return {
        "exclusion_reasons": {
            ExclusionReason.NOT_DIRECT.value: "Не прямой рейс или поезд",
            ExclusionReason.WRONG_CAR_TYPE.value: "Тип вагона вне методики (учитывается купе)",
            ExclusionReason.WRONG_CABIN.value: "Класс обслуживания вне эконома",
            ExclusionReason.REFUNDABLE_FARE.value: "Возвратный тариф либо возвратность не подтверждена",
            ExclusionReason.NOT_ROUND_TRIP.value: "Не круговой тариф",
            ExclusionReason.WRONG_PROPERTY_TYPE.value: "Не гостиница (апартаменты, хостел, гостевой дом)",
            ExclusionReason.WRONG_STARS.value: "Звёздность вне запрошенной категории",
            ExclusionReason.WRONG_ROUTE.value: "Маршрут не соответствует запросу",
            ExclusionReason.WRONG_DATES.value: "Даты не соответствуют наблюдению",
            ExclusionReason.NON_POSITIVE_PRICE.value: "Цена отсутствует или неположительна",
            ExclusionReason.WRONG_CURRENCY.value: "Валюта отличается от валюты методики",
            ExclusionReason.DISABLED_PLACES_GROUP.value: "Места целевого назначения: льготная цена, не рынок",
            ExclusionReason.SALE_FORBIDDEN.value: "Продажа запрещена перевозчиком",
            ExclusionReason.NO_PLACES.value: "Мест в продаже нет: цена справочная",
            ExclusionReason.FARE_COLLAPSED_NOT_CHEAPEST.value: (
                "Другой тариф того же рейса или поезда; в расчёт идёт самый дешёвый подходящий"
            ),
            ExclusionReason.DUPLICATE.value: "Дубликат того же предложения",
            ExclusionReason.STATISTICAL_OUTLIER.value: "Статистический выброс",
            ExclusionReason.UNCLASSIFIED_CAR_TYPE.value: "Источник не сообщил тип вагона",
        },
        "warning_codes": {
            WarningCode.PARTIAL_SAMPLE.value: "Выдача источника обрезана: выборка неполна",
            WarningCode.SMALL_SAMPLE.value: "Малая выборка",
            WarningCode.SINGLE_SOURCE.value: "Только один источник там, где ожидалось два",
            WarningCode.SOURCE_DISAGREEMENT.value: "Источники существенно расходятся в цене",
            WarningCode.OUTLIERS_NOT_REMOVED.value: "Выбросы не удалялись: выборка неоднородна",
            WarningCode.SOURCE_FAILURE_IN_SAMPLE.value: "Один из источников ответил ошибкой",
            WarningCode.STALE_FETCH.value: "Данные получены давно",
            WarningCode.SERVER_FILTER_UNCONFIRMED.value: (
                "Источник не подтвердил применение серверного фильтра"
            ),
            WarningCode.UNVERIFIED_CATEGORY_DROPPED.value: (
                "Источник не смог классифицировать часть тарифов"
            ),
        },
        "expected_matrix": expected_size(get_settings().horizon_days or HORIZON_DAYS),
    }
