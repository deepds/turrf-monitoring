"""Настройки приложения.

Отдельный набор, потому что ошибка конфигурации роняет запуск целиком и не
ловится ни одним тестом бизнес-логики. Список CORS через запятую однажды
уронил API в развёртывании: pydantic-settings пытается разобрать значение как
JSON **до** валидаторов.
"""

from __future__ import annotations

import pytest

from tmo.core.config import Settings


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("http://a:1,http://b:2", ["http://a:1", "http://b:2"]),
        ('["http://x:1","http://y:2"]', ["http://x:1", "http://y:2"]),
        ("http://solo:3", ["http://solo:3"]),
        ("http://a:1, http://b:2 ,", ["http://a:1", "http://b:2"]),
    ],
)
def test_cors_origins_accepts_both_formats(
    monkeypatch: pytest.MonkeyPatch, raw: str, expected: list[str]
) -> None:
    """Список через запятую пишут руками, JSON-массив генерируют оркестраторы."""
    monkeypatch.setenv("TMO_CORS_ORIGINS", raw)
    assert Settings().cors_origins == expected


def test_cors_origins_has_a_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TMO_CORS_ORIGINS", raising=False)
    assert Settings().cors_origins == ["http://localhost:5173"]


def test_admin_operations_are_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Пустой токен означает «выключено», а не «открыто всем»."""
    monkeypatch.delenv("TMO_ADMIN_TOKEN", raising=False)
    assert Settings().admin_token == ""


def test_storage_paths_are_created(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("TMO_RAW_STORAGE_PATH", str(tmp_path / "raw"))
    monkeypatch.setenv("TMO_EXPORT_STORAGE_PATH", str(tmp_path / "export"))
    settings = Settings()
    assert settings.raw_storage_path.exists()
    assert settings.export_storage_path.exists()


def test_source_limits_are_conservative_by_default() -> None:
    """Пределы проверяются там, откуда их берёт сбор, — в реестре источников.

    Прежняя версия проверяла поля настроек, которых не читал никто: сбор берёт
    одновременность из `sources.yaml`, а `settings.tutu_concurrency` оставался
    декорацией. Тест был зелёным и при рабочей точке, отличной от проверяемой.

    Границы — из замеров, а не из осторожности:

    * шесть одновременных источник держал три часа подряд без отказов;
    * на 12-15 ночью 08.08.2026 отвечал 503 и размыкал цепь;
    * дневной замер 12 одновременных при 240 в минуту — 503 через минуты.

    Двенадцать — верхняя граница наблюдавшегося: рабочая точка стоит ровно на
    ней сознательно, потому что меньшая одновременность не укладывается в окно.
    Выше идти нельзя без нового замера, и этот предел здесь ровно для того,
    чтобы такое изменение не прошло молча.
    """
    from tmo.catalog.registry import source_registry

    limits = {source.code: source for source in source_registry().sources}
    assert limits["tutu_mcp"].concurrency <= 12
    assert limits["rzd"].concurrency <= 6
    # Темп выше 180 в сочетании с высокой одновременностью — то самое сочетание,
    # которое ломалось. Поднимать его следует отдельной ночью и отдельным замером.
    assert limits["tutu_mcp"].rate_limit_per_minute <= 180


def test_soft_budget_is_below_hard_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """Жёсткий обрыв уничтожает уже собранное; мягкий бюджет обязан сработать раньше."""
    for name in ("TMO_BATCH_SOFT_BUDGET_SECONDS", "TMO_BATCH_HARD_TIMEOUT_SECONDS"):
        monkeypatch.delenv(name, raising=False)
    settings = Settings()
    assert settings.batch_soft_budget_seconds < settings.batch_hard_timeout_seconds
