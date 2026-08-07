"""Ворота публикации, покрытие и неизменяемость методики.

Ворота отвечают не на вопрос «есть ли данные», а на вопрос «можно ли за эти
данные отвечать».
"""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import select, update

from tmo.catalog.registry import methodology_profile
from tmo.core.enums import JobStatus, PublicationStatus, SnapshotStatus
from tmo.db import models
from tmo.db.session import session_scope
from tmo.services.calculation import active_run, calculate_snapshot
from tmo.services.coverage import compute_coverage, find_holes
from tmo.services.gates import (
    evaluate_publication,
    gate_calculation_validity,
    gate_collection_completeness,
    gate_data_validity,
    gate_publication_validity,
)
from tmo.services.pipeline import run_daily_pipeline
from tmo.services.publication import finalize_snapshot
from tmo.services.snapshot import register_methodology

SNAPSHOT = date(2026, 8, 7)


@pytest.fixture()
def published(database: str):
    from tmo.connectors.registry import close_all

    close_all()
    return run_daily_pipeline(
        snapshot_date=SNAPSHOT,
        horizon_days=3,
        replay_mode="SIMULATED",
        is_synthetic=True,
        recovery_rounds=1,
        batch_size=200,
    )


# --------------------------------------------------------------------------- #
# Покрытие
# --------------------------------------------------------------------------- #


def test_no_market_counts_as_completed_and_is_not_a_hole(published) -> None:
    """Отсутствие сообщения завершает наблюдение и не требует досбора вечно."""
    with session_scope() as session:
        coverage = compute_coverage(session, published.snapshot_id)
        holes = find_holes(session, published.snapshot_id)

    rail = coverage.by_family["RAIL"]
    assert rail.no_market > 0
    assert rail.completed == rail.planned
    assert rail.completion == pytest.approx(1.0)
    assert holes == []


def test_data_share_separates_empty_market_from_broken_parser(published) -> None:
    with session_scope() as session:
        coverage = compute_coverage(session, published.snapshot_id)
    assert 0.0 < coverage.total.data_share <= 1.0
    assert coverage.total.data_share > 0.5


def test_unfinished_observation_is_missing_not_completed(published) -> None:
    with session_scope() as session:
        job_id = session.scalar(
            select(models.CollectionJob.id).where(
                models.CollectionJob.snapshot_id == published.snapshot_id
            )
        )
        session.execute(
            update(models.CollectionJob)
            .where(models.CollectionJob.id == job_id)
            .values(status=JobStatus.PLANNED.value)
        )
    with session_scope() as session:
        coverage = compute_coverage(session, published.snapshot_id)
        assert coverage.total.missing == 1
        assert job_id in find_holes(session, published.snapshot_id)


# --------------------------------------------------------------------------- #
# Ворота
# --------------------------------------------------------------------------- #


def test_all_gates_pass_on_a_healthy_snapshot(published) -> None:
    profile = methodology_profile("baseline_v1")
    with session_scope() as session:
        coverage = compute_coverage(session, published.snapshot_id)
        run = active_run(session, published.snapshot_id)
        gates = [
            gate_collection_completeness(session, published.snapshot_id, coverage),
            gate_data_validity(session, run.id, profile),
            gate_publication_validity(session, run.id),
        ]
    assert all(gate.passed for gate in gates), [g.as_dict() for g in gates if not g.passed]


def test_unfinished_observation_blocks_the_first_gate(published) -> None:
    with session_scope() as session:
        session.execute(
            update(models.CollectionJob)
            .where(models.CollectionJob.snapshot_id == published.snapshot_id)
            .values(status=JobStatus.RUNNING.value)
        )
    with session_scope() as session:
        coverage = compute_coverage(session, published.snapshot_id)
        gate = gate_collection_completeness(session, published.snapshot_id, coverage)
    assert gate.passed is False
    assert gate.violations[0].rule == "NO_TERMINAL_OUTCOME"


def test_incomplete_dto_blocks_publication(published) -> None:
    """Пустая медиана у метрики с данными заставила бы фронтенд её досчитать."""
    with session_scope() as session:
        run = active_run(session, published.snapshot_id)
        metric_id = session.scalar(
            select(models.CalculatedMetric.id).where(
                models.CalculatedMetric.calculation_run_id == run.id,
                models.CalculatedMetric.is_no_market.is_(False),
            )
        )
        session.execute(
            update(models.CalculatedMetric)
            .where(models.CalculatedMetric.id == metric_id)
            .values(median_price=None)
        )
    with session_scope() as session:
        run = active_run(session, published.snapshot_id)
        gate = gate_publication_validity(session, run.id)
    assert gate.passed is False
    assert gate.violations[0].rule == "METRIC_DTO_COMPLETE"


def test_golden_failure_blocks_calculation_gate() -> None:
    gate = gate_calculation_validity({"passed": False, "failed": 2, "failures": []})
    assert gate.passed is False
    assert gate.violations[0].rule == "GOLDEN_DATASET"


def test_skipped_golden_does_not_pretend_to_pass() -> None:
    gate = gate_calculation_validity(None)
    assert gate.passed is True
    assert gate.details["status"] == "SKIPPED"


# --------------------------------------------------------------------------- #
# Статус публикации
# --------------------------------------------------------------------------- #


def test_low_coverage_degrades_then_fails(published) -> None:
    profile = methodology_profile("baseline_v1")
    with session_scope() as session:
        coverage = compute_coverage(session, published.snapshot_id)

    coverage.total.completed = int(coverage.total.planned * 0.90)
    status, notes = evaluate_publication(coverage=coverage, gates=[], profile=profile)
    assert status == PublicationStatus.DEGRADED.value
    assert any(note["code"] == "COVERAGE_BELOW_READY" for note in notes)

    coverage.total.completed = int(coverage.total.planned * 0.50)
    status, notes = evaluate_publication(coverage=coverage, gates=[], profile=profile)
    assert status == PublicationStatus.FAILED.value


def test_failed_snapshot_does_not_become_the_showcase(published) -> None:
    """Провалившийся снимок не становится витриной, но и не исчезает."""
    with session_scope() as session:
        session.execute(
            update(models.CollectionJob)
            .where(models.CollectionJob.snapshot_id == published.snapshot_id)
            .values(status=JobStatus.PLANNED.value)
        )
    with session_scope() as session:
        result = finalize_snapshot(session, published.snapshot_id)
    assert result.status == SnapshotStatus.FAILED.value

    with session_scope() as session:
        snapshot = session.get(models.MarketSnapshot, published.snapshot_id)
        assert snapshot.published_at is None
        assert snapshot.publication_notes


# --------------------------------------------------------------------------- #
# Методика
# --------------------------------------------------------------------------- #


def test_editing_registered_version_is_rejected(database: str) -> None:
    """Активную версию менять на месте запрещено: на неё ссылаются расчёты."""
    import os

    profile = methodology_profile("baseline_v1")
    with session_scope() as session:
        register_methodology(session, profile)
        session.add(
            models.CalculationRun(
                snapshot_id=None if False else _snapshot_for(session),
                methodology_version=profile.version,
                is_active=True,
                started_at=__import__("tmo.core.timeutil", fromlist=["now_utc"]).now_utc(),
                gate_results={},
            )
        )

    tampered = profile.model_copy(update={"title": "изменено на месте"})
    os.environ["TMO_ALLOW_METHODOLOGY_REREGISTER"] = "true"
    with session_scope() as session, pytest.raises(ValueError, match="изменена после регистрации"):
        register_methodology(session, tampered)


def _snapshot_for(session) -> int:
    snapshot = models.MarketSnapshot(
        snapshot_date=SNAPSHOT,
        attempt_no=1,
        status=SnapshotStatus.READY,
        horizon_days=3,
        created_at=__import__("tmo.core.timeutil", fromlist=["now_utc"]).now_utc(),
        quality_summary={},
        publication_notes=[],
    )
    session.add(snapshot)
    session.flush()
    return snapshot.id


def test_recalculation_creates_a_new_run_and_keeps_the_old(published) -> None:
    """Пересчёт не переписывает прежний расчёт: на него ссылались цифры."""
    with session_scope() as session:
        first = active_run(session, published.snapshot_id)
        first_id = first.id
        report = calculate_snapshot(session, published.snapshot_id, make_active=True)

    assert report.calculation_run_id != first_id
    with session_scope() as session:
        runs = list(
            session.scalars(
                select(models.CalculationRun).where(
                    models.CalculationRun.snapshot_id == published.snapshot_id
                )
            )
        )
        active = [run for run in runs if run.is_active]
    assert len(runs) >= 2
    assert len(active) == 1
    assert active[0].id == report.calculation_run_id
