"""SQLite persistence: finished scans (so report links survive a restart) and
two caches that keep repeat scans from paying for the same LLM work twice.

All access is synchronous and short; calls from async code go through
asyncio.to_thread in the pipeline.
"""

import hashlib
import json
import sqlite3
import threading
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS scans (
  id TEXT PRIMARY KEY, created REAL NOT NULL, url TEXT, title TEXT, state TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS cache (
  kind TEXT NOT NULL, key TEXT NOT NULL, created REAL NOT NULL, value TEXT NOT NULL,
  PRIMARY KEY (kind, key));
"""


def digest(*parts: object) -> str:
    """Stable hash of JSON-serialisable parts, used as a cache key."""
    blob = json.dumps(parts, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class Store:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.executescript(SCHEMA)
        self._db.commit()

    # scans -----------------------------------------------------------------

    def save_scan(self, scan_id: str, url: str, title: str, state: dict) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO scans (id, created, url, title, state) VALUES (?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET state = excluded.state",
                (scan_id, time.time(), url, title, json.dumps(state, ensure_ascii=False)),
            )
            self._db.commit()

    def load_scan(self, scan_id: str) -> dict | None:
        with self._lock:
            row = self._db.execute("SELECT state FROM scans WHERE id = ?", (scan_id,)).fetchone()
        return json.loads(row[0]) if row else None

    # caches ----------------------------------------------------------------

    def cache_get(self, kind: str, key: str, max_age_s: float | None = None) -> dict | None:
        with self._lock:
            row = self._db.execute(
                "SELECT created, value FROM cache WHERE kind = ? AND key = ?", (kind, key)
            ).fetchone()
        if not row:
            return None
        if max_age_s is not None and time.time() - row[0] > max_age_s:
            return None
        return json.loads(row[1])

    def cache_put(self, kind: str, key: str, value: dict) -> None:
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO cache (kind, key, created, value) VALUES (?,?,?,?)",
                (kind, key, time.time(), json.dumps(value, ensure_ascii=False)),
            )
            self._db.commit()

    def close(self) -> None:
        with self._lock:
            self._db.close()
