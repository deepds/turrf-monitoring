"""Замер источников: сколько стоит одно наблюдение и что источник выдерживает.

Скрипт нужен для capacity-анализа и калибровки порогов. Он обращается к живым
источникам, поэтому выборка намеренно мала: задача — измерить стоимость
обращения, а не собрать рынок.

```bash
python scripts/bench_sources.py --family AIR --concurrency 6 --samples 12
```

Результат печатается JSON-ом и складывается в docs/COLLECTION_CAPACITY_ANALYSIS.md
руками: автоматическая запись в документ скрыла бы, при каких условиях получен
замер.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tmo.catalog.registry import city_registry, source_registry
from tmo.connectors.contracts import AirQuery, HotelQuery, RailQuery
from tmo.connectors.registry import build_connector
from tmo.connectors.transport import TRANSPORT_POOL, TimeBudget
from tmo.core.enums import CollectionFamily
from tmo.core.logging import configure_logging


def build_queries(family: CollectionFamily, samples: int, base: date) -> list:
    registry = city_registry()
    pairs = registry.directed_pairs()
    queries = []
    index = 0
    while len(queries) < samples:
        origin, destination = pairs[index % len(pairs)]
        offset = 3 + (index % 25)
        if family is CollectionFamily.RAIL:
            queries.append(
                RailQuery(
                    origin_code=origin.code,
                    origin_name=origin.name,
                    origin_rzd_code=origin.rzd_express_code,
                    destination_code=destination.code,
                    destination_name=destination.name,
                    destination_rzd_code=destination.rzd_express_code,
                    service_date=base + timedelta(days=offset),
                )
            )
        elif family is CollectionFamily.AIR:
            queries.append(
                AirQuery(
                    origin_code=origin.code,
                    origin_name=origin.name,
                    origin_metro_code=origin.avia_metro_code,
                    destination_code=destination.code,
                    destination_name=destination.name,
                    destination_metro_code=destination.avia_metro_code,
                    departure_date=base + timedelta(days=offset),
                    return_date=base + timedelta(days=offset + 5),
                )
            )
        else:
            city = registry.ordered[index % len(registry.ordered)]
            stars = (3, 4, 5)[index % 3]
            check_in = base + timedelta(days=offset)
            queries.append(
                HotelQuery(
                    city_code=city.code,
                    city_name=city.name,
                    check_in=check_in,
                    check_out=check_in + timedelta(days=1),
                    stars=stars,
                )
            )
        index += 1
    return queries


def run(
    family: CollectionFamily,
    source_code: str,
    concurrency: int,
    samples: int,
    max_pages: int | None = None,
) -> dict:
    source = source_registry().get(source_code)
    overrides: dict = {"concurrency": concurrency}
    if max_pages:
        overrides["max_pages"] = max_pages
    source = source.model_copy(update=overrides)
    connector = build_connector(source)
    queries = build_queries(family, samples, date.today())

    started = time.perf_counter()

    def one(query):
        budget = TimeBudget(total_seconds=180)
        result = connector.collect(query, budget)
        return {
            "outcome": str(result.outcome),
            "latency_ms": result.latency_ms,
            "http_calls": result.http_calls,
            "pages_read": result.pages_read,
            "offers": result.offer_count,
            "total_matched": result.total_matched,
            "is_partial": result.is_partial,
            "error": result.error_code,
        }

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        rows = list(pool.map(one, queries))
    elapsed = time.perf_counter() - started

    latencies = sorted(row["latency_ms"] for row in rows if row["latency_ms"])
    ok = [row for row in rows if row["outcome"] in ("SUCCESS", "PARTIAL", "NO_MARKET")]
    failures: dict[str, int] = {}
    for row in rows:
        if row["outcome"] not in ("SUCCESS", "PARTIAL", "NO_MARKET"):
            failures[row["outcome"]] = failures.get(row["outcome"], 0) + 1

    total_calls = sum(row["http_calls"] for row in rows)
    return {
        "family": family.value,
        "source": source_code,
        "concurrency": concurrency,
        "max_pages": source.max_pages,
        "samples": samples,
        "elapsed_seconds": round(elapsed, 2),
        "ok": len(ok),
        "failures": failures,
        "http_calls_total": total_calls,
        "http_calls_per_observation": round(total_calls / max(1, len(rows)), 2),
        "calls_per_minute_achieved": round(total_calls / elapsed * 60, 1) if elapsed else 0,
        "observations_per_minute": round(len(rows) / elapsed * 60, 1) if elapsed else 0,
        "latency_ms": {
            "p50": latencies[len(latencies) // 2] if latencies else None,
            "p95": latencies[min(len(latencies) - 1, int(len(latencies) * 0.95))]
            if latencies
            else None,
            "max": latencies[-1] if latencies else None,
            "mean": round(statistics.mean(latencies)) if latencies else None,
        },
        "pages_read": {
            "mean": round(statistics.mean([row["pages_read"] for row in rows]), 2),
            "max": max(row["pages_read"] for row in rows),
        },
        "offers": {
            "mean": round(statistics.mean([row["offers"] for row in rows]), 1),
            "max": max(row["offers"] for row in rows),
        },
        "partial_share": round(sum(1 for row in rows if row["is_partial"]) / len(rows), 3),
        "rows": rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Замер стоимости обращений к источникам")
    parser.add_argument("--family", default="RAIL", choices=[f.value for f in CollectionFamily])
    parser.add_argument("--source", default="tutu_mcp")
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--samples", type=int, default=8)
    parser.add_argument("--max-pages", type=int, default=None)
    parser.add_argument("--rows", action="store_true", help="печатать построчный результат")
    args = parser.parse_args()

    configure_logging("WARNING", "text")
    TRANSPORT_POOL.reset()
    report = run(
        CollectionFamily(args.family), args.source, args.concurrency, args.samples, args.max_pages
    )
    if not args.rows:
        report.pop("rows", None)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
