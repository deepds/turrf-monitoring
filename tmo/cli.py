"""Командная строка.

Всё, что делает планировщик, можно сделать руками и посмотреть результат.
Это не удобство: расследование ночного отказа начинается с воспроизведения
шага в одиночку.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, is_dataclass
from datetime import date, timedelta
from typing import Any

from tmo.core.config import get_settings
from tmo.core.enums import CollectionFamily
from tmo.core.logging import configure_logging
from tmo.core.timeutil import snapshot_date_for


def _print(payload: Any) -> None:
    if is_dataclass(payload) and not isinstance(payload, type):
        payload = asdict(payload)
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def _parse_date(value: str | None) -> date | None:
    """Разбирает `--snapshot-date`.

    ``today`` означает текущий цикл сбора, а не календарный день: после 21:00
    ночной цикл работает уже над завтрашним снимком, и оператор, запустивший
    досбор в 22:30, должен попасть в тот же снимок, что и планировщик, а не
    завести второй за вчерашнюю дату.
    """
    if value in (None, "today"):
        return snapshot_date_for() if value == "today" else None
    if value == "yesterday":
        return snapshot_date_for() - timedelta(days=1)
    return date.fromisoformat(value)


def _origins(value: str | None) -> tuple[str, ...] | None:
    """Города отправления для ограниченного прогона.

    Пусто означает полную матрицу. Ограниченный прогон нужен, чтобы проверить
    конвейер целиком, не занимая источник на одиннадцать часов.
    """
    if not value or value == "all":
        return None
    return tuple(item.strip().upper() for item in value.split(",") if item.strip())


def _families(value: str | None) -> tuple[CollectionFamily, ...]:
    if not value or value == "all":
        return tuple(CollectionFamily)
    return tuple(CollectionFamily(item.strip().upper()) for item in value.split(","))


# --------------------------------------------------------------------------- #
# Команды
# --------------------------------------------------------------------------- #


def cmd_init_db(args: argparse.Namespace) -> int:
    """Создаёт схему напрямую. Для боевого стенда используйте миграции."""
    from tmo.db import models  # noqa: F401 — регистрация моделей
    from tmo.db.base import Base
    from tmo.db.session import get_engine

    Base.metadata.create_all(get_engine())
    _print({"status": "ok", "database": get_settings().database_url.split("@")[-1]})
    return 0


def cmd_plan(args: argparse.Namespace) -> int:
    from tmo.planner.matrix import build_matrix, expected_size

    target = _parse_date(args.snapshot_date) or snapshot_date_for()
    origins = _origins(getattr(args, "origins", None))
    matrix = build_matrix(
        target,
        horizon_days=args.horizon,
        families=_families(args.families),
        origins=origins,
    )
    _print(
        {
            "snapshot_date": target.isoformat(),
            "horizon_days": args.horizon,
            "origins": list(origins) if origins else "all",
            "planned": len(matrix),
            "by_family": matrix.counts_by_family(),
            "plan_digest": matrix.digest,
            "expected": expected_size(args.horizon),
        }
    )
    return 0


def cmd_collect(args: argparse.Namespace) -> int:
    from tmo.services.pipeline import run_daily_pipeline

    report = run_daily_pipeline(
        snapshot_date=_parse_date(args.snapshot_date),
        horizon_days=args.horizon,
        families=_families(args.families),
        replay_mode=args.replay,
        is_synthetic=bool(args.replay),
        recovery_rounds=args.recovery_rounds,
        batch_size=args.batch_size,
        soft_budget_seconds=args.soft_budget,
        origins=_origins(getattr(args, "origins", None)),
    )
    _print(report)
    return 0 if report.status != "FAILED" else 2


def cmd_demo(args: argparse.Namespace) -> int:
    """Демонстрационный снимок без обращения к источникам.

    Помечается ``is_synthetic`` и никогда не выдаётся за наблюдение рынка.
    """
    from tmo.services.pipeline import run_daily_pipeline

    report = run_daily_pipeline(
        snapshot_date=_parse_date(args.snapshot_date),
        horizon_days=args.horizon,
        replay_mode="SIMULATED",
        is_synthetic=True,
        recovery_rounds=0,
        batch_size=args.batch_size,
        soft_budget_seconds=args.soft_budget,
    )
    _print(report)
    return 0


def cmd_recalculate(args: argparse.Namespace) -> int:
    from sqlalchemy import select

    from tmo.db import models
    from tmo.db.session import session_scope
    from tmo.services.pipeline import recalculate

    target = _parse_date(args.snapshot_date) or snapshot_date_for()
    with session_scope() as session:
        snapshot_id = session.scalar(
            select(models.MarketSnapshot.id)
            .where(models.MarketSnapshot.snapshot_date == target)
            .order_by(models.MarketSnapshot.attempt_no.desc())
            .limit(1)
        )
    if snapshot_id is None:
        _print({"error": f"Снимок за {target} не найден"})
        return 1
    _print(recalculate(snapshot_id, profile_version=args.profile, make_active=not args.compare_only))
    return 0


def cmd_coverage(args: argparse.Namespace) -> int:
    from sqlalchemy import select

    from tmo.db import models
    from tmo.db.session import session_scope
    from tmo.services.coverage import compute_coverage

    target = _parse_date(args.snapshot_date) or snapshot_date_for()
    with session_scope() as session:
        snapshot_id = session.scalar(
            select(models.MarketSnapshot.id)
            .where(models.MarketSnapshot.snapshot_date == target)
            .order_by(models.MarketSnapshot.attempt_no.desc())
            .limit(1)
        )
        if snapshot_id is None:
            _print({"error": f"Снимок за {target} не найден"})
            return 1
        _print(compute_coverage(session, snapshot_id).as_dict())
    return 0


def cmd_check_sources(args: argparse.Namespace) -> int:
    """Живая проверка источников. Единственная команда, ходящая в сеть."""
    from tmo.catalog.registry import source_registry
    from tmo.connectors.registry import get_connector

    report = {}
    for source in source_registry().sources:
        if not source.is_enabled:
            continue
        try:
            report[source.code] = get_connector(source.code).health_check()
        except Exception as exc:
            report[source.code] = {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
    _print(report)
    return 0 if all(item.get("status") == "ok" for item in report.values()) else 1


def cmd_golden(args: argparse.Namespace) -> int:
    from tmo.services.golden import run_golden_suite

    result = run_golden_suite()
    _print(result)
    return 0 if result["passed"] else 1


def cmd_health(args: argparse.Namespace) -> int:
    from tmo.tasks.health import check

    healthy, message = check()
    _print({"healthy": healthy, "message": message})
    return 0 if healthy else 1


# --------------------------------------------------------------------------- #
# Разбор аргументов
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tmo", description="Мониторинг стоимости поездок: операции"
    )
    parser.add_argument("--log-level", default=None)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init-db", help="Создать схему базы").set_defaults(func=cmd_init_db)

    plan = sub.add_parser("plan", help="Показать матрицу наблюдений")
    plan.add_argument("--snapshot-date", default="today")
    plan.add_argument("--horizon", type=int, default=30)
    plan.add_argument("--families", default="all")
    plan.add_argument(
        "--origins",
        default="all",
        help="Города отправления через запятую, например MOW. По умолчанию все",
    )
    plan.set_defaults(func=cmd_plan)

    collect = sub.add_parser("collect", help="Полный суточный цикл")
    collect.add_argument("--snapshot-date", default="today")
    collect.add_argument("--horizon", type=int, default=30)
    collect.add_argument("--families", default="all")
    collect.add_argument(
        "--origins",
        default="all",
        help=(
            "Города отправления через запятую, например MOW. Ограниченный прогон "
            "на витрину не попадает"
        ),
    )
    collect.add_argument("--replay", default=None, choices=["SIMULATED", "RECORDED"])
    collect.add_argument("--recovery-rounds", type=int, default=2)
    collect.add_argument("--batch-size", type=int, default=None)
    collect.add_argument("--soft-budget", type=float, default=None)
    collect.set_defaults(func=cmd_collect)

    demo = sub.add_parser("demo-snapshot", help="Снимок на синтетических данных")
    demo.add_argument("--snapshot-date", default="today")
    demo.add_argument("--horizon", type=int, default=30)
    demo.add_argument("--days", type=int, default=None, help="Синоним --horizon")
    demo.add_argument("--batch-size", type=int, default=400)
    demo.add_argument("--soft-budget", type=float, default=600)
    demo.set_defaults(func=cmd_demo)

    recalc = sub.add_parser("recalculate", help="Пересчитать снимок другой методикой")
    recalc.add_argument("--snapshot-date", default="today")
    recalc.add_argument("--profile", default=None)
    recalc.add_argument("--compare-only", action="store_true")
    recalc.set_defaults(func=cmd_recalculate)

    coverage = sub.add_parser("coverage", help="Покрытие снимка")
    coverage.add_argument("--snapshot-date", default="today")
    coverage.set_defaults(func=cmd_coverage)

    sub.add_parser("check-sources", help="Живая проверка источников").set_defaults(
        func=cmd_check_sources
    )
    sub.add_parser("golden", help="Прогон Golden Dataset").set_defaults(func=cmd_golden)
    sub.add_parser("health", help="Проверка живости сбора").set_defaults(func=cmd_health)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    settings = get_settings()
    configure_logging(args.log_level or settings.log_level, settings.log_format)
    if getattr(args, "days", None):
        args.horizon = args.days
    return int(args.func(args) or 0)


if __name__ == "__main__":
    sys.exit(main())
