"""Исполнение сбора: план → сеть → запись.

Три фазы существуют ради одного правила: **сетевые операции не выполняются
внутри открытой транзакции**. Однажды сбор шёл внутри одной транзакции —
снимок создавался до обращений, коммит наступал после всех, — и ожидание
лимита темпа держало ``idle in transaction`` по шестнадцать минут, за которыми
вставала вся база.

Поэтому:

``plan``
    короткая транзакция: прочитать задания и превратить их в **значения**.
    Ни одного ORM-объекта наружу: ленивое поле отсоединённого объекта открыло
    бы новую транзакцию ровно там, где мы от неё уходим.

``execute``
    транзакции нет вовсе. Здесь живут HTTP, ожидание темпа и бюджет времени.

``persist``
    короткая транзакция: записать полученное.

Падение воркера во время второй фазы безопасно: терять нечего, кроме самих
обращений, а задания остаются в исходном состоянии и попадут в досбор.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from sqlalchemy import insert
from sqlalchemy import select as sa_select
from sqlalchemy import update as sa_update
from sqlalchemy.orm import Session

from tmo.catalog.registry import City, Source, city_registry, source_registry
from tmo.connectors.contracts import AirQuery, ConnectorResult, HotelQuery, Query, RailQuery
from tmo.connectors.registry import get_connector
from tmo.connectors.transport import TRANSPORT_POOL, TimeBudget
from tmo.core.config import Settings, get_settings
from tmo.core.enums import AttemptOutcome, CollectionFamily, JobStatus, NoMarketReason
from tmo.core.ids import idempotency_key
from tmo.core.logging import get_logger, log_context
from tmo.core.timeutil import now_utc
from tmo.db import models
from tmo.db.session import session_scope
from tmo.normalization.normalizer import normalize_batch
from tmo.storage.raw_store import RawStore

logger = get_logger(__name__)


# --------------------------------------------------------------------------- #
# Фаза 1: план
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class WorkItem:
    """Одно обращение: наблюдение × источник. Только значения, без ORM."""

    job_id: int
    snapshot_id: int
    snapshot_date: date
    job_key: str
    family: CollectionFamily
    source_code: str
    query: Query
    idempotency: str
    execution_scope: str


@dataclass(slots=True)
class AttemptRecord:
    """Результат обращения, готовый к записи."""

    item: WorkItem
    result: ConnectorResult
    normalized: list[Any] = field(default_factory=list)
    dropped_without_price: int = 0


def _city(code: str) -> City:
    return city_registry().get(code)


def build_query(job: models.CollectionJob) -> Query:
    """Превращает задание в запрос коннектора. Значения, а не ORM-объект."""
    if job.family == CollectionFamily.RAIL:
        origin, destination = _city(job.origin_code), _city(job.destination_code)
        return RailQuery(
            origin_code=origin.code,
            origin_name=origin.name,
            origin_rzd_code=origin.rzd_express_code,
            destination_code=destination.code,
            destination_name=destination.name,
            destination_rzd_code=destination.rzd_express_code,
            service_date=job.service_date,
            passengers=int(job.params.get("passengers", 1)),
        )
    if job.family == CollectionFamily.AIR:
        origin, destination = _city(job.origin_code), _city(job.destination_code)
        return AirQuery(
            origin_code=origin.code,
            origin_name=origin.name,
            origin_metro_code=origin.avia_metro_code,
            destination_code=destination.code,
            destination_name=destination.name,
            destination_metro_code=destination.avia_metro_code,
            departure_date=job.service_date,
            return_date=job.return_date,
            adults=int(job.params.get("passengers", 1)),
        )
    city = _city(job.city_code)
    return HotelQuery(
        city_code=city.code,
        city_name=city.name,
        check_in=job.check_in,
        check_out=job.check_out,
        stars=int(job.stars),
        adults=int(job.params.get("adults", 1)),
        rooms=int(job.params.get("rooms", 1)),
    )


def plan_batch(
    session: Session,
    job_ids: list[int],
    *,
    execution_scope: str = "PRIMARY",
    attempt_salt: str = "",
    source_codes: list[str] | None = None,
) -> list[WorkItem]:
    """Короткая транзакция: читает задания и раскладывает их по источникам."""
    jobs = list(
        session.scalars(
            sa_select(models.CollectionJob).where(models.CollectionJob.id.in_(job_ids))
        )
    )
    snapshot_dates = {
        row.id: row.snapshot_date
        for row in session.scalars(
            sa_select(models.MarketSnapshot).where(
                models.MarketSnapshot.id.in_({job.snapshot_id for job in jobs})
            )
        )
    }

    registry = source_registry()
    items: list[WorkItem] = []
    for job in jobs:
        family = CollectionFamily(job.family)
        sources = _sources_for(registry, family, source_codes)
        for source in sources:
            query = build_query(job)
            items.append(
                WorkItem(
                    job_id=job.id,
                    snapshot_id=job.snapshot_id,
                    snapshot_date=snapshot_dates[job.snapshot_id],
                    job_key=job.job_key,
                    family=family,
                    source_code=source.code,
                    query=query,
                    idempotency=idempotency_key(
                        snapshot_date=snapshot_dates[job.snapshot_id],
                        family=family.value,
                        job_key_value=job.job_key,
                        source_code=source.code,
                        execution_scope=execution_scope,
                        attempt_salt=attempt_salt,
                    ),
                    execution_scope=execution_scope,
                )
            )
    return items


def _sources_for(
    registry: Any, family: CollectionFamily, source_codes: list[str] | None
) -> list[Source]:
    if source_codes:
        return [registry.get(code) for code in source_codes if family in registry.get(code).families]
    return registry.for_family(family)


# --------------------------------------------------------------------------- #
# Фаза 2: сеть
# --------------------------------------------------------------------------- #


def execute_batch(
    items: list[WorkItem],
    *,
    budget: TimeBudget,
    settings: Settings | None = None,
    replay_mode: str | None = None,
) -> tuple[list[AttemptRecord], list[int]]:
    """Транзакции нет. Здесь и только здесь выполняются обращения к источникам.

    Возвращает записи попыток и идентификаторы наблюдений, до которых сбор
    **не дошёл**: при разомкнутой цепи остаток пачки не прожигается впустую.
    """
    settings = settings or get_settings()
    by_source: dict[str, list[WorkItem]] = {}
    for item in items:
        by_source.setdefault(item.source_code, []).append(item)

    records: list[AttemptRecord] = []
    untouched: list[int] = []
    for source_code, source_items in by_source.items():
        source = source_registry().get(source_code)
        connector = get_connector(source_code, replay_mode=replay_mode)
        # Число потоков берётся у регулятора, а не из реестра: реестр задаёт
        # потолок, а рабочую точку под ним источник подтверждает делом. Днём она
        # оседает на двух-трёх, ночью возвращается к потолку.
        governor = TRANSPORT_POOL.governor(
            source_code,
            source.concurrency,
            floor=settings.min_source_concurrency,
            growth_after=settings.concurrency_growth_after,
        )
        workers = max(1, min(governor.current, len(source_items)))

        # Цепь уже разомкнута — спрашивать бессмысленно и вредно: она остывает
        # 900 секунд, а пачка исполняется минуты. Прожигание остатка дало бы
        # сотни записей CIRCUIT_OPEN и заняло бы досбор той же пустотой.
        circuit = TRANSPORT_POOL.circuit(
            source_code, settings.circuit_failure_threshold, settings.circuit_reset_seconds
        )
        if circuit.is_open:
            logger.warning(
                "Пачка пропущена: цепь источника разомкнута",
                source=source_code,
                skipped=len(source_items),
                first_cause=circuit.first_cause,
            )
            untouched.extend(item.job_id for item in source_items)
            continue

        # Коннектор связывается умолчанием: замыкание над переменной цикла
        # однажды подставит чужой источник в чужую пачку.
        def _run(item: WorkItem, connector: Any = connector) -> AttemptRecord:
            with log_context(collection_job_id=item.job_id, snapshot_id=item.snapshot_id):
                if budget.exhausted:
                    return AttemptRecord(
                        item=item,
                        result=ConnectorResult(
                            source_code=item.source_code,
                            family=item.family,
                            outcome=AttemptOutcome.BUDGET_EXHAUSTED,
                            error_code="BUDGET_EXHAUSTED",
                            error_message="Мягкий бюджет пакета исчерпан до обращения",
                            requested_at=now_utc(),
                            fetched_at=now_utc(),
                        ),
                    )
                # Бюджет наблюдения не может превышать остаток бюджета пакета.
                item_budget = budget.child(min(budget.remaining, 120.0))
                result = connector.collect(item.query, item_budget)
                normalized, dropped = normalize_batch(
                    result.offers, item.query, source_code=item.source_code
                )
                return AttemptRecord(
                    item=item, result=result, normalized=normalized, dropped_without_price=dropped
                )

        if workers == 1:
            records.extend(_run(item) for item in source_items)
        else:
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix=source_code) as pool:
                records.extend(pool.map(_run, source_items))
    return records, untouched


# --------------------------------------------------------------------------- #
# Фаза 3: запись
# --------------------------------------------------------------------------- #


def persist_batch(
    session: Session,
    records: list[AttemptRecord],
    *,
    raw_store: RawStore | None = None,
) -> dict[str, int]:
    """Короткая транзакция: записывает попытки, сырые ответы и Offers."""
    raw_store = raw_store or RawStore()
    stats = {"attempts": 0, "raw": 0, "offers": 0, "jobs_updated": 0}
    offer_rows: list[dict[str, Any]] = []

    job_ids = sorted({record.item.job_id for record in records})
    jobs = {
        job.id: job
        for job in session.scalars(
            sa_select(models.CollectionJob).where(models.CollectionJob.id.in_(job_ids))
        )
    }
    existing_keys = set(
        session.scalars(
            sa_select(models.SourceAttempt.idempotency_key).where(
                models.SourceAttempt.idempotency_key.in_(
                    [record.item.idempotency for record in records]
                )
            )
        )
    )

    for record in records:
        item = record.item
        if item.idempotency in existing_keys:
            # Повторное исполнение той же области — не повод переписать
            # предыдущий результат. Принудительный повтор приходит с иной
            # солью и потому имеет другой ключ.
            continue
        existing_keys.add(item.idempotency)
        result = record.result
        attempt = models.SourceAttempt(
            snapshot_id=item.snapshot_id,
            collection_job_id=item.job_id,
            source_code=item.source_code,
            execution_scope=item.execution_scope,
            idempotency_key=item.idempotency,
            attempt_no=(jobs[item.job_id].retry_count if item.job_id in jobs else 0) + 1,
            outcome=result.outcome,
            no_market_reason=result.no_market_reason.value if result.no_market_reason else None,
            error_code=result.error_code,
            error_message=result.error_message,
            requested_at=result.requested_at or now_utc(),
            fetched_at=result.fetched_at,
            latency_ms=result.latency_ms,
            http_calls=result.http_calls,
            offers_parsed=len(record.normalized),
            is_partial=result.is_partial,
            partial_reason=result.partial_reason,
            pages_read=result.pages_read,
            total_matched=result.total_matched,
            connector_version=result.connector_version,
            source_tool_version=result.source_tool_version,
            diagnostics=_json_safe(result.diagnostics),
        )
        session.add(attempt)
        session.flush()
        stats["attempts"] += 1

        raw_ids: dict[int, int] = {}
        for artifact in result.raw_artifacts:
            stored = raw_store.store(
                artifact.payload,
                snapshot_date=item.snapshot_date,
                source_code=item.source_code,
                family=item.family.value,
                job_key=item.job_key,
                page=artifact.page_number,
            )
            raw = models.RawResponse(
                snapshot_id=item.snapshot_id,
                source_attempt_id=attempt.id,
                source_code=item.source_code,
                endpoint=artifact.endpoint[:512],
                request_params=_json_safe(artifact.request_params),
                requested_at=artifact.requested_at,
                fetched_at=artifact.fetched_at,
                http_status=artifact.http_status,
                content_type=artifact.content_type,
                storage_ref=stored.storage_ref,
                payload_bytes=stored.payload_bytes,
                payload_sha256=stored.payload_sha256,
                page_number=artifact.page_number,
                pagination=_json_safe(artifact.pagination),
                is_partial=artifact.is_partial,
                error_metadata=_json_safe(artifact.error_metadata),
            )
            session.add(raw)
            session.flush()
            raw_ids[artifact.page_number] = raw.id
            stats["raw"] += 1

        # Страницу артефакта знает нормализованное предложение через свой
        # индекс: связь Offer → RawResponse нужна, чтобы открыть исходный
        # ответ, из которого разобрана строка.
        default_raw = next(iter(raw_ids.values()), None)
        fetched_at = result.fetched_at or now_utc()
        for normalized in record.normalized:
            offer_rows.append(
                {
                    "snapshot_id": item.snapshot_id,
                    "collection_job_id": item.job_id,
                    "source_attempt_id": attempt.id,
                    "raw_response_id": raw_ids.get(normalized.raw_page, default_raw),
                    "source_code": item.source_code,
                    "kind": normalized.kind,
                    "schema_version": normalized.schema_version,
                    "fetched_at": fetched_at,
                    "currency": normalized.currency,
                    "price": normalized.price,
                    "source_price": normalized.source_price,
                    "price_basis": normalized.price_basis,
                    "origin_code": normalized.origin_code,
                    "destination_code": normalized.destination_code,
                    "city_code": normalized.city_code,
                    "departure_at": normalized.departure_at,
                    "arrival_at": normalized.arrival_at,
                    "return_departure_at": normalized.return_departure_at,
                    "return_arrival_at": normalized.return_arrival_at,
                    "check_in": normalized.check_in,
                    "check_out": normalized.check_out,
                    "nights": normalized.nights,
                    "transport_attributes": _json_safe(normalized.transport_attributes),
                    "property_attributes": _json_safe(normalized.property_attributes),
                    "source_metadata": _json_safe(normalized.source_metadata),
                    "fingerprint": normalized.fingerprint,
                    "equivalence_key": normalized.equivalence_key,
                    "deeplink": (normalized.deeplink or None),
                    "validation_flags": list(normalized.validation_flags),
                }
            )
            stats["offers"] += 1

    # Предложения пишутся пачкой. При построчной вставке через ORM полмиллиона
    # строк в сутки превращают запись в узкое место конвейера: замер показал
    # трёхкратную разницу на том же объёме.
    if offer_rows:
        session.execute(insert(models.Offer), offer_rows)

    session.flush()
    stats["jobs_updated"] = _refresh_jobs(session, job_ids)
    return stats


def _refresh_jobs(session: Session, job_ids: list[int]) -> int:
    """Пересчитывает состояние наблюдений по их попыткам.

    Состояние выводится из попыток, а не записывается по ходу дела: после
    досбора у наблюдения несколько попыток, и итог определяется всей их
    совокупностью, а не последней.
    """
    updated = 0
    jobs = list(
        session.scalars(sa_select(models.CollectionJob).where(models.CollectionJob.id.in_(job_ids)))
    )
    for job in jobs:
        attempts = list(
            session.scalars(
                sa_select(models.SourceAttempt).where(
                    models.SourceAttempt.collection_job_id == job.id
                )
            )
        )
        if not attempts:
            continue
        offers_count = _count_offers(session, job.id)

        # Лучший исход по каждому источнику: повторная попытка после сбоя
        # закрывает дыру, и наблюдение не должно оставаться проваленным.
        best: dict[str, models.SourceAttempt] = {}
        for attempt in attempts:
            current = best.get(attempt.source_code)
            if current is None or _outcome_rank(attempt.outcome) > _outcome_rank(current.outcome):
                best[attempt.source_code] = attempt

        outcomes = [attempt.outcome for attempt in best.values()]
        job.sources_planned = len(best)
        job.sources_succeeded = sum(1 for outcome in outcomes if AttemptOutcome(outcome).is_ok)
        job.offers_count = offers_count
        job.is_partial = any(attempt.is_partial for attempt in best.values())
        job.retry_count = max(0, len(attempts) - len(best))

        fetched = [a.fetched_at for a in best.values() if a.fetched_at]
        job.fetched_at = min(fetched) if fetched else None

        if any(AttemptOutcome(o) in (AttemptOutcome.SUCCESS, AttemptOutcome.PARTIAL) for o in outcomes):
            job.status = JobStatus.PARTIAL if job.is_partial else JobStatus.SUCCESS
            job.no_market_reason = None
        elif all(AttemptOutcome(o) is AttemptOutcome.NO_MARKET for o in outcomes):
            # Рынка нет — это ответ о рынке, а не сбой. Наблюдение закрыто.
            job.status = JobStatus.NO_MARKET
            reasons = {a.no_market_reason for a in best.values() if a.no_market_reason}
            job.no_market_reason = (
                NoMarketReason.NO_DIRECT_SERVICE.value
                if NoMarketReason.NO_DIRECT_SERVICE.value in reasons
                else (next(iter(reasons), NoMarketReason.EMPTY_RESPONSE.value))
            )
        else:
            job.status = JobStatus.FAILED
        job.completed_at = now_utc()
        updated += 1
    return updated


def _count_offers(session: Session, job_id: int) -> int:
    from sqlalchemy import func

    return int(
        session.scalar(
            sa_select(func.count(models.Offer.id)).where(models.Offer.collection_job_id == job_id)
        )
        or 0
    )


_OUTCOME_RANK = {
    AttemptOutcome.SUCCESS: 5,
    AttemptOutcome.PARTIAL: 4,
    AttemptOutcome.NO_MARKET: 3,
    AttemptOutcome.RATE_LIMITED: 2,
    AttemptOutcome.TIMEOUT: 2,
    AttemptOutcome.CIRCUIT_OPEN: 2,
    AttemptOutcome.BUDGET_EXHAUSTED: 2,
    AttemptOutcome.TRANSPORT_ERROR: 1,
    AttemptOutcome.SCHEMA_ERROR: 1,
    AttemptOutcome.FAILED: 0,
}


def _outcome_rank(outcome: str) -> int:
    return _OUTCOME_RANK.get(AttemptOutcome(outcome), 0)


def _json_safe(value: Any) -> Any:
    """Приводит структуру к JSON-совместимой без потери информации."""
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


# --------------------------------------------------------------------------- #
# Пакет целиком
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class BatchReport:
    planned_items: int
    attempts: int
    offers: int
    raw_responses: int
    budget_exhausted: bool
    elapsed_seconds: float
    #: Наблюдения, до которых сбор не дошёл: цепь источника была разомкнута.
    #: Они возвращены в план и попадут в досбор, когда цепь остынет.
    skipped_untouched: int = 0
    outcomes: dict[str, int] = field(default_factory=dict)


def run_batch(
    job_ids: list[int],
    *,
    execution_scope: str = "PRIMARY",
    attempt_salt: str = "",
    source_codes: list[str] | None = None,
    settings: Settings | None = None,
    replay_mode: str | None = None,
    soft_budget_seconds: float | None = None,
) -> BatchReport:
    """Полный цикл пакета: план → сеть → запись."""
    settings = settings or get_settings()
    budget = TimeBudget(
        total_seconds=float(soft_budget_seconds or settings.batch_soft_budget_seconds)
    )

    with session_scope() as session:
        items = plan_batch(
            session,
            job_ids,
            execution_scope=execution_scope,
            attempt_salt=attempt_salt,
            source_codes=source_codes,
        )
        for job in session.scalars(
            sa_select(models.CollectionJob).where(models.CollectionJob.id.in_(job_ids))
        ):
            # Отметка «выполняется» ставится только тому, что ещё предстоит
            # собрать. Наблюдение с терминальным статусом уже описано попыткой,
            # и переводить его в RUNNING значит стирать результат: пачка,
            # упёршаяся в разомкнутую цепь, вернёт его в план как несобранное.
            if JobStatus(job.status).is_collected:
                continue
            if job.first_dispatched_at is None:
                job.first_dispatched_at = now_utc()
            job.status = JobStatus.RUNNING

    records, untouched = execute_batch(
        items, budget=budget, settings=settings, replay_mode=replay_mode
    )

    with session_scope() as session:
        stats = persist_batch(session, records)
        if untouched:
            # Наблюдение, до которого сбор не дошёл, обязано вернуться в план,
            # а не остаться «выполняющимся»: иначе оно выглядит обработанным и
            # не попадает ни в дыры, ни в досбор.
            #
            # Условие по статусу — вторая линия обороны к проверке выше. В ночь
            # 08.08.2026 этот запрос вернул в план 6 116 уже собранных
            # наблюдений проживания и обрушил покрытие с 98 % до 45 %: он
            # обновлял по одному лишь списку идентификаторов, не глядя, что
            # наблюдение давно закрыто успехом.
            session.execute(
                sa_update(models.CollectionJob)
                .where(
                    models.CollectionJob.id.in_(untouched),
                    models.CollectionJob.status.in_(
                        [JobStatus.RUNNING.value, JobStatus.DISPATCHED.value]
                    ),
                )
                .values(status=JobStatus.PLANNED.value)
            )

    outcomes: dict[str, int] = {}
    for record in records:
        key = str(record.result.outcome)
        outcomes[key] = outcomes.get(key, 0) + 1

    return BatchReport(
        skipped_untouched=len(untouched),
        planned_items=len(items),
        attempts=stats["attempts"],
        offers=stats["offers"],
        raw_responses=stats["raw"],
        budget_exhausted=budget.exhausted,
        elapsed_seconds=round(budget.elapsed, 2),
        outcomes=outcomes,
    )
