"""Operator-set control state: target width (persisted) and the Auto switch (memory only: every start is advise-only).

The target is persisted with a stamp of the camera model it was set under: the file outlives app updates, and a
width typed under one camera model means something else under another, so the pipeline only acts on a matching stamp.
"""
import json
import logging
import math
import os
import threading
from pathlib import Path

log = logging.getLogger(__name__)
TARGET_MM_RANGE = (0.3, 5.0)
FIELDS = ("target_mm", "auto")


def send_enabled():
    """Deployment-level switch for sending to the printer; the UI and the send gate must use this one predicate."""
    return os.environ.get("CONTROL_SEND_ENABLED") == "1"


def _check_target(value):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("target_mm must be a number in mm, or null to clear it")
    low, high = TARGET_MM_RANGE
    if not low <= value <= high:
        raise ValueError(f"target_mm must be within {low}-{high} mm")
    return float(value)


def _check_auto(value):
    if not isinstance(value, bool):
        raise ValueError("auto must be true or false")
    return value


CHECKS = {"target_mm": _check_target, "auto": _check_auto}


def _check_stamp(value):
    """A stamp as the app writes it, or None (none given, or one this version can't read: the target then can't act)."""
    if not isinstance(value, dict):
        return None
    model, relative, by = value.get("camera_model"), value.get("relative"), value.get("by")
    if isinstance(model, str) and isinstance(relative, bool) and by in ("typed", "hold"):
        return {"camera_model": model, "relative": relative, "by": by}
    return None


class ControlState:
    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.Lock()
        self._values = {"target_mm": None, "auto": False}
        self._stamp = None
        self.load_error = None
        self.persist_error = None
        self._load()

    @property
    def target_mm(self):
        return self._values["target_mm"]

    @property
    def auto(self):
        return self._values["auto"]

    @property
    def target_stamp(self):
        return self._stamp

    def public(self):
        with self.lock:
            return dict(self._values)

    def snapshot(self):
        """Target, Auto and the target's stamp, read together."""
        with self.lock:
            return {**self._values, "target_stamp": self._stamp}

    def update(self, new, stamp=None):
        """Validate all fields, apply in memory, persist a changed target or stamp (a failing disk only sets
        persist_error). `stamp` comes from the app, never from a request body; it goes with a target being set."""
        if not isinstance(new, dict):
            raise ValueError("expected an object with target_mm and/or auto")
        unknown = sorted(set(new) - set(FIELDS))
        if unknown:
            raise ValueError(f"unknown field {unknown[0]!r}; allowed: target_mm, auto")
        checked = {k: CHECKS[k](v) for k, v in new.items()}
        with self.lock:
            values, new_stamp = {**self._values, **checked}, self._stamp
            if "target_mm" in checked:
                new_stamp = _check_stamp(stamp) if checked["target_mm"] is not None else None
            changed = values["target_mm"] != self._values["target_mm"] or new_stamp != self._stamp
            self._values, self._stamp = values, new_stamp
            if changed:
                self._persist()
            return dict(self._values)

    def _load(self):
        try:
            if not self.path.exists():
                return
            data = json.loads(self.path.read_text())
            if not isinstance(data, dict):
                raise ValueError("not a JSON object")
            if "target_mm" in data:
                self._values = {**self._values, "target_mm": _check_target(data["target_mm"])}
                self._stamp = _check_stamp(data.get("target_stamp")) if self._values["target_mm"] is not None else None
        except Exception as e:  # noqa: BLE001 - any unreadable file (even RecursionError) must leave the app starting
            self.load_error = f"{type(e).__name__}: {e}"
            log.warning("ignoring %s: %s", self.path, self.load_error)

    def _persist(self):
        try:
            self._write()
            self.persist_error = None
        except OSError as e:
            self.persist_error = str(e)
            log.warning("could not save %s: %s", self.path, e)

    def _write(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        data = {"target_mm": self._values["target_mm"]}
        if self._stamp:
            data["target_stamp"] = self._stamp
        with open(tmp, "w") as f:
            json.dump(data, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)
