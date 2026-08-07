"""Графики: транспорт (ЖД) и проживание.

Оба поддерживают выбор исторической даты наблюдения — «как рынок выглядел за
вчера». Авиа в графике транспорта не показывается: круговой тариф не
раскладывается на даты отправления, и ставить его в один ряд с плечом ЖД
означало бы сравнивать разные величины.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from tmo.api.deps import db_session, snapshot_context
from tmo.services.showcase import SnapshotContext, hotel_chart, rail_chart

router = APIRouter(prefix="/charts", tags=["Графики"])


@router.get("/rail", summary="Стоимость ЖД по датам отправления")
def rail(
    origin: str = Query(..., description="Код города отправления"),
    destination: str | None = Query(
        None, description="Код города назначения; без него — все направления"
    ),
    context: SnapshotContext = Depends(snapshot_context),
    session: Session = Depends(db_session),
) -> dict[str, Any]:
    data = rail_chart(session, context, origin=origin, destination=destination)
    return {
        "context": context.as_dict(),
        "mode": "ROUTE_DETAIL" if destination else "OVERVIEW",
        **data,
    }


@router.get("/hotels", summary="Стоимость одной ночи по всем городам")
def hotels(
    stars: int = Query(4, ge=3, le=5),
    context: SnapshotContext = Depends(snapshot_context),
    session: Session = Depends(db_session),
) -> dict[str, Any]:
    data = hotel_chart(session, context, stars=stars)
    return {"context": context.as_dict(), **data}
