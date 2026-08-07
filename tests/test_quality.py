"""Качество и уверенность.

Главное правило, которое проверяется здесь: **один источник по составу scope —
не повод для LOW.** Авиа и проживание в текущей постановке наблюдаются одним
источником, и понижать за это значило бы объявить всю витрину недостоверной.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from tmo.catalog.registry import methodology_profile
from tmo.core.enums import CollectionFamily, ConfidenceLevel, WarningCode
from tmo.core.timeutil import now_utc
from tmo.engine.quality import QualityInput, evaluate


@pytest.fixture()
def profile():
    return methodology_profile("baseline_v1")


def fresh() -> object:
    return now_utc() - timedelta(minutes=30)


def test_single_source_air_can_be_high(profile) -> None:
    """Авиа наблюдается одним источником по составу MVP — это не дефект."""
    result = evaluate(
        QualityInput(
            family=CollectionFamily.AIR,
            offers_count=25,
            sources_count=1,
            is_partial=False,
            fetched_at=fresh(),
        ),
        profile,
    )
    assert result.confidence_level is ConfidenceLevel.HIGH
    assert WarningCode.SINGLE_SOURCE.value not in result.warning_codes


def test_single_source_rail_is_warned(profile) -> None:
    """У ЖД источников ожидается два: потеря одного — потеря сверки."""
    result = evaluate(
        QualityInput(
            family=CollectionFamily.RAIL,
            offers_count=12,
            sources_count=1,
            is_partial=False,
            fetched_at=fresh(),
        ),
        profile,
    )
    assert WarningCode.SINGLE_SOURCE.value in result.warning_codes
    assert result.source_coverage == 0.5


def test_partial_sample_cannot_be_high(profile) -> None:
    """Обрезанная выдача смещает медиану неизвестно куда."""
    result = evaluate(
        QualityInput(
            family=CollectionFamily.HOTEL,
            offers_count=200,
            sources_count=1,
            is_partial=True,
            fetched_at=fresh(),
            total_matched=600,
            offers_seen=200,
        ),
        profile,
    )
    assert result.confidence_level is not ConfidenceLevel.HIGH
    assert WarningCode.PARTIAL_SAMPLE.value in result.warning_codes


def test_source_failure_caps_confidence(profile) -> None:
    """Выборка собрана не тем составом источников, каким планировалась."""
    result = evaluate(
        QualityInput(
            family=CollectionFamily.RAIL,
            offers_count=20,
            sources_count=1,
            is_partial=False,
            fetched_at=fresh(),
            had_source_failure=True,
        ),
        profile,
    )
    assert result.confidence_level is ConfidenceLevel.MEDIUM
    assert WarningCode.SOURCE_FAILURE_IN_SAMPLE.value in result.warning_codes


def test_tiny_sample_is_low(profile) -> None:
    result = evaluate(
        QualityInput(
            family=CollectionFamily.RAIL,
            offers_count=1,
            sources_count=1,
            is_partial=False,
            fetched_at=fresh(),
        ),
        profile,
    )
    assert result.confidence_level is ConfidenceLevel.LOW
    assert WarningCode.SMALL_SAMPLE.value in result.warning_codes


def test_stale_data_is_flagged(profile) -> None:
    """Свежесть считается от fetched_at, а не от даты снимка."""
    result = evaluate(
        QualityInput(
            family=CollectionFamily.RAIL,
            offers_count=20,
            sources_count=2,
            is_partial=False,
            fetched_at=now_utc() - timedelta(hours=30),
        ),
        profile,
    )
    assert WarningCode.STALE_FETCH.value in result.warning_codes
    assert result.confidence_level is not ConfidenceLevel.HIGH


def test_source_disagreement_is_a_warning_not_an_error(profile) -> None:
    """Разрыв объясним: тариф перевозчика против цены агента со своим сбором."""
    result = evaluate(
        QualityInput(
            family=CollectionFamily.RAIL,
            offers_count=20,
            sources_count=2,
            is_partial=False,
            fetched_at=fresh(),
            per_source_median={"rzd": Decimal("16998"), "tutu_mcp": Decimal("25239")},
        ),
        profile,
    )
    assert WarningCode.SOURCE_DISAGREEMENT.value in result.warning_codes
    # Предупреждение не обнуляет метрику.
    assert result.quality_score > 0.5


def test_rail_target_sample_reflects_real_market_size(profile) -> None:
    """Шесть поездов — полная выборка для ЖД, а не признак бедности данных."""
    result = evaluate(
        QualityInput(
            family=CollectionFamily.RAIL,
            offers_count=6,
            sources_count=2,
            is_partial=False,
            fetched_at=fresh(),
        ),
        profile,
    )
    assert result.components["sample"] == 1.0
    assert result.confidence_level is ConfidenceLevel.HIGH
