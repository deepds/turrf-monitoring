"""Ворота публикации.

Ворота проверяют не «есть ли данные», а «можно ли за эти данные отвечать».
Проверяется то, что реально попало в опубликованные цифры: включённые
предложения, а не всё собранное. Предложение, отбракованное методикой, ворота
не волнует — на витрину оно не попало.

Четыре уровня (SCOPE-R P17):

1. полнота сбора — у каждого запланированного наблюдения есть конечный исход;
2. валидность данных — включённое предложение соответствует запросу и методике;
3. валидность расчёта — Golden Dataset проходит;
4. валидность публикации — фронтенд не считает бизнес-метрики.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from tmo.catalog.registry import MethodologyProfile
from tmo.core.enums import CarType, CollectionFamily, JobStatus, MetricType, PropertyType
from tmo.db import models
from tmo.services.coverage import CoverageReport


@dataclass(slots=True)
class GateViolation:
    rule: str
    count: int
    sample: list[dict[str, Any]] = field(default_factory=list)
    message: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "rule": self.rule,
            "count": self.count,
            "message": self.message,
            "sample": self.sample[:5],
        }


@dataclass(slots=True)
class GateResult:
    gate: str
    passed: bool
    violations: list[GateViolation] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "gate": self.gate,
            "passed": self.passed,
            "violations": [violation.as_dict() for violation in self.violations],
            "details": self.details,
        }


def gate_collection_completeness(
    session: Session, snapshot_id: int, coverage: CoverageReport
) -> GateResult:
    """Ворота 1: у каждого запланированного наблюдения есть конечный исход."""
    unfinished = session.execute(
        select(models.CollectionJob.status, func.count(models.CollectionJob.id))
        .where(
            models.CollectionJob.snapshot_id == snapshot_id,
            models.CollectionJob.status.in_(
                [JobStatus.PLANNED.value, JobStatus.DISPATCHED.value, JobStatus.RUNNING.value]
            ),
        )
        .group_by(models.CollectionJob.status)
    ).all()

    violations = [
        GateViolation(
            rule="NO_TERMINAL_OUTCOME",
            count=int(count),
            message=f"{count} наблюдений остались в состоянии {status}",
        )
        for status, count in unfinished
    ]

    # Молчаливый пропуск источника: наблюдение завершено, но попыток нет.
    silent = session.scalar(
        select(func.count(models.CollectionJob.id)).where(
            models.CollectionJob.snapshot_id == snapshot_id,
            models.CollectionJob.status.in_(
                [
                    JobStatus.SUCCESS.value,
                    JobStatus.PARTIAL.value,
                    JobStatus.NO_MARKET.value,
                    JobStatus.FAILED.value,
                ]
            ),
            ~models.CollectionJob.id.in_(
                select(models.SourceAttempt.collection_job_id).where(
                    models.SourceAttempt.snapshot_id == snapshot_id
                )
            ),
        )
    )
    if silent:
        violations.append(
            GateViolation(
                rule="SILENT_SOURCE_SKIP",
                count=int(silent),
                message=f"{silent} наблюдений завершены без единой записи об обращении",
            )
        )

    return GateResult(
        gate="COLLECTION_COMPLETENESS",
        passed=not violations,
        violations=violations,
        details=coverage.as_dict(),
    )


def gate_data_validity(session: Session, run_id: int, profile: MethodologyProfile) -> GateResult:
    """Ворота 2: включённое предложение соответствует запросу и методике."""
    rows = session.execute(
        select(
            models.CalculatedMetric.id,
            models.CalculatedMetric.metric_type,
            models.CalculatedMetric.stars,
            models.CalculatedMetric.currency,
            models.Offer.id,
            models.Offer.price,
            models.Offer.currency,
            models.Offer.kind,
            models.Offer.transport_attributes,
            models.Offer.property_attributes,
            models.Offer.validation_flags,
            models.Offer.equivalence_key,
            models.Offer.source_code,
        )
        .join(models.MetricOfferLink, models.MetricOfferLink.metric_id == models.CalculatedMetric.id)
        .join(models.Offer, models.Offer.id == models.MetricOfferLink.offer_id)
        .where(
            models.CalculatedMetric.calculation_run_id == run_id,
            models.MetricOfferLink.is_included.is_(True),
        )
    ).all()

    counters: dict[str, list[dict[str, Any]]] = {}

    def flag(rule: str, payload: dict[str, Any]) -> None:
        counters.setdefault(rule, []).append(payload)

    allowed_stars = {int(item) for item in profile.selection["hotel"]["stars"]}
    allowed_types = set(profile.selection["hotel"]["property_types"])
    base_currency = str(profile.aggregation.get("currency", "RUB"))
    seen_equivalence: dict[int, set[str]] = {}

    for (
        metric_id,
        metric_type,
        metric_stars,
        _metric_currency,
        offer_id,
        price,
        currency,
        kind,
        transport,
        property_info,
        flags,
        equivalence,
        source_code,
    ) in rows:
        payload = {"metric_id": int(metric_id), "offer_id": int(offer_id), "source": source_code}
        transport = transport or {}
        property_info = property_info or {}
        flags = list(flags or [])

        if price is None or price <= 0:
            flag("PRICE_POSITIVE", payload)
        if currency != base_currency:
            flag("CURRENCY_MATCHES_PROFILE", {**payload, "currency": currency})
        if "ROUTE_MISMATCH" in flags:
            flag("ROUTE_MATCHES_REQUEST", payload)
        if any(item.endswith("DATE_MISMATCH") or item == "NIGHTS_MISMATCH" for item in flags):
            flag("DATES_MATCH_REQUEST", {**payload, "flags": flags})

        if kind == "RAIL":
            if transport.get("car_type") != CarType.COMPARTMENT.value:
                flag("RAIL_COMPARTMENT_ONLY", {**payload, "car_type": transport.get("car_type")})
        elif kind == "AIR":
            if not transport.get("is_direct"):
                flag("AIR_DIRECT_ONLY", payload)
            if not transport.get("is_round_trip"):
                # Сумма двух односторонних тарифов круговым тарифом не является.
                flag("AIR_ROUND_TRIP_NOT_SYNTHESIZED", payload)
        elif kind == "HOTEL":
            property_type = property_info.get("property_type") or PropertyType.UNKNOWN.value
            if property_type not in allowed_types:
                flag("HOTEL_PROPERTY_TYPE_ALLOWED", {**payload, "type": property_type})
            stars = property_info.get("stars")
            if stars is None or int(stars) not in allowed_stars:
                flag("HOTEL_STARS_ALLOWED", {**payload, "stars": stars})
            if metric_stars is not None and stars is not None and int(stars) != int(metric_stars):
                flag("HOTEL_STARS_ALLOWED", {**payload, "stars": stars, "metric": metric_stars})
            if "PRICE_DERIVED_FROM_PER_NIGHT" in flags and metric_type in (
                MetricType.HOTEL_STAY.value,
                MetricType.HOTEL_STAY,
            ):
                # Цена периода, полученная умножением ночи на число ночей, —
                # расчётная величина, выданная за наблюдаемую.
                flag("HOTEL_STAY_NOT_DERIVED_FROM_NIGHTS", payload)

        bucket = seen_equivalence.setdefault(int(metric_id), set())
        marker = f"{source_code}:{equivalence}"
        if marker in bucket:
            flag("NO_DUPLICATE_EQUIVALENCE_KEY", {**payload, "equivalence_key": equivalence})
        bucket.add(marker)

    # Статус пагинации обязан быть известен: неизвестная полнота выдачи хуже
    # известной неполной.
    unknown_pagination = session.scalar(
        select(func.count(models.SourceAttempt.id)).where(
            models.SourceAttempt.snapshot_id
            == select(models.CalculationRun.snapshot_id)
            .where(models.CalculationRun.id == run_id)
            .scalar_subquery(),
            models.SourceAttempt.outcome.in_(["SUCCESS", "PARTIAL"]),
            models.SourceAttempt.pages_read == 0,
        )
    )
    if unknown_pagination:
        counters.setdefault("PAGINATION_STATUS_KNOWN", []).extend(
            [{"attempts": int(unknown_pagination)}]
        )

    critical = set(profile.publication["critical_validation_rules"])
    violations = [
        GateViolation(
            rule=rule,
            count=len(items),
            sample=items[:5],
            message=f"Правило {rule} нарушено в {len(items)} включённых предложениях",
        )
        for rule, items in sorted(counters.items())
    ]
    blocking = [violation for violation in violations if violation.rule in critical]
    return GateResult(
        gate="DATA_VALIDITY",
        passed=not blocking,
        violations=violations,
        details={
            "checked_links": len(rows),
            "critical_rules": sorted(critical),
            "blocking": [violation.rule for violation in blocking],
        },
    )


def gate_calculation_validity(golden_result: dict[str, Any] | None) -> GateResult:
    """Ворота 3: Golden Dataset.

    Прогон эталонного набора — часть публикации, а не только CI: методика,
    не воспроизводящая заранее зафиксированные числа, готовой не считается.
    """
    if golden_result is None:
        return GateResult(
            gate="CALCULATION_VALIDITY",
            passed=True,
            details={"status": "SKIPPED", "note": "Golden Dataset не запускался в этом прогоне"},
        )
    passed = bool(golden_result.get("passed"))
    violations = [] if passed else [
        GateViolation(
            rule="GOLDEN_DATASET",
            count=int(golden_result.get("failed", 0)),
            message="Golden Dataset не воспроизводится текущей методикой",
            sample=list(golden_result.get("failures", []))[:5],
        )
    ]
    return GateResult(
        gate="CALCULATION_VALIDITY", passed=passed, violations=violations, details=golden_result
    )


def gate_publication_validity(session: Session, run_id: int) -> GateResult:
    """Ворота 4: API отдаёт готовые DTO, фронтенд ничего не досчитывает.

    Проверяется машинно: у каждой опубликованной метрики с данными должны быть
    заполнены median, min, счётчики и уровень уверенности. Если что-то из
    этого пусто, фронтенду пришлось бы это вычислить — а это запрещено.
    """
    incomplete = session.execute(
        select(
            models.CalculatedMetric.id,
            models.CalculatedMetric.metric_type,
            models.CalculatedMetric.median_price,
            models.CalculatedMetric.min_price,
            models.CalculatedMetric.confidence_level,
        ).where(
            models.CalculatedMetric.calculation_run_id == run_id,
            models.CalculatedMetric.is_no_market.is_(False),
            (models.CalculatedMetric.median_price.is_(None))
            | (models.CalculatedMetric.min_price.is_(None))
            | (models.CalculatedMetric.confidence_level.is_(None)),
        )
    ).all()

    violations = []
    if incomplete:
        violations.append(
            GateViolation(
                rule="METRIC_DTO_COMPLETE",
                count=len(incomplete),
                sample=[{"metric_id": int(row[0]), "type": str(row[1])} for row in incomplete[:5]],
                message="Метрика с данными не содержит готовых значений для витрины",
            )
        )

    orphan = session.scalar(
        select(func.count(models.CalculatedMetric.id)).where(
            models.CalculatedMetric.calculation_run_id == run_id,
            models.CalculatedMetric.offers_count > 0,
            ~models.CalculatedMetric.id.in_(
                select(models.MetricOfferLink.metric_id).where(
                    models.MetricOfferLink.is_included.is_(True)
                )
            ),
        )
    )
    if orphan:
        violations.append(
            GateViolation(
                rule="PROVENANCE_PRESENT",
                count=int(orphan),
                message="Метрика с предложениями не имеет ни одной связи с ними",
            )
        )

    return GateResult(
        gate="PUBLICATION_VALIDITY", passed=not violations, violations=violations
    )


def evaluate_publication(
    *,
    coverage: CoverageReport,
    gates: list[GateResult],
    profile: MethodologyProfile,
) -> tuple[str, list[dict[str, Any]]]:
    """Итоговый статус публикации и человеко-читаемые причины.

    Пороги берутся из профиля методики: порог в коде менял бы историю
    незаметно для расчёта.
    """
    from tmo.core.enums import PublicationStatus

    notes: list[dict[str, Any]] = []
    blocking = [gate for gate in gates if not gate.passed]
    for gate in blocking:
        notes.append(
            {
                "code": f"GATE_{gate.gate}",
                "severity": "CRITICAL",
                "message": f"Ворота {gate.gate} не пройдены",
                "violations": [violation.rule for violation in gate.violations],
            }
        )

    completion = coverage.total.completion
    data_share = coverage.total.data_share
    status = PublicationStatus.READY

    if blocking:
        status = PublicationStatus.FAILED
    else:
        if completion < profile.degraded_completion():
            status = PublicationStatus.FAILED
            notes.append(
                {
                    "code": "COVERAGE_BELOW_DEGRADED",
                    "severity": "CRITICAL",
                    "message": (
                        f"Покрытие {completion:.1%} ниже порога DEGRADED "
                        f"{profile.degraded_completion():.0%}"
                    ),
                }
            )
        elif completion < profile.ready_completion():
            status = PublicationStatus.DEGRADED
            notes.append(
                {
                    "code": "COVERAGE_BELOW_READY",
                    "severity": "WARNING",
                    "message": (
                        f"Покрытие {completion:.1%} ниже порога READY "
                        f"{profile.ready_completion():.0%}"
                    ),
                }
            )

        for family in CollectionFamily:
            bucket = coverage.by_family.get(family.value)
            if bucket is None or bucket.planned == 0:
                continue
            if bucket.completion < profile.degraded_completion(family):
                status = PublicationStatus.FAILED
                notes.append(
                    {
                        "code": f"COVERAGE_{family.value}_CRITICAL",
                        "severity": "CRITICAL",
                        "message": (
                            f"{family.value}: покрытие {bucket.completion:.1%} ниже "
                            f"{profile.degraded_completion(family):.0%}"
                        ),
                    }
                )
            elif bucket.completion < profile.ready_completion(family):
                if status is PublicationStatus.READY:
                    status = PublicationStatus.DEGRADED
                notes.append(
                    {
                        "code": f"COVERAGE_{family.value}_LOW",
                        "severity": "WARNING",
                        "message": (
                            f"{family.value}: покрытие {bucket.completion:.1%} ниже "
                            f"{profile.ready_completion(family):.0%}"
                        ),
                    }
                )

        min_data_share = float(profile.publication["min_data_share"])
        if data_share < min_data_share:
            # Всё «завершено», но данных нет: так выглядит сломанный разбор,
            # а не пустой рынок.
            status = PublicationStatus.FAILED
            notes.append(
                {
                    "code": "DATA_SHARE_TOO_LOW",
                    "severity": "CRITICAL",
                    "message": (
                        f"Доля наблюдений с данными {data_share:.1%} ниже {min_data_share:.0%}: "
                        "похоже на отказ разбора, а не на пустой рынок"
                    ),
                }
            )

    return status.value, notes
