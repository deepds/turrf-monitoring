"""Сквозной прогон конвейера на воспроизведении.

Проверяет то, что нельзя проверить по частям: план строится, наблюдения
собираются, расчёт применяется, ворота срабатывают, снимок публикуется — и
провенанс от опубликованной цифры доходит до исходного ответа источника.
"""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import func, select

from tmo.core.enums import CollectionFamily, JobStatus, MetricType, SnapshotStatus
from tmo.db import models
from tmo.db.session import session_scope
from tmo.services.pipeline import run_daily_pipeline
from tmo.services.showcase import metric_details, metric_offers, resolve_context, trips

SNAPSHOT = date(2026, 8, 7)
HORIZON = 4


@pytest.fixture()
def pipeline(database: str):
    """Полный цикл на маленьком горизонте: механика та же, объём меньше."""
    from tmo.connectors.registry import close_all

    close_all()
    return run_daily_pipeline(
        snapshot_date=SNAPSHOT,
        horizon_days=HORIZON,
        replay_mode="SIMULATED",
        is_synthetic=True,
        recovery_rounds=1,
        batch_size=200,
        soft_budget_seconds=300,
    )


def test_pipeline_publishes_a_snapshot(pipeline) -> None:
    assert pipeline.status in ("READY", "DEGRADED")
    assert pipeline.planned > 0
    assert pipeline.metrics == pipeline.planned
    assert pipeline.coverage_total == pytest.approx(1.0)


def test_every_planned_observation_has_a_terminal_outcome(pipeline) -> None:
    with session_scope() as session:
        statuses = dict(
            session.execute(
                select(models.CollectionJob.status, func.count(models.CollectionJob.id))
                .where(models.CollectionJob.snapshot_id == pipeline.snapshot_id)
                .group_by(models.CollectionJob.status)
            ).all()
        )
    assert JobStatus.PLANNED.value not in statuses
    assert JobStatus.RUNNING.value not in statuses
    assert JobStatus.DISPATCHED.value not in statuses


def test_no_silent_source_skip(pipeline) -> None:
    """У каждого наблюдения есть хотя бы одна запись об обращении."""
    with session_scope() as session:
        orphans = session.scalar(
            select(func.count(models.CollectionJob.id)).where(
                models.CollectionJob.snapshot_id == pipeline.snapshot_id,
                ~models.CollectionJob.id.in_(
                    select(models.SourceAttempt.collection_job_id).where(
                        models.SourceAttempt.snapshot_id == pipeline.snapshot_id
                    )
                ),
            )
        )
    assert orphans == 0


def test_rail_is_observed_by_two_sources(pipeline) -> None:
    """Оба источника ЖД опрашиваются: иначе сверка невозможна."""
    with session_scope() as session:
        sources = set(
            session.scalars(
                select(models.SourceAttempt.source_code)
                .join(
                    models.CollectionJob,
                    models.CollectionJob.id == models.SourceAttempt.collection_job_id,
                )
                .where(
                    models.SourceAttempt.snapshot_id == pipeline.snapshot_id,
                    models.CollectionJob.family == CollectionFamily.RAIL.value,
                )
            )
        )
    assert sources == {"tutu_mcp", "rzd"}


def test_no_market_is_recorded_as_observation_not_failure(pipeline) -> None:
    """Самара — Казань: прямого сообщения нет, и это ответ о рынке."""
    with session_scope() as session:
        jobs = list(
            session.scalars(
                select(models.CollectionJob).where(
                    models.CollectionJob.snapshot_id == pipeline.snapshot_id,
                    models.CollectionJob.family == CollectionFamily.RAIL.value,
                    models.CollectionJob.origin_code == "KUF",
                    models.CollectionJob.destination_code == "KZN",
                )
            )
        )
    assert jobs
    assert all(job.status == JobStatus.NO_MARKET.value for job in jobs)
    assert all(job.no_market_reason == "NO_DIRECT_SERVICE" for job in jobs)


def test_metric_provenance_reaches_the_raw_response(pipeline) -> None:
    """Опубликованная цифра раскрывается до файла исходного ответа."""
    with session_scope() as session:
        metric = session.scalars(
            select(models.CalculatedMetric).where(
                models.CalculatedMetric.snapshot_id == pipeline.snapshot_id,
                models.CalculatedMetric.metric_type == MetricType.RAIL_LEG.value,
                models.CalculatedMetric.offers_count > 0,
            ).limit(1)
        ).first()
        assert metric is not None
        details = metric_details(session, metric.id)
        offers = metric_offers(session, metric.id)

    assert details["methodology_version"] == "baseline_v1"
    assert details["median_price"] is not None
    assert details["fetched_at"] is not None
    assert offers
    included = [row for row in offers if row["is_included"]]
    assert included
    assert all(row["provenance"]["raw_storage_ref"] for row in included)
    assert all(row["provenance"]["raw_sha256"] for row in included)


def test_every_excluded_offer_has_a_reason(pipeline) -> None:
    with session_scope() as session:
        missing = session.scalar(
            select(func.count(models.MetricOfferLink.id))
            .join(
                models.CalculatedMetric,
                models.CalculatedMetric.id == models.MetricOfferLink.metric_id,
            )
            .where(
                models.CalculatedMetric.snapshot_id == pipeline.snapshot_id,
                models.MetricOfferLink.is_included.is_(False),
                models.MetricOfferLink.exclusion_reason.is_(None),
            )
        )
    assert missing == 0


def test_trip_cost_is_a_sum_of_observed_parts(pipeline) -> None:
    """Стоимость поездки складывается из отдельно наблюдавшихся составляющих."""
    with session_scope() as session:
        context = resolve_context(session)
        row = session.scalars(
            select(models.TripCostRow).where(
                models.TripCostRow.calculation_run_id == context.run.id,
                models.TripCostRow.is_complete.is_(True),
            ).limit(1)
        ).first()
        assert row is not None
        assert row.total_median == row.transport_median + row.accommodation_median
        assert row.transport_metric_ids
        assert row.accommodation_metric_id

        rows = trips(
            session,
            context,
            origin=row.origin_code,
            departure_date=row.departure_date,
            return_date=row.return_date,
            transport_mode=row.transport_mode,
            stars=row.stars,
        )
    assert rows
    assert all("transport_composition" in item for item in rows)


def test_rail_trip_says_it_is_a_sum_of_two_legs(pipeline) -> None:
    with session_scope() as session:
        context = resolve_context(session)
        row = session.scalars(
            select(models.TripCostRow).where(
                models.TripCostRow.calculation_run_id == context.run.id,
                models.TripCostRow.transport_mode == "RAIL",
                models.TripCostRow.is_complete.is_(True),
            ).limit(1)
        ).first()
        assert row is not None
        assert len(row.transport_metric_ids) == 2
        rows = trips(
            session,
            context,
            origin=row.origin_code,
            departure_date=row.departure_date,
            return_date=row.return_date,
            transport_mode="RAIL",
            stars=row.stars,
        )
    assert any("сумма двух" in item["transport_composition"].lower() for item in rows)


def test_synthetic_snapshot_is_marked_and_not_published_as_market(pipeline) -> None:
    """Воспроизведённый снимок витриной рынка не является."""
    from tmo.services.snapshot import latest_published

    with session_scope() as session:
        snapshot = session.get(models.MarketSnapshot, pipeline.snapshot_id)
        assert snapshot.is_synthetic is True
        assert SnapshotStatus(snapshot.status).is_published
        assert latest_published(session) is None
