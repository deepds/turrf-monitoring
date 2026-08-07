"""Финализация снимка: покрытие → ворота → статус публикации.

Провалившийся снимок не становится витриной, но и не исчезает: дашборд
показывает последний пригодный, явно указывая его дату и статус. Показать
вчерашние данные — нормально. Показать их как сегодняшние — нет.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from tmo.catalog.registry import methodology_profile
from tmo.core.enums import PublicationStatus, SnapshotStatus
from tmo.core.logging import get_logger
from tmo.core.timeutil import now_utc, sla_deadline
from tmo.db import models
from tmo.services import gates as gate_module
from tmo.services.calculation import active_run
from tmo.services.coverage import compute_coverage, persist_source_results

logger = get_logger(__name__)


@dataclass(slots=True)
class PublicationResult:
    snapshot_id: int
    status: str
    coverage_total: float
    notes: list[dict[str, Any]]
    gates: list[dict[str, Any]]
    within_sla: bool


def finalize_snapshot(
    session: Session,
    snapshot_id: int,
    *,
    profile_version: str | None = None,
    golden_result: dict[str, Any] | None = None,
) -> PublicationResult:
    snapshot = session.get(models.MarketSnapshot, snapshot_id)
    if snapshot is None:
        raise ValueError(f"Снимок {snapshot_id} не найден")
    profile = methodology_profile(profile_version)

    coverage = compute_coverage(session, snapshot_id)
    persist_source_results(session, snapshot_id)

    plan = session.scalars(
        select(models.CollectionPlan).where(models.CollectionPlan.snapshot_id == snapshot_id)
    ).first()
    if plan is not None:
        plan.completed = coverage.total.completed
        plan.successful = coverage.total.successful
        plan.partial = coverage.total.partial
        plan.no_market = coverage.total.no_market
        plan.failed = coverage.total.failed
        plan.missing = coverage.total.missing
        plan.by_family = {name: item.as_dict() for name, item in coverage.by_family.items()}

    run = active_run(session, snapshot_id)
    results = [gate_module.gate_collection_completeness(session, snapshot_id, coverage)]
    if run is not None:
        results.append(gate_module.gate_data_validity(session, run.id, profile))
        results.append(gate_module.gate_calculation_validity(golden_result))
        results.append(gate_module.gate_publication_validity(session, run.id))
    else:
        results.append(
            gate_module.GateResult(
                gate="DATA_VALIDITY",
                passed=False,
                violations=[
                    gate_module.GateViolation(
                        rule="NO_CALCULATION_RUN",
                        count=1,
                        message="Для снимка нет активного расчёта",
                    )
                ],
            )
        )

    status, notes = gate_module.evaluate_publication(
        coverage=coverage, gates=results, profile=profile
    )

    snapshot.coverage_total = coverage.total.completion
    snapshot.coverage_rail = coverage.by_family["RAIL"].completion
    snapshot.coverage_air = coverage.by_family["AIR"].completion
    snapshot.coverage_hotel = coverage.by_family["HOTEL"].completion
    snapshot.quality_summary = {
        "coverage": coverage.as_dict(),
        "gates": [gate.as_dict() for gate in results],
        "methodology_version": profile.version,
        "calculation_run_id": run.id if run else None,
    }
    snapshot.publication_notes = notes

    if status == PublicationStatus.FAILED.value:
        snapshot.status = SnapshotStatus.FAILED
        snapshot.published_at = None
    else:
        snapshot.status = (
            SnapshotStatus.READY
            if status == PublicationStatus.READY.value
            else SnapshotStatus.DEGRADED
        )
        snapshot.published_at = now_utc()

    if run is not None:
        run.gate_results = {gate.gate: gate.as_dict() for gate in results}

    within_sla = bool(
        snapshot.published_at and snapshot.published_at <= sla_deadline(snapshot.snapshot_date)
    )
    session.flush()

    logger.info(
        "Снимок финализирован",
        snapshot_id=snapshot_id,
        status=snapshot.status,
        coverage_total=snapshot.coverage_total,
        within_sla=within_sla,
        notes=[note["code"] for note in notes],
    )
    return PublicationResult(
        snapshot_id=snapshot_id,
        status=snapshot.status.value if hasattr(snapshot.status, "value") else str(snapshot.status),
        coverage_total=snapshot.coverage_total,
        notes=notes,
        gates=[gate.as_dict() for gate in results],
        within_sla=within_sla,
    )
