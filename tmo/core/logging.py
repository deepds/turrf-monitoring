"""Структурные логи.

Каждая запись сбора обязана нести идентификаторы, по которым цифру на витрине
можно проследить назад (SCOPE-R P24): ``snapshot_id``, ``collection_job_id``,
``source_attempt_id``, ``calculation_run_id``, ``metric_id``.
"""

from __future__ import annotations

import json
import logging
import sys
from contextvars import ContextVar
from typing import Any

#: Значение по умолчанию — None, а не пустой словарь: изменяемый объект в
#: умолчании ContextVar общий для всех контекстов, и запись в него из одной
#: задачи протекла бы во все остальные.
_context: ContextVar[dict[str, Any] | None] = ContextVar("tmo_log_context", default=None)


def _current_context() -> dict[str, Any]:
    return _context.get() or {}


TRACE_KEYS = (
    "snapshot_id",
    "collection_job_id",
    "source_attempt_id",
    "calculation_run_id",
    "metric_id",
)


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        payload.update(_current_context())
        extra = getattr(record, "tmo_extra", None)
        if extra:
            payload.update(extra)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


class BoundLogger:
    """Тонкая обёртка над stdlib: ключи передаются как kwargs."""

    __slots__ = ("_bound", "_logger")

    def __init__(self, logger: logging.Logger, bound: dict[str, Any] | None = None) -> None:
        self._logger = logger
        self._bound = bound or {}

    def bind(self, **kwargs: Any) -> BoundLogger:
        return BoundLogger(self._logger, {**self._bound, **kwargs})

    def _log(self, level: int, message: str, **kwargs: Any) -> None:
        if not self._logger.isEnabledFor(level):
            return
        self._logger.log(level, message, extra={"tmo_extra": {**self._bound, **kwargs}})

    def debug(self, message: str, **kwargs: Any) -> None:
        self._log(logging.DEBUG, message, **kwargs)

    def info(self, message: str, **kwargs: Any) -> None:
        self._log(logging.INFO, message, **kwargs)

    def warning(self, message: str, **kwargs: Any) -> None:
        self._log(logging.WARNING, message, **kwargs)

    def error(self, message: str, **kwargs: Any) -> None:
        self._log(logging.ERROR, message, **kwargs)

    def exception(self, message: str, **kwargs: Any) -> None:
        self._logger.exception(message, extra={"tmo_extra": {**self._bound, **kwargs}})


def configure_logging(level: str = "INFO", fmt: str = "json") -> None:
    root = logging.getLogger()
    root.handlers.clear()
    handler = logging.StreamHandler(sys.stdout)
    if fmt == "json":
        handler.setFormatter(_JsonFormatter())
    else:
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-7s %(name)s | %(message)s")
        )
    root.addHandler(handler)
    root.setLevel(level.upper())
    for noisy in ("httpx", "httpcore", "urllib3", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str) -> BoundLogger:
    return BoundLogger(logging.getLogger(name))


class log_context:
    """Добавляет трассировочные ключи ко всем записям внутри блока."""

    def __init__(self, **kwargs: Any) -> None:
        self._values = {k: v for k, v in kwargs.items() if v is not None}
        self._token = None

    def __enter__(self) -> log_context:
        self._token = _context.set({**_current_context(), **self._values})
        return self

    def __exit__(self, *exc: object) -> None:
        if self._token is not None:
            _context.reset(self._token)
