import json
import logging
import math
import sqlite3
import threading
import time

import numpy as np
import pytest

from server import store as store_module
from server.store import RETENTION_MS, TABLES, Store

NOW = 1_800_000_000_000

ROWS = {
    "width_samples": {"t_ms": NOW, "camera": 2, "width_px": 21.4, "mm_per_px": 0.061, "confidence": 0.9, "reason": "ok",
                      "group": "cam2:-90", "t_dep_ms": NOW - 500},
    "estimates": {"t_ms": NOW, "mm": 0.82, "sigma_mm": 0.04, "n": 12, "confidence": 0.8},
    "decisions": {"t_ms": NOW, "mode": "advise", "action_json": '{"operation": "flow_percent", "value": 105}',
                  "reason": "width 0.1 mm under target", "error_mm": -0.1, "predicted_mm": 0.79, "hold": 0, "dry": 0},
    "commands": {"t_ms": NOW, "operation": "flow_percent", "value": 105.0, "request_id": "abc", "state": "sent"},
}


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "prototype.db", clock_ms=lambda: NOW)
    yield s
    s.close()


def count(path, table):
    db = sqlite3.connect(path)
    try:
        return db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        db.close()


@pytest.mark.parametrize("table", list(TABLES))
def test_round_trip(store, table):
    getattr(store, f"insert_{table}")(ROWS[table])
    getattr(store, f"insert_{table}")({**ROWS[table], "t_ms": NOW + 1000})
    rows = getattr(store, f"recent_{table}")(10)
    assert [r["t_ms"] for r in rows] == [NOW + 1000, NOW]
    assert rows[1] == ROWS[table]
    assert len(getattr(store, f"recent_{table}")(1)) == 1


def test_decision_action_dict_is_stored_as_json(store):
    store.insert_decisions({"t_ms": NOW, "action_json": {"operation": "flow_percent", "value": 105}})
    row = store.recent_decisions(1)[0]
    assert json.loads(row["action_json"]) == {"operation": "flow_percent", "value": 105}
    assert row["reason"] is None and row["hold"] is None


def test_hold_bool_and_nan(store):
    store.insert_decisions({"t_ms": NOW, "hold": True, "error_mm": math.nan, "predicted_mm": math.inf})
    row = store.recent_decisions(1)[0]
    assert row["hold"] == 1 and row["error_mm"] is None and row["predicted_mm"] is None


@pytest.mark.parametrize("table,row", [
    ("estimates", {"t_ms": NOW, "jpeg": "x"}),
    ("estimates", {"mm": 0.8}),
    ("estimates", {"t_ms": NOW, "mm": b"\xff\xd8\xff"}),
    ("estimates", {"t_ms": NOW, "mm": [0.8]}),
    ("estimates", {"t_ms": NOW, "mm": "0.8"}),
    ("width_samples", {"t_ms": NOW, "reason": b"\xff\xd8\xff"}),
    ("width_samples", {"t_ms": NOW, "reason": "x" * 5000}),
    ("width_samples", {"t_ms": NOW, "reason": {"a": 1}}),
    ("decisions", {"t_ms": NOW, "action_json": {"blob": "x" * 5000}}),
])
def test_rejects_non_scalars(store, table, row):
    with pytest.raises(ValueError):
        store.insert(table, row)
    assert store.recent(table, 10) == []


def test_schema_refuses_blobs_even_on_raw_sql(store):
    with pytest.raises(sqlite3.IntegrityError):
        store.db.execute("INSERT INTO width_samples (t_ms, reason) VALUES (?, ?)", (NOW, b"\xff\xd8"))
    with pytest.raises(sqlite3.IntegrityError):
        store.db.execute("INSERT INTO estimates (t_ms, mm) VALUES (?, ?)", (NOW, b"\xff\xd8"))
    with pytest.raises(sqlite3.IntegrityError):
        store.db.execute("INSERT INTO commands (t_ms, state) VALUES (?, ?)", (NOW, "x" * 5000))


def test_wal_mode(store):
    assert store.db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_purge_deletes_only_old_rows(store):
    old = NOW - RETENTION_MS - 1
    for table in TABLES:
        store.insert(table, {**ROWS[table], "t_ms": old})
        store.insert(table, {**ROWS[table], "t_ms": NOW - RETENTION_MS})
        store.insert(table, ROWS[table])
    assert store.purge() == len(TABLES)
    for table in TABLES:
        assert [r["t_ms"] for r in store.recent(table, 10)] == [NOW, NOW - RETENTION_MS]


def test_purge_runs_at_start(tmp_path):
    path = tmp_path / "prototype.db"
    first = Store(path, clock_ms=lambda: NOW)
    first.insert_estimates({"t_ms": NOW - RETENTION_MS - 5000, "mm": 0.8})
    first.insert_estimates({"t_ms": NOW - 5000, "mm": 0.9})
    first.close()
    second = Store(path, clock_ms=lambda: NOW)
    try:
        assert [r["mm"] for r in second.recent_estimates(10)] == [0.9]
    finally:
        second.close()


def test_purge_runs_periodically(tmp_path, monkeypatch):
    monkeypatch.setattr(store_module, "COMMIT_EVERY_S", 0.05)
    monkeypatch.setattr(store_module, "PURGE_EVERY_S", 0.1)
    s = Store(tmp_path / "prototype.db", clock_ms=lambda: NOW)
    try:
        s.insert_estimates({"t_ms": NOW - RETENTION_MS - 5000, "mm": 0.8})
        deadline = time.monotonic() + 5
        while s.recent_estimates(10) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert s.recent_estimates(10) == []
    finally:
        s.close()


def test_commits_are_batched_and_flushable(tmp_path):
    path = tmp_path / "prototype.db"
    s = Store(path, clock_ms=lambda: NOW)
    try:
        s.insert_estimates(ROWS["estimates"])
        assert count(path, "estimates") == 0  # not committed yet: the flusher commits once per second
        deadline = time.monotonic() + 5
        while count(path, "estimates") == 0 and time.monotonic() < deadline:
            time.sleep(0.1)
        assert count(path, "estimates") == 1
        s.insert_estimates(ROWS["estimates"])
        s.flush()
        assert count(path, "estimates") == 2
    finally:
        s.close()


def test_close_commits_and_is_idempotent(tmp_path):
    path = tmp_path / "prototype.db"
    s = Store(path, clock_ms=lambda: NOW)
    s.insert_commands(ROWS["commands"])
    s.close()
    s.close()
    assert count(path, "commands") == 1


def test_concurrent_writers(store):
    per_thread, errors = 250, []

    def writer(i):
        try:
            for n in range(per_thread):
                store.insert_width_samples({**ROWS["width_samples"], "t_ms": NOW + n, "camera": i})
                store.insert_estimates({**ROWS["estimates"], "t_ms": NOW + n, "n": i})
                if n % 50 == 0:
                    store.recent_width_samples(5)
        except Exception as e:  # noqa: BLE001 - collected and asserted below
            errors.append(e)

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert len(store.recent_width_samples(1000)) == 4 * per_thread
    assert len(store.recent_estimates(1000)) == 4 * per_thread
    store.flush()
    assert count(store.path, "width_samples") == 4 * per_thread
    db = sqlite3.connect(store.path)
    try:
        per_camera = dict(db.execute("SELECT camera, COUNT(*) FROM width_samples GROUP BY camera"))
    finally:
        db.close()
    assert per_camera == {camera: per_thread for camera in range(4)}


def test_numpy_scalars_are_accepted(store):
    store.insert_estimates({"t_ms": np.int64(NOW), "mm": np.float32(0.5), "sigma_mm": np.float64(0.25), "n": np.int64(7),
                            "confidence": np.array(0.75)})
    store.insert_decisions({"t_ms": NOW, "hold": np.bool_(True), "error_mm": np.float32(-0.5)})
    row = store.recent_estimates(1)[0]
    assert row == {"t_ms": NOW, "mm": 0.5, "sigma_mm": 0.25, "n": 7, "confidence": 0.75}
    assert store.recent_decisions(1)[0]["hold"] == 1 and store.recent_decisions(1)[0]["error_mm"] == -0.5


@pytest.mark.parametrize("value", [np.zeros((4, 4)), np.zeros(1), np.zeros(1000), np.array([0.5, 0.6])])
def test_numpy_arrays_are_rejected(store, value):
    with pytest.raises(ValueError):
        store.insert_estimates({"t_ms": NOW, "mm": value})
    with pytest.raises(ValueError):
        store.insert_decisions({"t_ms": NOW, "action_json": {"x": value}})


@pytest.mark.parametrize("t_ms", [math.nan, math.inf, -math.inf, 1e300, 10 ** 400, 2 ** 63, True, None, np.float32("nan")])
def test_bad_timestamps_are_a_clear_value_error(store, t_ms):
    with pytest.raises(ValueError):
        store.insert_estimates({"t_ms": t_ms, "mm": 0.8})
    assert store.recent_estimates(5) == []


def test_non_finite_and_huge_numbers_in_other_columns(store):
    store.insert_width_samples({"t_ms": NOW, "camera": math.inf, "width_px": math.nan, "mm_per_px": -math.inf})
    row = store.recent_width_samples(1)[0]
    assert (row["camera"], row["width_px"], row["mm_per_px"]) == (None, None, None)
    for bad in (10 ** 400, 2 ** 70, 1e300):
        with pytest.raises(ValueError):
            store.insert_width_samples({"t_ms": NOW, "camera": bad})
    with pytest.raises(ValueError):
        store.insert_width_samples({"t_ms": NOW, "width_px": 10 ** 400})


def test_action_json_is_always_valid_json(store):
    action = {"operation": "flow_percent", "value": np.float32(105), "bad": math.nan, "worse": [math.inf, np.int64(2)],
              "flag": np.bool_(False), 3: "int key"}
    store.insert_decisions({"t_ms": NOW, "action_json": action})
    text = store.recent_decisions(1)[0]["action_json"]
    assert "NaN" not in text and "Infinity" not in text
    assert json.loads(text) == {"operation": "flow_percent", "value": 105.0, "bad": None, "worse": [None, 2],
                                "flag": False, "3": "int key"}
    for bad in ({"x": object()}, {"x": b"\xff"}, {"x": {1, 2}}):
        with pytest.raises(ValueError):
            store.insert_decisions({"t_ms": NOW, "action_json": bad})


def test_recent_limit_is_clamped(store):
    for i in range(1005):
        store.insert_estimates({"t_ms": NOW + i, "mm": 1.0})
    assert len(store.recent_estimates(10 ** 9)) == 1000
    assert len(store.recent_estimates(-5)) == 1
    assert len(store.recent_estimates(0)) == 1
    assert store.recent_estimates(1)[0]["t_ms"] == NOW + 1004


def test_flusher_survives_a_failing_commit(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(store_module, "COMMIT_EVERY_S", 0.05)

    class FailsOnce:
        def __init__(self, db):
            self._db, self.failed = db, False

        def commit(self):
            if not self.failed:
                self.failed = True
                raise sqlite3.OperationalError("database or disk is full")
            return self._db.commit()

        def __getattr__(self, name):
            return getattr(self._db, name)

    s = Store(tmp_path / "prototype.db", clock_ms=lambda: NOW)
    try:
        with caplog.at_level(logging.WARNING):
            s.db = FailsOnce(s.db)
            s.insert_estimates(ROWS["estimates"])
            deadline = time.monotonic() + 5
            while count(s.path, "estimates") == 0 and time.monotonic() < deadline:
                time.sleep(0.05)
            assert s.db.failed and count(s.path, "estimates") == 1
            s.insert_estimates({**ROWS["estimates"], "t_ms": NOW + 1})
            while count(s.path, "estimates") < 2 and time.monotonic() < deadline:
                time.sleep(0.05)
            assert count(s.path, "estimates") == 2
        assert s._thread.is_alive()
        assert any("disk is full" in rec.getMessage() for rec in caplog.records)
    finally:
        s.close()


def test_flusher_survives_a_failing_purge(tmp_path, monkeypatch):
    monkeypatch.setattr(store_module, "COMMIT_EVERY_S", 0.05)
    monkeypatch.setattr(store_module, "PURGE_EVERY_S", 0.0)
    s = Store(tmp_path / "prototype.db", clock_ms=lambda: NOW)
    calls = []

    def failing_purge(now_ms=None):
        calls.append(1)
        raise sqlite3.OperationalError("database is locked")

    try:
        s.purge = failing_purge
        deadline = time.monotonic() + 5
        while len(calls) < 3 and time.monotonic() < deadline:
            time.sleep(0.05)
        assert len(calls) >= 3 and s._thread.is_alive()
    finally:
        s.purge = Store.purge.__get__(s)
        s.close()


def test_corrupt_database_is_set_aside(tmp_path, caplog):
    path = tmp_path / "prototype.db"
    path.write_bytes(b"this is not a database" * 100)
    (tmp_path / "prototype.db-wal").write_bytes(b"junk wal")
    with caplog.at_level(logging.WARNING):
        s = Store(path, clock_ms=lambda: NOW)
    try:
        assert (tmp_path / f"prototype.db.corrupt-{NOW}").read_bytes().startswith(b"this is not a database")
        assert any("moved aside" in rec.getMessage() for rec in caplog.records)
        s.insert_estimates(ROWS["estimates"])
        assert s.recent_estimates(5) == [ROWS["estimates"]]
    finally:
        s.close()
    reopened = Store(path, clock_ms=lambda: NOW)
    try:
        assert reopened.recent_estimates(5) == [ROWS["estimates"]]
    finally:
        reopened.close()


def test_an_unopenable_path_is_not_treated_as_corruption(tmp_path):
    (tmp_path / "prototype.db").mkdir()
    with pytest.raises(sqlite3.OperationalError):
        Store(tmp_path / "prototype.db")
    assert (tmp_path / "prototype.db").is_dir()
    assert [p.name for p in tmp_path.iterdir()] == ["prototype.db"]
