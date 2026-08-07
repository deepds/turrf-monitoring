"""Зависимости API."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date

from fastapi import Depends, Header, HTTPException, Query, status
from sqlalchemy.orm import Session

from tmo.core.config import Settings, get_settings
from tmo.db.session import get_session_factory
from tmo.services.showcase import NoPublishedSnapshot, SnapshotContext, resolve_context


def db_session() -> Iterator[Session]:
    session = get_session_factory()()
    try:
        yield session
    finally:
        session.close()


def settings() -> Settings:
    return get_settings()


def snapshot_context(
    snapshot_date: date | None = Query(
        None,
        description=(
            "Историческая дата наблюдения. Без неё берётся последний "
            "опубликованный снимок."
        ),
    ),
    session: Session = Depends(db_session),
) -> SnapshotContext:
    try:
        return resolve_context(session, snapshot_date=snapshot_date)
    except NoPublishedSnapshot as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


def require_admin(
    x_admin_token: str | None = Header(None, alias="X-Admin-Token"),
    config: Settings = Depends(settings),
) -> None:
    """Административные операции закрыты токеном.

    Пустой токен в конфигурации означает, что операции выключены, а не что
    они открыты всем: включать их по умолчанию было бы тихим ослаблением
    доступа.
    """
    if not config.admin_token:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Административные операции отключены: не задан TMO_ADMIN_TOKEN",
        )
    if x_admin_token != config.admin_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Неверный административный токен"
        )
