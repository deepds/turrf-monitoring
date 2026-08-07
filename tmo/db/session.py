"""Сессии и движок.

Правило, ради которого этот модуль существует отдельно: **сетевые операции не
выполняются внутри открытой транзакции** (SCOPE-R C4.8). Сбор разделён на три
фазы, и ``session_scope`` открывается только вокруг коротких фаз плана и
записи.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from tmo.core.config import get_settings

_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def _create_engine(url: str) -> Engine:
    if url.startswith("sqlite"):
        engine = create_engine(url, future=True, pool_pre_ping=True)

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_connection, _record) -> None:
            cursor = dbapi_connection.cursor()
            # Без внешних ключей SQLite молча принимает битые ссылки, и тест
            # provenance проходит на данных, которые PostgreSQL не примет.
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

        return engine

    return create_engine(
        url,
        future=True,
        pool_pre_ping=True,
        pool_size=10,
        max_overflow=20,
        pool_recycle=1800,
        connect_args={
            # Предохранитель от повисшей транзакции на стороне СУБД: она
            # держит блокировки и не завершается сама.
            "options": "-c idle_in_transaction_session_timeout=600000",
        },
    )


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        _engine = _create_engine(get_settings().database_url)
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    global _session_factory
    if _session_factory is None:
        _session_factory = sessionmaker(
            bind=get_engine(), expire_on_commit=False, autoflush=False, future=True
        )
    return _session_factory


@contextmanager
def session_scope() -> Iterator[Session]:
    """Короткая транзакция. Внутри не должно быть сетевых обращений."""
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def reset_engine() -> None:
    """Сброс движка. Нужен тестам, переключающим базу."""
    global _engine, _session_factory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _session_factory = None
