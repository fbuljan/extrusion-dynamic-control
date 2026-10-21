"""Operator-set control state: target width and its allowed band (persisted), the Auto switch and per-lever tuning
(memory only: every start is advise-only, with the configured levers and steps).

The target is persisted with a stamp of the camera model it was set under: the file outlives app updates, and a
width typed under one camera model means something else under another, so the pipeline only acts on a matching stamp.
"""
import json
import logging
import math
import os
import threading
from pathlib import Path

from . import control_config

log = logging.getLogger(__name__)
TARGET_MM_RANGE = (0.3, 5.0)
BAND_MM_RANGE = (0.0, 1.0)
FIELDS = ("target_mm", "auto", "band_below_mm", "band_above_mm", "levers", "settings")
PERSISTED = ("target_mm", "band_below_mm", "band_above_mm", "settings")


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


def _check_band(name):
    def check(value):
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError(f"{name} must be a number in mm, or null for the default")
        low, high = BAND_MM_RANGE
        if not low <= value <= high:
            raise ValueError(f"{name} must be within {low}-{high} mm")
        return float(value)
    return check


def default_levers():
    return {op: {"enabled": True, "max_step": None} for op in control_config.LEVERS}


def _check_levers(value, current):
    """Merged per lever into the current tuning. A step can only be smaller than the configured one (None: that one)."""
    if not isinstance(value, dict):
        raise ValueError("levers must be an object of lever settings")
    merged = {op: dict(spec) for op, spec in current.items()}
    for op, spec in value.items():
        if op not in control_config.LEVERS:
            raise ValueError(f"{op!r} is not a controller lever (allowed: {', '.join(control_config.LEVERS)})")
        if not isinstance(spec, dict) or not set(spec) <= {"enabled", "max_step"}:
            raise ValueError(f"{op} takes enabled and/or max_step")
        if "enabled" in spec and not isinstance(spec["enabled"], bool):
            raise ValueError(f"{op} enabled must be true or false")
        step, top = spec.get("max_step"), control_config.DEFAULTS["levers"][op]["max_step"]
        if step is not None and (isinstance(step, bool) or not isinstance(step, (int, float)) or
                                 not math.isfinite(step) or step != int(step) or not 1 <= step <= top):
            raise ValueError(f"{op} max_step must be a whole number from 1 to {top:g}, or null for {top:g}")
        merged[op].update({k: (int(v) if k == "max_step" and v is not None else v) for k, v in spec.items()})
    if not any(spec["enabled"] for spec in merged.values()):
        raise ValueError("keep at least one lever on for Auto")
    return merged


def _check_settings(value, current):
    """Merged per key into the operator's settings; None drops a key back to the deployment's value."""
    if not isinstance(value, dict):
        raise ValueError("settings must be an object")
    merged = dict(current)
    for key, v in value.items():
        if key not in control_config.OPERATOR:
            raise ValueError(f"{key!r} is not an operator setting (allowed: {', '.join(control_config.OPERATOR)})")
        if v is None:
            merged.pop(key, None)
            continue
        lo, hi, kind = control_config.OPERATOR[key]
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not lo <= v <= hi or \
                kind is int and v != int(v):
            raise ValueError(f"{key} must be {'a whole number' if kind is int else 'a number'} from {lo:g} to {hi:g}")
        merged[key] = kind(v)
    return merged


CHECKS = {"target_mm": _check_target, "auto": _check_auto, "band_below_mm": _check_band("band_below_mm"),
          "band_above_mm": _check_band("band_above_mm")}


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
        self._values = {"target_mm": None, "auto": False, "band_below_mm": None, "band_above_mm": None,
                        "levers": default_levers(), "settings": {}}
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
            return self._copy()

    def snapshot(self):
        """Target, band, Auto, tuning and the target's stamp, read together."""
        with self.lock:
            return {**self._copy(), "target_stamp": self._stamp}

    def _copy(self):
        return {**self._values, "levers": {op: dict(spec) for op, spec in self._values["levers"].items()},
                "settings": dict(self._values["settings"])}

    def update(self, new, stamp=None):
        """Validate all fields, apply in memory, persist a changed target or stamp (a failing disk only sets
        persist_error). `stamp` comes from the app, never from a request body; it goes with a target being set."""
        if not isinstance(new, dict):
            raise ValueError(f"expected an object with {', '.join(FIELDS)}")
        unknown = sorted(set(new) - set(FIELDS))
        if unknown:
            raise ValueError(f"unknown field {unknown[0]!r}; allowed: {', '.join(FIELDS)}")
        checked = {k: CHECKS[k](v) for k, v in new.items() if k not in ("levers", "settings")}
        with self.lock:
            if "levers" in new:
                checked["levers"] = _check_levers(new["levers"], self._values["levers"])
            if "settings" in new:
                checked["settings"] = _check_settings(new["settings"], self._values["settings"])
            values, new_stamp = {**self._values, **checked}, self._stamp
            if "target_mm" in checked:
                new_stamp = _check_stamp(stamp) if checked["target_mm"] is not None else None
            changed = any(values[k] != self._values[k] for k in PERSISTED) or new_stamp != self._stamp
            self._values, self._stamp = values, new_stamp
            if changed:
                self._persist()
            return self._copy()

    def _load(self):
        try:
            if not self.path.exists():
                return
            data = json.loads(self.path.read_text())
            if not isinstance(data, dict):
                raise ValueError("not a JSON object")
            loaded = {key: CHECKS[key](data[key]) for key in PERSISTED if key in data and key != "settings"}  # all or nothing
            if "settings" in data:
                loaded["settings"] = _check_settings(data["settings"], {})
            self._values = {**self._values, **loaded}
            if "target_mm" in loaded:
                self._stamp = _check_stamp(data.get("target_stamp")) if loaded["target_mm"] is not None else None
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
        data = {key: self._values[key] for key in PERSISTED if key == "target_mm" or self._values[key] not in (None, {})}
        if self._stamp:
            data["target_stamp"] = self._stamp
        with open(tmp, "w") as f:
            json.dump(data, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)
