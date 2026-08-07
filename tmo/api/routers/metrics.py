"""Детализация цены и выгрузки.

Любая опубликованная цифра кликабельна и раскрывается здесь: до включённых и
исключённых предложений, до источника, до времени фактического получения и до
файла исходного ответа.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy.orm import Session

from tmo.api.deps import db_session
from tmo.core.config import get_settings
from tmo.services.export import to_csv, to_xlsx
from tmo.services.showcase import metric_details, metric_offers

router = APIRouter(tags=["Детализация цены"])


@router.get("/metrics/{metric_id}", summary="Метрика целиком")
def details(metric_id: int, session: Session = Depends(db_session)) -> dict[str, Any]:
    try:
        return metric_details(session, metric_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/metrics/{metric_id}/offers", summary="Предложения метрики")
def offers(
    metric_id: int,
    included: bool | None = Query(
        None, description="true — только включённые, false — только исключённые"
    ),
    limit: int = Query(1000, ge=1, le=5000),
    session: Session = Depends(db_session),
) -> dict[str, Any]:
    try:
        metric_details(session, metric_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    rows = metric_offers(session, metric_id, included=included, limit=limit)
    return {
        "metric_id": metric_id,
        "count": len(rows),
        "included_count": sum(1 for row in rows if row["is_included"]),
        "excluded_count": sum(1 for row in rows if not row["is_included"]),
        "offers": rows,
    }


@router.get("/exports/metrics/{metric_id}", summary="Выгрузка детализации")
def export(
    metric_id: int,
    fmt: str = Query("csv", pattern="^(csv|xlsx)$"),
    session: Session = Depends(db_session),
) -> Response:
    try:
        metric_details(session, metric_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    if len(metric_offers(session, metric_id, limit=get_settings().export_row_limit + 1)) > (
        get_settings().export_row_limit
    ):
        raise HTTPException(status_code=413, detail="Выгрузка превышает допустимый размер")

    if fmt == "xlsx":
        filename, payload = to_xlsx(session, metric_id)
        media = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    else:
        filename, payload = to_csv(session, metric_id)
        media = "text/csv; charset=utf-8"

    return Response(
        content=payload,
        media_type=media,
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}",
            "Cache-Control": "no-store",
        },
    )
