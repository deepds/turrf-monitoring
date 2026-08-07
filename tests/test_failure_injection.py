"""Инъекция отказов.

Проверяет не то, что система работает, а то, что она **правильно ломается**:
отказ обязан оставить конечную запись, частичный результат обязан сохраниться,
а разомкнутая цепь обязана отличаться от пустого рынка.
"""

from __future__ import annotations

import time
from datetime import date

import httpx
import pytest

from tmo.catalog.registry import source_registry
from tmo.connectors.base import BaseConnector
from tmo.connectors.contracts import ConnectorResult, RailQuery
from tmo.connectors.transport import (
    CircuitBreaker,
    RateLimiter,
    SourceTransport,
    TimeBudget,
)
from tmo.core.enums import AttemptOutcome
from tmo.core.errors import (
    BudgetExhausted,
    CircuitOpenError,
    ConnectorRateLimited,
    ConnectorSchemaError,
    ConnectorTimeout,
    ConnectorTransportError,
    HostNotAllowed,
)

QUERY = RailQuery(
    origin_code="MOW", origin_name="Москва", origin_rzd_code="2000000",
    destination_code="AER", destination_name="Сочи", destination_rzd_code="2064130",
    service_date=date(2026, 8, 21),
)


class ExplodingConnector(BaseConnector):
    """Коннектор, который падает заданной ошибкой."""

    code = "exploding"
    uses_network = False

    def __init__(self, source, error: Exception) -> None:
        super().__init__(source)
        self._error = error

    def transport(self):
        raise AssertionError("сеть не используется")

    def collect_rail(self, query, budget):
        raise self._error

    def collect_air(self, query, budget):
        raise self._error

    def collect_hotel(self, query, budget):
        raise self._error


@pytest.fixture()
def source():
    return source_registry().get("tutu_mcp")


@pytest.mark.parametrize(
    ("error", "outcome"),
    [
        (ConnectorTimeout("таймаут"), AttemptOutcome.TIMEOUT),
        (ConnectorRateLimited("темп"), AttemptOutcome.RATE_LIMITED),
        (ConnectorSchemaError("схема"), AttemptOutcome.SCHEMA_ERROR),
        (ConnectorTransportError("транспорт"), AttemptOutcome.TRANSPORT_ERROR),
        (CircuitOpenError("цепь"), AttemptOutcome.CIRCUIT_OPEN),
        (BudgetExhausted("бюджет"), AttemptOutcome.BUDGET_EXHAUSTED),
        (HostNotAllowed("хост"), AttemptOutcome.FAILED),
    ],
)
def test_every_failure_becomes_a_terminal_record(source, error, outcome) -> None:
    """Исключение не выпускается наружу: оно становится исходом попытки."""
    result = ExplodingConnector(source, error).collect(QUERY, TimeBudget(total_seconds=10))
    assert isinstance(result, ConnectorResult)
    assert result.outcome is outcome
    assert result.error_code
    assert result.fetched_at is not None


def test_unexpected_error_is_also_recorded(source) -> None:
    """Незапланированный отказ тоже обязан оставить запись, а не исчезнуть."""
    result = ExplodingConnector(source, ZeroDivisionError("нежданно")).collect(
        QUERY, TimeBudget(total_seconds=10)
    )
    assert result.outcome is AttemptOutcome.FAILED
    assert result.error_code == "ZeroDivisionError"


def test_circuit_open_is_distinct_from_no_market(source) -> None:
    """Разомкнутая цепь — наше решение перестать спрашивать, а не пустой рынок."""
    result = ExplodingConnector(source, CircuitOpenError("цепь")).collect(
        QUERY, TimeBudget(total_seconds=10)
    )
    assert result.outcome is AttemptOutcome.CIRCUIT_OPEN
    assert result.outcome is not AttemptOutcome.NO_MARKET
    assert result.no_market_reason is None


# --------------------------------------------------------------------------- #
# Размыкатель цепи
# --------------------------------------------------------------------------- #


def test_circuit_opens_after_threshold_and_closes_after_reset() -> None:
    breaker = CircuitBreaker(failure_threshold=3, reset_seconds=1)
    for _ in range(2):
        breaker.record_failure()
    assert breaker.is_open is False
    breaker.record_failure()
    assert breaker.is_open is True
    with pytest.raises(CircuitOpenError):
        breaker.check("tutu_mcp")
    time.sleep(1.05)
    assert breaker.is_open is False


def test_success_resets_the_failure_counter() -> None:
    breaker = CircuitBreaker(failure_threshold=3, reset_seconds=60)
    breaker.record_failure()
    breaker.record_failure()
    breaker.record_success()
    breaker.record_failure()
    assert breaker.is_open is False


# --------------------------------------------------------------------------- #
# Бюджет времени
# --------------------------------------------------------------------------- #


def test_budget_reports_exhaustion_without_killing_collected_data() -> None:
    # Бюджет задаётся отсчётом от прошлого, а не ожиданием: разрешение
    # таймера на Windows около 16 мс, и тест на пятидесяти миллисекундах
    # мигает.
    budget = TimeBudget(total_seconds=1.0, started_at=time.monotonic() - 5.0)
    assert budget.exhausted is True
    assert budget.remaining == 0.0
    assert budget.can_afford(0.1) is False


def test_child_budget_never_exceeds_parent() -> None:
    parent = TimeBudget(total_seconds=10)
    assert parent.child(60).total_seconds <= parent.remaining


def test_rate_limiter_refuses_to_wait_beyond_budget() -> None:
    """Ожидание темпа не должно съедать бюджет молча."""
    limiter = RateLimiter(per_minute=1)
    limiter.acquire()
    with pytest.raises(BudgetExhausted):
        limiter.acquire(budget=TimeBudget(total_seconds=0.5))


def test_rate_limiter_allows_within_window() -> None:
    limiter = RateLimiter(per_minute=100)
    for _ in range(5):
        assert limiter.acquire() == 0.0


# --------------------------------------------------------------------------- #
# Транспорт
# --------------------------------------------------------------------------- #


def build_transport(handler, **kwargs) -> SourceTransport:
    transport = SourceTransport(
        source_code="test",
        allowed_hosts=("example.test",),
        rate_limiter=RateLimiter(per_minute=0),
        circuit=CircuitBreaker(failure_threshold=kwargs.pop("threshold", 100), reset_seconds=60),
        max_retries=kwargs.pop("max_retries", 1),
        backoff_base=0.01,
        backoff_max=0.02,
    )
    transport._client = httpx.Client(transport=httpx.MockTransport(handler))
    return transport


def test_host_outside_allowlist_is_refused() -> None:
    transport = build_transport(lambda request: httpx.Response(200))
    with pytest.raises(HostNotAllowed):
        transport.get("https://evil.test/data")


def test_server_error_is_retried_then_reported() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(503)

    transport = build_transport(handler, max_retries=2)
    with pytest.raises(ConnectorTransportError):
        transport.get("https://example.test/data")
    assert calls["n"] == 3


def test_rate_limit_response_is_classified() -> None:
    transport = build_transport(lambda request: httpx.Response(429), max_retries=0)
    with pytest.raises(ConnectorRateLimited):
        transport.get("https://example.test/data")


def test_timeout_is_classified() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("слишком долго", request=request)

    transport = build_transport(handler, max_retries=0)
    with pytest.raises(ConnectorTimeout):
        transport.get("https://example.test/data")


def test_repeated_failures_open_the_circuit() -> None:
    transport = build_transport(
        lambda request: httpx.Response(500), max_retries=0, threshold=2
    )
    for _ in range(2):
        with pytest.raises(ConnectorTransportError):
            transport.get("https://example.test/data")
    with pytest.raises(CircuitOpenError):
        transport.get("https://example.test/data")


def test_successful_response_passes_through() -> None:
    transport = build_transport(lambda request: httpx.Response(200, json={"ok": True}))
    assert transport.get("https://example.test/data").json() == {"ok": True}


# --------------------------------------------------------------------------- #
# Обрыв пагинации
# --------------------------------------------------------------------------- #


def test_partial_pagination_is_marked_not_discarded() -> None:
    """Обрыв обхода сохраняет собранное и помечает выборку неполной."""
    from tmo.connectors.tutu import TutuConnector, _PageResult

    page = _PageResult(
        items=[{"a": 1}],
        page_of_item=[1],
        artifacts=[],
        meta={"has_more": True, "total_matched": 300},
        is_partial=True,
        partial_reason="SOURCE_PAGE_CAP",
        pages_read=10,
    )
    assert page.is_partial is True
    assert page.items, "собранное на прерванных страницах обязано сохраниться"
    assert TutuConnector._outcome([object()], True) is AttemptOutcome.PARTIAL
    assert TutuConnector._outcome([object()], False) is AttemptOutcome.SUCCESS
    assert TutuConnector._outcome([], False) is AttemptOutcome.NO_MARKET


# --------------------------------------------------------------------------- #
# Размыкатель: первопричина и прожигание пачки (ISSUE-1)
# --------------------------------------------------------------------------- #


def test_circuit_remembers_what_started_the_series() -> None:
    """Первопричина обязана пережить открытие цепи.

    Без неё в базе остаются только CIRCUIT_OPEN, и расследовать нечего:
    система знает, что перестала спрашивать, и не знает почему.
    """
    breaker = CircuitBreaker(failure_threshold=3, reset_seconds=60)
    breaker.record_failure(ConnectorTransportError("tutu_mcp ответил 503"))
    breaker.record_failure(ConnectorTimeout("таймаут"))
    breaker.record_failure(ConnectorTimeout("таймаут"))

    assert breaker.is_open is True
    assert breaker.first_cause is not None
    assert "503" in breaker.first_cause


def test_circuit_open_error_carries_the_first_cause() -> None:
    breaker = CircuitBreaker(failure_threshold=2, reset_seconds=60)
    breaker.record_failure(ConnectorTransportError("tutu_mcp ответил 503"))
    breaker.record_failure(ConnectorTimeout("таймаут"))

    with pytest.raises(CircuitOpenError) as info:
        breaker.check("tutu_mcp")
    assert "503" in str(info.value)


def test_success_forgets_the_cause() -> None:
    breaker = CircuitBreaker(failure_threshold=5, reset_seconds=60)
    breaker.record_failure(ConnectorTransportError("разовый сбой"))
    breaker.record_success()
    assert breaker.first_cause is None


def test_transport_reports_the_cause_to_the_circuit() -> None:
    """Причина доезжает от транспорта до размыкателя, а не теряется."""
    transport = build_transport(
        lambda request: httpx.Response(503), max_retries=0, threshold=2
    )
    for _ in range(2):
        with pytest.raises(ConnectorTransportError):
            transport.get("https://example.test/data")
    assert transport.circuit.first_cause is not None
    assert "503" in transport.circuit.first_cause


def test_open_circuit_skips_the_batch_instead_of_burning_it() -> None:
    """Пачка не прожигается при разомкнутой цепи.

    Цепь остывает 900 секунд, а пачка исполняется минуты. Прожигание давало
    сотни записей CIRCUIT_OPEN и занимало досбор той же пустотой; наблюдения
    при этом выглядели обработанными.
    """
    from datetime import date as date_type

    from tmo.connectors.transport import TRANSPORT_POOL
    from tmo.core.config import get_settings
    from tmo.core.enums import CollectionFamily
    from tmo.execution.runner import WorkItem, execute_batch

    TRANSPORT_POOL.reset()
    settings = get_settings()
    circuit = TRANSPORT_POOL.circuit(
        "tutu_mcp", settings.circuit_failure_threshold, settings.circuit_reset_seconds
    )
    for _ in range(settings.circuit_failure_threshold):
        circuit.record_failure(ConnectorTransportError("tutu_mcp ответил 503"))
    assert circuit.is_open is True

    items = [
        WorkItem(
            job_id=index,
            snapshot_id=1,
            snapshot_date=date_type(2026, 8, 7),
            job_key=f"RAIL:{index}",
            family=CollectionFamily.RAIL,
            source_code="tutu_mcp",
            query=QUERY,
            idempotency=f"key-{index}",
            execution_scope="PRIMARY",
        )
        for index in range(20)
    ]

    records, untouched = execute_batch(items, budget=TimeBudget(total_seconds=30))

    assert records == [], "при разомкнутой цепи обращений быть не должно"
    assert sorted(untouched) == list(range(20)), "все наблюдения обязаны вернуться в план"
    TRANSPORT_POOL.reset()
