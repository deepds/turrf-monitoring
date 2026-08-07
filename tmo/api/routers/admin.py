"""Административные операции.

Обе операции меняют состояние и потому закрыты токеном. Досбор и пересчёт
запускаются задачами и не выполняются в HTTP-запросе: обращение к источникам
внутри веб-обработчика превратило бы дашборд в клиента источников.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from tmo.api.deps import db_session, require_admin
from tmo.services.coverage import find_holes
from tmo.services.snapshot import snapshot_for_date

router = APIRouter(prefix="/admin", tags=["Администрирование"], dependencies=[Depends(require_admin)])


@router.post("/snapshots/{snapshot_date}/retry", summary="Досбор дыр снимка")
def retry(snapshot_date: date, session: Session = Depends(db_session)) -> dict[str, Any]:
    snapshot = snapshot_for_date(session, snapshot_date, published_only=False)
    if snapshot is None:
        raise HTTPException(status_code=404, detail=f"Снимок за {snapshot_date} не найден")
    holes = find_holes(session, snapshot.id)
    if not holes:
        return {"snapshot_id": snapshot.id, "queued": 0, "note": "Дыр нет"}

    from tmo.tasks.collection import recover_snapshot

    task = recover_snapshot.delay(snapshot.id)
    return {
        "snapshot_id": snapshot.id,
        "queued": len(holes),
        "task_id": task.id,
        "note": "Досбор поставлен в очередь maintenance",
    }


@router.post("/snapshots/{snapshot_date}/recalculate", summary="Пересчёт снимка")
def recalculate(
    snapshot_date: date,
    profile_version: str | None = None,
    make_active: bool = True,
    session: Session = Depends(db_session),
) -> dict[str, Any]:
    snapshot = snapshot_for_date(session, snapshot_date, published_only=False)
    if snapshot is None:
        raise HTTPException(status_code=404, detail=f"Снимок за {snapshot_date} не найден")

    from tmo.tasks.collection import recalculate_snapshot

    task = recalculate_snapshot.delay(
        snapshot.id, profile_version=profile_version, make_active=make_active
    )
    return {
        "snapshot_id": snapshot.id,
        "task_id": task.id,
        "profile_version": profile_version,
        "make_active": make_active,
        "note": "Новый CalculationRun будет создан; прежний останется неизменным",
    }
