"""Построение матрицы наблюдений.

План детерминирован: одинаковые дата снимка, справочник городов и горизонт
дают побайтово одинаковый набор наблюдений. Это проверяется отпечатком плана,
и именно поэтому пропущенное наблюдение отличимо от незапланированного.

Состав матрицы для горизонта 30 дней (SCOPE-R O4):

```text
RAIL   5 origins × 4 destinations × 30 дат                    =   600
AIR    20 маршрутов × C(30,2) пар дат                         = 8 700
HOTEL  5 городов × 3 категории × C(30,2) пар дат              = 6 525
HOTEL  5 городов × 3 категории × хвост D+30 → D+31            =    15
                                                              -------
                                                                15 840
```

Точки графика проживания (одна ночь) — это подмножество пар дат: пара
``(d, d+1)`` уже входит в 435 сочетаний. Отдельно достраивается только
последняя точка ``D+30 → D+31``, выходящая за горизонт правым концом.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from tmo.catalog.registry import CityRegistry, city_registry
from tmo.core.enums import CollectionFamily
from tmo.core.ids import digest, job_key, series_key
from tmo.core.timeutil import HORIZON_DAYS, date_pairs, horizon_dates

#: Категории звёздности, наблюдаемые отдельно.
STAR_CATEGORIES: tuple[int, ...] = (3, 4, 5)


@dataclass(frozen=True, slots=True)
class PlannedJob:
    """Одно логическое наблюдение до записи в базу."""

    family: CollectionFamily
    job_key: str
    series_key: str
    params: dict[str, Any]
    origin_code: str | None = None
    destination_code: str | None = None
    city_code: str | None = None
    service_date: date | None = None
    return_date: date | None = None
    check_in: date | None = None
    check_out: date | None = None
    stars: int | None = None
    day_offset: int | None = None
    nights: int | None = None


@dataclass(slots=True)
class CollectionMatrix:
    snapshot_date: date
    horizon_days: int
    jobs: list[PlannedJob] = field(default_factory=list)

    @property
    def digest(self) -> str:
        """Отпечаток плана: одинаковый вход даёт одинаковый набор наблюдений."""
        return digest(
            "plan",
            self.snapshot_date,
            self.horizon_days,
            sorted(job.job_key for job in self.jobs),
            length=40,
        )

    def counts_by_family(self) -> dict[str, int]:
        counts: dict[str, int] = {family.value: 0 for family in CollectionFamily}
        for job in self.jobs:
            counts[job.family.value] += 1
        return counts

    def __len__(self) -> int:
        return len(self.jobs)


def _offset(snapshot_date: date, value: date) -> int:
    return (value - snapshot_date).days


@dataclass(frozen=True, slots=True)
class Scope:
    """Область наблюдения: откуда едем и где ночуем.

    Два множества, а не одно, потому что у транспорта и проживания ограничение
    разворачивается в разные стороны. При поездках из Москвы транспорт
    наблюдается только из Москвы, а гостиницы — везде, кроме Москвы: ночуют в
    городе назначения.

    Пустая область (``origins=None``) означает полную матрицу и не ограничивает
    ничего — это обычный боевой прогон.
    """

    origins: frozenset[str] = frozenset()
    stay_cities: frozenset[str] = frozenset()

    @classmethod
    def of(cls, registry: CityRegistry, origins: tuple[str, ...] | None) -> Scope:
        if not origins:
            return cls()
        known = {city.code for city in registry.ordered}
        unknown = sorted(set(origins) - known)
        if unknown:
            raise ValueError(f"Города нет в справочнике: {', '.join(unknown)}")
        chosen = frozenset(origins)
        return cls(
            origins=chosen,
            # Города назначения: всё, куда можно уехать хотя бы из одного
            # выбранного города отправления.
            stay_cities=frozenset(code for code in known if {code} != chosen),
        )

    @property
    def is_restricted(self) -> bool:
        return bool(self.origins)

    def allows_route(self, origin_code: str) -> bool:
        return not self.origins or origin_code in self.origins

    def allows_stay(self, city_code: str) -> bool:
        return not self.origins or city_code in self.stay_cities

    def as_dict(self) -> dict[str, Any]:
        """Область в виде, пригодном для записи в снимок и чтения человеком."""
        if not self.is_restricted:
            return {}
        return {
            "origins": sorted(self.origins),
            "stay_cities": sorted(self.stay_cities),
        }


def _rail_jobs(
    snapshot_date: date, registry: CityRegistry, days: int, scope: Scope
) -> list[PlannedJob]:
    """Плечо в одну сторону на каждую служебную дату горизонта.

    Плечо, а не поездка: перевозчик продаёт билет в одну сторону, и наблюдать
    сочетания «поезд туда × поезд обратно» значило бы описывать набор
    комбинаций вместо выбора пассажира.
    """
    jobs: list[PlannedJob] = []
    for origin, destination in registry.directed_pairs():
        if not scope.allows_route(origin.code):
            continue
        for service_date in horizon_dates(snapshot_date, days=days):
            params = {
                "origin": origin.code,
                "destination": destination.code,
                "service_date": service_date.isoformat(),
                "passengers": 1,
                "car_type": "COMPARTMENT",
                "direct_only": True,
            }
            offset_params = {**params, "day_offset": _offset(snapshot_date, service_date)}
            jobs.append(
                PlannedJob(
                    family=CollectionFamily.RAIL,
                    job_key=job_key("RAIL", params),
                    series_key=series_key(
                        "RAIL", offset_params, offset_fields=("service_date",)
                    ),
                    params=params,
                    origin_code=origin.code,
                    destination_code=destination.code,
                    service_date=service_date,
                    day_offset=_offset(snapshot_date, service_date),
                )
            )
    return jobs


def _air_jobs(
    snapshot_date: date, registry: CityRegistry, days: int, scope: Scope
) -> list[PlannedJob]:
    """Настоящий круговой тариф на каждую пару дат внутри горизонта.

    Кругового тарифа «на произвольный интервал» не существует: он продаётся на
    конкретную пару дат. Отсюда квадратичный рост — 435 пар на маршрут.
    """
    jobs: list[PlannedJob] = []
    for origin, destination in registry.directed_pairs():
        if not scope.allows_route(origin.code):
            continue
        for departure, return_date in date_pairs(snapshot_date, days=days):
            params = {
                "origin": origin.code,
                "destination": destination.code,
                "departure_date": departure.isoformat(),
                "return_date": return_date.isoformat(),
                "passengers": 1,
                "cabin": "ECONOMY",
                "direct_only": True,
                "trip_type": "ROUND_TRIP",
            }
            offset_params = {
                **params,
                "departure_offset": _offset(snapshot_date, departure),
                "return_offset": _offset(snapshot_date, return_date),
            }
            jobs.append(
                PlannedJob(
                    family=CollectionFamily.AIR,
                    job_key=job_key("AIR", params),
                    series_key=series_key(
                        "AIR", offset_params, offset_fields=("departure_date", "return_date")
                    ),
                    params=params,
                    origin_code=origin.code,
                    destination_code=destination.code,
                    service_date=departure,
                    return_date=return_date,
                    day_offset=_offset(snapshot_date, departure),
                    nights=(return_date - departure).days,
                )
            )
    return jobs


def _hotel_jobs(
    snapshot_date: date, registry: CityRegistry, days: int, scope: Scope
) -> list[PlannedJob]:
    """Настоящая бронь на пару дат, включая хвостовую точку графика.

    Одна ночь — частный случай пары ``(d, d+1)``. Отдельно достраивается только
    ``D+30 → D+31``: она нужна графику проживания и выходит за горизонт правым
    концом.
    """
    jobs: list[PlannedJob] = []
    horizon_end = snapshot_date + timedelta(days=days)
    stays: list[tuple[date, date]] = list(date_pairs(snapshot_date, days=days))
    stays.append((horizon_end, horizon_end + timedelta(days=1)))

    for city in registry.ordered:
        if not scope.allows_stay(city.code):
            continue
        for stars in STAR_CATEGORIES:
            for check_in, check_out in stays:
                params = {
                    "city": city.code,
                    "check_in": check_in.isoformat(),
                    "check_out": check_out.isoformat(),
                    "stars": stars,
                    "adults": 1,
                    "rooms": 1,
                    "property_type": "HOTEL",
                }
                offset_params = {
                    **params,
                    "check_in_offset": _offset(snapshot_date, check_in),
                    "nights": (check_out - check_in).days,
                }
                jobs.append(
                    PlannedJob(
                        family=CollectionFamily.HOTEL,
                        job_key=job_key("HOTEL", params),
                        series_key=series_key(
                            "HOTEL", offset_params, offset_fields=("check_in", "check_out")
                        ),
                        params=params,
                        city_code=city.code,
                        check_in=check_in,
                        check_out=check_out,
                        stars=stars,
                        day_offset=_offset(snapshot_date, check_in),
                        nights=(check_out - check_in).days,
                    )
                )
    return jobs


def build_matrix(
    snapshot_date: date,
    *,
    horizon_days: int = HORIZON_DAYS,
    registry: CityRegistry | None = None,
    families: tuple[CollectionFamily, ...] = tuple(CollectionFamily),
    origins: tuple[str, ...] | None = None,
) -> CollectionMatrix:
    """Строит матрицу наблюдений для даты снимка.

    ``origins`` ограничивает матрицу поездками из перечисленных городов. Нужно
    нагрузочному прогону: полная матрица занимает у источника одиннадцать часов,
    и проверять на ней работу конвейера дорого.

    Ограничение задаётся поездками, а не городами, и по семействам разворачивается
    по-разному: транспорт наблюдается **из** указанных городов, проживание — **в**
    городах назначения. В поездке Москва→Сочи гостиница нужна в Сочи, поэтому при
    ``origins=("MOW",)`` московские гостиницы в матрицу не входят.
    """
    registry = registry or city_registry()
    scope = Scope.of(registry, origins)
    matrix = CollectionMatrix(snapshot_date=snapshot_date, horizon_days=horizon_days)
    if CollectionFamily.RAIL in families:
        matrix.jobs.extend(_rail_jobs(snapshot_date, registry, horizon_days, scope))
    if CollectionFamily.AIR in families:
        matrix.jobs.extend(_air_jobs(snapshot_date, registry, horizon_days, scope))
    if CollectionFamily.HOTEL in families:
        matrix.jobs.extend(_hotel_jobs(snapshot_date, registry, horizon_days, scope))

    keys = [job.job_key for job in matrix.jobs]
    if len(keys) != len(set(keys)):
        # Совпадение ключей означало бы, что два разных наблюдения пишутся в
        # одну строку и одно из них теряется молча.
        duplicates = sorted({key for key in keys if keys.count(key) > 1})[:5]
        raise ValueError(f"Матрица содержит повторяющиеся job_key: {duplicates}")
    return matrix


def expected_size(
    horizon_days: int = HORIZON_DAYS,
    city_count: int = 5,
    origin_count: int | None = None,
) -> dict[str, int]:
    """Ожидаемый размер матрицы. Используется тестами и capacity-анализом.

    ``origin_count`` — сколько городов отправления наблюдается. ``None`` означает
    все, то есть полную матрицу. Один город даёт 7 092 наблюдения против 15 840:
    транспорт сжимается вчетверо по числу маршрутов, проживание — на один город,
    потому что в городе отправления не ночуют.
    """
    origin_count = city_count if origin_count is None else origin_count
    routes = origin_count * (city_count - 1)
    pairs = horizon_days * (horizon_days - 1) // 2
    # Город отправления выпадает из проживания только когда он один: при двух и
    # более из каждого можно уехать в другой, и ночуют в итоге во всех.
    stay_cities = city_count - 1 if origin_count == 1 else city_count
    rail = routes * horizon_days
    air = routes * pairs
    hotel = stay_cities * len(STAR_CATEGORIES) * (pairs + 1)
    return {
        "RAIL": rail,
        "AIR": air,
        "HOTEL": hotel,
        "TOTAL": rail + air + hotel,
    }
