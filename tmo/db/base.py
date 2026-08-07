"""Базовые типы SQLAlchemy.

PostgreSQL — source of truth. SQLite допускается только там, где не проверяется
поведение, специфичное для PostgreSQL, поэтому JSON-колонки объявлены с
вариантом JSONB: на PostgreSQL по ним строятся индексы, на SQLite это обычный
JSON.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, MetaData, Numeric, TypeDecorator
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase

from tmo.core.timeutil import to_utc

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

#: JSON с вариантом JSONB на PostgreSQL.
JSONType = JSON().with_variant(JSONB, "postgresql")

#: Деньги. 12 знаков до запятой хватает с запасом, 2 после — копейки.
Money = Numeric(14, 2)


class UtcDateTime(TypeDecorator):
    """``timestamptz``, который всегда отдаёт aware-время в UTC.

    SQLite теряет tzinfo, и наивная метка из базы, сравнённая с aware-меткой
    из кода, роняет расчёт свежести. Тип приводит обе стороны к UTC явно.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Any) -> Any:
        if value is None:
            return None
        if isinstance(value, datetime):
            return to_utc(value)
        return value

    def process_result_value(self, value: Any, dialect: Any) -> Any:
        if value is None:
            return None
        if isinstance(value, datetime):
            return to_utc(value)
        return value


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
