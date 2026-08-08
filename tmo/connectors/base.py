"""Базовый коннектор.

Коннектор отвечает на один вопрос: «что источник показал по этому наблюдению».
Он не решает, что из показанного является рынком, — это методика.

Исключения наружу не выпускаются: любая ошибка превращается в
``ConnectorResult`` с конечным исходом. Молчаливый пропуск источника запрещён,
а пропуск с исключением, съеденным где-то выше, — это тот же пропуск.
"""

from __future__ import annotations

import threading
import time
from abc import ABC, abstractmethod
from typing import Any

from tmo.catalog.registry import Source
from tmo.connectors.contracts import AirQuery, ConnectorResult, HotelQuery, Query, RailQuery
from tmo.connectors.transport import TRANSPORT_POOL, SourceTransport, TimeBudget
from tmo.core.config import Settings, get_settings
from tmo.core.enums import AttemptOutcome, CollectionFamily
from tmo.core.errors import ConnectorError
from tmo.core.logging import get_logger
from tmo.core.timeutil import now_utc

logger = get_logger(__name__)


class BaseConnector(ABC):
    """Общий каркас: транспорт, диспетчеризация по семейству, обработка отказов."""

    code: str = ""
    version: str = "2.0.0"
    #: Обращается ли коннектор к сети. False у источника воспроизведения:
    #: попытка получить у него транспорт — ошибка, а не отсутствие данных.
    uses_network: bool = True

    def __init__(self, source: Source, settings: Settings | None = None) -> None:
        self.source = source
        self.settings = settings or get_settings()
        self.log = get_logger(f"tmo.connectors.{source.code}").bind(source=source.code)
        self._transport: SourceTransport | None = None
        self._transport_lock = threading.Lock()

    # -- транспорт -----------------------------------------------------------

    def transport(self) -> SourceTransport:
        """Клиент источника, общий на процесс. Создание — под блокировкой.

        Переиспользуется между наблюдениями: установка TLS-соединения к
        MCP-серверу стоит дороже самого запроса, а лимитер и размыкатель обязаны
        быть общими — иначе два потока дадут вдвое больший фактический темп.

        Без блокировки потоки пачки одновременно видят ``None`` и создают каждый
        свой клиент. Побеждает последний записавший, а остальные продолжают
        работать через собственные объекты: у наблюдения счётчик обращений
        считается на одном клиенте, а запросы уходят через другой, и в базу
        попадает ноль. Лимит темпа при этом уцелел — он живёт в общем
        ``TRANSPORT_POOL``, — но соединений открывается вшестеро больше, чем
        задумано.

        Ровно эта гонка однажды была устранена в ``TutuConnector.mcp()``.
        Здесь, уровнем ниже, она осталась и проявилась только на живом
        многопоточном прогоне 08.08.2026.
        """
        if self._transport is not None:
            return self._transport
        with self._transport_lock:
            # Проверка повторяется внутри блокировки: пока поток ждал, клиента
            # мог создать кто-то другой.
            if self._transport is not None:
                return self._transport
            settings = self.settings
            self._transport = SourceTransport(
                source_code=self.source.code,
                allowed_hosts=self.source.allowed_hosts,
                rate_limiter=TRANSPORT_POOL.limiter(
                    self.source.code, self.source.rate_limit_per_minute
                ),
                circuit=TRANSPORT_POOL.circuit(
                    self.source.code,
                    settings.circuit_failure_threshold,
                    settings.circuit_reset_seconds,
                ),
                connect_timeout=settings.connect_timeout_seconds,
                read_timeout=settings.read_timeout_seconds,
                max_retries=settings.max_transport_retries,
                backoff_base=settings.backoff_base_seconds,
                backoff_max=settings.backoff_max_seconds,
                default_headers=self.default_headers(),
            )
        return self._transport

    def default_headers(self) -> dict[str, str]:
        return {}

    def close(self) -> None:
        if self._transport is not None:
            self._transport.close()
            self._transport = None

    # -- сбор ----------------------------------------------------------------

    def collect(self, query: Query, budget: TimeBudget) -> ConnectorResult:
        """Единая точка входа. Любой отказ становится результатом, а не исключением."""
        family = _family_of(query)
        started = time.perf_counter()
        requested_at = now_utc()
        # Источник воспроизведения сети не имеет вовсе: счётчик обращений у
        # него нулевой, и это не ошибка, а свойство.
        transport = self.transport() if self.uses_network else None
        # Счётчик берётся пооточный: клиент общий на источник, и дельта его
        # общего счётчика вокруг одного наблюдения считает чужие обращения.
        calls_before = transport.thread_call_count if transport else 0
        try:
            if isinstance(query, RailQuery):
                result = self.collect_rail(query, budget)
            elif isinstance(query, AirQuery):
                result = self.collect_air(query, budget)
            else:
                result = self.collect_hotel(query, budget)
        except ConnectorError as exc:
            result = ConnectorResult(
                source_code=self.source.code,
                family=family,
                outcome=exc.outcome,
                error_code=type(exc).__name__,
                error_message=str(exc)[:2000],
                connector_version=self.version,
            )
        except Exception as exc:
            self.log.exception("Непредвиденная ошибка коннектора", error=str(exc))
            result = ConnectorResult(
                source_code=self.source.code,
                family=family,
                outcome=AttemptOutcome.FAILED,
                error_code=type(exc).__name__,
                error_message=str(exc)[:2000],
                connector_version=self.version,
            )

        result.requested_at = result.requested_at or requested_at
        result.fetched_at = result.fetched_at or now_utc()
        result.latency_ms = result.latency_ms or int((time.perf_counter() - started) * 1000)
        result.http_calls = result.http_calls or (
            (transport.thread_call_count - calls_before) if transport else 0
        )
        result.connector_version = self.version
        return result

    @abstractmethod
    def collect_rail(self, query: RailQuery, budget: TimeBudget) -> ConnectorResult: ...

    @abstractmethod
    def collect_air(self, query: AirQuery, budget: TimeBudget) -> ConnectorResult: ...

    @abstractmethod
    def collect_hotel(self, query: HotelQuery, budget: TimeBudget) -> ConnectorResult: ...

    def health_check(self) -> dict[str, Any]:
        return {"source": self.source.code, "status": "unknown"}

    # -- вспомогательное -----------------------------------------------------

    def unsupported(self, family: CollectionFamily) -> ConnectorResult:
        return ConnectorResult(
            source_code=self.source.code,
            family=family,
            outcome=AttemptOutcome.FAILED,
            error_code="UNSUPPORTED_FAMILY",
            error_message=f"{self.source.code} не покрывает семейство {family.value}",
            connector_version=self.version,
        )


def _family_of(query: Query) -> CollectionFamily:
    if isinstance(query, RailQuery):
        return CollectionFamily.RAIL
    if isinstance(query, AirQuery):
        return CollectionFamily.AIR
    return CollectionFamily.HOTEL
