"""Загрузка справочников: города, источники, профили методики.

Справочники читаются один раз и кэшируются: они не меняются в ходе прогона, а
повторное чтение YAML из воркера — лишний ввод-вывод на каждую задачу.
"""

from __future__ import annotations

from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from tmo.core.config import get_settings
from tmo.core.enums import CollectionFamily
from tmo.core.errors import ConfigurationError, MethodologyError

# --------------------------------------------------------------------------- #
# Города
# --------------------------------------------------------------------------- #


class City(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    code: str
    name: str
    name_en: str
    timezone: str
    rzd_express_code: str
    avia_metro_code: str
    avia_airport_codes: tuple[str, ...] = ()
    multi_airport: bool = False
    sort_order: int = 0


class MarketGap(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    origin: str
    destination: str
    family: CollectionFamily
    expectation: str
    note: str = ""


class CityRegistry(BaseModel):
    model_config = ConfigDict(frozen=True)

    cities: tuple[City, ...]
    known_market_gaps: tuple[MarketGap, ...] = ()

    @property
    def codes(self) -> tuple[str, ...]:
        return tuple(city.code for city in self.ordered)

    @property
    def ordered(self) -> tuple[City, ...]:
        return tuple(sorted(self.cities, key=lambda c: (c.sort_order, c.code)))

    def get(self, code: str) -> City:
        for city in self.cities:
            if city.code == code:
                return city
        raise ConfigurationError(f"Город {code!r} отсутствует в справочнике")

    def directed_pairs(self) -> list[tuple[City, City]]:
        """Все направленные пары городов: 5 × 4 = 20 маршрутов."""
        ordered = self.ordered
        return [(a, b) for a in ordered for b in ordered if a.code != b.code]

    def expected_gap(self, origin: str, destination: str, family: CollectionFamily) -> str | None:
        for gap in self.known_market_gaps:
            if gap.origin == origin and gap.destination == destination and gap.family == family:
                return gap.expectation
        return None


# --------------------------------------------------------------------------- #
# Источники
# --------------------------------------------------------------------------- #


class Source(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    code: str
    name: str
    protocol: str
    endpoint: str
    allowed_hosts: tuple[str, ...] = ()
    families: tuple[CollectionFamily, ...] = ()
    requires_credentials: bool = False
    price_semantics: dict[str, str] = Field(default_factory=dict)
    rate_limit_per_minute: int = 60
    concurrency: int = 4
    max_pages: int = 1
    page_size: int = 0
    is_enabled: bool = True
    notes: str = ""

    @property
    def is_synthetic(self) -> bool:
        return self.protocol == "REPLAY"


class SourceRegistry(BaseModel):
    model_config = ConfigDict(frozen=True)

    sources: tuple[Source, ...]

    def get(self, code: str) -> Source:
        for source in self.sources:
            if source.code == code:
                return source
        raise ConfigurationError(f"Источник {code!r} отсутствует в реестре")

    def for_family(self, family: CollectionFamily, *, enabled_only: bool = True) -> list[Source]:
        return [
            source
            for source in self.sources
            if family in source.families and (source.is_enabled or not enabled_only)
        ]


# --------------------------------------------------------------------------- #
# Методика
# --------------------------------------------------------------------------- #


class MethodologyProfile(BaseModel):
    """Версия методики. Считается неизменяемой после первого использования."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: str
    title: str
    effective_from: date
    description: str = ""
    selection: dict[str, Any]
    aggregation: dict[str, Any]
    outliers: dict[str, Any]
    quality: dict[str, Any]
    confidence: dict[str, Any]
    publication: dict[str, Any]
    trip_cost: dict[str, Any]

    @field_validator("selection")
    @classmethod
    def _check_selection(cls, value: dict[str, Any]) -> dict[str, Any]:
        for block in ("rail", "air", "hotel"):
            if block not in value:
                raise MethodologyError(f"В профиле отсутствует блок selection.{block}")
        rail = value["rail"]
        # Безусловно допустимый тип — только купе. Сидячее место открывается
        # поимённым списком `high_speed_seated_trains`, и это не придирка к
        # форме записи: `SEDENTARY` объединяет «Сапсан» со средней ценой
        # 17 323 ₽ и пригородный сидячий вагон за 292 ₽. Разрешить тип целиком
        # значило бы расширить выборку в пятьдесят раз по цене одной строки в
        # профиле — ровно то, от чего эта проверка и стоит.
        unconditional = {
            str(item) for item in (rail.get("car_types") or [rail.get("car_type")]) if item
        }
        if unconditional != {"COMPARTMENT"}:
            raise MethodologyError(
                "Безусловно допускается только купе: selection.rail.car_types=[COMPARTMENT]. "
                "Сидячее место открывается списком high_speed_seated_trains, а не типом вагона"
            )
        if rail.get("trip_type") != "ONE_WAY":
            raise MethodologyError(
                "ЖД наблюдается плечом: selection.rail.trip_type должен быть ONE_WAY"
            )
        air = value["air"]
        if air.get("trip_type") != "ROUND_TRIP":
            raise MethodologyError(
                "Авиа наблюдается настоящим круговым тарифом: air.trip_type=ROUND_TRIP"
            )
        if air.get("refundable") is not False:
            raise MethodologyError("MVP считает невозвратные авиатарифы: air.refundable=false")
        hotel = value["hotel"]
        if list(hotel.get("property_types") or []) != ["HOTEL"]:
            raise MethodologyError(
                "Апартаменты нельзя смешивать с гостиницами: hotel.property_types=[HOTEL]"
            )
        return value

    @field_validator("trip_cost")
    @classmethod
    def _check_trip_cost(cls, value: dict[str, Any]) -> dict[str, Any]:
        if value.get("accommodation") != "REAL_MULTI_NIGHT_QUERY":
            raise MethodologyError(
                "Проживание поездки считается настоящим multi-night запросом, "
                "а не произведением цены одной ночи на число ночей"
            )
        if value.get("air") != "REAL_ROUND_TRIP_PLUS_REAL_STAY":
            raise MethodologyError("Авиапоездка считается настоящим круговым тарифом")
        return value

    # -- удобные проекции ---------------------------------------------------

    def selection_for(self, family: CollectionFamily) -> dict[str, Any]:
        return dict(self.selection[family.value.lower()])

    def target_sample(self, family: CollectionFamily) -> int:
        return int(self.quality["target_sample_size"][family.value])

    def expected_sources(self, family: CollectionFamily) -> int:
        return int(self.quality["expected_sources"][family.value])

    def min_offers_for(self, level: str, family: CollectionFamily) -> int:
        return int(self.confidence[f"min_offers_for_{level}"][family.value])

    def ready_completion(self, family: CollectionFamily | None = None) -> float:
        if family is None:
            return float(self.publication["ready_completion"])
        return float(self.publication["ready_completion_by_family"][family.value])

    def degraded_completion(self, family: CollectionFamily | None = None) -> float:
        if family is None:
            return float(self.publication["degraded_completion"])
        return float(self.publication["degraded_completion_by_family"][family.value])


# --------------------------------------------------------------------------- #
# Загрузка
# --------------------------------------------------------------------------- #


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ConfigurationError(f"Файл справочника не найден: {path}")
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    if not isinstance(data, dict):
        raise ConfigurationError(f"Ожидался словарь в {path}")
    return data


@lru_cache(maxsize=1)
def city_registry() -> CityRegistry:
    data = _read_yaml(get_settings().catalog_path / "cities.yaml")
    return CityRegistry.model_validate(data)


@lru_cache(maxsize=1)
def source_registry() -> SourceRegistry:
    data = _read_yaml(get_settings().catalog_path / "sources.yaml")
    return SourceRegistry.model_validate(data)


@lru_cache(maxsize=8)
def methodology_profile(version: str | None = None) -> MethodologyProfile:
    settings = get_settings()
    version = version or settings.active_methodology_profile
    path = settings.catalog_path / "profiles" / f"{version}.yaml"
    return MethodologyProfile.model_validate(_read_yaml(path))


def available_profiles() -> list[str]:
    directory = get_settings().catalog_path / "profiles"
    return sorted(p.stem for p in directory.glob("*.yaml"))


def reset_catalog_cache() -> None:
    city_registry.cache_clear()
    source_registry.cache_clear()
    methodology_profile.cache_clear()
