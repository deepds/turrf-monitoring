"""Жизненный цикл Market Snapshot.

Снимок создаётся один раз за операционные сутки и проходит фазы:

```text
PLANNING → COLLECTING → RECOVERING → CALCULATING → READY | DEGRADED | FAILED
```

Повторный сбор за ту же дату не переписывает снимок, а создаёт следующий
``attempt_no``: наблюдение неизменяемо, и «вчерашняя картина» обязана остаться
такой, какой её видели вчера.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from tmo.catalog.registry import (
    MethodologyProfile,
    city_registry,
    methodology_profile,
    source_registry,
)
from tmo.core.config import env_flag, get_settings
from tmo.core.enums import CollectionFamily, JobStatus, SnapshotStatus
from tmo.core.logging import get_logger
from tmo.core.timeutil import HORIZON_DAYS, now_utc, snapshot_date_for
from tmo.db import models
from tmo.planner.matrix import CollectionMatrix, build_matrix

logger = get_logger(__name__)


def sync_reference_data(session: Session) -> None:
    """Переносит справочники из YAML в базу.

    Витрина и экспорт должны быть самодостаточны: имя города в выгруженном
    файле не должно зависеть от того, лежит ли рядом YAML.
    """
    for city in city_registry().ordered:
        row = session.get(models.City, city.code)
        payload = city.model_dump(mode="json")
        if row is None:
            session.add(
                models.City(
                    code=city.code,
                    name=city.name,
                    name_en=city.name_en,
                    timezone=city.timezone,
                    sort_order=city.sort_order,
                    attributes=payload,
                )
            )
        else:
            row.name, row.name_en = city.name, city.name_en
            row.timezone, row.sort_order, row.attributes = (
                city.timezone,
                city.sort_order,
                payload,
            )

    for source in source_registry().sources:
        row = session.get(models.Source, source.code)
        payload = source.model_dump(mode="json")
        if row is None:
            session.add(
                models.Source(
                    code=source.code,
                    name=source.name,
                    protocol=source.protocol,
                    endpoint=source.endpoint,
                    is_enabled=source.is_enabled,
                    attributes=payload,
                )
            )
        else:
            row.name, row.protocol = source.name, source.protocol
            row.endpoint, row.is_enabled, row.attributes = (
                source.endpoint,
                source.is_enabled,
                payload,
            )


def register_methodology(session: Session, profile: MethodologyProfile) -> str:
    """Регистрирует версию методики и стережёт её неизменяемость.

    Хеш содержимого фиксируется при первом применении. Расхождение означает,
    что активную версию изменили на месте: это запрещено, потому что все
    прежние расчёты ссылаются на неё как на неизменную.
    """
    payload = profile.model_dump(mode="json")
    content_hash = hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()

    row = session.get(models.MethodologyProfileRecord, profile.version)
    if row is None:
        session.add(
            models.MethodologyProfileRecord(
                version=profile.version,
                title=profile.title,
                effective_from=profile.effective_from,
                content_hash=content_hash,
                content=payload,
                registered_at=now_utc(),
            )
        )
    elif row.content_hash != content_hash:
        used_by = session.scalar(
            select(func.count(models.CalculationRun.id)).where(
                models.CalculationRun.methodology_version == profile.version
            )
        )
        if used_by or not env_flag("TMO_ALLOW_METHODOLOGY_REREGISTER"):
            logger.error(
                "Активная версия методики изменена на месте",
                version=profile.version,
                registered_hash=row.content_hash,
                current_hash=content_hash,
                used_by_runs=int(used_by or 0),
            )
            raise ValueError(
                f"Версия методики {profile.version} изменена после регистрации "
                f"(на неё ссылается расчётов: {int(used_by or 0)}). "
                "Изменение правила требует новой версии, а не правки существующей."
            )
        # До первого расчёта версия ещё ничего не объясняет: переиздать её
        # безопасно. Флаг существует только для разработки и никогда не
        # позволяет переписать версию, на которую уже сослался расчёт.
        logger.warning(
            "Профиль методики переиздан (TMO_ALLOW_METHODOLOGY_REREGISTER)",
            version=profile.version,
        )
        row.content_hash = content_hash
        row.content = payload
        row.title = profile.title
        row.effective_from = profile.effective_from
        row.registered_at = now_utc()
    return content_hash


@dataclass(slots=True)
class SnapshotCreation:
    snapshot_id: int
    snapshot_date: date
    attempt_no: int
    planned: int
    plan_digest: str
    by_family: dict[str, int]


def create_snapshot(
    session: Session,
    *,
    snapshot_date: date | None = None,
    horizon_days: int | None = None,
    families: tuple[CollectionFamily, ...] = tuple(CollectionFamily),
    is_synthetic: bool = False,
    profile_version: str | None = None,
    matrix: CollectionMatrix | None = None,
) -> SnapshotCreation:
    """Создаёт снимок и детерминированный план наблюдений."""
    settings = get_settings()
    snapshot_date = snapshot_date or snapshot_date_for()
    horizon_days = horizon_days or settings.horizon_days or HORIZON_DAYS

    sync_reference_data(session)
    register_methodology(session, methodology_profile(profile_version))

    previous = session.scalar(
        select(func.max(models.MarketSnapshot.attempt_no)).where(
            models.MarketSnapshot.snapshot_date == snapshot_date
        )
    )
    attempt_no = int(previous or 0) + 1

    snapshot = models.MarketSnapshot(
        snapshot_date=snapshot_date,
        attempt_no=attempt_no,
        status=SnapshotStatus.PLANNING,
        horizon_days=horizon_days,
        is_synthetic=is_synthetic,
        started_at=now_utc(),
        created_at=now_utc(),
        quality_summary={},
        publication_notes=[],
    )
    session.add(snapshot)
    session.flush()

    matrix = matrix or build_matrix(
        snapshot_date, horizon_days=horizon_days, families=families
    )
    session.add_all(
        models.CollectionJob(
            snapshot_id=snapshot.id,
            family=job.family,
            job_key=job.job_key,
            series_key=job.series_key,
            origin_code=job.origin_code,
            destination_code=job.destination_code,
            city_code=job.city_code,
            service_date=job.service_date,
            return_date=job.return_date,
            check_in=job.check_in,
            check_out=job.check_out,
            stars=job.stars,
            day_offset=job.day_offset,
            nights=job.nights,
            params=job.params,
            status=JobStatus.PLANNED,
        )
        for job in matrix.jobs
    )

    counts = matrix.counts_by_family()
    session.add(
        models.CollectionPlan(
            snapshot_id=snapshot.id,
            planned=len(matrix),
            missing=len(matrix),
            by_family=counts,
            plan_digest=matrix.digest,
            built_at=now_utc(),
        )
    )
    snapshot.status = SnapshotStatus.COLLECTING
    session.flush()

    logger.info(
        "Снимок создан",
        snapshot_id=snapshot.id,
        snapshot_date=snapshot_date.isoformat(),
        attempt_no=attempt_no,
        planned=len(matrix),
        by_family=counts,
    )
    return SnapshotCreation(
        snapshot_id=snapshot.id,
        snapshot_date=snapshot_date,
        attempt_no=attempt_no,
        planned=len(matrix),
        plan_digest=matrix.digest,
        by_family=counts,
    )


def latest_published(session: Session) -> models.MarketSnapshot | None:
    """Последний пригодный снимок.

    Если сегодняшний прогон провалился, витриной остаётся предыдущий, а его
    дата и статус показываются пользователю: подменять вчерашние наблюдения
    сегодняшними запрещено, а скрывать, что показано вчерашнее, — тем более.
    """
    return session.scalars(
        select(models.MarketSnapshot)
        .where(
            models.MarketSnapshot.status.in_(
                [SnapshotStatus.READY.value, SnapshotStatus.DEGRADED.value]
            ),
            models.MarketSnapshot.is_synthetic.is_(False),
        )
        .order_by(
            models.MarketSnapshot.snapshot_date.desc(),
            models.MarketSnapshot.attempt_no.desc(),
        )
        .limit(1)
    ).first()


def latest_any(session: Session, *, include_synthetic: bool = True) -> models.MarketSnapshot | None:
    query = select(models.MarketSnapshot).where(
        models.MarketSnapshot.status.in_(
            [SnapshotStatus.READY.value, SnapshotStatus.DEGRADED.value]
        )
    )
    if not include_synthetic:
        query = query.where(models.MarketSnapshot.is_synthetic.is_(False))
    return session.scalars(
        query.order_by(
            models.MarketSnapshot.snapshot_date.desc(),
            models.MarketSnapshot.attempt_no.desc(),
        ).limit(1)
    ).first()


def snapshot_for_date(
    session: Session, snapshot_date: date, *, published_only: bool = True
) -> models.MarketSnapshot | None:
    query = select(models.MarketSnapshot).where(
        models.MarketSnapshot.snapshot_date == snapshot_date
    )
    if published_only:
        query = query.where(
            models.MarketSnapshot.status.in_(
                [SnapshotStatus.READY.value, SnapshotStatus.DEGRADED.value]
            )
        )
    return session.scalars(
        query.order_by(models.MarketSnapshot.attempt_no.desc()).limit(1)
    ).first()


def available_snapshot_dates(session: Session, *, limit: int = 60) -> list[dict[str, Any]]:
    """Даты наблюдения, доступные для исторических графиков."""
    rows = session.execute(
        select(
            models.MarketSnapshot.snapshot_date,
            models.MarketSnapshot.status,
            models.MarketSnapshot.is_synthetic,
            models.MarketSnapshot.coverage_total,
            models.MarketSnapshot.published_at,
        )
        .where(
            models.MarketSnapshot.status.in_(
                [SnapshotStatus.READY.value, SnapshotStatus.DEGRADED.value]
            )
        )
        .order_by(models.MarketSnapshot.snapshot_date.desc())
        .limit(limit)
    ).all()
    return [
        {
            "snapshot_date": row.snapshot_date.isoformat(),
            "status": row.status,
            "is_synthetic": bool(row.is_synthetic),
            "coverage_total": round(float(row.coverage_total), 4),
            "published_at": row.published_at.isoformat() if row.published_at else None,
        }
        for row in rows
    ]
