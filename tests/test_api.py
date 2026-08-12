"""Контракт REST v1.

Отдельно проверяется главное свойство витрины: **она не обращается к
источникам**. Проверка не декларативная — сеть в тестах отключена, и любой
исходящий запрос упал бы.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from tmo.catalog.registry import methodology_profile
from tmo.services.pipeline import run_daily_pipeline

#: Активная версия методики, а не её имя на сегодня. Зашитая строка ломала бы
#: эти тесты при каждой смене правил — притом что проверяют они не методику, а
#: то, что версия вообще доезжает до ответа.
ACTIVE_VERSION = methodology_profile().version

SNAPSHOT = date(2026, 8, 7)
HORIZON = 3


@pytest.fixture()
def client(database: str):
    from tmo.connectors.registry import close_all

    close_all()
    run_daily_pipeline(
        snapshot_date=SNAPSHOT,
        horizon_days=HORIZON,
        replay_mode="SIMULATED",
        is_synthetic=True,
        recovery_rounds=0,
        batch_size=200,
    )
    from tmo.api.app import create_app

    return TestClient(create_app())


def test_health(client: TestClient) -> None:
    response = client.get("/api/v1/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["database"] == "ok"
    assert body["latest_snapshot"]["snapshot_date"] == SNAPSHOT.isoformat()


def test_latest_snapshot_exposes_status_and_coverage(client: TestClient) -> None:
    body = client.get("/api/v1/market-snapshots/latest").json()
    assert body["snapshot_date"] == SNAPSHOT.isoformat()
    assert body["status"] in ("READY", "DEGRADED")
    assert body["is_synthetic"] is True
    assert 0.0 <= body["coverage_total"] <= 1.0
    assert body["methodology_version"] == ACTIVE_VERSION
    assert "overview" in body


def test_snapshot_list_and_by_date(client: TestClient) -> None:
    listing = client.get("/api/v1/market-snapshots").json()["snapshots"]
    assert listing
    by_date = client.get(f"/api/v1/market-snapshots/{SNAPSHOT.isoformat()}")
    assert by_date.status_code == 200
    missing = client.get("/api/v1/market-snapshots/2020-01-01")
    assert missing.status_code == 404


def test_trips_returns_ready_numbers(client: TestClient) -> None:
    departure = SNAPSHOT + timedelta(days=1)
    return_date = SNAPSHOT + timedelta(days=3)
    response = client.get(
        "/api/v1/showcase/trips",
        params={
            "origin": "MOW",
            "departure_date": departure.isoformat(),
            "return_date": return_date.isoformat(),
            "transport_mode": "AIR",
            "stars": 4,
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["label"] == "Расчётная стоимость поездки"
    assert body["trips"]
    row = body["trips"][0]
    # Фронтенд ничего не досчитывает: все величины приходят готовыми.
    for key in (
        "transport_median", "transport_min", "accommodation_median",
        "total_median", "total_min", "confidence_level", "offers_count",
    ):
        assert key in row
    assert row["transport_composition"]


def test_trips_rejects_dates_outside_the_horizon(client: TestClient) -> None:
    response = client.get(
        "/api/v1/showcase/trips",
        params={
            "origin": "MOW",
            "departure_date": (SNAPSHOT + timedelta(days=90)).isoformat(),
            "return_date": (SNAPSHOT + timedelta(days=95)).isoformat(),
            "transport_mode": "AIR",
            "stars": 4,
        },
    )
    assert response.status_code == 422


def test_trips_rejects_return_before_departure(client: TestClient) -> None:
    response = client.get(
        "/api/v1/showcase/trips",
        params={
            "origin": "MOW",
            "departure_date": (SNAPSHOT + timedelta(days=3)).isoformat(),
            "return_date": (SNAPSHOT + timedelta(days=1)).isoformat(),
            "transport_mode": "AIR",
            "stars": 4,
        },
    )
    assert response.status_code == 422


def test_rail_chart_overview_and_detail(client: TestClient) -> None:
    overview = client.get("/api/v1/charts/rail", params={"origin": "MOW"}).json()
    assert overview["mode"] == "OVERVIEW"
    assert len(overview["series"]) == 4
    assert overview["parameters"]["car_type"] == "COMPARTMENT"

    detail = client.get(
        "/api/v1/charts/rail", params={"origin": "MOW", "destination": "AER"}
    ).json()
    assert detail["mode"] == "ROUTE_DETAIL"
    assert len(detail["series"]) == 1
    assert detail["series"][0]["points"]


def test_rail_chart_supports_historical_snapshot(client: TestClient) -> None:
    response = client.get(
        "/api/v1/charts/rail",
        params={"origin": "MOW", "snapshot_date": SNAPSHOT.isoformat()},
    )
    assert response.status_code == 200
    assert response.json()["context"]["snapshot_date"] == SNAPSHOT.isoformat()


def test_hotel_chart_covers_all_cities(client: TestClient) -> None:
    body = client.get("/api/v1/charts/hotels", params={"stars": 4}).json()
    assert len(body["series"]) == 5
    assert body["parameters"] == {
        "stars": 4, "nights": 1, "adults": 1, "rooms": 1, "property_type": "HOTEL"
    }


def test_metric_details_and_offers(client: TestClient) -> None:
    chart = client.get("/api/v1/charts/rail", params={"origin": "MOW"}).json()
    point = next(
        p for series in chart["series"] for p in series["points"] if not p["is_no_market"]
    )
    metric_id = point["metric_id"]

    details = client.get(f"/api/v1/metrics/{metric_id}").json()
    assert details["metric_id"] == metric_id
    assert details["methodology_version"] == ACTIVE_VERSION
    assert details["fetched_at"]
    assert details["source_attempts"]

    offers = client.get(f"/api/v1/metrics/{metric_id}/offers").json()
    assert offers["count"] > 0
    assert offers["included_count"] > 0
    excluded = [row for row in offers["offers"] if not row["is_included"]]
    assert all(row["exclusion_reason"] for row in excluded)
    included = [row for row in offers["offers"] if row["is_included"]]
    assert all(row["provenance"]["raw_storage_ref"] for row in included)


def test_offers_can_be_filtered(client: TestClient) -> None:
    chart = client.get("/api/v1/charts/rail", params={"origin": "MOW"}).json()
    metric_id = next(
        p["metric_id"] for s in chart["series"] for p in s["points"] if not p["is_no_market"]
    )
    only_included = client.get(
        f"/api/v1/metrics/{metric_id}/offers", params={"included": "true"}
    ).json()
    assert only_included["excluded_count"] == 0


def test_missing_metric_is_404(client: TestClient) -> None:
    assert client.get("/api/v1/metrics/999999").status_code == 404


def test_exports(client: TestClient) -> None:
    chart = client.get("/api/v1/charts/rail", params={"origin": "MOW"}).json()
    metric_id = next(
        p["metric_id"] for s in chart["series"] for p in s["points"] if not p["is_no_market"]
    )

    csv = client.get(f"/api/v1/exports/metrics/{metric_id}", params={"fmt": "csv"})
    assert csv.status_code == 200
    assert csv.headers["content-type"].startswith("text/csv")
    assert csv.content.startswith(b"\xef\xbb\xbf"), "без BOM Excel ломает кириллицу"
    assert "Причина исключения".encode() in csv.content

    xlsx = client.get(f"/api/v1/exports/metrics/{metric_id}", params={"fmt": "xlsx"})
    assert xlsx.status_code == 200
    assert xlsx.content[:2] == b"PK"


def test_coverage_endpoint(client: TestClient) -> None:
    body = client.get(f"/api/v1/coverage/{SNAPSHOT.isoformat()}").json()
    assert body["coverage"]["total"]["planned"] > 0
    assert "by_family" in body["coverage"]
    assert "matrix" in body
    assert "holes" in body


def test_reference_endpoints(client: TestClient) -> None:
    cities = client.get("/api/v1/reference/cities").json()
    assert [city["code"] for city in cities["cities"]] == ["MOW", "LED", "AER", "KUF", "KZN"]
    assert cities["known_market_gaps"]

    methodology = client.get("/api/v1/reference/methodology").json()
    assert methodology["version"] == ACTIVE_VERSION
    # Безусловно допустимый тип вагона — только купе. Сидячее место методика
    # открывает поимённым списком поездов, а не расширением типа: проверяется
    # инвариант, а не то, каким ключом он записан в текущей версии.
    rail = methodology["selection"]["rail"]
    unconditional = set(rail.get("car_types") or [rail.get("car_type")])
    assert unconditional == {"COMPARTMENT"}

    dictionary = client.get("/api/v1/reference/dictionary").json()
    # Код без расшифровки на экране бесполезен.
    assert "FARE_COLLAPSED_NOT_CHEAPEST" in dictionary["exclusion_reasons"]
    assert "PARTIAL_SAMPLE" in dictionary["warning_codes"]
    assert dictionary["expected_matrix"]["TOTAL"] == 15840

    sources = client.get("/api/v1/reference/sources").json()
    assert {item["code"] for item in sources["sources"]} >= {"tutu_mcp", "rzd"}


def test_admin_is_closed_without_token(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Пустой токен означает «выключено», а не «открыто всем».

    Токен задаётся явно пустым: в развёрнутом окружении он приходит из `.env`,
    и тест, полагающийся на его отсутствие, проверял бы там другую ветку —
    «неверный токен» вместо «операции выключены».
    """
    from tmo.core.config import reset_settings_cache

    monkeypatch.setenv("TMO_ADMIN_TOKEN", "")
    reset_settings_cache()
    try:
        response = client.post(f"/api/v1/admin/snapshots/{SNAPSHOT.isoformat()}/retry")
        assert response.status_code == 403
    finally:
        reset_settings_cache()


def test_openapi_is_served(client: TestClient) -> None:
    schema = client.get("/openapi.json").json()
    paths = set(schema["paths"])
    for required in (
        "/api/v1/market-snapshots/latest",
        "/api/v1/showcase/trips",
        "/api/v1/charts/rail",
        "/api/v1/charts/hotels",
        "/api/v1/metrics/{metric_id}",
        "/api/v1/metrics/{metric_id}/offers",
        "/api/v1/coverage/{snapshot_date}",
        "/api/v1/exports/metrics/{metric_id}",
    ):
        assert required in paths


def test_showcase_never_touches_the_network(client: TestClient, monkeypatch) -> None:
    """Витрина обязана работать только по собранным данным.

    Перекрывается единственная дорога к источникам — транспорт коннектора.
    Обращение к нему из веб-обработчика означало бы, что дашборд стал клиентом
    Туту и РЖД.
    """
    from tmo.connectors.transport import SourceTransport

    def explode(*args: object, **kwargs: object) -> None:
        raise AssertionError("витрина обратилась к внешнему источнику")

    monkeypatch.setattr(SourceTransport, "request", explode)

    assert client.get("/api/v1/market-snapshots/latest").status_code == 200
    assert client.get("/api/v1/charts/hotels", params={"stars": 3}).status_code == 200
    assert (
        client.get(
            "/api/v1/showcase/trips",
            params={
                "origin": "MOW",
                "departure_date": (SNAPSHOT + timedelta(days=1)).isoformat(),
                "return_date": (SNAPSHOT + timedelta(days=2)).isoformat(),
                "transport_mode": "RAIL",
                "stars": 3,
            },
        ).status_code
        == 200
    )


# --------------------------------------------------------------------------- #
# Авиа: линия при фиксированной длительности и полная сетка
# --------------------------------------------------------------------------- #


def test_air_chart_requires_explicit_trip_length(client: TestClient) -> None:
    """Длительность задаётся явно: на каждую дату вылета своя цена для каждой.

    Выбирать её за пользователя означало бы показать один срез сетки как «цену
    авиа», не сказав, какой именно.
    """
    body = client.get("/api/v1/charts/air", params={"origin": "MOW", "nights": 1}).json()
    assert body["parameters"]["nights"] == 1
    assert body["parameters"]["trip_type"] == "ROUND_TRIP"
    assert body["available_nights"]
    assert body["series"]
    point = body["series"][0]["points"][0]
    # Обратная дата — часть наблюдения: без неё точка неотличима от плеча.
    assert point["return_date"]
    assert point["metric_id"]


def test_air_chart_route_detail(client: TestClient) -> None:
    body = client.get(
        "/api/v1/charts/air", params={"origin": "MOW", "destination": "AER", "nights": 1}
    ).json()
    assert body["mode"] == "ROUTE_DETAIL"
    assert len(body["series"]) == 1


def test_air_chart_rejects_impossible_trip_length(client: TestClient) -> None:
    assert client.get("/api/v1/charts/air", params={"origin": "MOW", "nights": 0}).status_code == 422
    assert client.get("/api/v1/charts/air", params={"origin": "MOW", "nights": 99}).status_code == 422


def test_air_grid_returns_all_observed_date_pairs(client: TestClient) -> None:
    body = client.get(
        "/api/v1/charts/air-grid", params={"origin": "MOW", "destination": "AER"}
    ).json()
    assert body["origin"]["code"] == "MOW"
    assert body["destination"]["code"] == "AER"
    assert body["cells"]
    assert body["departure_dates"]
    assert body["nights_options"]
    # Каждая клетка — наблюдение с собственной метрикой, а не производная.
    assert all(cell["metric_id"] for cell in body["cells"])
    assert all(cell["return_date"] for cell in body["cells"])


def test_air_grid_scale_excludes_cells_without_price(client: TestClient) -> None:
    """Клетка без рынка не входит в шкалу: серый цвет читался бы как «дёшево»."""
    body = client.get(
        "/api/v1/charts/air-grid", params={"origin": "MOW", "destination": "AER"}
    ).json()
    scale = body["scale"]
    priced = [cell["median"] for cell in body["cells"] if cell["median"] is not None]
    assert scale["priced_cells"] == len(priced)
    assert scale["total_cells"] == len(body["cells"])
    if priced:
        assert scale["min"] == pytest.approx(min(priced), abs=0.01)
        assert scale["max"] == pytest.approx(max(priced), abs=0.01)


def test_air_grid_scale_is_per_route(client: TestClient) -> None:
    """Шкала строится по одному маршруту: цены разных направлений несравнимы."""
    first = client.get(
        "/api/v1/charts/air-grid", params={"origin": "MOW", "destination": "AER"}
    ).json()["scale"]
    second = client.get(
        "/api/v1/charts/air-grid", params={"origin": "MOW", "destination": "LED"}
    ).json()["scale"]
    assert first["min"] is not None and second["min"] is not None
    assert (first["min"], first["max"]) != (second["min"], second["max"])
