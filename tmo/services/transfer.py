"""Перенос снимков между стендами.

Снимок — это девять связанных таблиц, и перенести его копированием строк
нельзя: первичные ключи на приёмнике заняты своими. Поэтому выгрузка пишет
строки как есть, а загрузка **переназначает** все идентификаторы по карте
«старый → новый», в порядке зависимостей.

Два уровня, потому что два разных вопроса.

``showcase``
    всё, что нужно витрине: снимок, план, наблюдения, попытки, расчёт, метрики,
    витрина поездок, сводка по источникам. Единицы мегабайт — проходит через
    git-репозиторий.

``evidence``
    то же плюс ``offers``, ``raw_responses`` и ``metric_offer_links``. Сотни
    мегабайт: один снимок — это 442 305 предложений и столько же связей. В
    git-репозиторий такое класть нельзя ни разу, ни тем более ежедневно.

**Цена уровня ``showcase`` названа прямо.** Без предложений перестаёт работать
раскрытие цифры до конкретного предложения конкретного источника — принцип, из
которого выросла вся система. Поэтому снимок помечается ``evidence_included``,
и витрина обязана это показывать: пустой список предложений иначе читается как
дефект расчёта, а не как отсутствие переноса.

Формат — NDJSON, по файлу на таблицу, gzip. Не SQL-дамп: дамп привязан к версии
схемы и к именам последовательностей, а нам нужно грузить в живую базу, у
которой уже есть свои снимки.
"""

from __future__ import annotations

import gzip
import hashlib
import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date, datetime
from functools import cache
from pathlib import Path
from typing import Any

from sqlalchemy import func, insert, select
from sqlalchemy.orm import Session

from tmo.catalog.registry import available_profiles
from tmo.core.logging import get_logger
from tmo.core.timeutil import now_utc
from tmo.db import models
from tmo.version import APP_VERSION

logger = get_logger(__name__)

#: Версия формата выгрузки. Меняется при любом изменении состава файлов или
#: смысла полей: загрузка обязана отказаться от того, чего не понимает, а не
#: догадываться.
BUNDLE_FORMAT = 2

SHOWCASE = "showcase"
EVIDENCE = "evidence"


class ImportRefused(Exception):
    """Загрузка невозможна и продолжать нельзя.

    Отдельный тип, потому что все причины отказа здесь — про достоверность, а не
    про технику: чужая версия формата, отсутствующая методика, испорченный файл,
    путь, уводящий за пределы каталога. Подставить умолчание в любом из этих
    случаев значит получить на витрине цифры, которые никто не считал.
    """


@dataclass(frozen=True, slots=True)
class TableSpec:
    """Таблица в выгрузке и её место в графе зависимостей.

    ``parents`` — поля, которые на приёмнике придётся переписать новыми
    идентификаторами: имя поля → имя таблицы, чью карту брать.
    """

    name: str
    model: type
    parents: dict[str, str] = field(default_factory=dict)
    level: str = SHOWCASE


#: Порядок обязателен: он же порядок загрузки. Ребёнок не может быть записан
#: раньше родителя — его внешний ключ ещё некуда указывать.
TABLES: tuple[TableSpec, ...] = (
    TableSpec("market_snapshots", models.MarketSnapshot),
    TableSpec("collection_plans", models.CollectionPlan, {"snapshot_id": "market_snapshots"}),
    TableSpec("collection_jobs", models.CollectionJob, {"snapshot_id": "market_snapshots"}),
    TableSpec(
        "source_attempts",
        models.SourceAttempt,
        {"snapshot_id": "market_snapshots", "collection_job_id": "collection_jobs"},
    ),
    TableSpec(
        "raw_responses",
        models.RawResponse,
        {"snapshot_id": "market_snapshots", "source_attempt_id": "source_attempts"},
        level=EVIDENCE,
    ),
    TableSpec(
        "offers",
        models.Offer,
        {
            "snapshot_id": "market_snapshots",
            "collection_job_id": "collection_jobs",
            "source_attempt_id": "source_attempts",
            "raw_response_id": "raw_responses",
        },
        level=EVIDENCE,
    ),
    TableSpec(
        "snapshot_source_results",
        models.SnapshotSourceResult,
        {"snapshot_id": "market_snapshots"},
    ),
    TableSpec("calculation_runs", models.CalculationRun, {"snapshot_id": "market_snapshots"}),
    TableSpec(
        "calculated_metrics",
        models.CalculatedMetric,
        {
            "snapshot_id": "market_snapshots",
            "calculation_run_id": "calculation_runs",
            "collection_job_id": "collection_jobs",
        },
    ),
    TableSpec(
        "metric_offer_links",
        models.MetricOfferLink,
        {"metric_id": "calculated_metrics", "offer_id": "offers"},
        level=EVIDENCE,
    ),
    TableSpec(
        "trip_cost_mart",
        models.TripCostRow,
        {"snapshot_id": "market_snapshots", "calculation_run_id": "calculation_runs"},
    ),
)


def tables_for(level: str) -> tuple[TableSpec, ...]:
    if level == EVIDENCE:
        return TABLES
    return tuple(spec for spec in TABLES if spec.level == SHOWCASE)


# --------------------------------------------------------------------------- #
# Выгрузка
# --------------------------------------------------------------------------- #


def _columns(model: type) -> list[str]:
    return [column.name for column in model.__table__.columns]


def _jsonable(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, (dict, list, str, int, float, bool)) or value is None:
        return value
    return str(value)


def _rows(session: Session, spec: TableSpec, snapshot_id: int) -> Iterator[dict[str, Any]]:
    """Строки таблицы, относящиеся к снимку.

    Две таблицы не знают о снимке напрямую и отбираются через родителя:
    ``metric_offer_links`` — через метрики, и это единственный случай, когда
    отбор идёт подзапросом. Список идентификаторов вместо подзапроса здесь
    неприемлем: их 442 305, и планировщик на таком ``IN`` перестаёт пользоваться
    индексом (COLLECTION_RELIABILITY, «Удаление данных»).
    """
    model = spec.model
    columns = _columns(model)

    if spec.name == "metric_offer_links":
        query = select(model).where(
            model.metric_id.in_(
                select(models.CalculatedMetric.id).where(
                    models.CalculatedMetric.snapshot_id == snapshot_id
                )
            )
        )
    elif spec.name == "market_snapshots":
        query = select(model).where(model.id == snapshot_id)
    else:
        query = select(model).where(model.snapshot_id == snapshot_id)

    for row in session.scalars(query.execution_options(yield_per=5000)):
        yield {name: _jsonable(getattr(row, name)) for name in columns}


def _write_table(path: Path, rows: Iterator[dict[str, Any]]) -> tuple[int, str]:
    """Пишет NDJSON.gz и возвращает число строк и sha256 сжатого файла."""
    digest = hashlib.sha256()
    count = 0
    with gzip.open(path, "wt", encoding="utf-8", compresslevel=6) as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, default=str))
            handle.write("\n")
            count += 1
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return count, digest.hexdigest()


def export_snapshot(
    session: Session,
    snapshot_id: int,
    destination: Path,
    *,
    level: str = SHOWCASE,
    origin_stand: str = "",
) -> dict[str, Any]:
    """Выгружает снимок в каталог. Возвращает манифест."""
    if level not in (SHOWCASE, EVIDENCE):
        raise ValueError(f"Неизвестный уровень выгрузки: {level!r}")

    snapshot = session.get(models.MarketSnapshot, snapshot_id)
    if snapshot is None:
        raise ValueError(f"Снимок {snapshot_id} не найден")

    run = session.scalars(
        select(models.CalculationRun)
        .where(models.CalculationRun.snapshot_id == snapshot_id)
        .order_by(models.CalculationRun.id.desc())
        .limit(1)
    ).first()

    destination.mkdir(parents=True, exist_ok=True)
    files: dict[str, dict[str, Any]] = {}
    for spec in tables_for(level):
        path = destination / f"{spec.name}.ndjson.gz"
        count, digest = _write_table(path, _rows(session, spec, snapshot_id))
        files[spec.name] = {"rows": count, "sha256": digest, "bytes": path.stat().st_size}
        logger.info("Таблица выгружена", table=spec.name, rows=count)

    manifest = {
        "bundle_format": BUNDLE_FORMAT,
        "app_version": APP_VERSION,
        "level": level,
        "evidence_included": level == EVIDENCE,
        "origin_stand": origin_stand,
        "exported_at": now_utc().isoformat(),
        "snapshot": {
            "snapshot_date": snapshot.snapshot_date.isoformat(),
            "attempt_no": snapshot.attempt_no,
            "status": str(snapshot.status),
            "is_synthetic": bool(snapshot.is_synthetic),
            "is_partial_scope": bool(snapshot.is_partial_scope),
            "coverage_total": round(float(snapshot.coverage_total), 4),
        },
        "methodology_version": run.methodology_version if run else None,
        "files": files,
    }
    # Цифровой отпечаток содержимого, а не файла манифеста: по нему приёмник
    # узнаёт повторную загрузку того же снимка независимо от имени каталога и
    # времени выгрузки.
    manifest["content_digest"] = hashlib.sha256(
        json.dumps(
            {name: meta["sha256"] for name, meta in sorted(files.items())},
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()

    (destination / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    # `level` здесь занят самим логгером — поле называется `bundle_level`.
    logger.info(
        "Снимок выгружен",
        snapshot_id=snapshot_id,
        bundle_level=level,
        digest=manifest["content_digest"][:12],
    )
    return manifest


# --------------------------------------------------------------------------- #
# Загрузка
# --------------------------------------------------------------------------- #


def archive_snapshot(
    session: Session,
    snapshot_id: int,
    destination: Path,
    *,
    level: str = EVIDENCE,
    origin_stand: str = "",
) -> tuple[Path, dict[str, Any]]:
    """Выгружает снимок одним файлом ``.tar.gz``.

    Каталог из десятка файлов удобен машине и неудобен человеку: перенос идёт
    через браузер — скачать один файл и загрузить один файл. Внутри тот же
    формат, что и у каталожной выгрузки, поэтому обе дороги ведут в одну
    загрузку.

    Файлы внутри уже сжаты, поэтому tar пишется без второго сжатия: gzip
    поверх gzip даёт минус проценты и плюс минуты.
    """
    import tarfile
    import tempfile

    with tempfile.TemporaryDirectory(prefix="tmo-archive-") as workdir:
        bundle = Path(workdir) / "bundle"
        manifest = export_snapshot(
            session, snapshot_id, bundle, level=level, origin_stand=origin_stand
        )
        snapshot_date = manifest["snapshot"]["snapshot_date"]
        attempt = manifest["snapshot"]["attempt_no"]
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tarfile.open(destination, "w") as archive:
            for path in sorted(bundle.iterdir()):
                archive.add(path, arcname=f"{snapshot_date}-v{attempt}/{path.name}")

    logger.info(
        "Снимок упакован",
        snapshot_id=snapshot_id,
        bundle_level=level,
        bytes=destination.stat().st_size,
    )
    return destination, manifest


def archive_name(snapshot_date: date | str, attempt_no: int, level: str) -> str:
    """Имя файла архива. Дата, версия и уровень — прямо в имени.

    Файл переезжает через браузер и папку «Загрузки», где теряется всякий
    контекст. Имя — единственное, что доедет вместе с ним.
    """
    day = snapshot_date if isinstance(snapshot_date, str) else snapshot_date.isoformat()
    return f"tmo-snapshot-{day}-v{attempt_no}-{level}.tar"


def extract_archive(archive: Path, destination: Path) -> Path:
    """Распаковывает архив и возвращает каталог с ``manifest.json``.

    Архив приходит из браузера, то есть извне. Распаковка чужого tar — это
    запись по путям, которые задал автор архива: ``../`` в имени участника
    уводит запись куда угодно, символьная ссылка — тем более. Поэтому
    применяется фильтр ``data`` и проверка того, что каждый участник остаётся
    внутри каталога назначения.
    """
    import tarfile

    destination.mkdir(parents=True, exist_ok=True)
    resolved = destination.resolve()

    with tarfile.open(archive, "r:*") as handle:
        for member in handle.getmembers():
            if member.islnk() or member.issym():
                raise ImportRefused(f"Ссылки в архиве не допускаются: {member.name}")
            target = (resolved / member.name).resolve()
            if not target.is_relative_to(resolved):
                raise ImportRefused(f"Путь участника выходит за каталог: {member.name}")
        handle.extractall(resolved, filter="data")

    manifests = list(resolved.rglob("manifest.json"))
    if not manifests:
        raise ImportRefused("В архиве нет manifest.json — это не выгрузка снимка")
    return manifests[0].parent


def import_archive(
    session: Session, archive: Path, *, force: bool = False, batch_size: int = 5000
) -> dict[str, Any]:
    """Загружает снимок из одного файла ``.tar.gz``."""
    import tempfile

    with tempfile.TemporaryDirectory(prefix="tmo-import-") as workdir:
        bundle = extract_archive(archive, Path(workdir))
        return import_snapshot(session, bundle, force=force, batch_size=batch_size)


def _read_table(path: Path) -> Iterator[dict[str, Any]]:
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                yield json.loads(line)


def _verify(path: Path, expected: str) -> None:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    if digest.hexdigest() != expected:
        raise ImportRefused(
            f"Контрольная сумма {path.name} не совпадает: файл повреждён или подменён"
        )


@cache
def _temporal_columns(model: type) -> tuple[tuple[str, str], ...]:
    """Колонки, которые в JSON становятся строками и обязаны вернуться типами.

    NDJSON не знает ни дат, ни моментов времени: выгрузка пишет их строками
    ISO. Отдать такую строку драйверу нельзя — SQLite отвергает её сразу, а
    PostgreSQL молча примет и запишет текст в поле времени.
    """
    result: list[tuple[str, str]] = []
    for column in model.__table__.columns:
        kind = column.type.__class__.__name__.upper()
        if "DATETIME" in kind:
            result.append((column.name, "datetime"))
        elif "DATE" in kind:
            result.append((column.name, "date"))
    return tuple(result)


def _coerce_temporal(row: dict[str, Any], model: type) -> None:
    for name, kind in _temporal_columns(model):
        value = row.get(name)
        if not isinstance(value, str):
            continue
        row[name] = (
            datetime.fromisoformat(value) if kind == "datetime" else date.fromisoformat(value)
        )


def _chunks(rows: Iterator[dict[str, Any]], size: int) -> Iterator[list[dict[str, Any]]]:
    batch: list[dict[str, Any]] = []
    for row in rows:
        batch.append(row)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


def _remap_parents(
    row: dict[str, Any], spec: TableSpec, id_maps: dict[str, dict[int, int]]
) -> None:
    """Переписывает внешние ключи строки новыми идентификаторами."""
    for column, parent in spec.parents.items():
        value = row.get(column)
        if value is None:
            continue
        remapped = id_maps.get(parent, {}).get(int(value))
        # Родитель не переносился — уровень showcase не тащит предложения, а на
        # них ссылается raw_response_id. Связь обнуляется, а не выдумывается:
        # ссылка на чужую строку хуже отсутствующей.
        row[column] = remapped


def _imported_key(original: str, content_digest: str, attempt_no: int) -> str:
    """Ключ идемпотентности загруженной попытки.

    ``idempotency_key`` уникален по всей базе, а собирается из даты снимка,
    наблюдения, источника и области исполнения — и ничего не знает о том, кто
    собирал. Два стенда, собравшие **одну дату**, порождают побайтово
    одинаковые ключи, поэтому загрузка снимка с соседнего стенда упирается в
    уникальность даже на чистой базе.

    Пространство имён — отпечаток выгрузки **и номер попытки**. Отпечатка
    одного мало: один и тот же архив разрешено загрузить дважды, и обе копии
    получили бы одинаковые ключи. Номер попытки у копий разный по построению.

    Смысл ключа при этом не теряется: он защищает от повторной записи попытки
    внутри одного прогона, а прогон, породивший эти попытки, шёл на другом
    стенде и здесь не повторится.
    """
    prefix = f"i{content_digest[:6]}a{attempt_no}"
    # Колонка — 64 символа. Обрезается исходный ключ, а не префикс: без
    # префикса пропадает вся защита, а исходный ключ и так лишь ссылка на
    # прогон, которого на этом стенде нет.
    return f"{prefix}:{original}"[:64]


def _next_attempt_no(session: Session, snapshot_date: date) -> int:
    """Следующий свободный номер попытки за эту дату.

    Отсюда и берутся версии v1, v2 на витрине. Сохранить исходный номер нельзя:
    он занят снимком, собранным здесь, а уникальность пары «дата + попытка»
    держит база.
    """
    current = session.scalar(
        select(func.max(models.MarketSnapshot.attempt_no)).where(
            models.MarketSnapshot.snapshot_date == snapshot_date
        )
    )
    return int(current or 0) + 1


def already_imported(session: Session, content_digest: str) -> models.MarketSnapshot | None:
    return session.scalars(
        select(models.MarketSnapshot)
        .where(models.MarketSnapshot.source_digest == content_digest)
        .order_by(models.MarketSnapshot.attempt_no.desc())
    ).first()


def import_snapshot(
    session: Session, source: Path, *, batch_size: int = 5000, force: bool = False
) -> dict[str, Any]:
    """Загружает выгруженный снимок, переназначая все идентификаторы.

    ``force`` разрешает загрузить снимок, который здесь уже есть. Без него
    повторная загрузка того же файла возвращает ``DUPLICATE`` и не пишет
    ничего: принести один и тот же архив дважды — обычное дело, и молча
    удвоить версии значило бы наказать за неосторожность.

    Решение принимает человек, а не код: с ``force`` копия ложится следующей
    версией той же даты — v2, v3, — и обе остаются видимыми.
    """
    manifest_path = source / "manifest.json"
    if not manifest_path.exists():
        raise ImportRefused(f"В {source} нет manifest.json — это не выгрузка снимка")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    if manifest.get("bundle_format") != BUNDLE_FORMAT:
        raise ImportRefused(
            f"Формат выгрузки {manifest.get('bundle_format')} не совпадает с "
            f"поддерживаемым {BUNDLE_FORMAT}"
        )

    version = manifest.get("methodology_version")
    if version and version not in available_profiles():
        # Подставить активную методику было бы худшим из возможных решений:
        # цифры остались бы прежними, а объяснение к ним — чужим.
        raise ImportRefused(
            f"Методика {version} на этом стенде не зарегистрирована. "
            f"Доступны: {', '.join(available_profiles())}"
        )

    existing = already_imported(session, manifest["content_digest"])
    if existing is not None and not force:
        logger.info("Снимок уже загружен", snapshot_id=existing.id)
        return {
            "status": "DUPLICATE",
            "snapshot_id": existing.id,
            "snapshot_date": existing.snapshot_date.isoformat(),
            "attempt_no": existing.attempt_no,
            "version_label": f"v{existing.attempt_no}",
            "imported_at": existing.imported_at.isoformat() if existing.imported_at else None,
            "origin_stand": existing.origin_stand,
        }

    level = manifest.get("level", SHOWCASE)
    specs = tables_for(level)
    for spec in specs:
        path = source / f"{spec.name}.ndjson.gz"
        if not path.exists():
            raise ImportRefused(f"В выгрузке нет {path.name}, объявленного уровнем {level}")
        _verify(path, manifest["files"][spec.name]["sha256"])

    snapshot_date = date.fromisoformat(manifest["snapshot"]["snapshot_date"])
    attempt_no = _next_attempt_no(session, snapshot_date)
    id_maps: dict[str, dict[int, int]] = {}
    totals: dict[str, int] = {}

    referenced = {parent for spec in specs for parent in spec.parents.values()}

    for spec in specs:
        path = source / f"{spec.name}.ndjson.gz"
        mapping: dict[int, int] = {}
        written = 0
        needs_map = spec.name in referenced

        for chunk in _chunks(_read_table(path), batch_size):
            old_ids: list[int] = []
            rows: list[dict[str, Any]] = []
            for row in chunk:
                old_ids.append(int(row.pop("id")))
                _remap_parents(row, spec, id_maps)
                _coerce_temporal(row, spec.model)
                if spec.name == "source_attempts":
                    row["idempotency_key"] = _imported_key(
                        row["idempotency_key"], manifest["content_digest"], attempt_no
                    )
                if spec.name == "market_snapshots":
                    row["attempt_no"] = attempt_no
                    row["source_digest"] = manifest["content_digest"]
                    row["origin_stand"] = manifest.get("origin_stand") or "неизвестен"
                    row["imported_at"] = now_utc()
                    row["evidence_included"] = bool(manifest.get("evidence_included"))
                rows.append(row)

            if needs_map:
                # Порядок возврата идентификаторов задаётся явно. Без этого
                # соответствие «старый → новый» держалось бы на неоговорённом
                # поведении СУБД, и карта однажды перемешалась бы молча — с
                # предложениями, привязанными к чужим наблюдениям.
                result = session.execute(
                    insert(spec.model).returning(spec.model.id),
                    rows,
                    execution_options={"sort_by_parameter_order": True},
                )
                new_ids = [int(value) for value in result.scalars()]
                if len(new_ids) != len(old_ids):
                    raise ImportRefused(
                        f"{spec.name}: записано {len(new_ids)} строк из {len(old_ids)}"
                    )
                mapping.update(zip(old_ids, new_ids, strict=True))
            else:
                session.execute(insert(spec.model), rows)
            written += len(rows)

        session.flush()
        id_maps[spec.name] = mapping
        totals[spec.name] = written
        logger.info("Таблица загружена", table=spec.name, rows=written)

    snapshot_id = next(iter(id_maps["market_snapshots"].values()))
    logger.info(
        "Снимок загружен",
        snapshot_id=snapshot_id,
        snapshot_date=snapshot_date.isoformat(),
        attempt_no=attempt_no,
        bundle_level=level,
    )
    return {
        "status": "IMPORTED",
        "snapshot_id": snapshot_id,
        "snapshot_date": snapshot_date.isoformat(),
        "attempt_no": attempt_no,
        "version_label": f"v{attempt_no}",
        "level": level,
        "evidence_included": bool(manifest.get("evidence_included")),
        "rows": totals,
    }
