"""Хранилище исходных ответов источников.

Тела ответов не лежат в базе: их десятки тысяч в сутки и до полумегабайта
каждый. В базе — метаданные и ссылка, на диске — сжатое тело.

Ответ неизменяем. Имя файла содержит хеш содержимого, поэтому повторная
запись того же ответа не создаёт второй копии, а подмена содержимого задним
числом обнаруживается сверкой хеша.
"""

from __future__ import annotations

import gzip
import hashlib
import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from tmo.core.config import get_settings


@dataclass(frozen=True, slots=True)
class StoredPayload:
    storage_ref: str
    payload_bytes: int
    payload_sha256: str


class RawStore:
    """Файловое хранилище с раскладкой по дате снимка и источнику."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root or get_settings().raw_storage_path)

    def _directory(self, snapshot_date: date, source_code: str) -> Path:
        return self.root / snapshot_date.isoformat() / source_code

    def store(
        self,
        payload: Any,
        *,
        snapshot_date: date,
        source_code: str,
        family: str,
        job_key: str,
        page: int = 1,
    ) -> StoredPayload:
        body = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
        checksum = hashlib.sha256(body).hexdigest()
        directory = self._directory(snapshot_date, source_code)
        directory.mkdir(parents=True, exist_ok=True)
        name = f"{family.lower()}_{job_key.split(':')[-1][:16]}_p{page}_{checksum[:12]}.json.gz"
        path = directory / name
        if not path.exists():
            # Запись через временный файл: оборванная запись не должна
            # оставить полуфайл, который потом невозможно разобрать.
            temporary = path.with_suffix(".tmp")
            with gzip.open(temporary, "wb", compresslevel=6) as handle:
                handle.write(body)
            temporary.replace(path)
        return StoredPayload(
            storage_ref=str(path.relative_to(self.root)).replace("\\", "/"),
            payload_bytes=len(body),
            payload_sha256=checksum,
        )

    def load(self, storage_ref: str) -> Any:
        path = self.root / storage_ref
        if not path.exists():
            raise FileNotFoundError(f"Сырой ответ не найден: {storage_ref}")
        with gzip.open(path, "rb") as handle:
            return json.loads(handle.read().decode("utf-8"))

    def verify(self, storage_ref: str, expected_sha256: str) -> bool:
        """Сверяет хеш: подмена сырого ответа задним числом обнаружима."""
        path = self.root / storage_ref
        if not path.exists():
            return False
        with gzip.open(path, "rb") as handle:
            return hashlib.sha256(handle.read()).hexdigest() == expected_sha256

    def usage_bytes(self) -> int:
        return sum(p.stat().st_size for p in self.root.rglob("*.json.gz"))

    def purge_before(self, cutoff: date) -> int:
        """Удаляет тела ответов старше даты. Метаданные и Offers остаются.

        Retention применяется только к телам: без них нельзя воспроизвести
        разбор, но опубликованная цифра и её выборка остаются объяснимыми.
        """
        removed = 0
        for directory in sorted(self.root.iterdir()):
            if not directory.is_dir():
                continue
            try:
                day = date.fromisoformat(directory.name)
            except ValueError:
                continue
            if day >= cutoff:
                continue
            for path in directory.rglob("*.json.gz"):
                path.unlink()
                removed += 1
            for path in sorted(directory.rglob("*"), reverse=True):
                if path.is_dir():
                    path.rmdir()
            directory.rmdir()
        return removed
