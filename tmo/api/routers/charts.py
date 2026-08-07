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
from tmo.services.showcase import (
    SnapshotContext,
    air_chart,
    air_grid,
    hotel_chart,
    rail_chart,
)

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


@router.get("/air", summary="Стоимость кругового авиатарифа по датам вылета")
def air(
    origin: str = Query(..., description="Код города отправления"),
    nights: int = Query(
        7,
        ge=1,
        le=29,
        description=(
            "Длительность поездки в ночах. Задаётся явно: авиа наблюдается парой "
            "дат, и на каждую дату вылета приходится своя цена для каждой "
            "длительности."
        ),
    ),
    destination: str | None = Query(
        None, description="Код города назначения; без него — все направления"
    ),
    context: SnapshotContext = Depends(snapshot_context),
    session: Session = Depends(db_session),
) -> dict[str, Any]:
    data = air_chart(session, context, origin=origin, nights=nights, destination=destination)
    return {
        "context": context.as_dict(),
        "mode": "ROUTE_DETAIL" if destination else "OVERVIEW",
        **data,
    }


@router.get("/air-grid", summary="Полная сетка наблюдений авиа по одному маршруту")
def air_grid_route(
    origin: str = Query(..., description="Код города отправления"),
    destination: str = Query(..., description="Код города назначения"),
    context: SnapshotContext = Depends(snapshot_context),
    session: Session = Depends(db_session),
) -> dict[str, Any]:
    """Сетка «дата вылета × длительность поездки» для одного маршрута.

    Один маршрут, а не все сразу: цены разных направлений несравнимы, и общая
    шкала цвета покрасила бы дальнее направление сплошь «дорогим».
    """
    data = air_grid(session, context, origin=origin, destination=destination)
    return {"context": context.as_dict(), **data}


@router.get("/hotels", summary="Стоимость одной ночи по всем городам")
def hotels(
    stars: int = Query(4, ge=3, le=5),
    context: SnapshotContext = Depends(snapshot_context),
    session: Session = Depends(db_session),
) -> dict[str, Any]:
    data = hotel_chart(session, context, stars=stars)
    return {"context": context.as_dict(), **data}
