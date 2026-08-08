"""Market Snapshots: последний и исторические."""

from __future__ import annotations

from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from tmo.api.deps import db_session
from tmo.services.showcase import NoPublishedSnapshot, resolve_context, snapshot_overview
from tmo.services.snapshot import available_snapshot_dates

router = APIRouter(prefix="/market-snapshots", tags=["Снимки рынка"])


@router.get("", summary="Доступные даты наблюдения")
def list_snapshots(limit: int = 60, session: Session = Depends(db_session)) -> dict[str, Any]:
    return {"snapshots": available_snapshot_dates(session, limit=limit)}


@router.get("/latest", summary="Последний опубликованный снимок")
def latest(session: Session = Depends(db_session)) -> dict[str, Any]:
    try:
        context = resolve_context(session)
    except NoPublishedSnapshot as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {
        **context.as_dict(),
        "overview": snapshot_overview(session, context.snapshot),
    }


@router.get("/current", summary="Состояние сбора за текущие сутки")
def current(session: Session = Depends(db_session)) -> dict[str, Any]:
    """Что происходит со снимком, который ещё собирается.

    Отдельный эндпоинт, а не поле готового снимка: витрина показывает
    последний **закрытый** день, и текущие сутки к нему отношения не имеют.
    Смешивать их в одном ответе значило бы предлагать читателю самому
    догадываться, к какому из двух снимков относится каждое число.

    ``null`` в ``progress`` означает, что снимок за сегодня ещё не открыт, —
    это не ошибка, а состояние ночи до 00:30.
    """
    from tmo.services import cycle

    return {"progress": cycle.progress(session)}


@router.get("/{snapshot_date}", summary="Снимок за конкретную дату")
def by_date(
    snapshot_date: date,
    attempt_no: int | None = None,
    session: Session = Depends(db_session),
) -> dict[str, Any]:
    try:
        context = resolve_context(session, snapshot_date=snapshot_date, attempt_no=attempt_no)
    except NoPublishedSnapshot as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {
        **context.as_dict(),
        "overview": snapshot_overview(session, context.snapshot),
    }
