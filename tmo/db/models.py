"""Модель данных.

Три уровня, которые не смешиваются:

1. **Наблюдение** — что рынок показал: ``MarketSnapshot`` → ``CollectionJob`` →
   ``SourceAttempt`` → ``RawResponse`` → ``Offer``. Неизменяемо после закрытия
   снимка.
2. **Расчёт** — что методика из этого вывела: ``CalculationRun`` →
   ``CalculatedMetric`` → ``MetricOfferLink``. Новый расчёт создаёт новый Run и
   не трогает старый.
3. **Витрина** — что видит руководитель: ``TripCostRow`` и индексы над
   метриками.

Решение об исключении предложения — часть расчёта, а не наблюдения. Поэтому
``exclusion_reason`` лежит в ``MetricOfferLink``, а не в ``Offer``: при смене
методики то же предложение может попасть в выборку.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from tmo.core.enums import (
    AttemptOutcome,
    CollectionFamily,
    ConfidenceLevel,
    JobStatus,
    MetricType,
    SnapshotStatus,
)
from tmo.db.base import Base, JSONType, Money, UtcDateTime

_ENUM_LEN = 32


# --------------------------------------------------------------------------- #
# Справочники (реплика YAML в базе — чтобы витрина и экспорт были самодостаточны)
# --------------------------------------------------------------------------- #


class City(Base):
    __tablename__ = "cities"

    code: Mapped[str] = mapped_column(String(8), primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    name_en: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="Europe/Moscow")
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    attributes: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)


class Source(Base):
    __tablename__ = "sources"

    code: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    protocol: Mapped[str] = mapped_column(String(16), nullable=False)
    endpoint: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    is_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    attributes: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)


class MethodologyProfileRecord(Base):
    """Зарегистрированная версия методики.

    ``content_hash`` фиксирует содержимое файла на момент первого применения.
    Расхождение хеша означает, что активную версию изменили на месте — это
    запрещено (SCOPE-R R2) и должно быть замечено, а не унаследовано молча.
    """

    __tablename__ = "methodology_profiles"

    version: Mapped[str] = mapped_column(String(64), primary_key=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    content: Mapped[dict] = mapped_column(JSONType, nullable=False)
    registered_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)


# --------------------------------------------------------------------------- #
# Наблюдение
# --------------------------------------------------------------------------- #


class MarketSnapshot(Base):
    """Состояние наблюдаемого рынка за конкретный календарный день."""

    __tablename__ = "market_snapshots"
    __table_args__ = (
        UniqueConstraint("snapshot_date", "attempt_no", name="uq_snapshot_date_attempt"),
        Index("ix_market_snapshots_status_date", "status", "snapshot_date"),
    )

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True)
    snapshot_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    #: Повторный сбор за ту же дату создаёт новый снимок, а не переписывает
    #: старый: наблюдение неизменяемо.
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[SnapshotStatus] = mapped_column(
        String(_ENUM_LEN), nullable=False, default=SnapshotStatus.PLANNING
    )
    horizon_days: Mapped[int] = mapped_column(Integer, nullable=False, default=30)
    #: Снимок, собранный воспроизведением записанных ответов. Витриной рынка
    #: не является ни при каких настройках.
    is_synthetic: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    started_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    primary_collection_finished_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    recovery_finished_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    published_at: Mapped[datetime | None] = mapped_column(UtcDateTime)

    coverage_total: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    coverage_rail: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    coverage_air: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    coverage_hotel: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    quality_summary: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    #: Почему снимок DEGRADED или FAILED. Пустой список у READY.
    publication_notes: Mapped[list] = mapped_column(JSONType, nullable=False, default=list)

    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)

    plan: Mapped[CollectionPlan | None] = relationship(
        back_populates="snapshot", uselist=False, cascade="all, delete-orphan"
    )
    jobs: Mapped[list[CollectionJob]] = relationship(
        back_populates="snapshot", cascade="all, delete-orphan"
    )


class CollectionPlan(Base):
    """Полный ожидаемый набор наблюдений снимка. Строится детерминированно."""

    __tablename__ = "collection_plans"

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True)
    snapshot_id: Mapped[int] = mapped_column(
        ForeignKey("market_snapshots.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    planned: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    successful: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    partial: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    no_market: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    missing: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    by_family: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    #: Отпечаток плана: одинаковые вход и горизонт дают одинаковый план.
    plan_digest: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    built_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)

    snapshot: Mapped[MarketSnapshot] = relationship(back_populates="plan")


class CollectionJob(Base):
    """Логическое наблюдение рынка, независимое от конкретного коннектора."""

    __tablename__ = "collection_jobs"
    __table_args__ = (
        UniqueConstraint("snapshot_id", "job_key", name="uq_collection_jobs_snapshot_job_key"),
        Index("ix_collection_jobs_snapshot_family_status", "snapshot_id", "family", "status"),
        Index("ix_collection_jobs_route", "family", "origin_code", "destination_code", "service_date"),
        Index("ix_collection_jobs_hotel", "family", "city_code", "check_in", "stars"),
        Index("ix_collection_jobs_series", "series_key", "snapshot_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True)
    snapshot_id: Mapped[int] = mapped_column(
        ForeignKey("market_snapshots.id", ondelete="CASCADE"), nullable=False, index=True
    )
    family: Mapped[CollectionFamily] = mapped_column(String(_ENUM_LEN), nullable=False)
    job_key: Mapped[str] = mapped_column(String(64), nullable=False)
    #: Тот же логический ряд в разных снимках: даты заменены смещением от D.
    series_key: Mapped[str] = mapped_column(String(64), nullable=False)

    # Параметры наблюдения. Плоские колонки, а не только JSON: по ним строятся
    # индексы витрины, и запрос «медиана Москва → Сочи на 20 августа» не должен
    # разбирать JSON на миллионе строк.
    origin_code: Mapped[str | None] = mapped_column(String(8))
    destination_code: Mapped[str | None] = mapped_column(String(8))
    city_code: Mapped[str | None] = mapped_column(String(8))
    service_date: Mapped[date | None] = mapped_column(Date)
    return_date: Mapped[date | None] = mapped_column(Date)
    check_in: Mapped[date | None] = mapped_column(Date)
    check_out: Mapped[date | None] = mapped_column(Date)
    stars: Mapped[int | None] = mapped_column(Integer)
    #: Смещение служебной даты от даты снимка: 1..30. Ось исторических графиков.
    day_offset: Mapped[int | None] = mapped_column(Integer)
    nights: Mapped[int | None] = mapped_column(Integer)
    params: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)

    status: Mapped[JobStatus] = mapped_column(
        String(_ENUM_LEN), nullable=False, default=JobStatus.PLANNED
    )
    no_market_reason: Mapped[str | None] = mapped_column(String(_ENUM_LEN))
    is_partial: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    offers_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    sources_planned: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    sources_succeeded: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    first_dispatched_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    completed_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    #: Момент фактического получения данных, самый ранний по источникам.
    #: Свежесть считается отсюда, а не от snapshot_date.
    fetched_at: Mapped[datetime | None] = mapped_column(UtcDateTime)

    snapshot: Mapped[MarketSnapshot] = relationship(back_populates="jobs")
    attempts: Mapped[list[SourceAttempt]] = relationship(
        back_populates="job", cascade="all, delete-orphan"
    )


class SourceAttempt(Base):
    """Одно обращение к одному источнику по одному наблюдению.

    Конечная запись обязательна для каждой попытки: молчаливый пропуск
    источника запрещён (SCOPE-R P1.4).
    """

    __tablename__ = "source_attempts"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_source_attempts_idempotency_key"),
        Index("ix_source_attempts_job_source", "collection_job_id", "source_code"),
        Index("ix_source_attempts_snapshot_outcome", "snapshot_id", "source_code", "outcome"),
        Index("ix_source_attempts_fetched_at", "fetched_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True)
    snapshot_id: Mapped[int] = mapped_column(
        ForeignKey("market_snapshots.id", ondelete="CASCADE"), nullable=False, index=True
    )
    collection_job_id: Mapped[int] = mapped_column(
        ForeignKey("collection_jobs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_code: Mapped[str] = mapped_column(String(32), nullable=False)
    #: Плановый сбор / досбор / ручной повтор. Входит в ключ идемпотентности.
    execution_scope: Mapped[str] = mapped_column(String(24), nullable=False, default="PRIMARY")
    idempotency_key: Mapped[str] = mapped_column(String(64), nullable=False)
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    outcome: Mapped[AttemptOutcome] = mapped_column(String(_ENUM_LEN), nullable=False)
    no_market_reason: Mapped[str | None] = mapped_column(String(_ENUM_LEN))
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(Text)

    requested_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    #: Фактическое время получения ответа. Основа расчёта свежести.
    fetched_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    http_calls: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    offers_parsed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: Выдача обрезана: обращение состоялось, предложения настоящие, но выборка
    #: неполна и медиана смещена неизвестно куда.
    is_partial: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    partial_reason: Mapped[str | None] = mapped_column(String(64))
    pages_read: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: Сколько объектов источник насчитал всего, если сообщил. None означает
    #: «не сообщил» и отличается от нуля.
    total_matched: Mapped[int | None] = mapped_column(Integer)
    connector_version: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    source_tool_version: Mapped[str | None] = mapped_column(String(64))
    diagnostics: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)

    job: Mapped[CollectionJob] = relationship(back_populates="attempts")
    raw_responses: Mapped[list[RawResponse]] = relationship(
        back_populates="attempt", cascade="all, delete-orphan"
    )


class RawResponse(Base):
    """Метаданные исходного ответа источника. Тело лежит в raw storage.

    Неизменяем. Хранится дольше расчётов: без него нельзя воспроизвести
    разбор и объяснить старую цифру.
    """

    __tablename__ = "raw_responses"
    __table_args__ = (
        Index("ix_raw_responses_snapshot_source", "snapshot_id", "source_code"),
        Index("ix_raw_responses_fetched_at", "fetched_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True)
    snapshot_id: Mapped[int] = mapped_column(
        ForeignKey("market_snapshots.id", ondelete="CASCADE"), nullable=False
    )
    source_attempt_id: Mapped[int] = mapped_column(
        ForeignKey("source_attempts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_code: Mapped[str] = mapped_column(String(32), nullable=False)
    endpoint: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    request_params: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    requested_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    http_status: Mapped[int | None] = mapped_column(Integer)
    content_type: Mapped[str] = mapped_column(String(64), nullable=False, default="application/json")
    #: Путь в raw storage. Тело не лежит в базе: сотни тысяч ответов в сутки.
    storage_ref: Mapped[str] = mapped_column(String(512), nullable=False)
    payload_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    payload_sha256: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    page_number: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    pagination: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    is_partial: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    error_metadata: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)

    attempt: Mapped[SourceAttempt] = relationship(back_populates="raw_responses")


class Offer(Base):
    """Нормализованное предложение рынка.

    Здесь нет решения о включении в расчёт: оно принимается методикой и живёт
    в ``MetricOfferLink``. Одно и то же предложение при другой методике может
    попасть в выборку.
    """

    __tablename__ = "offers"
    __table_args__ = (
        Index("ix_offers_job", "collection_job_id"),
        Index("ix_offers_snapshot_kind", "snapshot_id", "kind"),
        # Внешний ключ без индекса делает удаление квадратичным: PostgreSQL
        # строит индексы только под первичные и уникальные ключи.
        Index("ix_offers_raw_response_id", "raw_response_id"),
        Index("ix_offers_equivalence", "collection_job_id", "source_code", "equivalence_key"),
        CheckConstraint("price > 0", name="offer_price_positive"),
    )

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True)
    snapshot_id: Mapped[int] = mapped_column(
        ForeignKey("market_snapshots.id", ondelete="CASCADE"), nullable=False
    )
    collection_job_id: Mapped[int] = mapped_column(
        ForeignKey("collection_jobs.id", ondelete="CASCADE"), nullable=False
    )
    source_attempt_id: Mapped[int] = mapped_column(
        ForeignKey("source_attempts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    raw_response_id: Mapped[int | None] = mapped_column(
        ForeignKey("raw_responses.id", ondelete="SET NULL")
    )
    source_code: Mapped[str] = mapped_column(String(32), nullable=False)
    kind: Mapped[str] = mapped_column(String(_ENUM_LEN), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(16), nullable=False, default="2.0")

    fetched_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="RUB")
    #: Цена, приведённая к единице методики: за одного пассажира за плечо (ЖД),
    #: за одного пассажира круговой (авиа), за номер за весь период (отель).
    price: Mapped[Decimal] = mapped_column(Money, nullable=False)
    #: Что источник прислал до приведения. Без него разрыв между источниками
    #: нельзя объяснить, а только заметить.
    source_price: Mapped[Decimal | None] = mapped_column(Money)
    price_basis: Mapped[str] = mapped_column(String(_ENUM_LEN), nullable=False)

    origin_code: Mapped[str | None] = mapped_column(String(8))
    destination_code: Mapped[str | None] = mapped_column(String(8))
    city_code: Mapped[str | None] = mapped_column(String(8))
    departure_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    arrival_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    return_departure_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    return_arrival_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    check_in: Mapped[date | None] = mapped_column(Date)
    check_out: Mapped[date | None] = mapped_column(Date)
    nights: Mapped[int | None] = mapped_column(Integer)

    #: Транспортные атрибуты: перевозчик, номер рейса/поезда, тип вагона,
    #: сервисный класс, число сегментов, признак прямого.
    transport_attributes: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    #: Атрибуты объекта размещения: название, звёзды, тип, категория номера.
    property_attributes: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    #: Поля источника, не входящие в бизнес-модель. В движок не попадают.
    source_metadata: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)

    #: Отпечаток строки источника: различает тарифные варианты одного объекта.
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    #: Ключ физического объекта рынка: рейс, поезд+класс, объект размещения.
    #: По нему схлопывается тарифная сетка.
    equivalence_key: Mapped[str] = mapped_column(String(64), nullable=False)
    #: Ссылка для ручной проверки цены за полминуты.
    deeplink: Mapped[str | None] = mapped_column(String(1024))
    #: Нарушения валидации, обнаруженные при нормализации. Не удаляют
    #: предложение — методика решит, что с ним делать.
    validation_flags: Mapped[list] = mapped_column(JSONType, nullable=False, default=list)


class SnapshotSourceResult(Base):
    """Свод по источнику за снимок. Заполняется при финализации."""

    __tablename__ = "snapshot_source_results"
    __table_args__ = (
        UniqueConstraint("snapshot_id", "source_code", "family", name="uq_snapshot_source_family"),
    )

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True)
    snapshot_id: Mapped[int] = mapped_column(
        ForeignKey("market_snapshots.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_code: Mapped[str] = mapped_column(String(32), nullable=False)
    family: Mapped[CollectionFamily] = mapped_column(String(_ENUM_LEN), nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    success: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    partial: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    no_market: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failures_by_outcome: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    offers_parsed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    http_calls: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    p50_latency_ms: Mapped[int | None] = mapped_column(Integer)
    p95_latency_ms: Mapped[int | None] = mapped_column(Integer)
    first_fetched_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    last_fetched_at: Mapped[datetime | None] = mapped_column(UtcDateTime)


# --------------------------------------------------------------------------- #
# Расчёт
# --------------------------------------------------------------------------- #


class CalculationRun(Base):
    """Применение одной версии методики к одному снимку. Неизменяем."""

    __tablename__ = "calculation_runs"
    __table_args__ = (
        Index("ix_calculation_runs_snapshot_active", "snapshot_id", "is_active"),
    )

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True)
    snapshot_id: Mapped[int] = mapped_column(
        ForeignKey("market_snapshots.id", ondelete="CASCADE"), nullable=False, index=True
    )
    methodology_version: Mapped[str] = mapped_column(
        ForeignKey("methodology_profiles.version"), nullable=False
    )
    #: Активный расчёт снимка ровно один. Старые остаются для сравнения версий.
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    started_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    metrics_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    offers_considered: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    offers_included: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    gate_results: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    notes: Mapped[str] = mapped_column(Text, nullable=False, default="")


class CalculatedMetric(Base):
    """Опубликованная цифра. Каждая раскрывается до исходных предложений."""

    __tablename__ = "calculated_metrics"
    __table_args__ = (
        UniqueConstraint(
            "calculation_run_id", "collection_job_id", "metric_type",
            name="uq_metric_run_job_type",
        ),
        Index("ix_metrics_route", "calculation_run_id", "metric_type", "origin_code",
              "destination_code", "service_date"),
        Index("ix_metrics_hotel", "calculation_run_id", "metric_type", "city_code",
              "check_in", "stars"),
        Index("ix_metrics_series", "series_key", "snapshot_id"),
        Index("ix_metrics_snapshot_type", "snapshot_id", "metric_type"),
    )

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True)
    calculation_run_id: Mapped[int] = mapped_column(
        ForeignKey("calculation_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    snapshot_id: Mapped[int] = mapped_column(
        ForeignKey("market_snapshots.id", ondelete="CASCADE"), nullable=False
    )
    collection_job_id: Mapped[int] = mapped_column(
        ForeignKey("collection_jobs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    metric_type: Mapped[MetricType] = mapped_column(String(_ENUM_LEN), nullable=False)
    series_key: Mapped[str] = mapped_column(String(64), nullable=False)

    origin_code: Mapped[str | None] = mapped_column(String(8))
    destination_code: Mapped[str | None] = mapped_column(String(8))
    city_code: Mapped[str | None] = mapped_column(String(8))
    service_date: Mapped[date | None] = mapped_column(Date)
    return_date: Mapped[date | None] = mapped_column(Date)
    check_in: Mapped[date | None] = mapped_column(Date)
    check_out: Mapped[date | None] = mapped_column(Date)
    stars: Mapped[int | None] = mapped_column(Integer)
    day_offset: Mapped[int | None] = mapped_column(Integer)
    nights: Mapped[int | None] = mapped_column(Integer)

    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="RUB")
    median_price: Mapped[Decimal | None] = mapped_column(Money)
    min_price: Mapped[Decimal | None] = mapped_column(Money)
    max_price: Mapped[Decimal | None] = mapped_column(Money)
    p25_price: Mapped[Decimal | None] = mapped_column(Money)
    p75_price: Mapped[Decimal | None] = mapped_column(Money)

    offers_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    offers_excluded: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    sources_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    source_coverage: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    quality_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    confidence_level: Mapped[ConfidenceLevel] = mapped_column(String(_ENUM_LEN), nullable=False)
    is_partial: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    #: Нет рынка — это ответ о рынке, а не отсутствие метрики. Метрика есть,
    #: цены в ней нет, и причина указана.
    is_no_market: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    no_market_reason: Mapped[str | None] = mapped_column(String(_ENUM_LEN))
    warning_codes: Mapped[list] = mapped_column(JSONType, nullable=False, default=list)
    #: Медиана по каждому источнику отдельно: расхождение должно быть видно.
    per_source: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    fetched_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    computed_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)


class MetricOfferLink(Base):
    """Связь метрики с предложением. Исключённые связи сохраняются с причиной."""

    __tablename__ = "metric_offer_links"
    __table_args__ = (
        UniqueConstraint("metric_id", "offer_id", name="uq_metric_offer"),
        Index("ix_metric_offer_links_metric_included", "metric_id", "is_included"),
        Index("ix_metric_offer_links_offer", "offer_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True)
    metric_id: Mapped[int] = mapped_column(
        ForeignKey("calculated_metrics.id", ondelete="CASCADE"), nullable=False
    )
    offer_id: Mapped[int] = mapped_column(ForeignKey("offers.id", ondelete="CASCADE"), nullable=False)
    is_included: Mapped[bool] = mapped_column(Boolean, nullable=False)
    #: Обязателен для каждого исключённого предложения.
    exclusion_reason: Mapped[str | None] = mapped_column(String(_ENUM_LEN))
    exclusion_detail: Mapped[str | None] = mapped_column(String(255))
    #: Цена, которой предложение участвовало в расчёте.
    contributed_price: Mapped[Decimal | None] = mapped_column(Money)


class TripCostRow(Base):
    """Витрина блока «Куда ехать».

    Расчётная стоимость поездки: сумма отдельно наблюдавшихся составляющих.
    Пакетным туром не является и так не подписывается.
    """

    __tablename__ = "trip_cost_mart"
    __table_args__ = (
        UniqueConstraint(
            "calculation_run_id", "origin_code", "destination_code",
            "departure_date", "return_date", "transport_mode", "stars",
            name="uq_trip_cost_row",
        ),
        Index("ix_trip_cost_lookup", "calculation_run_id", "origin_code",
              "departure_date", "return_date", "transport_mode", "stars"),
    )

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True)
    calculation_run_id: Mapped[int] = mapped_column(
        ForeignKey("calculation_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    snapshot_id: Mapped[int] = mapped_column(
        ForeignKey("market_snapshots.id", ondelete="CASCADE"), nullable=False
    )
    origin_code: Mapped[str] = mapped_column(String(8), nullable=False)
    destination_code: Mapped[str] = mapped_column(String(8), nullable=False)
    departure_date: Mapped[date] = mapped_column(Date, nullable=False)
    return_date: Mapped[date] = mapped_column(Date, nullable=False)
    transport_mode: Mapped[str] = mapped_column(String(8), nullable=False)
    stars: Mapped[int] = mapped_column(Integer, nullable=False)
    nights: Mapped[int] = mapped_column(Integer, nullable=False)

    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="RUB")
    transport_median: Mapped[Decimal | None] = mapped_column(Money)
    transport_min: Mapped[Decimal | None] = mapped_column(Money)
    accommodation_median: Mapped[Decimal | None] = mapped_column(Money)
    accommodation_min: Mapped[Decimal | None] = mapped_column(Money)
    total_median: Mapped[Decimal | None] = mapped_column(Money)
    total_min: Mapped[Decimal | None] = mapped_column(Money)

    #: Идентификаторы метрик-составляющих: провенанс поездки ведёт через них.
    transport_metric_ids: Mapped[list] = mapped_column(JSONType, nullable=False, default=list)
    accommodation_metric_id: Mapped[int | None] = mapped_column(BigInteger().with_variant(Integer, "sqlite"))
    offers_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    sources_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    quality_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    confidence_level: Mapped[ConfidenceLevel] = mapped_column(String(_ENUM_LEN), nullable=False)
    is_partial: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_complete: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    warning_codes: Mapped[list] = mapped_column(JSONType, nullable=False, default=list)
    #: Чего не хватило, если поездка неполная. Пустая клетка должна объяснять
    #: себя, а не просто отсутствовать.
    missing_components: Mapped[list] = mapped_column(JSONType, nullable=False, default=list)
