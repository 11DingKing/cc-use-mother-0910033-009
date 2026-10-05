"""JSON 文件存储：单文件、原子写、可纯内存运行。"""
from __future__ import annotations

import copy
import json
import os
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


class JsonStore:
    """cases 与 reminders 的单文件存储；path 为 None 时仅内存（测试用）。"""

    def __init__(self, path: str | os.PathLike[str] | None = None) -> None:
        self._path = Path(path) if path else None
        self._lock = threading.RLock()
        self._data: dict[str, Any] = {"cases": {}, "reminders": {}}
        if self._path and self._path.exists():
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            self._data["cases"] = raw.get("cases", {})
            self._data["reminders"] = raw.get("reminders", {})

    @contextmanager
    def transaction(self) -> Iterator[dict[str, Any]]:
        """在锁内读取/修改数据，正常退出时原子落盘。"""
        with self._lock:
            yield self._data
            self._save()

    def snapshot(self) -> dict[str, Any]:
        """只读路径使用：返回深拷贝，不落盘。"""
        with self._lock:
            return copy.deepcopy(self._data)

    def _save(self) -> None:
        if not self._path:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(self._data, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        os.replace(tmp, self._path)
