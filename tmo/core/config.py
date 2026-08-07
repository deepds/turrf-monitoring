"""Настройки приложения.

Здесь только инфраструктура: адреса, лимиты процесса, таймауты транспорта.
Бизнес-пороги (coverage, confidence, outlier) живут в версионируемом профиле
методики, а не здесь — иначе изменение порога переписывало бы историю
незаметно для расчёта.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PACKAGE_ROOT = Path(__file__).resolve().parent.parent
PROJECT_ROOT = PACKAGE_ROOT.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="TMO_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- окружение -------------------------------------------------------
    environment: str = "development"
    log_level: str = "INFO"
    log_format: str = "json"

    # --- хранилища -------------------------------------------------------
    database_url: str = "postgresql+psycopg://tmo:tmo@localhost:5432/tmo"
    redis_url: str = "redis://localhost:6379/0"
    raw_storage_path: Path = PROJECT_ROOT / "var" / "raw"
    export_storage_path: Path = PROJECT_ROOT / "var" / "export"

    # --- справочники -----------------------------------------------------
    catalog_path: Path = PACKAGE_ROOT / "catalog" / "data"
    active_methodology_profile: str = "baseline_v1"

    # --- сбор ------------------------------------------------------------
    #: Горизонт наблюдения в днях. Меняется только вместе с методикой.
    horizon_days: int = 30
    #: Разрешить обращения к внешним источникам. Выключается в тестах и в UI-only
    #: развёртывании: дашборд не имеет права ходить в источники ни при каких
    #: настройках, но и фоновые задачи не должны стучаться в сеть без нужды.
    sources_enabled: bool = True
    #: Одновременных обращений на источник. Замер 07.08.2026: при 12
    #: одновременных Туту начинает отвечать 503, и размыкатель цепи открывается
    #: через восемь отказов подряд, унося весь остаток пачки. Значения по
    #: умолчанию заданы в реестре источников; здесь — аварийное переопределение.
    tutu_concurrency: int = 6
    rzd_concurrency: int = 4
    #: Обращений в минуту на источник. Лимиты источниками не документированы;
    #: значения получены замером и держат запас.
    tutu_rate_limit_per_minute: int = 120
    rzd_rate_limit_per_minute: int = 90
    #: Транспортные таймауты, секунды.
    connect_timeout_seconds: float = 10.0
    read_timeout_seconds: float = 45.0
    #: Повторы транспортных ошибок внутри одной попытки источника.
    max_transport_retries: int = 2
    backoff_base_seconds: float = 0.8
    backoff_max_seconds: float = 8.0
    #: Размыкатель цепи: после скольких подряд отказов и на сколько размыкать.
    circuit_failure_threshold: int = 8
    circuit_reset_seconds: int = 900

    # --- бюджеты времени --------------------------------------------------
    #: Мягкий бюджет пакета сбора. Меньше жёсткого таймаута Celery: при
    #: исчерпании обход прекращается добровольно и собранное сохраняется.
    batch_soft_budget_seconds: int = 240
    batch_hard_timeout_seconds: int = 300
    #: Сколько логических наблюдений уходит в один Celery-таск.
    batch_size: int = 40
    #: Сколько минут без завершённых задач при непустой очереди означает застой.
    stall_threshold_minutes: int = 15

    # --- API -------------------------------------------------------------
    api_prefix: str = "/api/v1"
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:5173"])
    #: Токен административных операций. Пустой = админ-эндпоинты выключены.
    admin_token: str = ""
    export_row_limit: int = 50_000

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_origins(cls, value: object) -> object:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @field_validator("raw_storage_path", "export_storage_path", mode="after")
    @classmethod
    def _ensure_dir(cls, value: Path) -> Path:
        value.mkdir(parents=True, exist_ok=True)
        return value

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    """Сброс кэша настроек. Нужен тестам, меняющим переменные окружения."""
    get_settings.cache_clear()


def env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")
