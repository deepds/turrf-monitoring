"""Транспортный слой обращений к источникам.

Здесь собраны три предохранителя, каждый из которых поставлен по конкретному
инциденту первой версии:

* **лимит темпа** — одновременный залп из тысячи с лишним наблюдений источник
  не держит, и именно он открывал размыкатель цепи;
* **размыкатель цепи** — после серии отказов перестаём спрашивать вовсе, иначе
  ночь уходит на таймауты;
* **бюджет времени** — жёсткий обрыв задачи уничтожал уже собранное, поэтому
  обход прекращается добровольно и результат помечается частичным.

Ни один из них не держит транзакцию базы: ожидание темпа внутри открытой
транзакции однажды остановило всю СУБД (COLLECTION_RELIABILITY, отказ 4).
"""

from __future__ import annotations

import random
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx

from tmo.core.errors import (
    BudgetExhausted,
    CircuitOpenError,
    ConnectorRateLimited,
    ConnectorTimeout,
    ConnectorTransportError,
    HostNotAllowed,
)
from tmo.core.logging import get_logger

logger = get_logger(__name__)


class RateLimiter:
    """Скользящее окно на минуту, общее для всех потоков одного процесса."""

    def __init__(self, per_minute: int) -> None:
        self.per_minute = per_minute
        self._events: deque[float] = deque()
        self._lock = threading.Lock()

    def acquire(self, *, budget: TimeBudget | None = None) -> float:
        """Ждёт разрешения. Возвращает, сколько секунд пришлось ждать."""
        if self.per_minute <= 0:
            return 0.0
        waited = 0.0
        while True:
            with self._lock:
                now = time.monotonic()
                while self._events and now - self._events[0] >= 60.0:
                    self._events.popleft()
                if len(self._events) < self.per_minute:
                    self._events.append(now)
                    return waited
                sleep_for = 60.0 - (now - self._events[0]) + 0.01
            if budget is not None and not budget.can_afford(sleep_for):
                raise BudgetExhausted(
                    "Ожидание лимита темпа не укладывается в бюджет времени",
                    wait_seconds=sleep_for,
                )
            time.sleep(min(sleep_for, 5.0))
            waited += min(sleep_for, 5.0)


class CircuitBreaker:
    """Размыкатель цепи на источник.

    Разомкнутая цепь — это не сбой наблюдения, а наше собственное решение
    перестать спрашивать. Оно записывается отдельным исходом ``CIRCUIT_OPEN``,
    чтобы досбор понимал, что причина на нашей стороне и к утру пройдёт.
    """

    def __init__(self, failure_threshold: int, reset_seconds: int) -> None:
        self.failure_threshold = failure_threshold
        self.reset_seconds = reset_seconds
        self._failures = 0
        self._opened_at: float | None = None
        self._first_cause: str | None = None
        self._lock = threading.Lock()

    @property
    def is_open(self) -> bool:
        with self._lock:
            if self._opened_at is None:
                return False
            if time.monotonic() - self._opened_at >= self.reset_seconds:
                self._opened_at = None
                self._failures = 0
                self._first_cause = None
                return False
            return True

    def record_success(self) -> None:
        with self._lock:
            self._failures = 0
            self._opened_at = None
            self._first_cause = None

    def record_failure(self, cause: Exception | None = None) -> None:
        """Считает отказ и, при переполнении, размыкает цепь.

        Первый отказ серии запоминается и попадает в лог вместе с открытием
        цепи. Без этого расследовать нечего: система знает, что перестала
        спрашивать, и не знает почему — все последующие записи содержат лишь
        ``CIRCUIT_OPEN``.
        """
        with self._lock:
            if self._failures == 0 and cause is not None:
                self._first_cause = f"{type(cause).__name__}: {cause}"[:400]
            self._failures += 1
            if self._failures >= self.failure_threshold and self._opened_at is None:
                self._opened_at = time.monotonic()
                logger.error(
                    "Размыкатель цепи разомкнут",
                    failures=self._failures,
                    reset_seconds=self.reset_seconds,
                    first_cause=self._first_cause,
                    last_cause=f"{type(cause).__name__}: {cause}"[:400] if cause else None,
                )

    @property
    def first_cause(self) -> str | None:
        """Чем начиналась серия отказов, открывшая цепь."""
        return self._first_cause

    def check(self, source_code: str) -> None:
        if self.is_open:
            # Причина, с которой началась серия, едет с каждым отказом: иначе
            # в базе останутся только CIRCUIT_OPEN без объяснения.
            suffix = f" (началось с: {self._first_cause})" if self._first_cause else ""
            raise CircuitOpenError(
                f"Размыкатель цепи источника {source_code} разомкнут{suffix}",
                source_code=source_code,
            )


@dataclass(slots=True)
class TimeBudget:
    """Мягкий бюджет времени.

    Меньше жёсткого таймаута задачи: при исчерпании обход прекращается сам,
    собранное сохраняется, результат помечается ``PARTIAL``. Жёсткий таймаут
    остаётся последней страховкой и в норме не срабатывает.
    """

    total_seconds: float
    started_at: float = 0.0

    def __post_init__(self) -> None:
        if not self.started_at:
            self.started_at = time.monotonic()

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started_at

    @property
    def remaining(self) -> float:
        return max(0.0, self.total_seconds - self.elapsed)

    @property
    def exhausted(self) -> bool:
        return self.remaining <= 0.0

    def can_afford(self, seconds: float) -> bool:
        return self.remaining > seconds

    def child(self, seconds: float) -> TimeBudget:
        """Вложенный бюджет, не превышающий остаток родительского."""
        return TimeBudget(total_seconds=min(seconds, self.remaining))


class SourceTransport:
    """HTTP-клиент источника: allowlist, лимит темпа, повторы, размыкатель.

    Клиент один на источник и переиспользуется между наблюдениями: соединение
    к MCP-серверу устанавливается заметно дольше самого запроса.
    """

    def __init__(
        self,
        *,
        source_code: str,
        allowed_hosts: tuple[str, ...],
        rate_limiter: RateLimiter,
        circuit: CircuitBreaker,
        connect_timeout: float = 10.0,
        read_timeout: float = 45.0,
        max_retries: int = 2,
        backoff_base: float = 0.8,
        backoff_max: float = 8.0,
        user_agent: str = "travel-monitoring-observatory/2.0",
        default_headers: dict[str, str] | None = None,
    ) -> None:
        self.source_code = source_code
        self.allowed_hosts = tuple(allowed_hosts)
        self.rate_limiter = rate_limiter
        self.circuit = circuit
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.backoff_max = backoff_max
        self.call_count = 0
        # Счётчик обращений одного наблюдения. Общий счётчик для этого не
        # годится: клиент один на источник и делится между потоками пачки,
        # поэтому его дельта, снятая вокруг одного наблюдения, включает чужие
        # обращения и в конкуренции вырождается. В ночь 08.08.2026 из-за этого
        # у Туту в базе стояло 0,01 обращения на наблюдение при фактических
        # один-восемь, тогда как у РЖД, считающего сам, поле было верным.
        self._local = threading.local()
        self._client = httpx.Client(
            timeout=httpx.Timeout(read_timeout, connect=connect_timeout),
            headers={"User-Agent": user_agent, **(default_headers or {})},
            follow_redirects=True,
            limits=httpx.Limits(max_connections=32, max_keepalive_connections=16),
        )

    @property
    def thread_call_count(self) -> int:
        """Обращения, сделанные текущим потоком. Наблюдение живёт в одном."""
        return int(getattr(self._local, "calls", 0))

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> SourceTransport:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _check_host(self, url: str) -> None:
        host = (urlparse(url).hostname or "").lower()
        if self.allowed_hosts and host not in self.allowed_hosts:
            raise HostNotAllowed(
                f"Хост {host!r} вне allowlist источника {self.source_code}",
                source_code=self.source_code,
            )

    def request(
        self,
        method: str,
        url: str,
        *,
        budget: TimeBudget | None = None,
        **kwargs: Any,
    ) -> httpx.Response:
        """Один обращение с повторами транспортных ошибок.

        Повторяются только транспортные отказы и 5xx/429: повтор запроса,
        отвергнутого по схеме, вернёт ту же ошибку и потратит лимит темпа.
        """
        self._check_host(url)
        self.circuit.check(self.source_code)

        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            if budget is not None and budget.exhausted:
                raise BudgetExhausted(
                    "Бюджет времени исчерпан до обращения", source_code=self.source_code
                )
            self.rate_limiter.acquire(budget=budget)
            self.call_count += 1
            self._local.calls = self.thread_call_count + 1
            try:
                response = self._client.request(method, url, **kwargs)
            except httpx.TimeoutException as exc:
                last_error = ConnectorTimeout(
                    f"Таймаут обращения к {self.source_code}: {exc}", source_code=self.source_code
                )
            except httpx.HTTPError as exc:
                last_error = ConnectorTransportError(
                    f"Транспортная ошибка {self.source_code}: {exc}", source_code=self.source_code
                )
            else:
                if response.status_code == 429:
                    last_error = ConnectorRateLimited(
                        f"Источник {self.source_code} ограничил темп",
                        source_code=self.source_code,
                    )
                elif response.status_code >= 500:
                    last_error = ConnectorTransportError(
                        f"{self.source_code} ответил {response.status_code}",
                        source_code=self.source_code,
                    )
                else:
                    self.circuit.record_success()
                    return response

            if attempt >= self.max_retries:
                break
            delay = min(self.backoff_base * (2**attempt), self.backoff_max)
            delay += random.uniform(0, delay * 0.25)
            if budget is not None and not budget.can_afford(delay):
                break
            time.sleep(delay)

        self.circuit.record_failure(last_error)
        raise last_error or ConnectorTransportError(
            f"Не удалось обратиться к {self.source_code}", source_code=self.source_code
        )

    def post_json(self, url: str, payload: Any, **kwargs: Any) -> httpx.Response:
        return self.request("POST", url, json=payload, **kwargs)

    def get(self, url: str, **kwargs: Any) -> httpx.Response:
        return self.request("GET", url, **kwargs)


class TransportPool:
    """Общие на процесс лимитеры и размыкатели по коду источника.

    Лимит темпа обязан быть общим: два воркерских потока со своими счётчиками
    дают вдвое больший фактический темп, чем настроено.
    """

    def __init__(self) -> None:
        self._limiters: dict[str, RateLimiter] = {}
        self._circuits: dict[str, CircuitBreaker] = {}
        self._lock = threading.Lock()

    def limiter(self, source_code: str, per_minute: int) -> RateLimiter:
        with self._lock:
            if source_code not in self._limiters:
                self._limiters[source_code] = RateLimiter(per_minute)
            return self._limiters[source_code]

    def circuit(self, source_code: str, threshold: int, reset_seconds: int) -> CircuitBreaker:
        with self._lock:
            if source_code not in self._circuits:
                self._circuits[source_code] = CircuitBreaker(threshold, reset_seconds)
            return self._circuits[source_code]

    def reset(self) -> None:
        with self._lock:
            self._limiters.clear()
            self._circuits.clear()


#: Пул на процесс. Воркер Celery работает в одном процессе на пул потоков.
TRANSPORT_POOL = TransportPool()
