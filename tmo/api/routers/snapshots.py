"""Market Snapshots: последний и исторические."""

from __future__ import annotations

import tempfile
from datetime import date
from pathlib import Path
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from tmo.api.deps import db_session
from tmo.core.config import get_settings
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


@router.get("/{snapshot_date}/archive", summary="Скачать снимок одним файлом")
def download_archive(
    snapshot_date: date,
    attempt_no: int | None = None,
    level: str = "evidence",
    background: BackgroundTasks = None,
    session: Session = Depends(db_session),
):
    """Отдаёт снимок архивом для переноса на другой стенд.

    Уровень по умолчанию — `evidence`: перенос через браузер существует именно
    для объёмов, которые не проходят через репозиторий, и обрезать его молча до
    витринного значило бы отдать не то, за чем пришли.
    """
    from tmo.services.snapshot import snapshot_for_date
    from tmo.services.transfer import EVIDENCE, SHOWCASE, archive_name, archive_snapshot

    if level not in (SHOWCASE, EVIDENCE):
        raise HTTPException(status_code=400, detail=f"Неизвестный уровень: {level}")

    snapshot = snapshot_for_date(
        session, snapshot_date, published_only=False, attempt_no=attempt_no
    )
    if snapshot is None:
        raise HTTPException(status_code=404, detail=f"Снимок за {snapshot_date} не найден")

    settings = get_settings()
    name = archive_name(snapshot.snapshot_date, snapshot.attempt_no, level)
    target = Path(settings.export_storage_path) / name
    archive_snapshot(
        session,
        snapshot.id,
        target,
        level=level,
        origin_stand=settings.stand_name or settings.environment,
    )
    # Файл удаляется после отдачи: архив полной матрицы — 54 МБ, и хранить
    # каждую выгрузку на диске незачем.
    if background is not None:
        background.add_task(target.unlink, missing_ok=True)
    return FileResponse(target, media_type="application/x-tar", filename=name)


@router.post("/archive", summary="Загрузить снимок из файла")
async def upload_archive(
    file: UploadFile = File(..., description="Архив, полученный выгрузкой"),
    force: bool = False,
) -> dict[str, Any]:
    """Принимает архив снимка и загружает его новой версией своей даты.

    Совпадение с уже загруженным снимком **не является ошибкой**: возвращается
    `status: DUPLICATE` и сведения о том, что уже лежит в базе. Решение —
    загрузить копию отдельной версией или отказаться — принимает человек, и
    повтор с `force=true` его выражает.

    Сессия берётся своя, а не из зависимости `db_session`: та закрывает сессию,
    но не фиксирует её. Читающим эндпоинтам это безразлично, а загрузка на ней
    молча откатывалась бы — и рапортовала об успехе.
    """
    from tmo.db.session import session_scope
    from tmo.services.transfer import ImportRefused, import_archive

    settings = get_settings()
    with tempfile.NamedTemporaryFile(
        dir=settings.export_storage_path, suffix=".tar", delete=False
    ) as handle:
        target = Path(handle.name)
        while chunk := await file.read(1 << 20):
            handle.write(chunk)
    try:
        with session_scope() as session:
            return import_archive(session, target, force=force)
    except ImportRefused as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    finally:
        target.unlink(missing_ok=True)


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
