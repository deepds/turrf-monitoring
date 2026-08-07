"""Capacity: полная матрица в пределах бюджета.

Проверяется не скорость источника (её измеряет ``scripts/bench_sources.py``), а
то, что **наша** часть — планирование, разбор, отбор и агрегация — не является
узким местом на полном суточном объёме.

Замер источников и живой прогон в CI не выполняются: тест, зависящий от чужого
сервиса, ломается не по нашей вине.
"""

from __future__ import annotations

import time
from datetime import date

import pytest

from tmo.planner.matrix import build_matrix, expected_size

pytestmark = pytest.mark.capacity

SNAPSHOT = date(2026, 8, 7)

#: Бюджеты нашей части. Взяты с запасом: цель — поймать деградацию на порядок,
#: а не мерить машину.
PLANNING_BUDGET_SECONDS = 20.0
DIGEST_BUDGET_SECONDS = 10.0


def test_full_matrix_is_planned_within_budget() -> None:
    started = time.perf_counter()
    matrix = build_matrix(SNAPSHOT)
    elapsed = time.perf_counter() - started

    assert len(matrix) == 15_840
    assert elapsed < PLANNING_BUDGET_SECONDS, f"планирование заняло {elapsed:.1f} с"


def test_plan_digest_is_computed_within_budget() -> None:
    matrix = build_matrix(SNAPSHOT)
    started = time.perf_counter()
    digest = matrix.digest
    elapsed = time.perf_counter() - started

    assert digest
    assert elapsed < DIGEST_BUDGET_SECONDS


def test_matrix_memory_footprint_is_reasonable() -> None:
    """План целиком держится в памяти воркера, не требуя стриминга."""
    import sys

    matrix = build_matrix(SNAPSHOT)
    approx_bytes = sum(
        sys.getsizeof(job.job_key) + sys.getsizeof(job.series_key) + sys.getsizeof(job.params)
        for job in matrix.jobs
    )
    assert approx_bytes < 64 * 1024 * 1024


def test_selection_handles_a_realistic_air_sample() -> None:
    """Живое авиа-наблюдение даёт до 1 365 тарифных строк на один запрос."""
    from decimal import Decimal

    from tmo.catalog.registry import methodology_profile
    from tmo.core.enums import CollectionFamily
    from tmo.engine.selection import Candidate, select

    profile = methodology_profile("baseline_v1")
    candidates = [
        Candidate(
            ref=index,
            source_code="tutu_mcp",
            price=Decimal(20000 + index),
            currency="RUB",
            equivalence_key=f"flight-{index // 4}",
            transport={
                "is_direct": True,
                "is_round_trip": True,
                "cabin": "ECONOMIC",
                "refundable": index % 4 == 3,
            },
        )
        for index in range(1400)
    ]

    started = time.perf_counter()
    result = select(
        candidates,
        family=CollectionFamily.AIR,
        rules=profile.selection_for(CollectionFamily.AIR),
        outlier_rules=profile.outliers,
    )
    elapsed = time.perf_counter() - started

    assert len(result.decisions) == 1400
    assert result.included
    assert elapsed < 2.0, f"отбор занял {elapsed:.2f} с на 1 400 строках"


def test_expected_matrix_matches_capacity_document() -> None:
    """Числа capacity-анализа обязаны совпадать с тем, что строит планировщик."""
    assert expected_size(30) == {
        "RAIL": 600,
        "AIR": 8700,
        "HOTEL": 6540,
        "TOTAL": 15840,
    }
