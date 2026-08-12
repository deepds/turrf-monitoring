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
from typing import Annotated

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

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

    #: Имя стенда. Едет в выгруженном снимке и показывается на витрине рядом с
    #: загруженной версией: две версии одной даты без указания происхождения
    #: сравнивать невозможно.
    stand_name: str = ""

    # --- справочники -----------------------------------------------------
    catalog_path: Path = PACKAGE_ROOT / "catalog" / "data"
    #: Активная версия методики. Прежние версии остаются на диске и доступны
    #: для сравнительных расчётов: `recalculate --profile baseline_v1` считает
    #: старым правилом, не трогая витрину.
    #:
    #: Меняется вместе с Golden Dataset. Его ожидания записаны текущей
    #: методикой и прогоняются воротами расчёта, поэтому версия, переключённая
    #: без перезаписи набора, роняет ворота — и наоборот.
    active_methodology_profile: str = "baseline_v3"

    # --- сбор ------------------------------------------------------------
    #: Горизонт наблюдения в днях. Меняется только вместе с методикой.
    horizon_days: int = 30
    #: Разрешить обращения к внешним источникам. Выключается в тестах и в UI-only
    #: развёртывании: дашборд не имеет права ходить в источники ни при каких
    #: настройках, но и фоновые задачи не должны стучаться в сеть без нужды.
    sources_enabled: bool = True
    # Одновременность и темп обращений живут в реестре источников
    # (`tmo/catalog/data/sources.yaml`) и только там. Здесь они были продублированы
    # как «аварийное переопределение», которого не существовало: сбор берёт оба
    # значения из реестра, а этих полей не читал никто. Развёрнутый контейнер
    # показывал `tutu_concurrency = 8` при фактической рабочей точке 3, и рычаг,
    # к которому потянулись бы в инциденте, не сделал бы ничего.
    #: Транспортные таймауты, секунды.
    connect_timeout_seconds: float = 10.0
    read_timeout_seconds: float = 45.0
    #: Повторы транспортных ошибок внутри одной попытки источника.
    max_transport_retries: int = 2
    backoff_base_seconds: float = 0.8
    backoff_max_seconds: float = 8.0
    # Регулятор одновременности. Потолок — в реестре источников; здесь только
    # пол и темп возврата к потолку. Пол не ноль: перестать спрашивать вовсе —
    # работа размыкателя цепи, и смешивать две меры в одной ручке нельзя.
    min_source_concurrency: int = 2
    #: Сколько чистых обращений подряд стоит одна ступень вверх. При 6,26
    #: обращения на авианаблюдение 32 — это примерно пять наблюдений: подъём с
    #: пола до потолка занимает минуты, а не часы.
    concurrency_growth_after: int = 32
    #: Одновременность досбора. Отдельная и низкая, потому что досбор работает
    #: по подвыборке, отобранной по признаку «источник её не отдал»: дешёвые
    #: наблюдения закрылись с первого раза, а в пропусках осели самые тяжёлые.
    #: Замер 09.08.2026: 1,4 страницы на авианаблюдение в конце первичного
    #: прохода против 7,5 в досборе тех же наблюдений. Нащупывать рабочую точку
    #: заново на такой пачке — значит размыкать цепь на каждом заходе.
    recovery_concurrency: int = 2
    #: Размыкатель цепи: после скольких подряд отказов и на сколько размыкать.
    circuit_failure_threshold: int = 8
    #: Потолок остывания. Пауза удваивается с каждым размыканием подряд, и без
    #: потолка уходит в часы.
    circuit_reset_max_seconds: int = 1800
    # 300, а не 900: цепь остывает дольше, чем идёт пачка, и досбор упирался
    # в неё же. Пять минут достаточно, чтобы переждать всплеск отказов
    # источника, и не съедают окно сбора.
    circuit_reset_seconds: int = 300

    # --- бюджеты времени --------------------------------------------------
    #: Мягкий бюджет пакета сбора. Меньше жёсткого таймаута Celery: при
    #: исчерпании обход прекращается добровольно и собранное сохраняется.
    batch_soft_budget_seconds: int = 240
    batch_hard_timeout_seconds: int = 300
    #: Сколько логических наблюдений уходит в один Celery-таск.
    batch_size: int = 40
    #: Сколько минут без завершённых задач при непустой очереди означает застой.
    #:
    #: Обязан быть заметно больше срока аренды шага (15 минут): пока аренда
    #: умершего шага не истекла, новый шаг не начнётся, завершений не будет — и
    #: порог, равный сроку аренды, объявляет застоем **штатное восстановление**.
    #: На meduza 08.08.2026 это дало замкнутый круг: autoheal перезапускал
    #: воркер ровно тогда, когда пачка собиралась записаться, перезапуск ронял
    #: её и добавлял новых сирот, за полчаса не записалось ни одной попытки.
    #: 30 минут — аренда плюс полная пачка плюс запас.
    stall_threshold_minutes: int = 30
    #: Сколько раз наблюдение добирается, прежде чем признать его дырой.
    #: Досбор идёт до результата, но не бесконечно: наблюдение, на котором
    #: источник спотыкается систематически, при неограниченном повторе съедает
    #: обращения, нужные остальным, и держит снимок открытым до полуночи ради
    #: дыры, которая не закроется.
    max_job_attempts: int = 12

    # --- API -------------------------------------------------------------
    api_prefix: str = "/api/v1"
    # NoDecode обязателен: без него pydantic-settings пытается разобрать
    # значение переменной окружения как JSON **до** валидаторов, и обычный
    # список через запятую роняет запуск приложения целиком.
    cors_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["http://localhost:5173"]
    )
    #: Токен административных операций. Пустой = админ-эндпоинты выключены.
    admin_token: str = ""
    export_row_limit: int = 50_000

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_origins(cls, value: object) -> object:
        """Принимает и список через запятую, и JSON-массив.

        Оба формата встречаются в реальных развёртываниях: первый пишут руками
        в ``.env``, второй генерируют оркестраторы.
        """
        if not isinstance(value, str):
            return value
        text = value.strip()
        if text.startswith("["):
            import json

            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, list):
                return [str(item).strip() for item in parsed if str(item).strip()]
        return [item.strip() for item in text.split(",") if item.strip()]

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
