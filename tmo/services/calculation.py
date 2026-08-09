"""Расчёт: наблюдения → опубликованные метрики.

Расчёт отделён от наблюдения. Один и тот же снимок можно пересчитать другой
версией методики, и это создаст **новый** ``CalculationRun``; прежний остаётся
неизменным. Именно поэтому решение об исключении предложения хранится в связи
метрика↔предложение, а не в самом предложении.

Провенанс строится здесь и обязан быть полным:

```text
CalculatedMetric → CalculationRun → MethodologyProfile
                 → MetricOfferLink → Offer → RawResponse → source request
```
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy import func, insert, select, update
from sqlalchemy.orm import Session

from tmo.catalog.registry import MethodologyProfile, methodology_profile
from tmo.core.enums import (
    CollectionFamily,
    ConfidenceLevel,
    JobStatus,
    MetricType,
    TransportMode,
)
from tmo.core.logging import get_logger
from tmo.core.money import add, quantize
from tmo.core.timeutil import now_utc
from tmo.db import models
from tmo.engine.quality import QualityInput, evaluate
from tmo.engine.selection import Candidate
from tmo.engine.selection import select as run_selection
from tmo.engine.statistics import median, summarize
from tmo.services.snapshot import register_methodology

logger = get_logger(__name__)

_FAMILY_METRIC = {
    CollectionFamily.RAIL: MetricType.RAIL_LEG,
    CollectionFamily.AIR: MetricType.AIR_ROUND_TRIP,
}


@dataclass(slots=True)
class CalculationReport:
    calculation_run_id: int
    snapshot_id: int
    methodology_version: str
    metrics: int
    offers_considered: int
    offers_included: int
    trip_rows: int
    no_market_metrics: int
    elapsed_seconds: float


def metric_type_for(job: models.CollectionJob) -> MetricType:
    family = CollectionFamily(job.family)
    if family in _FAMILY_METRIC:
        return _FAMILY_METRIC[family]
    # Одна ночь и период — разные вопросы к рынку, и метрики у них разные:
    # точка графика проживания против стоимости проживания в поездке.
    return MetricType.HOTEL_NIGHT if (job.nights or 0) == 1 else MetricType.HOTEL_STAY


def calculate_snapshot(
    session: Session,
    snapshot_id: int,
    *,
    profile_version: str | None = None,
    make_active: bool = True,
    build_trip_mart: bool = True,
) -> CalculationReport:
    """Применяет методику к снимку и создаёт новый расчёт."""
    started = now_utc()
    profile = methodology_profile(profile_version)
    register_methodology(session, profile)

    snapshot = session.get(models.MarketSnapshot, snapshot_id)
    if snapshot is None:
        raise ValueError(f"Снимок {snapshot_id} не найден")

    if make_active:
        # Активный расчёт снимка ровно один; прежние остаются для сравнения
        # версий методики и никогда не переписываются.
        session.execute(
            update(models.CalculationRun)
            .where(models.CalculationRun.snapshot_id == snapshot_id)
            .values(is_active=False)
        )

    run = models.CalculationRun(
        snapshot_id=snapshot_id,
        methodology_version=profile.version,
        is_active=make_active,
        started_at=started,
        gate_results={},
    )
    session.add(run)
    session.flush()

    jobs = list(
        session.scalars(
            select(models.CollectionJob).where(models.CollectionJob.snapshot_id == snapshot_id)
        )
    )
    failures_by_job = _job_failures(session, snapshot_id)
    diagnostics_by_job = _job_diagnostics(session, snapshot_id)

    metrics = 0
    considered = 0
    included_total = 0
    no_market = 0

    # Наблюдения обрабатываются порциями, а предложения читаются кортежами, а
    # не ORM-объектами. На полной матрице это разница между сотнями тысяч
    # живых объектов в сессии и постоянным потреблением памяти: авиа даёт до
    # 590 тарифных строк на одно наблюдение.
    for chunk in _chunks(jobs, JOB_CHUNK):
        active = [
            job
            for job in chunk
            if JobStatus(job.status)
            not in (JobStatus.PLANNED, JobStatus.DISPATCHED, JobStatus.RUNNING)
        ]
        if not active:
            continue
        offers_by_job = _load_offers(session, [job.id for job in active])

        metric_rows: list[dict[str, Any]] = []
        prepared: list[tuple[models.CollectionJob, SelectionOutcome]] = []
        for job in active:
            offers = offers_by_job.get(job.id, [])
            considered += len(offers)
            outcome = _evaluate_job(
                job=job,
                offers=offers,
                profile=profile,
                had_source_failure=failures_by_job.get(job.id, False),
                diagnostics=diagnostics_by_job.get(job.id, {}),
                run_id=run.id,
            )
            prepared.append((job, outcome))
            metric_rows.append(outcome.metric_row)
            included_total += outcome.included_count
            if outcome.metric_row["is_no_market"]:
                no_market += 1

        metric_ids = list(
            session.scalars(
                insert(models.CalculatedMetric).returning(models.CalculatedMetric.id),
                metric_rows,
            )
        )
        metrics += len(metric_ids)

        link_rows: list[dict[str, Any]] = []
        for metric_id, (_, outcome) in zip(metric_ids, prepared, strict=True):
            for link in outcome.links:
                link_rows.append({**link, "metric_id": metric_id})
        if link_rows:
            session.execute(insert(models.MetricOfferLink), link_rows)
        session.flush()

    trip_rows = 0
    if build_trip_mart:
        trip_rows = build_trip_cost_mart(session, run=run, snapshot=snapshot, profile=profile)

    run.finished_at = now_utc()
    run.metrics_count = metrics
    run.offers_considered = considered
    run.offers_included = included_total
    session.flush()

    elapsed = (run.finished_at - started).total_seconds()
    logger.info(
        "Расчёт завершён",
        calculation_run_id=run.id,
        snapshot_id=snapshot_id,
        methodology_version=profile.version,
        metrics=metrics,
        offers_included=included_total,
        trip_rows=trip_rows,
        elapsed_seconds=round(elapsed, 2),
    )
    return CalculationReport(
        calculation_run_id=run.id,
        snapshot_id=snapshot_id,
        methodology_version=profile.version,
        metrics=metrics,
        offers_considered=considered,
        offers_included=included_total,
        trip_rows=trip_rows,
        no_market_metrics=no_market,
        elapsed_seconds=round(elapsed, 2),
    )


#: Сколько наблюдений обрабатывается за одну порцию расчёта.
JOB_CHUNK = 400


@dataclass(slots=True)
class OfferRow:
    """Предложение для расчёта: только поля, нужные отбору."""

    id: int
    source_code: str
    price: Decimal
    currency: str
    equivalence_key: str
    transport: dict[str, Any]
    property_info: dict[str, Any]
    validation_flags: list[str]


@dataclass(slots=True)
class SelectionOutcome:
    metric_row: dict[str, Any]
    links: list[dict[str, Any]]
    included_count: int


def _chunks(items: list[Any], size: int) -> Iterator[list[Any]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _load_offers(session: Session, job_ids: list[int]) -> dict[int, list[OfferRow]]:
    rows = session.execute(
        select(
            models.Offer.id,
            models.Offer.collection_job_id,
            models.Offer.source_code,
            models.Offer.price,
            models.Offer.currency,
            models.Offer.equivalence_key,
            models.Offer.transport_attributes,
            models.Offer.property_attributes,
            models.Offer.validation_flags,
        ).where(models.Offer.collection_job_id.in_(job_ids))
    ).all()
    grouped: dict[int, list[OfferRow]] = defaultdict(list)
    for row in rows:
        grouped[int(row[1])].append(
            OfferRow(
                id=int(row[0]),
                source_code=str(row[2]),
                price=row[3],
                currency=str(row[4]),
                equivalence_key=str(row[5]),
                transport=row[6] or {},
                property_info=row[7] or {},
                validation_flags=list(row[8] or []),
            )
        )
    return grouped


def _evaluate_job(
    *,
    run_id: int,
    job: models.CollectionJob,
    offers: list[OfferRow],
    profile: MethodologyProfile,
    had_source_failure: bool,
    diagnostics: dict[str, Any],
) -> SelectionOutcome:
    family = CollectionFamily(job.family)
    rules = profile.selection_for(family)
    if family is CollectionFamily.HOTEL and job.stars is not None:
        # Отбор обязан сравнивать звёздность предложения со звёздностью **этой**
        # метрики, а не с объединением разрешённых профилем.
        #
        # Матрица проживания — город × звёздность × даты, и у каждой метрики своя
        # категория. Профиль разрешает 3, 4 и 5 звёзд; фильтр по объединению
        # пропускал в метрику «четыре звезды» предложения трёх и пяти. Ворота
        # это ловили — 55 253 нарушения `HOTEL_STARS_ALLOWED` в снимке
        # 09.08.2026, — и снимок не публиковался. Правильно не публиковался:
        # медиана «четырёхзвёздочного проживания», посчитанная по трём и пяти
        # звёздам, описывает не ту величину, что заявлена в её названии.
        allowed = {int(item) for item in (rules.get("stars") or [3, 4, 5])}
        rules = {**rules, "stars": sorted(allowed & {int(job.stars)})}
    candidates = [
        Candidate(
            ref=offer.id,
            source_code=offer.source_code,
            price=offer.price,
            currency=offer.currency,
            equivalence_key=offer.equivalence_key,
            transport=offer.transport,
            property_info=offer.property_info,
            validation_flags=offer.validation_flags,
        )
        for offer in offers
    ]
    selection = run_selection(
        candidates,
        family=family,
        rules=rules,
        outlier_rules=profile.outliers,
        currency=str(profile.aggregation.get("currency", "RUB")),
    )

    prices = selection.prices
    stats = summarize(prices)
    per_source: dict[str, list[Decimal]] = defaultdict(list)
    for decision in selection.included:
        per_source[decision.candidate.source_code].append(decision.candidate.price)
    per_source_median = {
        code: quantize(median(values)) for code, values in per_source.items() if values
    }

    fetched_at = job.fetched_at
    is_no_market = not prices
    quality = evaluate(
        QualityInput(
            family=family,
            offers_count=len(prices),
            sources_count=len(per_source_median),
            is_partial=bool(job.is_partial),
            fetched_at=fetched_at,
            total_matched=diagnostics.get("total_matched"),
            offers_seen=len(offers) or None,
            per_source_median={k: v for k, v in per_source_median.items() if v},
            had_source_failure=had_source_failure,
            server_filter_unconfirmed=diagnostics.get("server_filter_unconfirmed", False),
            unverified_category_count=int(diagnostics.get("unverified_category", 0) or 0),
            outliers_not_removed=bool(
                selection.outlier and selection.outlier.reason == "TOO_MANY_OUTLIERS"
            ),
        ),
        profile,
    )

    metric_row = {
        "calculation_run_id": run_id,
        "snapshot_id": job.snapshot_id,
        "collection_job_id": job.id,
        "metric_type": metric_type_for(job).value,
        "series_key": job.series_key,
        "origin_code": job.origin_code,
        "destination_code": job.destination_code,
        "city_code": job.city_code,
        "service_date": job.service_date,
        "return_date": job.return_date,
        "check_in": job.check_in,
        "check_out": job.check_out,
        "stars": job.stars,
        "day_offset": job.day_offset,
        "nights": job.nights,
        "currency": str(profile.aggregation.get("currency", "RUB")),
        "median_price": stats.median,
        "min_price": stats.minimum,
        "max_price": stats.maximum,
        "p25_price": stats.p25,
        "p75_price": stats.p75,
        "offers_count": len(prices),
        "offers_excluded": len(selection.excluded),
        "sources_count": len(per_source_median),
        "source_coverage": quality.source_coverage,
        "quality_score": quality.quality_score,
        "confidence_level": quality.confidence_level.value,
        "is_partial": bool(job.is_partial),
        "is_no_market": is_no_market,
        "no_market_reason": job.no_market_reason if is_no_market else None,
        "warning_codes": quality.warning_codes,
        "per_source": {
            code: {"median": float(value), "offers": len(per_source[code])}
            for code, value in per_source_median.items()
            if value is not None
        },
        "fetched_at": fetched_at,
        "computed_at": now_utc(),
    }

    # Исключённые предложения сохраняются вместе с причиной: это то, что
    # позволяет объяснить цифру, а не только показать её.
    links = [
        {
            "offer_id": decision.candidate.ref,
            "is_included": decision.included,
            "exclusion_reason": decision.reason.value if decision.reason else None,
            "exclusion_detail": (decision.detail or None),
            "contributed_price": decision.candidate.price if decision.included else None,
        }
        for decision in selection.decisions
    ]
    return SelectionOutcome(metric_row=metric_row, links=links, included_count=len(prices))


def _job_failures(session: Session, snapshot_id: int) -> dict[int, bool]:
    """У каких наблюдений хотя бы один источник ответил технической ошибкой."""
    rows = session.execute(
        select(models.SourceAttempt.collection_job_id, models.SourceAttempt.outcome).where(
            models.SourceAttempt.snapshot_id == snapshot_id
        )
    ).all()
    failures: dict[int, bool] = {}
    for job_id, outcome in rows:
        from tmo.core.enums import AttemptOutcome

        if AttemptOutcome(str(outcome)).is_technical_failure:
            failures[int(job_id)] = True
    return failures


def _job_diagnostics(session: Session, snapshot_id: int) -> dict[int, dict[str, Any]]:
    """Диагностика попыток, влияющая на качество метрики."""
    rows = session.execute(
        select(
            models.SourceAttempt.collection_job_id,
            models.SourceAttempt.total_matched,
            models.SourceAttempt.diagnostics,
        ).where(models.SourceAttempt.snapshot_id == snapshot_id)
    ).all()
    result: dict[int, dict[str, Any]] = {}
    for job_id, total_matched, diagnostics in rows:
        entry = result.setdefault(int(job_id), {"total_matched": None, "unverified_category": 0})
        if total_matched is not None:
            entry["total_matched"] = max(entry["total_matched"] or 0, int(total_matched))
        diagnostics = diagnostics or {}
        unverified = diagnostics.get("unverified_seat_category")
        if unverified:
            entry["unverified_category"] = max(entry["unverified_category"], int(unverified))
        confirmed = diagnostics.get("server_filters_confirmed")
        if isinstance(confirmed, dict) and not all(confirmed.values()):
            entry["server_filter_unconfirmed"] = True
    return result


# --------------------------------------------------------------------------- #
# Витрина «Куда ехать»
# --------------------------------------------------------------------------- #


def build_trip_cost_mart(
    session: Session,
    *,
    run: models.CalculationRun,
    snapshot: models.MarketSnapshot,
    profile: MethodologyProfile,
) -> int:
    """Собирает расчётную стоимость поездки из отдельно наблюдавшихся частей.

    Ни одна составляющая не выдумывается. Авиа — настоящий круговой тариф,
    ЖД — два отдельно наблюдавшихся плеча, проживание — настоящая бронь на пару
    дат. Если составляющей нет, строка помечается неполной и перечисляет,
    чего именно не хватило: пустая клетка обязана объяснять себя.
    """
    metrics = list(
        session.scalars(
            select(models.CalculatedMetric).where(
                models.CalculatedMetric.calculation_run_id == run.id
            )
        )
    )
    rail: dict[tuple, models.CalculatedMetric] = {}
    air: dict[tuple, models.CalculatedMetric] = {}
    hotel: dict[tuple, models.CalculatedMetric] = {}
    for metric in metrics:
        if metric.metric_type == MetricType.RAIL_LEG:
            rail[(metric.origin_code, metric.destination_code, metric.service_date)] = metric
        elif metric.metric_type == MetricType.AIR_ROUND_TRIP:
            air[
                (metric.origin_code, metric.destination_code, metric.service_date, metric.return_date)
            ] = metric
        else:
            hotel[(metric.city_code, metric.check_in, metric.check_out, metric.stars)] = metric

    rows: list[models.TripCostRow] = []
    stars_options = sorted({m.stars for m in hotel.values() if m.stars})
    for (origin, destination, departure, return_date), air_metric in air.items():
        for stars in stars_options:
            stay = hotel.get((destination, departure, return_date, stars))
            rows.append(
                _trip_row(
                    run=run,
                    snapshot=snapshot,
                    origin=origin,
                    destination=destination,
                    departure=departure,
                    return_date=return_date,
                    mode=TransportMode.AIR,
                    stars=stars,
                    transport_metrics=[air_metric],
                    stay=stay,
                )
            )

    for (origin, destination, departure, return_date) in list(air.keys()):
        outbound = rail.get((origin, destination, departure))
        inbound = rail.get((destination, origin, return_date))
        if outbound is None and inbound is None:
            continue
        for stars in stars_options:
            stay = hotel.get((destination, departure, return_date, stars))
            rows.append(
                _trip_row(
                    run=run,
                    snapshot=snapshot,
                    origin=origin,
                    destination=destination,
                    departure=departure,
                    return_date=return_date,
                    mode=TransportMode.RAIL,
                    stars=stars,
                    transport_metrics=[m for m in (outbound, inbound) if m is not None],
                    stay=stay,
                    expected_transport_parts=2,
                )
            )

    session.add_all(rows)
    session.flush()
    return len(rows)


def _trip_row(
    *,
    run: models.CalculationRun,
    snapshot: models.MarketSnapshot,
    origin: str,
    destination: str,
    departure: Any,
    return_date: Any,
    mode: TransportMode,
    stars: int,
    transport_metrics: list[models.CalculatedMetric],
    stay: models.CalculatedMetric | None,
    expected_transport_parts: int = 1,
) -> models.TripCostRow:
    missing: list[str] = []
    if len(transport_metrics) < expected_transport_parts:
        missing.append("TRANSPORT_LEG")
    transport_median = add(*[m.median_price for m in transport_metrics]) if transport_metrics else None
    transport_min = add(*[m.min_price for m in transport_metrics]) if transport_metrics else None
    if transport_median is None:
        missing.append("TRANSPORT_PRICE")

    stay_median = stay.median_price if stay else None
    stay_min = stay.min_price if stay else None
    if stay is None:
        missing.append("ACCOMMODATION")
    elif stay_median is None:
        missing.append("ACCOMMODATION_PRICE")

    total_median = add(transport_median, stay_median)
    total_min = add(transport_min, stay_min)

    parts = [*transport_metrics, *([stay] if stay else [])]
    warnings = sorted({code for metric in parts for code in (metric.warning_codes or [])})
    confidence = _worst_confidence(parts)
    quality = min((metric.quality_score for metric in parts), default=0.0)
    offers = sum(metric.offers_count for metric in parts)
    sources = max((metric.sources_count for metric in parts), default=0)

    return models.TripCostRow(
        calculation_run_id=run.id,
        snapshot_id=snapshot.id,
        origin_code=origin,
        destination_code=destination,
        departure_date=departure,
        return_date=return_date,
        transport_mode=mode.value,
        stars=stars,
        nights=(return_date - departure).days,
        transport_median=quantize(transport_median),
        transport_min=quantize(transport_min),
        accommodation_median=quantize(stay_median),
        accommodation_min=quantize(stay_min),
        total_median=quantize(total_median),
        total_min=quantize(total_min),
        transport_metric_ids=[metric.id for metric in transport_metrics],
        accommodation_metric_id=stay.id if stay else None,
        offers_count=offers,
        sources_count=sources,
        quality_score=round(quality, 4),
        confidence_level=confidence,
        is_partial=any(metric.is_partial for metric in parts),
        is_complete=total_median is not None,
        warning_codes=warnings,
        missing_components=missing,
    )


_CONFIDENCE_ORDER = {
    ConfidenceLevel.LOW: 0,
    ConfidenceLevel.MEDIUM: 1,
    ConfidenceLevel.HIGH: 2,
}


def _worst_confidence(metrics: list[models.CalculatedMetric]) -> ConfidenceLevel:
    """Поездка не может быть надёжнее своей слабейшей составляющей."""
    if not metrics:
        return ConfidenceLevel.LOW
    return min(
        (ConfidenceLevel(metric.confidence_level) for metric in metrics),
        key=lambda level: _CONFIDENCE_ORDER[level],
    )


def active_run(session: Session, snapshot_id: int) -> models.CalculationRun | None:
    return session.scalars(
        select(models.CalculationRun)
        .where(
            models.CalculationRun.snapshot_id == snapshot_id,
            models.CalculationRun.is_active.is_(True),
        )
        .order_by(models.CalculationRun.id.desc())
        .limit(1)
    ).first()


def run_statistics(session: Session, run_id: int) -> dict[str, Any]:
    rows = session.execute(
        select(
            models.CalculatedMetric.metric_type,
            func.count(models.CalculatedMetric.id),
            func.sum(models.CalculatedMetric.offers_count),
        )
        .where(models.CalculatedMetric.calculation_run_id == run_id)
        .group_by(models.CalculatedMetric.metric_type)
    ).all()
    return {
        str(metric_type): {"metrics": int(count), "offers": int(offers or 0)}
        for metric_type, count, offers in rows
    }
