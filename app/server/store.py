"""SQLite log of width samples, estimates, decisions and commands. Scalars only: images can never land here."""
import json
import logging
import math
import os
import sqlite3
import threading
import time
from pathlib import Path

log = logging.getLogger(__name__)

RETENTION_MS = 7 * 24 * 3600 * 1000
PURGE_EVERY_S = 3600
COMMIT_EVERY_S = 1.0
MAX_TEXT = 1000
MAX_RECENT = 1000
INT64 = 2 ** 63

# column -> SQL type; the first column of every table is the timestamp
TABLES = {
    "width_samples": {"t_ms": "INTEGER", "camera": "INTEGER", "width_px": "REAL", "mm_per_px": "REAL",
                      "confidence": "REAL", "reason": "TEXT", "group": "TEXT", "t_dep_ms": "INTEGER"},
    "estimates": {"t_ms": "INTEGER", "mm": "REAL", "sigma_mm": "REAL", "n": "INTEGER", "confidence": "REAL"},
    "decisions": {"t_ms": "INTEGER", "mode": "TEXT", "action_json": "TEXT", "reason": "TEXT", "error_mm": "REAL",
                  "predicted_mm": "REAL", "hold": "INTEGER", "dry": "INTEGER"},
    "commands": {"t_ms": "INTEGER", "operation": "TEXT", "value": "REAL", "request_id": "TEXT", "state": "TEXT"},
}


def _schema(table, columns):
    cols = []
    for name, sql_type in columns.items():
        q = f'"{name}"'  # quoted: "group" is an SQL keyword
        check = f"typeof({q}) IN ('null','text') AND length({q}) <= {MAX_TEXT}" if sql_type == "TEXT" \
            else f"typeof({q}) IN ('null','integer','real')"
        cols.append(f"{q} {sql_type} CHECK ({check})")
    return (f"CREATE TABLE IF NOT EXISTS {table} ({', '.join(cols)});"
            f"CREATE INDEX IF NOT EXISTS {table}_t ON {table} (t_ms)")


def _plain(value):
    """A numpy scalar (or 0-d array) as the Python number it holds; anything else unchanged."""
    if not isinstance(value, (bool, int, float, str)) and callable(getattr(value, "item", None)) \
            and getattr(value, "shape", None) == ():
        return value.item()
    return value


def _json_safe(value):
    value = _plain(value)
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if value is None or isinstance(value, (bool, int, str)):
        return value
    raise ValueError(f"action_json cannot hold a {type(value).__name__}")


def _scalar(table, column, sql_type, value):
    value = _plain(value)
    if value is None:
        return None
    if column == "action_json" and not isinstance(value, str):
        value = json.dumps(_json_safe(value), allow_nan=False)
    if sql_type == "TEXT":
        if not isinstance(value, str) or len(value) > MAX_TEXT:
            raise ValueError(f"{table}.{column} must be text of at most {MAX_TEXT} characters")
        return value
    if isinstance(value, bool):
        return int(value)
    if not isinstance(value, (int, float)):
        raise ValueError(f"{table}.{column} must be a number")
    try:
        finite = math.isfinite(value)
        if not finite and column == "t_ms":
            raise ValueError(f"{table}.t_ms must be a finite number")
        if sql_type == "INTEGER":
            number = int(value) if finite else None
            if number is not None and not -INT64 <= number < INT64:
                raise ValueError(f"{table}.{column} is out of range")
            return number
        return float(value) if finite else None
    except OverflowError:
        raise ValueError(f"{table}.{column} is out of range") from None


class Store:
    def __init__(self, path, retention_ms=RETENTION_MS, clock_ms=None):
        self.path = Path(path)
        self.retention_ms = retention_ms
        self.clock_ms = clock_ms or (lambda: int(time.time() * 1000))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.dirty = False
        self.last_purge_s = time.monotonic()
        self.last_error = None
        try:
            self._open()
        except sqlite3.DatabaseError as e:
            if isinstance(e, sqlite3.OperationalError):
                raise
            self._set_aside(e)
            self._open()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="store-flush", daemon=True)
        self._thread.start()

    def _open(self):
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        try:
            self.db.row_factory = sqlite3.Row
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=NORMAL")
            for table, columns in TABLES.items():
                self.db.executescript(_schema(table, columns))
            self.purge()
        except Exception:
            self.db.close()
            raise

    def _set_aside(self, error):
        stamp = self.clock_ms()
        for suffix in ("", "-wal", "-shm"):
            source = self.path.with_name(self.path.name + suffix)
            if source.exists():
                os.replace(source, source.with_name(f"{self.path.name}.corrupt-{stamp}{suffix}"))
        log.warning("%s is not a usable database (%s); moved aside, starting a new one", self.path, error)

    def insert_width_samples(self, row):
        self.insert("width_samples", row)

    def insert_estimates(self, row):
        self.insert("estimates", row)

    def insert_decisions(self, row):
        self.insert("decisions", row)

    def insert_commands(self, row):
        self.insert("commands", row)

    def recent_width_samples(self, limit=100):
        return self.recent("width_samples", limit)

    def recent_estimates(self, limit=100):
        return self.recent("estimates", limit)

    def recent_decisions(self, limit=100):
        return self.recent("decisions", limit)

    def recent_commands(self, limit=100):
        return self.recent("commands", limit)

    def insert(self, table, row):
        if table not in TABLES:
            raise ValueError(f"unknown table {table!r}")
        if not isinstance(row, dict):
            raise ValueError(f"{table} row must be a dict")
        columns = TABLES[table]
        unknown = sorted(set(row) - set(columns))
        if unknown:
            raise ValueError(f"{table} has no column {unknown[0]!r}")
        if isinstance(_plain(row.get("t_ms")), bool):
            raise ValueError(f"{table}.t_ms must be a number")
        values = [_scalar(table, c, t, row.get(c)) for c, t in columns.items()]
        if values[0] is None:
            raise ValueError(f"{table}.t_ms is required")
        with self.lock:
            names = ", ".join(f'"{c}"' for c in columns)
            self.db.execute(f"INSERT INTO {table} ({names}) VALUES ({', '.join('?' * len(columns))})", values)
            self.dirty = True

    def recent(self, table, limit=100):
        """Newest first. Sees uncommitted rows too: it reads through the writing connection."""
        if table not in TABLES:
            raise ValueError(f"unknown table {table!r}")
        limit = max(1, min(int(limit), MAX_RECENT))
        with self.lock:
            rows = self.db.execute(f"SELECT * FROM {table} ORDER BY t_ms DESC, rowid DESC LIMIT ?", (limit,))
            return [dict(r) for r in rows]

    def purge(self, now_ms=None):
        """Delete rows older than the retention window; returns how many."""
        cutoff = (self.clock_ms() if now_ms is None else now_ms) - self.retention_ms
        with self.lock:
            deleted = sum(self.db.execute(f"DELETE FROM {t} WHERE t_ms < ?", (cutoff,)).rowcount for t in TABLES)
            self.db.commit()
            self.dirty = False
            self.last_purge_s = time.monotonic()
        return deleted

    def flush(self):
        with self.lock:
            if self.dirty:
                self.db.commit()
                self.dirty = False

    def close(self):
        if self._stop.is_set():
            return
        self._stop.set()
        self._thread.join(timeout=2 * COMMIT_EVERY_S)
        with self.lock:
            self.db.commit()
            self.db.close()

    def _run(self):
        while not self._stop.wait(COMMIT_EVERY_S):
            try:
                self.flush()
                if time.monotonic() - self.last_purge_s >= PURGE_EVERY_S:
                    self.purge()
                self.last_error = None
            except Exception as e:  # noqa: BLE001 - the flusher must outlive any single failure
                if str(e) != self.last_error:
                    log.warning("store flush/purge failed, will retry: %s", e)
                self.last_error = str(e)
