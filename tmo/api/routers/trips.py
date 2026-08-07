"""Блок «Куда ехать».

Использует последний опубликованный снимок. Историческая дата наблюдения в
этом блоке не выбирается: руководителю нужна актуальная картина, а не архив.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from tmo.api.deps import db_session
from tmo.core.config import get_settings
from tmo.core.enums import TransportMode
from tmo.core.timeutil import HORIZON_DAYS
from tmo.services.showcase import (
    NoPublishedSnapshot,
    available_origins,
    resolve_context,
)
from tmo.services.showcase import (
    trips as trips_query,
)

router = APIRouter(prefix="/showcase", tags=["Куда ехать"])


@router.get("/origins", summary="Города отправления")
def origins() -> dict[str, Any]:
    return {"origins": available_origins()}


@router.get("/trips", summary="Варианты поездок из выбранного города")
def trips(
    origin: str = Query(..., description="Код города отправления"),
    departure_date: date = Query(..., description="Дата отправления"),
    return_date: date = Query(..., description="Дата возвращения"),
    transport_mode: TransportMode = Query(TransportMode.AIR),
    stars: int = Query(4, ge=3, le=5),
    session: Session = Depends(db_session),
) -> dict[str, Any]:
    try:
        context = resolve_context(session)
    except NoPublishedSnapshot as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    horizon = get_settings().horizon_days or HORIZON_DAYS
    lower = context.snapshot.snapshot_date + timedelta(days=1)
    upper = context.snapshot.snapshot_date + timedelta(days=horizon)

    if return_date <= departure_date:
        raise HTTPException(
            status_code=422, detail="Дата возвращения должна быть позже даты отправления"
        )
    if departure_date < lower or return_date > upper:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Даты должны лежать в горизонте наблюдения {lower.isoformat()} … "
                f"{upper.isoformat()} снимка за {context.snapshot.snapshot_date.isoformat()}"
            ),
        )

    rows = trips_query(
        session,
        context,
        origin=origin,
        departure_date=departure_date,
        return_date=return_date,
        transport_mode=transport_mode.value,
        stars=stars,
    )
    return {
        "context": context.as_dict(),
        "request": {
            "origin": origin,
            "departure_date": departure_date.isoformat(),
            "return_date": return_date.isoformat(),
            "transport_mode": transport_mode.value,
            "stars": stars,
            "nights": (return_date - departure_date).days,
        },
        "label": "Расчётная стоимость поездки",
        "trips": rows,
    }
