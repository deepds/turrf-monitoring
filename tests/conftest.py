"""Общая обвязка тестов.

По умолчанию тесты идут на SQLite — быстро и без внешних зависимостей.
PostgreSQL-набор включается переменной ``TMO_TEST_DATABASE_URL`` и проверяет
то, что SQLite воспроизвести не может. Одно не заменяет другое.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Iterator
from datetime import date
from pathlib import Path

import pytest

os.environ.setdefault("TMO_SOURCES_ENABLED", "false")
os.environ.setdefault("TMO_LOG_LEVEL", "WARNING")
os.environ.setdefault("TMO_LOG_FORMAT", "text")

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session")
def _workdir() -> Iterator[Path]:
    with tempfile.TemporaryDirectory(prefix="tmo-tests-") as directory:
        yield Path(directory)


@pytest.fixture(autouse=True, scope="session")
def _isolated_storage(_workdir: Path) -> Iterator[None]:
    """Хранилища тестов не трогают рабочие каталоги проекта."""
    os.environ["TMO_RAW_STORAGE_PATH"] = str(_workdir / "raw")
    os.environ["TMO_EXPORT_STORAGE_PATH"] = str(_workdir / "export")
    yield


@pytest.fixture()
def database(_workdir: Path, request: pytest.FixtureRequest) -> Iterator[str]:
    """Пустая база на тест. По умолчанию SQLite, при заданном URL — PostgreSQL."""
    from tmo.core.config import reset_settings_cache
    from tmo.db import models  # noqa: F401 — регистрация моделей
    from tmo.db.base import Base
    from tmo.db.session import get_engine, reset_engine

    url = os.environ.get("TMO_TEST_DATABASE_URL")
    if url:
        # Каждый тест получает свою схему: параллельный прогон не должен
        # видеть чужие снимки.
        schema = f"test_{abs(hash(request.node.nodeid)) % 10_000_000}"
        os.environ["TMO_DATABASE_URL"] = url
        reset_settings_cache()
        reset_engine()
        from sqlalchemy import text

        engine = get_engine()
        with engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            connection.execute(text(f'SET search_path TO "{schema}"'))
        os.environ["TMO_DATABASE_URL"] = f"{url}?options=-csearch_path%3D{schema}"
        reset_settings_cache()
        reset_engine()
        engine = get_engine()
        Base.metadata.create_all(engine)
        yield os.environ["TMO_DATABASE_URL"]
        with engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        reset_engine()
        return

    path = _workdir / f"{abs(hash(request.node.nodeid)) % 10_000_000}.sqlite3"
    if path.exists():
        path.unlink()
    os.environ["TMO_DATABASE_URL"] = f"sqlite:///{path.as_posix()}"
    reset_settings_cache()
    reset_engine()
    Base.metadata.create_all(get_engine())
    yield os.environ["TMO_DATABASE_URL"]
    reset_engine()


@pytest.fixture()
def session(database: str):
    from tmo.db.session import session_scope

    with session_scope() as value:
        yield value


@pytest.fixture()
def profile():
    from tmo.catalog.registry import methodology_profile

    return methodology_profile("baseline_v1")


@pytest.fixture()
def snapshot_date() -> date:
    return date(2026, 8, 7)


@pytest.fixture(autouse=True)
def _allow_reregister() -> Iterator[None]:
    """В тестах профиль перечитывается многократно из чистой базы."""
    os.environ["TMO_ALLOW_METHODOLOGY_REREGISTER"] = "true"
    yield


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Пропускает наборы, для которых нет условий, вместо ложного зелёного."""
    skip_postgres = pytest.mark.skip(reason="нужен TMO_TEST_DATABASE_URL с PostgreSQL")
    skip_live = pytest.mark.skip(reason="живые источники: запуск через TMO_TEST_LIVE=1")
    has_postgres = bool(os.environ.get("TMO_TEST_DATABASE_URL"))
    has_live = os.environ.get("TMO_TEST_LIVE") == "1"
    for item in items:
        if "postgres" in item.keywords and not has_postgres:
            item.add_marker(skip_postgres)
        if "live" in item.keywords and not has_live:
            item.add_marker(skip_live)
