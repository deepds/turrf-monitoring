"""Golden Dataset: воспроизводимость методики.

Набор устроен так, что проверяет **боевой код**, а не свою копию: записанный
ответ источника разбирается тем же парсером, нормализуется тем же
нормализатором и считается той же методикой, что и в ночном прогоне. Замена
любого звена заглушкой превратила бы набор в проверку заглушки.

Раскладка (SCOPE-R P23):

```text
golden/recorded_raw/<case>.json      — что ответил источник
golden/expected_offers/<case>.json   — что должно получиться после разбора и отбора
golden/expected_metrics/<case>.json  — какая цифра должна выйти
```

Без прохождения набора методика готовой не считается.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from tmo.catalog.registry import methodology_profile
from tmo.connectors.contracts import AirQuery, HotelQuery, RailQuery
from tmo.connectors.rzd import RzdConnector
from tmo.connectors.tutu import TutuConnector
from tmo.core.enums import CollectionFamily
from tmo.core.money import quantize
from tmo.engine.quality import QualityInput, evaluate
from tmo.engine.selection import Candidate, select
from tmo.engine.statistics import summarize
from tmo.normalization.normalizer import normalize_batch

GOLDEN_ROOT = Path(__file__).resolve().parents[2] / "golden"


@dataclass(slots=True)
class CaseResult:
    case: str
    passed: bool
    failures: list[str] = field(default_factory=list)
    actual: dict[str, Any] = field(default_factory=dict)


def _query_from(family: CollectionFamily, payload: dict[str, Any]) -> Any:
    if family is CollectionFamily.RAIL:
        return RailQuery(
            origin_code=payload["origin_code"],
            origin_name=payload["origin_name"],
            origin_rzd_code=payload.get("origin_rzd_code", ""),
            destination_code=payload["destination_code"],
            destination_name=payload["destination_name"],
            destination_rzd_code=payload.get("destination_rzd_code", ""),
            service_date=date.fromisoformat(payload["service_date"]),
            passengers=int(payload.get("passengers", 1)),
        )
    if family is CollectionFamily.AIR:
        return AirQuery(
            origin_code=payload["origin_code"],
            origin_name=payload["origin_name"],
            origin_metro_code=payload.get("origin_metro_code", ""),
            destination_code=payload["destination_code"],
            destination_name=payload["destination_name"],
            destination_metro_code=payload.get("destination_metro_code", ""),
            departure_date=date.fromisoformat(payload["departure_date"]),
            return_date=date.fromisoformat(payload["return_date"]),
            adults=int(payload.get("adults", 1)),
        )
    return HotelQuery(
        city_code=payload["city_code"],
        city_name=payload["city_name"],
        check_in=date.fromisoformat(payload["check_in"]),
        check_out=date.fromisoformat(payload["check_out"]),
        stars=int(payload["stars"]),
        adults=int(payload.get("adults", 1)),
        rooms=int(payload.get("rooms", 1)),
    )


def _parse(case: dict[str, Any], query: Any) -> list[Any]:
    """Разбор боевым парсером источника."""
    family = CollectionFamily(case["family"])
    payload = case["payload"]
    source = case["source"]
    if source == "rzd":
        parser = RzdConnector.__new__(RzdConnector)
        offers, _ = parser._parse(payload.get("Trains") or [], query)
        return offers
    parser = TutuConnector.__new__(TutuConnector)
    if family is CollectionFamily.RAIL:
        return parser._parse_rail(payload.get("offers") or [], query)
    if family is CollectionFamily.AIR:
        return parser._parse_air(payload.get("offers") or [], query)
    return parser._parse_hotels(
        payload.get("hotels") or [], query, payload.get("stay") or {}
    )


def run_case(case_path: Path, *, profile_version: str | None = None) -> CaseResult:
    case = json.loads(case_path.read_text(encoding="utf-8"))
    name = case.get("case") or case_path.stem
    family = CollectionFamily(case["family"])
    profile = methodology_profile(profile_version)
    query = _query_from(family, case["query"])

    provider_offers = _parse(case, query)
    normalized, dropped = normalize_batch(
        provider_offers, query, source_code=case.get("source_code", case["source"])
    )
    candidates = [
        Candidate(
            ref=index,
            source_code=item.source_code,
            price=item.price,
            currency=item.currency,
            equivalence_key=item.equivalence_key,
            transport=item.transport_attributes,
            property_info=item.property_attributes,
            validation_flags=item.validation_flags,
        )
        for index, item in enumerate(normalized)
    ]
    selection = select(
        candidates,
        family=family,
        rules=profile.selection_for(family),
        outlier_rules=profile.outliers,
    )
    stats = summarize(selection.prices)
    per_source: dict[str, list[Decimal]] = {}
    for decision in selection.included:
        per_source.setdefault(decision.candidate.source_code, []).append(decision.candidate.price)

    quality = evaluate(
        QualityInput(
            family=family,
            offers_count=len(selection.prices),
            sources_count=len(per_source),
            is_partial=bool(case.get("is_partial")),
            fetched_at=None,
            per_source_median={},
            outliers_not_removed=bool(
                selection.outlier and selection.outlier.reason == "TOO_MANY_OUTLIERS"
            ),
        ),
        profile,
    )

    exclusions: dict[str, int] = {}
    for decision in selection.excluded:
        key = decision.reason.value if decision.reason else "UNKNOWN"
        exclusions[key] = exclusions.get(key, 0) + 1

    actual = {
        "parsed_offers": len(provider_offers),
        "normalized_offers": len(normalized),
        "dropped_without_price": dropped,
        "included_offers": len(selection.included),
        "excluded_offers": len(selection.excluded),
        "exclusion_reasons": exclusions,
        "median": float(stats.median) if stats.median is not None else None,
        "min": float(stats.minimum) if stats.minimum is not None else None,
        "offers_count": len(selection.prices),
        "sources_count": len(per_source),
        "confidence": quality.confidence_level.value,
    }

    expected_offers = _load_expected(GOLDEN_ROOT / "expected_offers" / f"{name}.json")
    expected_metrics = _load_expected(GOLDEN_ROOT / "expected_metrics" / f"{name}.json")
    expected = {**expected_offers, **expected_metrics}

    failures = [
        f"{key}: ожидалось {value!r}, получено {actual.get(key)!r}"
        for key, value in expected.items()
        if not _matches(value, actual.get(key))
    ]
    return CaseResult(case=name, passed=not failures, failures=failures, actual=actual)


def _load_expected(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _matches(expected: Any, actual: Any) -> bool:
    if isinstance(expected, float) and isinstance(actual, (int, float)):
        # Копеечная толерантность: сравнение денег до цента, не до бита.
        return abs(quantize(Decimal(str(expected))) - quantize(Decimal(str(actual)))) <= Decimal(
            "0.01"
        )
    return expected == actual


def run_golden_suite(*, profile_version: str | None = None, root: Path | None = None) -> dict[str, Any]:
    """Прогоняет весь набор. Результат подаётся в ворота публикации."""
    root = root or GOLDEN_ROOT
    raw_dir = root / "recorded_raw"
    cases = sorted(raw_dir.glob("*.json")) if raw_dir.exists() else []
    results = [run_case(path, profile_version=profile_version) for path in cases]
    failed = [result for result in results if not result.passed]
    return {
        "passed": not failed,
        "total": len(results),
        "failed": len(failed),
        "cases": [
            {"case": result.case, "passed": result.passed, "failures": result.failures}
            for result in results
        ],
        "failures": [
            {"case": result.case, "failures": result.failures, "actual": result.actual}
            for result in failed
        ],
    }
