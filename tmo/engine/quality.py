"""Качество и уверенность метрики.

Балл — не оценка «хорошести» цены, а мера того, насколько наблюдение полно.
Он складывается из четырёх измеримых вещей: сколько предложений в выборке,
сколько источников её подтвердили, была ли выдача обрезана и насколько свежи
данные. Пороги и веса живут в профиле методики.

Одно правило важнее остальных: **источник один по составу MVP — это не повод
для LOW**. Авиа и проживание в текущем scope наблюдаются одним источником, и
понижать за это значило бы объявить всю витрину недостоверной.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from tmo.catalog.registry import MethodologyProfile
from tmo.core.enums import CollectionFamily, ConfidenceLevel, WarningCode
from tmo.core.timeutil import age_minutes


@dataclass(slots=True)
class QualityInput:
    family: CollectionFamily
    offers_count: int
    sources_count: int
    is_partial: bool
    fetched_at: datetime | None
    #: Сколько объектов источник насчитал против того, что мы прочитали.
    total_matched: int | None = None
    offers_seen: int | None = None
    #: Медиана по каждому источнику отдельно — для проверки расхождения.
    per_source_median: dict[str, Decimal] = field(default_factory=dict)
    #: Хотя бы один источник наблюдения ответил технической ошибкой.
    had_source_failure: bool = False
    #: Серверные фильтры источника не подтверждены эхом ответа.
    server_filter_unconfirmed: bool = False
    #: Источник сообщил о строках, которые не смог классифицировать.
    unverified_category_count: int = 0
    outliers_not_removed: bool = False


@dataclass(slots=True)
class QualityResult:
    quality_score: float
    confidence_level: ConfidenceLevel
    source_coverage: float
    warning_codes: list[str]
    components: dict[str, float]


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


def evaluate(data: QualityInput, profile: MethodologyProfile) -> QualityResult:
    quality = profile.quality
    weights = quality["weights"]
    target = profile.target_sample(data.family)
    expected_sources = max(1, profile.expected_sources(data.family))

    sample_component = _clamp(data.offers_count / target) if target else 0.0
    source_coverage = _clamp(data.sources_count / expected_sources)

    completeness = 1.0
    if data.is_partial:
        # Доля прочитанного от объявленного источником. Если он не сообщил
        # общее число, обрезка штрафуется фиксированно: неизвестный масштаб
        # смещения хуже известного.
        if data.total_matched and data.offers_seen:
            completeness = _clamp(data.offers_seen / data.total_matched)
        else:
            completeness = 0.5

    freshness = 0.0
    if data.fetched_at is not None:
        age = age_minutes(data.fetched_at)
        fresh = float(quality["fresh_minutes"])
        stale = float(quality["stale_minutes"])
        if age <= fresh:
            freshness = 1.0
        elif age >= stale:
            freshness = 0.0
        else:
            freshness = _clamp(1.0 - (age - fresh) / max(1.0, stale - fresh))

    components = {
        "sample": sample_component,
        "sources": source_coverage,
        "completeness": completeness,
        "freshness": freshness,
    }
    score = sum(components[name] * float(weights[name]) for name in components)

    warnings = _warnings(data, profile)
    level = _confidence(score, data, profile, warnings)
    return QualityResult(
        quality_score=round(score, 4),
        confidence_level=level,
        source_coverage=round(source_coverage, 4),
        warning_codes=warnings,
        components={k: round(v, 4) for k, v in components.items()},
    )


def _warnings(data: QualityInput, profile: MethodologyProfile) -> list[str]:
    codes: list[str] = []
    if data.is_partial:
        codes.append(WarningCode.PARTIAL_SAMPLE.value)
    if data.offers_count < profile.min_offers_for("medium", data.family):
        codes.append(WarningCode.SMALL_SAMPLE.value)
    if data.sources_count <= 1 and profile.expected_sources(data.family) > 1:
        # Один источник там, где их ожидалось два, — это потеря сверки.
        codes.append(WarningCode.SINGLE_SOURCE.value)
    if data.had_source_failure:
        codes.append(WarningCode.SOURCE_FAILURE_IN_SAMPLE.value)
    if data.outliers_not_removed:
        codes.append(WarningCode.OUTLIERS_NOT_REMOVED.value)
    if data.server_filter_unconfirmed:
        codes.append(WarningCode.SERVER_FILTER_UNCONFIRMED.value)
    if data.unverified_category_count > 0:
        codes.append(WarningCode.UNVERIFIED_CATEGORY_DROPPED.value)

    if data.fetched_at is not None and age_minutes(data.fetched_at) >= float(
        profile.quality["stale_minutes"]
    ):
        codes.append(WarningCode.STALE_FETCH.value)

    medians = [value for value in data.per_source_median.values() if value]
    if len(medians) >= 2:
        low, high = min(medians), max(medians)
        threshold = Decimal(str(profile.confidence["source_disagreement_threshold"]))
        if low > 0 and (high - low) / low > threshold:
            # Разрыв объясним: один источник отдаёт тариф перевозчика, другой —
            # цену агента со своим сбором. Это предупреждение, а не ошибка.
            codes.append(WarningCode.SOURCE_DISAGREEMENT.value)
    return codes


def _confidence(
    score: float,
    data: QualityInput,
    profile: MethodologyProfile,
    warnings: list[str],
) -> ConfidenceLevel:
    confidence = profile.confidence
    if data.offers_count < int(confidence["min_offers_to_publish"]):
        return ConfidenceLevel.LOW

    level = ConfidenceLevel.LOW
    if score >= float(confidence["high_min_score"]):
        level = ConfidenceLevel.HIGH
    elif score >= float(confidence["medium_min_score"]):
        level = ConfidenceLevel.MEDIUM

    # Жёсткие понижения. Балл может быть высоким за счёт свежести и
    # источников, но выборка из двух предложений остаётся выборкой из двух.
    if level is ConfidenceLevel.HIGH and data.offers_count < profile.min_offers_for(
        "high", data.family
    ):
        level = ConfidenceLevel.MEDIUM
    if level is not ConfidenceLevel.LOW and data.offers_count < profile.min_offers_for(
        "medium", data.family
    ):
        level = ConfidenceLevel.LOW
    if (
        confidence.get("partial_caps_at_medium", True)
        and data.is_partial
        and level is ConfidenceLevel.HIGH
    ):
        # Обрезанная выдача не может быть HIGH: медиана смещена, и неизвестно
        # насколько.
        level = ConfidenceLevel.MEDIUM
    if (
        confidence.get("source_failure_caps_at_medium", True)
        and data.had_source_failure
        and level is ConfidenceLevel.HIGH
    ):
        # Выборка собрана не тем составом источников, каким планировалась.
        level = ConfidenceLevel.MEDIUM
    if WarningCode.STALE_FETCH.value in warnings and level is ConfidenceLevel.HIGH:
        level = ConfidenceLevel.MEDIUM
    return level
