"""Покрытие и качество снимка."""

from __future__ import annotations

from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from tmo.api.deps import db_session
from tmo.services.coverage import compute_coverage, find_holes
from tmo.services.showcase import coverage_matrix, snapshot_overview
from tmo.services.snapshot import snapshot_for_date

router = APIRouter(prefix="/coverage", tags=["Покрытие и качество"])


@router.get("/{snapshot_date}", summary="Покрытие снимка за дату")
def coverage(
    snapshot_date: date,
    attempt_no: int | None = None,
    session: Session = Depends(db_session),
) -> dict[str, Any]:
    snapshot = snapshot_for_date(
        session, snapshot_date, published_only=False, attempt_no=attempt_no
    )
    if snapshot is None:
        detail = f"Снимок за {snapshot_date} не найден"
        if attempt_no is not None:
            detail = f"{detail}: нет версии v{attempt_no}"
        raise HTTPException(status_code=404, detail=detail)
    report = compute_coverage(session, snapshot.id)
    holes = find_holes(session, snapshot.id, limit=200)
    return {
        "overview": snapshot_overview(session, snapshot),
        "coverage": report.as_dict(),
        "matrix": coverage_matrix(session, snapshot.id),
        "holes": {"count": len(holes), "job_ids": holes[:200]},
    }
