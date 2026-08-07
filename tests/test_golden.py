"""Golden Dataset.

Набор проверяет боевой код: записанный ответ источника разбирается тем же
парсером, нормализуется тем же нормализатором и считается той же методикой, что
и в ночном прогоне.

Без прохождения набора методика готовой не считается — это условие ворот
публикации, а не только CI.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tmo.services.golden import GOLDEN_ROOT, run_case, run_golden_suite

CASES = sorted((GOLDEN_ROOT / "recorded_raw").glob("*.json"))


def test_dataset_is_not_empty() -> None:
    """Пустой набор не должен выглядеть пройденным."""
    assert CASES, "Golden Dataset пуст: recorded_raw не содержит случаев"


@pytest.mark.parametrize("case_path", CASES, ids=lambda p: p.stem)
def test_case_reproduces_expected_values(case_path: Path) -> None:
    result = run_case(case_path)
    assert result.passed, "\n".join(result.failures)


def test_suite_passes_as_a_whole() -> None:
    report = run_golden_suite()
    assert report["passed"], json.dumps(report["failures"], ensure_ascii=False, indent=2)
    assert report["total"] == len(CASES)


def test_every_case_has_expected_files() -> None:
    """Случай без эталона молча проходит любой проверкой."""
    for case_path in CASES:
        name = case_path.stem
        assert (GOLDEN_ROOT / "expected_offers" / f"{name}.json").exists(), name
        assert (GOLDEN_ROOT / "expected_metrics" / f"{name}.json").exists(), name


def test_fare_grid_collapses_on_live_air_response() -> None:
    """107 тарифных строк живой выдачи дают 30 предложений рынка.

    Без схлопывания медиана описывала бы тарифную сетку: цена «Лайт» и
    «Максимум» на одном рейсе различаются в полтора раза.
    """
    result = run_case(GOLDEN_ROOT / "recorded_raw" / "air_round_trip_fare_grid.json")
    assert result.actual["parsed_offers"] == 107
    assert result.actual["offers_count"] == 30
    assert result.actual["exclusion_reasons"]["FARE_COLLAPSED_NOT_CHEAPEST"] > 0
    assert result.actual["exclusion_reasons"]["REFUNDABLE_FARE"] > 0


def test_server_hotel_filter_is_not_sufficient() -> None:
    """Серверный фильтр применён и подтверждён — и всё равно пропустил не отели.

    В живой выдаче Сочи 3★ при отправленном и подтверждённом
    ``hotel_types=['hotel']`` пришли гостевой дом и апарт-отель. Локальная
    проверка типа размещения обязательна.
    """
    result = run_case(GOLDEN_ROOT / "recorded_raw" / "hotel_sochi_3star_one_night.json")
    assert result.actual["exclusion_reasons"].get("WRONG_PROPERTY_TYPE", 0) >= 2


def test_no_direct_service_produces_no_offers_and_no_error() -> None:
    """Самара — Казань: отсутствие сообщения не является сбоем."""
    result = run_case(GOLDEN_ROOT / "recorded_raw" / "rail_no_direct_service.json")
    assert result.actual["parsed_offers"] == 0
    assert result.actual["median"] is None


def test_two_sources_disagree_within_expected_band() -> None:
    """Туту дороже РЖД на объяснимую величину, а не на порядок.

    Один источник отдаёт тариф перевозчика, другой — цену агента со своим
    сбором. Разрыв в разы означал бы дефект выборки, а не находку.
    """
    tutu = run_case(GOLDEN_ROOT / "recorded_raw" / "rail_tutu_compartment.json").actual
    rzd = run_case(GOLDEN_ROOT / "recorded_raw" / "rail_rzd_compartment.json").actual
    assert tutu["median"] > rzd["median"]
    gap = (tutu["median"] - rzd["median"]) / rzd["median"]
    assert 0.0 < gap < 0.5, f"расхождение медиан {gap:.1%} вне объяснимого диапазона"
