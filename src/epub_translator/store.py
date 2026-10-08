"""Finished translations, kept on disk so an interrupted run can pick up where it stopped."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path


class Store:
    def __init__(self, path: Path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path)
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS translations ("
            "key TEXT PRIMARY KEY, name TEXT, model TEXT, created REAL, data TEXT NOT NULL)"
        )
        self._db.commit()

    def get(self, key: str) -> dict[int, str] | None:
        row = self._db.execute("SELECT data FROM translations WHERE key = ?", (key,)).fetchone()
        if row is None:
            return None
        try:
            return {int(k): str(v) for k, v in json.loads(row[0]).items()}
        except (ValueError, AttributeError):
            return None

    def put(self, key: str, name: str, model: str, translations: dict[int, str]) -> None:
        data = json.dumps(translations, ensure_ascii=False)
        self._db.execute(
            "INSERT OR REPLACE INTO translations VALUES (?, ?, ?, ?, ?)",
            (key, name, model, time.time(), data),
        )
        self._db.commit()

    def close(self) -> None:
        self._db.close()
