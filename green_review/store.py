"""JSON 文件仓储和内存仓储。

该应用是单进程工具，仓储使用一把可重入锁保护高层操作；写入采用临时文件
原子替换，避免进程中断留下半截 JSON。
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Iterable

from .utils import atomic_write_json, read_json, utc_now_iso

COLLECTIONS = (
    "events",
    "sites",
    "product_scopes",
    "evidence_versions",
    "metric_versions",
    "certificates",
    "factors",
    "verifications",
    "confirmations",
    "issues",
    "cases",
    "impacts",
    "decisions",
)


def default_data() -> dict[str, Any]:
    return {
        "meta": {
            "schema_version": 1,
            "created_at": utc_now_iso(),
        },
        "sequences": {},
        **{name: [] for name in COLLECTIONS},
    }


class Store:
    def __init__(self, path: str | Path | None = None, *, data: dict[str, Any] | None = None):
        self.path = Path(path) if path else None
        self.lock = threading.RLock()
        if data is not None:
            self.data = data
        elif self.path and self.path.exists():
            loaded = read_json(self.path)
            self.data = self._upgrade(loaded)
        else:
            self.data = default_data()
        if self.path:
            self.save()

    @staticmethod
    def _upgrade(data: dict[str, Any]) -> dict[str, Any]:
        base = default_data()
        base.update(data)
        for name in COLLECTIONS:
            base.setdefault(name, [])
        base.setdefault("sequences", {})
        base.setdefault("meta", {"schema_version": 1})
        return base

    def save(self) -> None:
        if self.path is not None:
            with self.lock:
                atomic_write_json(self.path, self.data)

    def collection(self, name: str) -> list[dict[str, Any]]:
        if name not in COLLECTIONS:
            raise KeyError(f"unknown collection {name!r}")
        return self.data[name]

    def all(self, name: str) -> list[dict[str, Any]]:
        with self.lock:
            return list(self.collection(name))

    def find(self, name: str, **filters: Any) -> list[dict[str, Any]]:
        rows = self.all(name)
        result = []
        for row in rows:
            if all(row.get(key) == value for key, value in filters.items()):
                result.append(row)
        return result

    def find_one(self, name: str, **filters: Any) -> dict[str, Any] | None:
        for row in self.find(name, **filters):
            return row
        return None

    def get(self, name: str, record_id: Any, id_field: str = "id") -> dict[str, Any]:
        row = self.find_one(name, **{id_field: record_id})
        if row is None:
            raise KeyError(f"{name} with {id_field}={record_id!r} not found")
        return row

    def add(self, name: str, record: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            self.collection(name).append(record)
            return record

    def update(self, name: str, record: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            rows = self.collection(name)
            for index, row in enumerate(rows):
                if row is record or (row.get("id") == record.get("id") and record.get("id") is not None):
                    rows[index] = record
                    return record
            rows.append(record)
            return record

    def next_sequence(self, key: str) -> int:
        with self.lock:
            current = int(self.data["sequences"].get(key, 0)) + 1
            self.data["sequences"][key] = current
            return current

    def next_id(self, prefix: str, key: str | None = None, width: int = 4) -> str:
        number = self.next_sequence(key or prefix)
        return f"{prefix}-{number:0{width}d}"

    def replace_all(self, name: str, records: Iterable[dict[str, Any]]) -> None:
        with self.lock:
            self.data[name] = list(records)


# 常见仓储命名别名。
Repository = Store
JsonStore = Store
