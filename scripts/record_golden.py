"""Запись Golden Dataset из живых ответов источников.

Golden Dataset строится на **записанных ответах настоящих источников**, а не на
выдуманных структурах: только так тест ловит изменение источника, а не
изменение нашего представления о нём.

```bash
python scripts/record_golden.py --case rail_direct_compartment \
    --family RAIL --source tutu_mcp \
    --origin MOW --destination AER --service-date 2026-08-21
```

Ожидаемые значения (`expected_offers`, `expected_metrics`) скрипт считает
текущей методикой и записывает рядом. Это осознанный компромисс: эталон
фиксирует **сегодняшнее** поведение, чтобы завтрашнее изменение стало видимым.
Перед фиксацией числа обязаны быть проверены глазами — на то они и эталон.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tmo.catalog.registry import city_registry, source_registry
from tmo.connectors.contracts import AirQuery, HotelQuery, RailQuery
from tmo.connectors.registry import build_connector
from tmo.connectors.transport import TimeBudget
from tmo.core.enums import CollectionFamily
from tmo.core.logging import configure_logging
from tmo.services.golden import GOLDEN_ROOT, run_case


def build_query(args: argparse.Namespace):
    registry = city_registry()
    family = CollectionFamily(args.family)
    if family is CollectionFamily.RAIL:
        origin, destination = registry.get(args.origin), registry.get(args.destination)
        return RailQuery(
            origin_code=origin.code,
            origin_name=origin.name,
            origin_rzd_code=origin.rzd_express_code,
            destination_code=destination.code,
            destination_name=destination.name,
            destination_rzd_code=destination.rzd_express_code,
            service_date=date.fromisoformat(args.service_date),
        )
    if family is CollectionFamily.AIR:
        origin, destination = registry.get(args.origin), registry.get(args.destination)
        return AirQuery(
            origin_code=origin.code,
            origin_name=origin.name,
            origin_metro_code=origin.avia_metro_code,
            destination_code=destination.code,
            destination_name=destination.name,
            destination_metro_code=destination.avia_metro_code,
            departure_date=date.fromisoformat(args.service_date),
            return_date=date.fromisoformat(args.return_date),
        )
    city = registry.get(args.city)
    return HotelQuery(
        city_code=city.code,
        city_name=city.name,
        check_in=date.fromisoformat(args.check_in),
        check_out=date.fromisoformat(args.check_out),
        stars=args.stars,
    )


def query_payload(query, family: CollectionFamily) -> dict:
    if family is CollectionFamily.RAIL:
        return {
            "origin_code": query.origin_code,
            "origin_name": query.origin_name,
            "origin_rzd_code": query.origin_rzd_code,
            "destination_code": query.destination_code,
            "destination_name": query.destination_name,
            "destination_rzd_code": query.destination_rzd_code,
            "service_date": query.service_date.isoformat(),
            "passengers": query.passengers,
        }
    if family is CollectionFamily.AIR:
        return {
            "origin_code": query.origin_code,
            "origin_name": query.origin_name,
            "origin_metro_code": query.origin_metro_code,
            "destination_code": query.destination_code,
            "destination_name": query.destination_name,
            "destination_metro_code": query.destination_metro_code,
            "departure_date": query.departure_date.isoformat(),
            "return_date": query.return_date.isoformat(),
            "adults": query.adults,
        }
    return {
        "city_code": query.city_code,
        "city_name": query.city_name,
        "check_in": query.check_in.isoformat(),
        "check_out": query.check_out.isoformat(),
        "stars": query.stars,
        "adults": query.adults,
        "rooms": query.rooms,
    }


def write_case(
    case: str,
    family: CollectionFamily,
    source: str,
    query_dict: dict,
    payload: object,
    note: str = "",
) -> Path:
    raw_dir = GOLDEN_ROOT / "recorded_raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    path = raw_dir / f"{case}.json"
    path.write_text(
        json.dumps(
            {
                "case": case,
                "family": family.value,
                "source": source,
                "source_code": source,
                "note": note,
                "recorded_at": date.today().isoformat(),
                "query": query_dict,
                "payload": payload,
            },
            ensure_ascii=False,
            indent=1,
        ),
        encoding="utf-8",
    )
    return path


def write_expected(case: str) -> dict:
    """Считает ожидаемые значения текущей методикой и записывает их."""
    result = run_case(GOLDEN_ROOT / "recorded_raw" / f"{case}.json")
    actual = result.actual

    offers_dir = GOLDEN_ROOT / "expected_offers"
    metrics_dir = GOLDEN_ROOT / "expected_metrics"
    offers_dir.mkdir(parents=True, exist_ok=True)
    metrics_dir.mkdir(parents=True, exist_ok=True)

    (offers_dir / f"{case}.json").write_text(
        json.dumps(
            {
                key: actual[key]
                for key in (
                    "parsed_offers",
                    "normalized_offers",
                    "dropped_without_price",
                    "included_offers",
                    "excluded_offers",
                    "exclusion_reasons",
                )
            },
            ensure_ascii=False,
            indent=1,
        ),
        encoding="utf-8",
    )
    (metrics_dir / f"{case}.json").write_text(
        json.dumps(
            {
                key: actual[key]
                for key in ("median", "min", "offers_count", "sources_count", "confidence")
            },
            ensure_ascii=False,
            indent=1,
        ),
        encoding="utf-8",
    )
    return actual


def main() -> int:
    parser = argparse.ArgumentParser(description="Запись Golden Dataset")
    parser.add_argument("--case", required=True)
    parser.add_argument("--family", required=True, choices=[f.value for f in CollectionFamily])
    parser.add_argument("--source", default="tutu_mcp")
    parser.add_argument("--origin")
    parser.add_argument("--destination")
    parser.add_argument("--city")
    parser.add_argument("--service-date")
    parser.add_argument("--return-date")
    parser.add_argument("--check-in")
    parser.add_argument("--check-out")
    parser.add_argument("--stars", type=int, default=3)
    parser.add_argument("--note", default="")
    parser.add_argument(
        "--from-file",
        help="взять сырой ответ из файла вместо обращения к источнику",
    )
    args = parser.parse_args()

    configure_logging("WARNING", "text")
    family = CollectionFamily(args.family)
    query = build_query(args)

    if args.from_file:
        payload = json.loads(Path(args.from_file).read_text(encoding="utf-8"))
    else:
        source = source_registry().get(args.source)
        connector = build_connector(source)
        result = connector.collect(query, TimeBudget(total_seconds=180))
        if not result.raw_artifacts:
            print(f"Источник не вернул сырого ответа: {result.outcome} {result.error_message}")
            return 1
        payload = result.raw_artifacts[0].payload

    path = write_case(args.case, family, args.source, query_payload(query, family), payload, args.note)
    actual = write_expected(args.case)
    print(json.dumps({"case": args.case, "file": str(path), "expected": actual},
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
