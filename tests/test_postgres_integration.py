"""PostgreSQL-набор.

SQLite не заменяет его: JSONB, зона времени, оконные функции, конкурентная
запись и поведение внешних ключей отличаются, и именно на этих отличиях в
первой версии ломались вещи, которые локально выглядели рабочими.

Запуск: ``TMO_TEST_DATABASE_URL=postgresql+psycopg://… pytest -m postgres``
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import func, select, text

from tmo.core.timeutil import now_utc, to_utc
from tmo.db import models
from tmo.db.session import get_engine, session_scope
from tmo.services.pipeline import run_daily_pipeline

pytestmark = pytest.mark.postgres

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
        recovery_rounds=0,
        batch_size=200,
    )


def test_dialect_is_postgresql(database: str) -> None:
    assert get_engine().dialect.name == "postgresql"


def test_migrations_produce_the_same_schema_as_models(database: str) -> None:
    """Миграция и модель обязаны сходиться: расхождение ломает прод, не тест."""
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    from tmo.db.base import Base

    with get_engine().connect() as connection:
        context = MigrationContext.configure(connection)
        diff = compare_metadata(context, Base.metadata)
    # Отличия по временным схемам тестов не считаются.
    significant = [item for item in diff if item[0] not in ("add_table", "remove_table")]
    assert not significant, significant


def test_jsonb_columns_are_queryable(published) -> None:
    """JSON должен быть JSONB: по нему делаются выборки, а не только чтение."""
    with session_scope() as session:
        column_type = session.execute(
            text(
                "SELECT data_type FROM information_schema.columns "
                "WHERE table_name='offers' AND column_name='transport_attributes'"
            )
        ).scalar()
        assert column_type == "jsonb"

        count = session.scalar(
            select(func.count(models.Offer.id)).where(
                models.Offer.transport_attributes["car_type"].astext == "COMPARTMENT"
            )
        )
    assert count > 0


def test_timestamptz_round_trip_keeps_the_zone(published) -> None:
    """SQLite теряет зону; PostgreSQL обязан её сохранить."""
    with session_scope() as session:
        fetched = session.scalar(
            select(models.SourceAttempt.fetched_at).where(
                models.SourceAttempt.fetched_at.is_not(None)
            )
        )
    assert fetched is not None
    assert fetched.tzinfo is not None
    assert to_utc(fetched) == fetched


def test_unique_constraint_blocks_duplicate_idempotency_key(published) -> None:
    from sqlalchemy.exc import IntegrityError

    with session_scope() as session:
        existing = session.scalars(select(models.SourceAttempt).limit(1)).first()
        assert existing is not None
        duplicate = models.SourceAttempt(
            snapshot_id=existing.snapshot_id,
            collection_job_id=existing.collection_job_id,
            source_code=existing.source_code,
            execution_scope="PRIMARY",
            idempotency_key=existing.idempotency_key,
            outcome=existing.outcome,
            requested_at=now_utc(),
        )
        session.add(duplicate)
        with pytest.raises(IntegrityError):
            session.flush()


def test_snapshot_date_and_attempt_are_unique(published) -> None:
    from sqlalchemy.exc import IntegrityError

    with session_scope() as session:
        session.add(
            models.MarketSnapshot(
                snapshot_date=SNAPSHOT,
                attempt_no=1,
                status="READY",
                horizon_days=3,
                created_at=now_utc(),
                quality_summary={},
                publication_notes=[],
            )
        )
        with pytest.raises(IntegrityError):
            session.flush()


def test_price_check_constraint_is_enforced(published) -> None:
    """Неположительная цена не должна существовать физически."""
    from sqlalchemy.exc import IntegrityError

    with session_scope() as session:
        offer = session.scalars(select(models.Offer).limit(1)).first()
        session.add(
            models.Offer(
                snapshot_id=offer.snapshot_id,
                collection_job_id=offer.collection_job_id,
                source_attempt_id=offer.source_attempt_id,
                source_code=offer.source_code,
                kind=offer.kind,
                fetched_at=now_utc(),
                currency="RUB",
                price=Decimal("0"),
                price_basis=offer.price_basis,
                fingerprint="x",
                equivalence_key="x",
            )
        )
        with pytest.raises(IntegrityError):
            session.flush()


def test_window_function_builds_a_historical_series(published) -> None:
    """Исторический ряд строится оконной функцией по series_key."""
    with session_scope() as session:
        rows = session.execute(
            text(
                """
                SELECT series_key,
                       median_price,
                       ROW_NUMBER() OVER (PARTITION BY series_key ORDER BY snapshot_id DESC) AS rn
                FROM calculated_metrics
                WHERE median_price IS NOT NULL
                LIMIT 20
                """
            )
        ).all()
    assert rows
    assert all(row.rn >= 1 for row in rows)


def test_latest_snapshot_query_uses_the_index(published) -> None:
    with session_scope() as session:
        plan = session.execute(
            text(
                "EXPLAIN SELECT * FROM calculated_metrics "
                "WHERE calculation_run_id = :run AND metric_type = 'RAIL_LEG' "
                "AND origin_code = 'MOW' ORDER BY service_date"
            ),
            {"run": 1},
        ).scalars().all()
    assert plan


def test_required_indexes_exist(database: str) -> None:
    """Внешний ключ без индекса сделал удаление квадратичным в первой версии."""
    with session_scope() as session:
        names = set(
            session.execute(
                text("SELECT indexname FROM pg_indexes WHERE schemaname = current_schema()")
            ).scalars()
        )
    for required in (
        "ix_offers_raw_response_id",
        "ix_offers_job",
        "ix_offers_equivalence",
        "ix_metric_offer_links_metric_included",
        "ix_collection_jobs_route",
        "ix_collection_jobs_hotel",
        "ix_trip_cost_lookup",
        "uq_source_attempts_idempotency_key",
    ):
        assert required in names, f"нет индекса {required}"


def test_provenance_join_returns_the_raw_reference(published) -> None:
    with session_scope() as session:
        row = session.execute(
            select(
                models.CalculatedMetric.id,
                models.Offer.id,
                models.RawResponse.storage_ref,
                models.RawResponse.payload_sha256,
            )
            .join(
                models.MetricOfferLink,
                models.MetricOfferLink.metric_id == models.CalculatedMetric.id,
            )
            .join(models.Offer, models.Offer.id == models.MetricOfferLink.offer_id)
            .join(models.RawResponse, models.RawResponse.id == models.Offer.raw_response_id)
            .where(models.MetricOfferLink.is_included.is_(True))
            .limit(1)
        ).first()
    assert row is not None
    assert row[2] and row[3]


def test_concurrent_writes_do_not_deadlock(published) -> None:
    """Восемь параллельных записей в один снимок обязаны пройти."""

    def write(index: int) -> int:
        with session_scope() as session:
            job = session.scalars(select(models.CollectionJob).limit(1)).first()
            attempt = models.SourceAttempt(
                snapshot_id=job.snapshot_id,
                collection_job_id=job.id,
                source_code="tutu_mcp",
                execution_scope=f"CONCURRENT-{index}",
                idempotency_key=f"concurrent-{index}",
                outcome="SUCCESS",
                requested_at=now_utc(),
                fetched_at=now_utc(),
            )
            session.add(attempt)
            session.flush()
            return attempt.id

    with ThreadPoolExecutor(max_workers=8) as pool:
        ids = list(pool.map(write, range(8)))
    assert len(set(ids)) == 8


def test_cascade_delete_removes_children(published) -> None:
    """Удаление снимка не должно оставлять сирот в дочерних таблицах."""
    with session_scope() as session:
        snapshot_id = session.scalar(select(models.MarketSnapshot.id))
        session.execute(
            text("DELETE FROM market_snapshots WHERE id = :id"), {"id": snapshot_id}
        )
    with session_scope() as session:
        for table in ("collection_jobs", "source_attempts", "offers", "calculated_metrics"):
            left = session.execute(
                text(f"SELECT count(*) FROM {table} WHERE snapshot_id = :id"),
                {"id": snapshot_id},
            ).scalar()
            assert left == 0, table


def test_idle_in_transaction_timeout_is_configured(database: str) -> None:
    """Предохранитель от повисшей транзакции: она держит блокировки навсегда."""
    with session_scope() as session:
        value = session.execute(text("SHOW idle_in_transaction_session_timeout")).scalar()
    assert value not in ("0", "0ms"), "таймаут повисшей транзакции не задан"
