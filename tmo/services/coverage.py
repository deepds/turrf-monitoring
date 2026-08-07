"""Покрытие снимка и обнаружение дыр.

Ключевое различение: **`NO_MARKET` — это завершённое наблюдение, а не дыра.**
Самара — Казань не имеет прямого сообщения; добирать её каждую ночь значило бы
тратить обращения впустую и держать снимок вечно неполным.

Дырой является только техническое: результата нет, либо источник ответил
таймаутом, ограничением темпа, ошибкой схемы, транспортной ошибкой или
разомкнутой цепью.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from tmo.core.enums import AttemptOutcome, CollectionFamily, JobStatus
from tmo.db import models


@dataclass(slots=True)
class FamilyCoverage:
    family: str
    planned: int = 0
    completed: int = 0
    successful: int = 0
    partial: int = 0
    no_market: int = 0
    failed: int = 0
    missing: int = 0

    @property
    def completion(self) -> float:
        """Доля завершённых наблюдений. `NO_MARKET` входит в завершённые."""
        return round(self.completed / self.planned, 4) if self.planned else 0.0

    @property
    def data_share(self) -> float:
        """Доля наблюдений с данными среди завершённых.

        Защищает от снимка, где всё «завершено» как пустой рынок из-за
        сломанного разбора: покрытие 100 %, а цифр нет.
        """
        return round((self.successful + self.partial) / self.completed, 4) if self.completed else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "family": self.family,
            "planned": self.planned,
            "completed": self.completed,
            "successful": self.successful,
            "partial": self.partial,
            "no_market": self.no_market,
            "failed": self.failed,
            "missing": self.missing,
            "completion": self.completion,
            "data_share": self.data_share,
        }


@dataclass(slots=True)
class CoverageReport:
    snapshot_id: int
    total: FamilyCoverage
    by_family: dict[str, FamilyCoverage] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "total": self.total.as_dict(),
            "by_family": {name: item.as_dict() for name, item in self.by_family.items()},
        }


_STATUS_FIELD = {
    JobStatus.SUCCESS.value: "successful",
    JobStatus.PARTIAL.value: "partial",
    JobStatus.NO_MARKET.value: "no_market",
    JobStatus.FAILED.value: "failed",
}


def compute_coverage(session: Session, snapshot_id: int) -> CoverageReport:
    rows = session.execute(
        select(
            models.CollectionJob.family,
            models.CollectionJob.status,
            func.count(models.CollectionJob.id),
        )
        .where(models.CollectionJob.snapshot_id == snapshot_id)
        .group_by(models.CollectionJob.family, models.CollectionJob.status)
    ).all()

    by_family = {family.value: FamilyCoverage(family=family.value) for family in CollectionFamily}
    total = FamilyCoverage(family="TOTAL")

    for family, status, count in rows:
        bucket = by_family.setdefault(str(family), FamilyCoverage(family=str(family)))
        bucket.planned += count
        total.planned += count
        field_name = _STATUS_FIELD.get(str(status))
        if field_name is None:
            # PLANNED / DISPATCHED / RUNNING — наблюдение не завершилось.
            bucket.missing += count
            total.missing += count
            continue
        setattr(bucket, field_name, getattr(bucket, field_name) + count)
        setattr(total, field_name, getattr(total, field_name) + count)
        bucket.completed += count
        total.completed += count

    return CoverageReport(snapshot_id=snapshot_id, total=total, by_family=by_family)


def find_holes(session: Session, snapshot_id: int, *, limit: int | None = None) -> list[int]:
    """Идентификаторы наблюдений, подлежащих досбору.

    Пустой ответ источника сюда не попадает: «предложений нет» — это ответ о
    рынке. Попадает только техническое — включая наблюдения, до которых сбор
    вовсе не дошёл.
    """
    query = (
        select(models.CollectionJob.id)
        .where(
            models.CollectionJob.snapshot_id == snapshot_id,
            models.CollectionJob.status.in_(
                [
                    JobStatus.PLANNED.value,
                    JobStatus.DISPATCHED.value,
                    JobStatus.RUNNING.value,
                    JobStatus.FAILED.value,
                ]
            ),
        )
        .order_by(models.CollectionJob.id)
    )
    if limit:
        query = query.limit(limit)
    return list(session.scalars(query))


def source_results(session: Session, snapshot_id: int) -> list[dict[str, Any]]:
    """Свод по источникам: что именно они отвечали за снимок."""
    rows = session.execute(
        select(
            models.SourceAttempt.source_code,
            models.CollectionJob.family,
            models.SourceAttempt.outcome,
            func.count(models.SourceAttempt.id),
            func.sum(models.SourceAttempt.offers_parsed),
            func.sum(models.SourceAttempt.http_calls),
            func.min(models.SourceAttempt.fetched_at),
            func.max(models.SourceAttempt.fetched_at),
        )
        .join(models.CollectionJob, models.CollectionJob.id == models.SourceAttempt.collection_job_id)
        .where(models.SourceAttempt.snapshot_id == snapshot_id)
        .group_by(
            models.SourceAttempt.source_code,
            models.CollectionJob.family,
            models.SourceAttempt.outcome,
        )
    ).all()

    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for source_code, family, outcome, count, offers, calls, first, last in rows:
        key = (str(source_code), str(family))
        entry = grouped.setdefault(
            key,
            {
                "source_code": source_code,
                "family": family,
                "attempts": 0,
                "success": 0,
                "partial": 0,
                "no_market": 0,
                "failures_by_outcome": {},
                "offers_parsed": 0,
                "http_calls": 0,
                "first_fetched_at": None,
                "last_fetched_at": None,
            },
        )
        entry["attempts"] += count
        entry["offers_parsed"] += int(offers or 0)
        entry["http_calls"] += int(calls or 0)
        outcome_enum = AttemptOutcome(str(outcome))
        if outcome_enum is AttemptOutcome.SUCCESS:
            entry["success"] += count
        elif outcome_enum is AttemptOutcome.PARTIAL:
            entry["partial"] += count
        elif outcome_enum is AttemptOutcome.NO_MARKET:
            entry["no_market"] += count
        else:
            entry["failures_by_outcome"][outcome_enum.value] = (
                entry["failures_by_outcome"].get(outcome_enum.value, 0) + count
            )
        for field_name, value in (("first_fetched_at", first), ("last_fetched_at", last)):
            if value is None:
                continue
            current = entry[field_name]
            if current is None:
                entry[field_name] = value
            elif field_name == "first_fetched_at":
                entry[field_name] = min(current, value)
            else:
                entry[field_name] = max(current, value)
    return list(grouped.values())


def persist_source_results(session: Session, snapshot_id: int) -> int:
    """Материализует свод по источникам при финализации снимка."""
    session.query(models.SnapshotSourceResult).filter(
        models.SnapshotSourceResult.snapshot_id == snapshot_id
    ).delete(synchronize_session=False)

    latencies = _latency_percentiles(session, snapshot_id)
    rows = source_results(session, snapshot_id)
    for entry in rows:
        key = (entry["source_code"], entry["family"])
        p50, p95 = latencies.get(key, (None, None))
        session.add(
            models.SnapshotSourceResult(
                snapshot_id=snapshot_id,
                source_code=entry["source_code"],
                family=entry["family"],
                attempts=entry["attempts"],
                success=entry["success"],
                partial=entry["partial"],
                no_market=entry["no_market"],
                failures_by_outcome=entry["failures_by_outcome"],
                offers_parsed=entry["offers_parsed"],
                http_calls=entry["http_calls"],
                p50_latency_ms=p50,
                p95_latency_ms=p95,
                first_fetched_at=entry["first_fetched_at"],
                last_fetched_at=entry["last_fetched_at"],
            )
        )
    return len(rows)


def _latency_percentiles(
    session: Session, snapshot_id: int
) -> dict[tuple[str, str], tuple[int | None, int | None]]:
    """Перцентили считаются в приложении: они нужны и на SQLite тоже."""
    rows = session.execute(
        select(
            models.SourceAttempt.source_code,
            models.CollectionJob.family,
            models.SourceAttempt.latency_ms,
        )
        .join(models.CollectionJob, models.CollectionJob.id == models.SourceAttempt.collection_job_id)
        .where(
            models.SourceAttempt.snapshot_id == snapshot_id,
            models.SourceAttempt.latency_ms.is_not(None),
        )
    ).all()

    buckets: dict[tuple[str, str], list[int]] = {}
    for source_code, family, latency in rows:
        buckets.setdefault((str(source_code), str(family)), []).append(int(latency))

    result: dict[tuple[str, str], tuple[int | None, int | None]] = {}
    for key, values in buckets.items():
        values.sort()
        result[key] = (
            values[int(len(values) * 0.50)] if values else None,
            values[min(len(values) - 1, int(len(values) * 0.95))] if values else None,
        )
    return result
