"""Ошибки домена.

Каждая ошибка коннектора несёт `outcome`: диспетчер обязан записать конечный
исход попытки, а не проглотить исключение.
"""

from __future__ import annotations

from tmo.core.enums import AttemptOutcome


class TmoError(Exception):
    """Базовая ошибка приложения."""


class ConfigurationError(TmoError):
    """Некорректная конфигурация: справочник, профиль, переменные окружения."""


class MethodologyError(TmoError):
    """Профиль методики нарушает собственные инварианты."""


class ConnectorError(TmoError):
    """Ошибка обращения к источнику с явным исходом."""

    outcome: AttemptOutcome = AttemptOutcome.FAILED

    def __init__(self, message: str, *, source_code: str | None = None, **context: object) -> None:
        super().__init__(message)
        self.source_code = source_code
        self.context = context


class ConnectorTimeout(ConnectorError):
    outcome = AttemptOutcome.TIMEOUT


class ConnectorRateLimited(ConnectorError):
    outcome = AttemptOutcome.RATE_LIMITED


class ConnectorSchemaError(ConnectorError):
    """Источник ответил, но форма ответа не соответствует ожидаемой.

    Отдельный класс потому, что schema drift лечится изменением коннектора, а
    не повтором запроса: досбор такой дыры бессмыслен до правки кода.
    """

    outcome = AttemptOutcome.SCHEMA_ERROR


class ConnectorTransportError(ConnectorError):
    outcome = AttemptOutcome.TRANSPORT_ERROR


class CircuitOpenError(ConnectorError):
    outcome = AttemptOutcome.CIRCUIT_OPEN


class BudgetExhausted(ConnectorError):
    """Мягкий бюджет времени исчерпан: собранное сохраняется как PARTIAL."""

    outcome = AttemptOutcome.BUDGET_EXHAUSTED


class HostNotAllowed(ConnectorError):
    """Попытка обратиться к хосту вне allowlist источника."""

    outcome = AttemptOutcome.FAILED


class PublicationBlocked(TmoError):
    """Quality gate не пропустил снимок в публикацию."""

    def __init__(self, message: str, *, gate: str, details: dict | None = None) -> None:
        super().__init__(message)
        self.gate = gate
        self.details = details or {}
