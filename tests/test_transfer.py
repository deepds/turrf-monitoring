"""Перенос снимка между стендами.

Проверяется не «файлы записались», а то, ради чего перенос делается: снимок,
загруженный на другом стенде, обязан **показываться витриной** и показываться
как отдельная версия, не подменяя собранный здесь.

Круг «выгрузить → загрузить в ту же базу» тем и хорош, что моделирует худший
случай: все идентификаторы заняты, и любая ошибка переназначения проявится
сразу — связями, ведущими на чужие строки.
"""

from __future__ import annotations

from datetime import date

import pytest

from tmo.services.pipeline import run_daily_pipeline
from tmo.services.transfer import EVIDENCE, SHOWCASE, ImportRefused, export_snapshot, import_snapshot

DAY = date(2026, 8, 9)


@pytest.fixture()
def collected_snapshot(database: str):
    """Полный снимок на воспроизведённых ответах: план, сбор, расчёт, витрина.

    Все три семейства, а не одно: строка витрины поездок — это поездка целиком,
    транспорт плюс проживание на пару дат. Снимок без строк витрины не проверил
    бы главного — что перенесённое действительно показывается.
    """
    report = run_daily_pipeline(
        snapshot_date=DAY,
        horizon_days=3,
        replay_mode="synthetic",
        is_synthetic=True,
    )
    return report


def _export(session, snapshot_id, tmp_path, level):
    return export_snapshot(
        session, snapshot_id, tmp_path / "bundle", level=level, origin_stand="node67"
    )


def test_showcase_bundle_carries_the_showcase(collected_snapshot, tmp_path) -> None:
    """Уровень showcase обязан нести всё, чем витрина рисует цифры."""
    from tmo.db.session import session_scope

    with session_scope() as session:
        manifest = _export(session, collected_snapshot.snapshot_id, tmp_path, SHOWCASE)

    assert manifest["files"]["calculated_metrics"]["rows"] > 0
    assert manifest["files"]["trip_cost_mart"]["rows"] > 0
    assert manifest["files"]["calculation_runs"]["rows"] == 1
    # И не несёт того, что не обещал.
    assert "offers" not in manifest["files"]
    assert manifest["evidence_included"] is False


def test_import_creates_a_new_version_of_the_same_date(collected_snapshot, tmp_path) -> None:
    """Загруженный снимок становится следующей версией своей даты.

    Сохранить исходный номер попытки нельзя: он занят снимком, собранным здесь,
    и уникальность пары «дата + попытка» держит база. Отсюда и берутся v1, v2.
    """
    from tmo.db.session import session_scope
    from tmo.services.snapshot import available_snapshot_dates

    with session_scope() as session:
        _export(session, collected_snapshot.snapshot_id, tmp_path, SHOWCASE)

    with session_scope() as session:
        result = import_snapshot(session, tmp_path / "bundle")

    assert result["status"] == "IMPORTED"
    assert result["attempt_no"] == 2
    assert result["version_label"] == "v2"

    with session_scope() as session:
        listed = available_snapshot_dates(session)
    versions = [v["label"] for v in listed[0]["versions"]]
    assert versions == ["v2", "v1"], "обе версии обязаны быть видны витрине"


def test_imported_snapshot_renders_in_the_showcase(collected_snapshot, tmp_path) -> None:
    """Главная проверка: по загруженному снимку витрина отдаёт цифры.

    Перенос, после которого снимок лежит в базе и не показывается, бесполезен.
    """
    from tmo.db.session import session_scope
    from tmo.services.showcase import resolve_context

    with session_scope() as session:
        _export(session, collected_snapshot.snapshot_id, tmp_path, SHOWCASE)
    with session_scope() as session:
        imported = import_snapshot(session, tmp_path / "bundle")

    with session_scope() as session:
        context = resolve_context(
            session, snapshot_date=DAY, attempt_no=imported["attempt_no"]
        )
        assert context.snapshot.id == imported["snapshot_id"]
        assert context.run is not None, "у загруженного снимка обязан быть расчёт"
        payload = context.as_dict()

    assert payload["version_label"] == "v2"
    assert payload["coverage_total"] > 0


def test_links_point_inside_the_imported_snapshot(collected_snapshot, tmp_path) -> None:
    """Связи обязаны вести на строки загруженного снимка, а не исходного.

    Это и есть та ошибка, ради которой круг гоняется в одной базе: при
    переназначении идентификаторов достаточно один раз забыть переписать
    внешний ключ, и метрика новой версии сошлётся на предложения старой. Цифры
    при этом останутся правдоподобными.
    """
    from sqlalchemy import select

    from tmo.db import models
    from tmo.db.session import session_scope

    with session_scope() as session:
        _export(session, collected_snapshot.snapshot_id, tmp_path, EVIDENCE)
    with session_scope() as session:
        imported = import_snapshot(session, tmp_path / "bundle")

    new_id = imported["snapshot_id"]
    with session_scope() as session:
        metrics = list(
            session.scalars(
                select(models.CalculatedMetric).where(
                    models.CalculatedMetric.snapshot_id == new_id
                )
            )
        )
        assert metrics, "метрики загруженного снимка не найдены"

        foreign = session.scalar(
            select(models.MetricOfferLink)
            .join(models.Offer, models.Offer.id == models.MetricOfferLink.offer_id)
            .join(
                models.CalculatedMetric,
                models.CalculatedMetric.id == models.MetricOfferLink.metric_id,
            )
            .where(
                models.CalculatedMetric.snapshot_id == new_id,
                models.Offer.snapshot_id != new_id,
            )
        )
        assert foreign is None, "связь метрики ведёт на предложение чужого снимка"

        stray = session.scalar(
            select(models.CalculatedMetric).where(
                models.CalculatedMetric.snapshot_id == new_id,
                models.CalculatedMetric.collection_job_id.in_(
                    select(models.CollectionJob.id).where(
                        models.CollectionJob.snapshot_id != new_id
                    )
                ),
            )
        )
        assert stray is None, "метрика ссылается на наблюдение чужого снимка"


def test_same_bundle_is_not_imported_twice(collected_snapshot, tmp_path) -> None:
    """Повторная загрузка того же файла не плодит версии.

    Перенос идёт через репозиторий и разворачивается руками; принести один и
    тот же каталог дважды — обычное дело, а не исключительная ситуация.
    """
    from tmo.db.session import session_scope

    with session_scope() as session:
        _export(session, collected_snapshot.snapshot_id, tmp_path, SHOWCASE)
    with session_scope() as session:
        first = import_snapshot(session, tmp_path / "bundle")
    with session_scope() as session:
        second = import_snapshot(session, tmp_path / "bundle")

    assert first["status"] == "IMPORTED"
    assert second["status"] == "ALREADY_IMPORTED"
    assert second["snapshot_id"] == first["snapshot_id"]


def test_corrupted_bundle_is_refused(collected_snapshot, tmp_path) -> None:
    """Испорченный файл обязан остановить загрузку, а не пройти частично."""
    from tmo.db.session import session_scope

    with session_scope() as session:
        _export(session, collected_snapshot.snapshot_id, tmp_path, SHOWCASE)

    victim = tmp_path / "bundle" / "calculated_metrics.ndjson.gz"
    victim.write_bytes(victim.read_bytes() + b"\x00")

    with session_scope() as session, pytest.raises(ImportRefused, match="Контрольная сумма"):
        import_snapshot(session, tmp_path / "bundle")


def test_unknown_methodology_refuses_the_import(collected_snapshot, tmp_path) -> None:
    """Методики нет на приёмнике — загрузка останавливается.

    Подставить активную было бы худшим решением: цифры остались бы прежними, а
    объяснение к ним — чужим.
    """
    import json

    from tmo.db.session import session_scope

    with session_scope() as session:
        _export(session, collected_snapshot.snapshot_id, tmp_path, SHOWCASE)

    manifest_path = tmp_path / "bundle" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["methodology_version"] = "baseline_v99"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")

    with session_scope() as session, pytest.raises(ImportRefused, match="не зарегистрирована"):
        import_snapshot(session, tmp_path / "bundle")
