"""Реестр коннекторов: какой источник каким кодом обслуживается.

Коннекторы кэшируются на процесс. У Туту это принципиально: схемы инструментов
читаются один раз, а TLS-соединение переиспользуется — иначе на каждое из
шестнадцати тысяч наблюдений приходился бы лишний handshake.
"""

from __future__ import annotations

import threading
from pathlib import Path

from tmo.catalog.registry import Source, source_registry
from tmo.connectors.base import BaseConnector
from tmo.connectors.replay import ReplayConnector
from tmo.connectors.rzd import RzdConnector
from tmo.connectors.tutu import TutuConnector
from tmo.core.config import Settings, get_settings
from tmo.core.errors import ConfigurationError

_CONNECTORS: dict[str, type[BaseConnector]] = {
    "tutu_mcp": TutuConnector,
    "rzd": RzdConnector,
    "replay": ReplayConnector,
}

_cache: dict[str, BaseConnector] = {}
_lock = threading.Lock()


def build_connector(
    source: Source,
    settings: Settings | None = None,
    **kwargs: object,
) -> BaseConnector:
    cls = _CONNECTORS.get(source.code)
    if cls is None:
        raise ConfigurationError(f"Нет коннектора для источника {source.code!r}")
    return cls(source, settings or get_settings(), **kwargs)  # type: ignore[arg-type]


def get_connector(source_code: str, *, replay_mode: str | None = None,
                  fixtures_dir: Path | None = None) -> BaseConnector:
    """Коннектор источника, общий на процесс.

    В режиме воспроизведения возвращается ``ReplayConnector``, но **с
    идентичностью настоящего источника**: код, семантика цены и ожидаемое
    число источников остаются прежними. Иначе демонстрационный прогон
    показывал бы один источник там, где в бою их два, и проверял бы не ту
    конфигурацию, которая работает ночью.
    """
    key = f"{source_code}:{replay_mode or ''}"
    with _lock:
        if key not in _cache:
            source = source_registry().get(source_code)
            if replay_mode:
                kwargs: dict[str, object] = {"mode": replay_mode}
                if fixtures_dir is not None:
                    kwargs["fixtures_dir"] = fixtures_dir
                _cache[key] = ReplayConnector(source, get_settings(), **kwargs)  # type: ignore[arg-type]
            elif source_code == "replay":
                _cache[key] = ReplayConnector(source, get_settings(), mode="SIMULATED")
            else:
                _cache[key] = build_connector(source)
        return _cache[key]


def close_all() -> None:
    with _lock:
        for connector in _cache.values():
            connector.close()
        _cache.clear()


def registered_codes() -> tuple[str, ...]:
    return tuple(_CONNECTORS)
