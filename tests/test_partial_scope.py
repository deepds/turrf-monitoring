"""Прогон по ограниченной области: поездки из одного города.

Нужен, чтобы проверять конвейер целиком, не занимая источник на одиннадцать
часов. Опасность у него ровно одна и неочевидная: **его собственное покрытие
может быть стопроцентным при том, что описана четверть рынка**. Ворота считают
покрытие от плана, а план у такого снимка свой — значит они его пропустят, и
остановить его можно только отдельной меткой.

Здесь проверяется и польза (матрица действительно сжимается), и защита (снимок
не попадает на витрину ни при каком статусе).
"""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import select

from tmo.core.enums import CollectionFamily, SnapshotStatus
from tmo.db import models
from tmo.db.session import session_scope
from tmo.planner.matrix import Scope, build_matrix, expected_size
from tmo.services.snapshot import create_snapshot, latest_published

SNAPSHOT = date(2026, 8, 8)
HORIZON = 30
MOSCOW = "MOW"


# --------------------------------------------------------------------------- #
# Область
# --------------------------------------------------------------------------- #


def test_scope_expands_in_two_directions() -> None:
    """Транспорт — из указанных городов, проживание — в городах назначения.

    В поездке Москва→Сочи гостиница нужна в Сочи. Одним списком городов это не
    описывается: для транспорта Москва единственная разрешённая, для проживания
    единственная запрещённая.
    """
    from tmo.catalog.registry import city_registry

    scope = Scope.of(city_registry(), (MOSCOW,))

    assert scope.allows_route(MOSCOW)
    assert not scope.allows_stay(MOSCOW)
    for other in ("LED", "AER", "KUF", "KZN"):
        assert not scope.allows_route(other)
        assert scope.allows_stay(other)


def test_two_origins_keep_every_city_as_a_destination() -> None:
    """При двух городах отправления ночуют во всех: из каждого едут в другой."""
    from tmo.catalog.registry import city_registry

    scope = Scope.of(city_registry(), (MOSCOW, "LED"))
    assert scope.allows_stay(MOSCOW)
    assert scope.allows_stay("LED")


def test_unknown_origin_is_refused_loudly() -> None:
    """Опечатка в коде города обязана останавливать, а не сужать матрицу молча."""
    from tmo.catalog.registry import city_registry

    with pytest.raises(ValueError, match="справочник"):
        Scope.of(city_registry(), ("МОСКВА",))


def test_restricted_matrix_matches_the_arithmetic() -> None:
    """7 092 наблюдения против 15 840: 120 ЖД, 1 740 авиа, 5 232 проживания."""
    restricted = build_matrix(SNAPSHOT, horizon_days=HORIZON, origins=(MOSCOW,))
    counts = restricted.counts_by_family()

    assert counts == {"RAIL": 120, "AIR": 1740, "HOTEL": 5232}
    assert len(restricted) == 7092
    assert len(restricted) == expected_size(HORIZON, origin_count=1)["TOTAL"]


def test_full_matrix_is_untouched_by_the_feature() -> None:
    """Без ограничения матрица прежняя — 15 840 наблюдений."""
    full = build_matrix(SNAPSHOT, horizon_days=HORIZON)
    assert len(full) == expected_size(HORIZON)["TOTAL"] == 15840


def test_restricted_matrix_is_a_subset_of_the_full_one() -> None:
    """Ограничение отбирает наблюдения, а не порождает другие.

    Иначе ряды ограниченного прогона нельзя было бы сравнивать с боевыми: ключ
    наблюдения обязан совпадать до байта.
    """
    full = {job.job_key for job in build_matrix(SNAPSHOT, horizon_days=HORIZON).jobs}
    restricted = {
        job.job_key
        for job in build_matrix(SNAPSHOT, horizon_days=HORIZON, origins=(MOSCOW,)).jobs
    }
    assert restricted < full


def test_restricted_plan_is_deterministic() -> None:
    """Одинаковый вход даёт одинаковый отпечаток, разная область — разный."""
    first = build_matrix(SNAPSHOT, horizon_days=HORIZON, origins=(MOSCOW,))
    second = build_matrix(SNAPSHOT, horizon_days=HORIZON, origins=(MOSCOW,))
    full = build_matrix(SNAPSHOT, horizon_days=HORIZON)

    assert first.digest == second.digest
    assert first.digest != full.digest


# --------------------------------------------------------------------------- #
# Защита витрины
# --------------------------------------------------------------------------- #


def test_restricted_snapshot_records_its_scope(database: str) -> None:
    with session_scope() as session:
        creation = create_snapshot(
            session, snapshot_date=SNAPSHOT, horizon_days=HORIZON, origins=(MOSCOW,)
        )
        snapshot = session.get(models.MarketSnapshot, creation.snapshot_id)

        assert snapshot.is_partial_scope is True
        assert snapshot.scope == {
            "origins": [MOSCOW],
            "stay_cities": ["AER", "KUF", "KZN", "LED"],
        }
        assert creation.planned == 7092


def test_full_snapshot_is_not_marked_partial(database: str) -> None:
    with session_scope() as session:
        creation = create_snapshot(session, snapshot_date=SNAPSHOT, horizon_days=HORIZON)
        snapshot = session.get(models.MarketSnapshot, creation.snapshot_id)

        assert snapshot.is_partial_scope is False
        assert snapshot.scope == {}


@pytest.mark.parametrize("status", [SnapshotStatus.READY, SnapshotStatus.DEGRADED])
def test_restricted_snapshot_never_reaches_the_showcase(database: str, status) -> None:
    """Даже пройдя ворота, ограниченный снимок не становится витриной.

    Это главная защита всей затеи: покрытие у него своё, стопроцентное, и ворота
    возражать не станут. Выдать четверть рынка за рынок он не должен.
    """
    with session_scope() as session:
        creation = create_snapshot(
            session, snapshot_date=SNAPSHOT, horizon_days=HORIZON, origins=(MOSCOW,)
        )
        snapshot = session.get(models.MarketSnapshot, creation.snapshot_id)
        snapshot.status = status
        snapshot.coverage_total = 1.0

    with session_scope() as session:
        assert latest_published(session) is None


def test_full_snapshot_still_reaches_the_showcase(database: str) -> None:
    """Обратная проверка: защита не должна отсекать боевой снимок."""
    with session_scope() as session:
        creation = create_snapshot(session, snapshot_date=SNAPSHOT, horizon_days=HORIZON)
        snapshot = session.get(models.MarketSnapshot, creation.snapshot_id)
        snapshot.status = SnapshotStatus.READY

    with session_scope() as session:
        found = latest_published(session)
        assert found is not None
        assert found.id == creation.snapshot_id


def test_restricted_run_does_not_shadow_a_published_full_run(database: str) -> None:
    """Нагрузочный прогон рядом с боевым не должен подменять его на витрине."""
    with session_scope() as session:
        full = create_snapshot(session, snapshot_date=SNAPSHOT, horizon_days=HORIZON)
        session.get(models.MarketSnapshot, full.snapshot_id).status = SnapshotStatus.READY

    with session_scope() as session:
        restricted = create_snapshot(
            session, snapshot_date=SNAPSHOT, horizon_days=HORIZON, origins=(MOSCOW,)
        )
        node = session.get(models.MarketSnapshot, restricted.snapshot_id)
        node.status = SnapshotStatus.READY
        # Попытка позже — по дате и attempt_no он выиграл бы отбор.
        assert node.attempt_no > 1

    with session_scope() as session:
        assert latest_published(session).id == full.snapshot_id


# --------------------------------------------------------------------------- #
# Сквозной прогон
# --------------------------------------------------------------------------- #


def test_pipeline_runs_end_to_end_on_a_restricted_matrix(database: str) -> None:
    """Конвейер проходит от плана до ворот и на ограниченной области.

    Горизонт здесь маленький: проверяется механика, а не нагрузка.
    """
    from tmo.connectors.registry import close_all
    from tmo.services.pipeline import run_daily_pipeline

    close_all()
    report = run_daily_pipeline(
        snapshot_date=SNAPSHOT,
        horizon_days=4,
        replay_mode="SIMULATED",
        is_synthetic=True,
        recovery_rounds=0,
        batch_size=200,
        soft_budget_seconds=300,
        origins=(MOSCOW,),
    )

    assert report.planned > 0
    assert report.metrics == report.planned
    assert report.coverage_total == pytest.approx(1.0)

    with session_scope() as session:
        families = set(
            session.scalars(
                select(models.CollectionJob.family).where(
                    models.CollectionJob.snapshot_id == report.snapshot_id
                )
            )
        )
        origins = set(
            session.scalars(
                select(models.CollectionJob.origin_code).where(
                    models.CollectionJob.snapshot_id == report.snapshot_id,
                    models.CollectionJob.origin_code.is_not(None),
                )
            )
        )
        stay = set(
            session.scalars(
                select(models.CollectionJob.city_code).where(
                    models.CollectionJob.snapshot_id == report.snapshot_id,
                    models.CollectionJob.city_code.is_not(None),
                )
            )
        )

    assert families == {family.value for family in CollectionFamily}
    assert origins == {MOSCOW}
    assert MOSCOW not in stay
