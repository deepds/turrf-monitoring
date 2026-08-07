"""Идемпотентность исполнения.

Две ловушки первой версии, обе молчаливые:

1. Область отбора не входила в ключ — досбор возвращал результат планового
   прогона как `SKIPPED_IDEMPOTENT` вместо новой попытки.
2. Принудительный прогон переиспользовал задачу вместе со ссылкой на прежнюю
   пачку, и текущая висела на `0/N` навсегда.
"""

from __future__ import annotations

from datetime import date

from sqlalchemy import func, select

from tmo.core.ids import idempotency_key
from tmo.db import models
from tmo.db.session import session_scope
from tmo.services.pipeline import collect_jobs
from tmo.services.snapshot import create_snapshot

SNAPSHOT = date(2026, 8, 7)


def _make_snapshot() -> int:
    with session_scope() as session:
        creation = create_snapshot(session, snapshot_date=SNAPSHOT, horizon_days=2)
        return creation.snapshot_id


def _rail_jobs(snapshot_id: int, limit: int = 4) -> list[int]:
    with session_scope() as session:
        return list(
            session.scalars(
                select(models.CollectionJob.id)
                .where(
                    models.CollectionJob.snapshot_id == snapshot_id,
                    models.CollectionJob.family == "RAIL",
                )
                .order_by(models.CollectionJob.id)
                .limit(limit)
            )
        )


def _attempts(job_ids: list[int]) -> int:
    with session_scope() as session:
        return int(
            session.scalar(
                select(func.count(models.SourceAttempt.id)).where(
                    models.SourceAttempt.collection_job_id.in_(job_ids)
                )
            )
            or 0
        )


def test_key_depends_on_execution_scope() -> None:
    """Плановый сбор и досбор одного наблюдения — разные исполнения."""
    common = {
        "snapshot_date": SNAPSHOT,
        "family": "RAIL",
        "job_key_value": "RAIL:abc",
        "source_code": "rzd",
    }
    primary = idempotency_key(**common, execution_scope="PRIMARY")
    recovery = idempotency_key(**common, execution_scope="RECOVERY")
    assert primary != recovery


def test_forced_retry_is_a_new_execution() -> None:
    """Соль делает принудительный повтор действительно новым."""
    common = {
        "snapshot_date": SNAPSHOT,
        "family": "RAIL",
        "job_key_value": "RAIL:abc",
        "source_code": "rzd",
        "execution_scope": "RECOVERY",
    }
    assert idempotency_key(**common, attempt_salt="recovery-1") != idempotency_key(
        **common, attempt_salt="recovery-2"
    )


def test_same_scope_twice_does_not_duplicate_attempts(database: str) -> None:
    """Повтор той же области не создаёт вторую запись об обращении."""
    from tmo.connectors.registry import close_all

    close_all()
    snapshot_id = _make_snapshot()
    jobs = _rail_jobs(snapshot_id)

    collect_jobs(jobs, execution_scope="PRIMARY", replay_mode="SIMULATED")
    first = _attempts(jobs)
    collect_jobs(jobs, execution_scope="PRIMARY", replay_mode="SIMULATED")
    second = _attempts(jobs)

    assert first > 0
    assert second == first


def test_recovery_scope_creates_new_attempts(database: str) -> None:
    """Досбор обязан выполниться, а не вернуть прежний результат."""
    from tmo.connectors.registry import close_all

    close_all()
    snapshot_id = _make_snapshot()
    jobs = _rail_jobs(snapshot_id)

    collect_jobs(jobs, execution_scope="PRIMARY", replay_mode="SIMULATED")
    baseline = _attempts(jobs)
    collect_jobs(
        jobs, execution_scope="RECOVERY", attempt_salt="recovery-1", replay_mode="SIMULATED"
    )
    after = _attempts(jobs)

    assert after > baseline


def test_repeated_forced_retries_keep_adding_attempts(database: str) -> None:
    """Инструкция прямо советует запускать досбор: он не должен быть no-op."""
    from tmo.connectors.registry import close_all

    close_all()
    snapshot_id = _make_snapshot()
    jobs = _rail_jobs(snapshot_id, limit=2)

    counts = []
    for attempt in range(1, 4):
        collect_jobs(
            jobs,
            execution_scope="RECOVERY",
            attempt_salt=f"recovery-{attempt}",
            replay_mode="SIMULATED",
        )
        counts.append(_attempts(jobs))

    assert counts == sorted(counts)
    assert counts[-1] > counts[0]


def test_job_state_is_derived_from_all_attempts(database: str) -> None:
    """После досбора итог определяется совокупностью попыток, а не последней."""
    from tmo.connectors.registry import close_all

    close_all()
    snapshot_id = _make_snapshot()
    jobs = _rail_jobs(snapshot_id, limit=2)
    collect_jobs(jobs, execution_scope="PRIMARY", replay_mode="SIMULATED")
    collect_jobs(
        jobs, execution_scope="RECOVERY", attempt_salt="recovery-1", replay_mode="SIMULATED"
    )

    with session_scope() as session:
        rows = list(
            session.scalars(
                select(models.CollectionJob).where(models.CollectionJob.id.in_(jobs))
            )
        )
    assert all(row.status in ("SUCCESS", "PARTIAL", "NO_MARKET") for row in rows)
    assert all(row.retry_count >= 1 for row in rows)
