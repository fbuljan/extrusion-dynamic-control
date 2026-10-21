"""Summarise a print from a record.py capture, and watch a growing capture for the print end. Offline: no broker, stdlib only.

Usage:
  python tools/mqtt/print_watch.py data/captures/NAME --summary
  python tools/mqtt/print_watch.py data/captures/NAME --watch [--poll 30] [--refresh 600]

Reads telemetry.jsonl, previews.jsonl and ir.jsonl (frame metadata only: image files are never opened) and, if present,
writer.jsonl (gateway status/result lines as {"recv_ms", "topic", "retain", "msg"}). Writes summary.json and SUMMARY.md into the
capture folder; --watch also writes a PRINT_END marker when the print ends. Times are Europe/Berlin.

Print end rules (first to fire wins; the rule and times go into PRINT_END and the summary):
  idle             no extrusion and no motion for IDLE_S (default 180 s)
  heater_off       nozzle target < 100 C (or state "off") for 20 s, after it had been >= 150 C, with no extrusion
  program_cleared  the program name is empty for 30 s with no extrusion
  program_changed  a different program name for 10 s
The end time reported is the last extruding message. A run with under 60 s of extrusion is not a print.
Values from sources flagged invalid (or readbacks older than 10 s) are never used.
"""
import argparse
import bisect
import json
import math
import os
import signal
import time
import traceback
from array import array
from collections import Counter
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo("Europe/Berlin")
except Exception:  # no tz database: CEST is right for 20-25 Oct 2026
    TZ = timezone(timedelta(hours=2), "CEST")

GAP_S = 2.0                # telemetry gap worth reporting; a gap breaks every end-rule streak and counts as unobserved time
IDLE_S = 180.0
PROGRAM_GONE_S = 30.0
PROGRAM_CHANGED_S = 10.0
HEATER_OFF_S = 20.0
HEATER_OFF_C = 100.0
HEATER_ON_C = 150.0
MIN_PRINT_EXTRUSION_S = 60.0
LAYER_HYSTERESIS_MM = 0.05
MIN_LAYER_EXTRUDE_S = 1.0  # a Z level with less extrusion than this is a transit through Z, not a layer
STALE_READBACK_MS = 10_000
UNSEEN_S = 5.0             # a setpoint that reappears after this long without a valid reading has an uncertain change time
STATUS_GAP_S = 5.0
FRAME_GAP_S = 10.0         # RGB is silent 7-9 s at a time while one camera holds the role (normal); longer is reported as a gap
MAX_LIST = 2000
CHUNK = 8 << 20
SOURCES = ("motion", "process", "melt", "gateway", "controls", "duet_readback")


def num(x):
    return float(x) if isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) else None


def ns_ms(ns):
    return ns // 1_000_000 if isinstance(ns, int) else None


def dig(d, *keys):
    for k in keys:
        if not isinstance(d, dict):
            return None
        d = d.get(k)
    return d


def local(ms):
    return datetime.fromtimestamp(ms / 1000, TZ)


def iso(ms):
    return None if ms is None else local(ms).isoformat(timespec="milliseconds")


def hms(ms):
    return "-" if ms is None else local(ms).strftime("%H:%M:%S")


def stamp(ms):
    return "-" if ms is None else local(ms).strftime("%Y-%m-%d %H:%M:%S %Z")


def dur(s):
    if s is None:
        return "-"
    s = int(round(s))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h} h {m:02d} min {sec:02d} s" if h else f"{m} min {sec:02d} s" if m else f"{sec} s"


def describe(vals, nd=2):
    v = sorted(x for x in vals if x == x)
    if not v:
        return None

    def q(p):
        i = (len(v) - 1) * p
        lo = int(i)
        return v[lo] + (v[min(lo + 1, len(v) - 1)] - v[lo]) * (i - lo)
    return {"n": len(v), "min": round(v[0], nd), "p5": round(q(.05), nd), "median": round(q(.5), nd),
            "mean": round(sum(v) / len(v), nd), "p95": round(q(.95), nd), "max": round(v[-1], nd)}


def clean(o):
    """JSON-safe copy: NaN/inf become null, private '_' keys are dropped."""
    if isinstance(o, dict):
        return {k: clean(v) for k, v in o.items() if not (isinstance(k, str) and k.startswith("_"))}
    if isinstance(o, (list, tuple)):
        return [clean(v) for v in o]
    return None if isinstance(o, float) and not math.isfinite(o) else o


def write_atomic(path, text):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(text)
    os.replace(tmp, path)


class Tail:
    """Reads the complete new lines of a growing file; a half-written last line is left for the next call."""

    def __init__(self, path):
        self.path, self.off, self.pending = path, 0, 0

    def lines(self):
        try:
            size = os.path.getsize(self.path)
        except OSError:
            return
        if size < self.off:
            log(f"WARNING {os.path.basename(self.path)} shrank; rereading from the start (counts may double)")
            self.off = 0
        buf = b""
        with open(self.path, "rb") as f:
            f.seek(self.off)
            while True:
                chunk = f.read(CHUNK)
                if not chunk:
                    break
                parts = (buf + chunk).split(b"\n")
                buf = parts.pop()
                for part in parts:
                    self.off += len(part) + 1
                    if part.strip():
                        yield part
        self.pending = len(buf)
        if buf.strip():
            try:
                json.loads(buf)
            except ValueError:
                return
            self.off += len(buf)
            self.pending = 0
            yield buf


class Series:
    """A value that matters when it changes: only valid, fresh readings are offered; every change is kept."""

    def __init__(self, key, label, unit="", digits=1):
        self.key, self.label, self.unit, self.digits = key, label, unit, digits
        self.cur = self.last_t = self.first = self.lo = self.hi = None
        self.used, self.dropped = 0, 0
        self.rejected = Counter()
        self.events = []

    def add(self, t, v, src_ms=None):
        if isinstance(v, float):
            v = round(v, self.digits)
        self.used += 1
        if self.cur is None:
            self.first = v
        elif v != self.cur:
            unseen = (t - self.last_t) / 1000
            ev = {"t_ms": t, "from": self.cur, "to": v, "source_ms": src_ms,
                  "unobserved_s": round(unseen, 1) if unseen > UNSEEN_S else None}
            if len(self.events) < MAX_LIST:
                self.events.append(ev)
            else:
                self.dropped += 1
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            self.lo = v if self.lo is None else min(self.lo, v)
            self.hi = v if self.hi is None else max(self.hi, v)
        self.cur, self.last_t = v, t

    def reject(self, why):
        self.rejected[why] += 1

    def offer(self, t, v, why, src_ms=None):
        if why or v is None:
            self.reject(why or "absent")
        else:
            self.add(t, v, src_ms)


class FrameLog:
    """Counts of frame metadata lines (previews.jsonl, ir.jsonl); only the metadata is read, never an image."""

    def __init__(self):
        self.n = self.bad = self.files = self.inline_images = 0
        self.first = self.last = None
        self.cam, self.sessions = Counter(), Counter()
        self.cam_span, self.last_kind, self.gaps, self.gap_count = {}, {}, [], 0

    def feed(self, raw):
        try:
            m = json.loads(raw)
            t = int(m["recv_ms"])
            kind, idx = m.get("camera_kind"), m.get("camera_index")
        except (ValueError, KeyError, TypeError, AttributeError):
            self.bad += 1
            return
        self.n += 1
        self.first = t if self.first is None else self.first
        self.last = t
        self.cam[(kind, idx, m.get("role"))] += 1
        span = self.cam_span.setdefault((kind, idx), [t, t])
        span[1] = t
        self.sessions[m.get("session_id")] += 1
        self.files += "file" in m
        self.inline_images += "jpeg_base64" in m
        prev = self.last_kind.get(kind)
        if prev is not None and t - prev > FRAME_GAP_S * 1000:
            self.gap_count += 1
            if len(self.gaps) < MAX_LIST:
                self.gaps.append({"camera_kind": kind, "start_ms": prev, "end_ms": t, "seconds": round((t - prev) / 1000, 1),
                                  "start_local": iso(prev), "end_local": iso(t)})
        self.last_kind[kind] = t

    def summary(self):
        by_cam = Counter()
        for (kind, idx, _), n in self.cam.items():
            by_cam[(kind, idx)] += n
        key = lambda kv: tuple(str(x) for x in kv[0])
        return {"messages": self.n, "parse_errors": self.bad, "with_file_reference": self.files, "inline_images_ignored": self.inline_images,
                "first_local": iso(self.first), "last_local": iso(self.last), "session_ids": len(self.sessions),
                "by_kind_and_camera": [{"camera_kind": k[0], "camera_index": k[1], "n": n, "first_local": iso(self.cam_span[k][0]),
                                        "last_local": iso(self.cam_span[k][1])} for k, n in sorted(by_cam.items(), key=key)],
                "by_kind_camera_role": [{"camera_kind": k[0], "camera_index": k[1], "role": k[2], "n": n} for k, n in sorted(self.cam.items(), key=key)],
                "gap_threshold_s": FRAME_GAP_S, "gap_count": self.gap_count, "gaps": self.gaps}


class Capture:
    """Everything learned from the capture so far. feed_* take one raw line; summarize() works on what was fed."""

    def __init__(self):
        self.t, self.dt, self.fl = array("q"), array("H"), array("B")   # recv_ms, observed ms since previous, bit0 usable 1 ext 2 moving 4
        self.x, self.y, self.z, self.vel = array("d"), array("d"), array("d"), array("d")
        self.nz_t, self.nz_cur, self.nz_tgt = array("q"), array("d"), array("d")
        self.bd_t, self.bd_cur, self.bd_tgt = array("q"), array("d"), array("d")
        self.melt_t, self.melt_v, self.sub_t, self.sub_v = array("q"), array("d"), array("q"), array("d")
        self.bad_tel = self.time_regressions = self.seq_regressions = 0
        self.sid = self.last_seq = None
        self.sessions, self.gaps, self.gap_total_ms, self.gap_count = [], [], 0, 0
        self.lag = [0, None, None, 0.0]
        self.src_n, self.src_ok = Counter(), Counter()
        self.stale, self.motion_unusable = Counter(), 0
        self.series = {k: Series(k, *a) for k, a in {
            "flow": ("Extrusion multiplier", "%", 1), "nozzle_target": ("Nozzle target", "C", 1),
            "nozzle_state": ("Nozzle heater state", "", 0), "bed_target": ("Bed target", "C", 1),
            "bed_state": ("Bed heater state", "", 0), "speed_override": ("Speed override", "%", 1),
            "pid_cooling": ("pid_state.cooling.enabled (info)", "", 0),
            "pid_speed": ("pid_state.speed_substrate.enabled (info)", "", 0),
            "pid_speed_mode": ("pid_state.speed_mode (info)", "", 0)}.items()}
        self.last_src = {"nozzle": None, "bed": None, "melt": None, "process": None}
        self.nz_last = None
        self.programs, self.prog = [], None
        self.gw_key, self.gw_events, self.gw_time, self.gw_prev = None, [], Counter(), None
        self.run, self.runs, self.ended, self.n_state = None, [], [], 0
        self.writer = {"n": 0, "retained": 0, "other": Counter(), "bad": 0, "first": None, "last": None, "prev": None,
                       "changes": [], "admitted": [], "results": [], "gaps": [], "auth_s": Counter(), "retained_first": False}
        self.frames = {"previews": FrameLog(), "ir": FrameLog()}

    # ---- telemetry -------------------------------------------------------------------------------------------------
    def feed_telemetry(self, raw):
        try:
            e = json.loads(raw)
            m, t, src = e["msg"], int(e["recv_ms"]), e["msg"]["sources"]
            if not isinstance(src, dict):
                raise TypeError
        except (ValueError, KeyError, TypeError):
            self.bad_tel += 1
            return
        if self.t and t < self.t[-1]:
            self.time_regressions += 1
            t = self.t[-1]
        gap = t - self.t[-1] if self.t else 0
        brk = gap > GAP_S * 1000
        if brk:
            self.gap_count += 1
            self.gap_total_ms += gap
            if len(self.gaps) < MAX_LIST:
                self.gaps.append({"start_ms": self.t[-1], "end_ms": t, "seconds": round(gap / 1000, 2)})
        dtc = 0 if brk else min(gap, 65535)
        sid, seq, pub = m.get("session_id"), m.get("sequence"), m.get("published_utc_ms")
        if sid != self.sid:
            self.sessions.append({"t_ms": t, "from": self.sid, "to": sid, "sequence": seq, "initial": self.sid is None})
            brk = brk or self.sid is not None
            self.sid, self.last_seq = sid, None
        if isinstance(seq, int):
            if self.last_seq is not None and seq < self.last_seq:
                self.seq_regressions += 1
            self.last_seq = seq
        if isinstance(pub, int):
            d = t - pub
            self.lag[0] += 1
            self.lag[1] = d if self.lag[1] is None else min(self.lag[1], d)
            self.lag[2] = d if self.lag[2] is None else max(self.lag[2], d)
            self.lag[3] += d
        pub_ms = pub if isinstance(pub, int) else t
        for name, s in src.items():
            self.src_n[name] += 1
            if isinstance(s, dict) and s.get("valid") is True:
                self.src_ok[name] += 1

        flags, x, y, z, vel, ex, mv, name = 0, math.nan, math.nan, math.nan, math.nan, False, False, ""
        mo = src.get("motion") if isinstance(src.get("motion"), dict) else {}
        mp = mo.get("payload") if isinstance(mo.get("payload"), dict) else {}
        if mo.get("valid") is True and mp.get("connection_ok") is not False and isinstance(mp.get("extrusion_active"), bool) \
                and isinstance(mp.get("is_moving"), bool):
            ex, mv = mp["extrusion_active"], mp["is_moving"]
            flags = 1 | (2 if ex else 0) | (4 if mv else 0)
            pos = mp.get("position_mm") if isinstance(mp.get("position_mm"), dict) else {}
            x, y, z = (num(pos.get(k)) for k in "xyz")
            x, y, z = (math.nan if v is None else v for v in (x, y, z))
            v = num(mp.get("velocity_magnitude"))
            vel = math.nan if v is None else v
            name = str(mp.get("active_program_name") or "").strip()
            ovr = num(mp.get("speed_override_percent"))
            self.series["speed_override"].offer(t, ovr, None, ns_ms(mo.get("source_utc_ns")))
            if name != self.prog:
                self.programs.append({"t_ms": t, "from": self.prog, "to": name, "initial": self.prog is None})
                self.prog = name
        elif mo:
            self.motion_unusable += 1
            self.series["speed_override"].reject("invalid")

        self._readback(t, pub_ms, src)
        self._ir(t, src)
        self._gateway(t, dtc, src.get("gateway"))
        self.t.append(t)
        self.dt.append(dtc)
        self.fl.append(flags)
        self.x.append(x)
        self.y.append(y)
        self.z.append(z)
        self.vel.append(vel)
        if flags:
            self._detect(t, dtc, brk, ex, mv, name)
            self.n_state += 1

    def _fresh(self, src_ms, pub_ms):
        return src_ms is None or pub_ms - src_ms <= STALE_READBACK_MS

    def _readback(self, t, pub_ms, src):
        dr = src.get("duet_readback") if isinstance(src.get("duet_readback"), dict) else {}
        p = dr.get("payload") if isinstance(dr.get("payload"), dict) else {}
        ok = dr.get("valid") is True and bool(p)
        dr_ms = ns_ms(dr.get("source_utc_ns"))
        S = self.series

        def offer(key, v, valid, src_ms):
            why = None if valid else "invalid"
            if valid and v is not None and not self._fresh(src_ms, pub_ms):
                why = "stale"
                self.stale[key] += 1
            S[key].offer(t, v, why, src_ms)

        if not ok:
            for k in ("flow", "nozzle_target", "nozzle_state", "bed_target", "bed_state"):
                S[k].reject("invalid")
            return
        if "extrusion_percent" in p or "extrusion_valid" in p:
            offer("flow", num(p.get("extrusion_percent")), p.get("extrusion_valid") is True, ns_ms(p.get("extrusion_source_utc_ns")))
        else:
            S["flow"].reject("absent")
        temps = p.get("temperatures") if isinstance(p.get("temperatures"), dict) else {}
        for key, tag, cols in (("nozzle", "nozzle", (self.nz_t, self.nz_cur, self.nz_tgt)),
                               ("bed", "bed", (self.bd_t, self.bd_cur, self.bd_tgt))):
            h = temps.get(key) if isinstance(temps.get(key), dict) else None
            if h is None:
                S[tag + "_target"].reject("absent")
                continue
            valid = h.get("valid") is True
            hns = h.get("source_utc_ns")
            hms_ = ns_ms(hns)
            tgt, cur, state = num(h.get("active_target_c")), num(h.get("current_c")), h.get("state")
            offer(tag + "_target", tgt, valid, hms_)
            offer(tag + "_state", state if isinstance(state, str) else None, valid, hms_)
            if valid and self._fresh(hms_, pub_ms):
                if tag == "nozzle" and tgt is not None:
                    self.nz_last = (t, tgt, state)
                if hns != self.last_src[tag] and cur is not None:
                    self.last_src[tag] = hns
                    cols[0].append(t)
                    cols[1].append(cur)
                    cols[2].append(math.nan if tgt is None else tgt)
        for i, f in enumerate(p.get("fans") or []):
            if not isinstance(f, dict):
                continue
            key = f"fan{i}"
            if key not in S:
                S[key] = Series(key, f"Fan {i} PWM ({f.get('name') or 'unnamed'})", "%", 1)
            v = num(f.get("actualValue"))
            offer(key, None if v is None else v * 100, True, dr_ms)

    def _ir(self, t, src):
        me = src.get("melt") if isinstance(src.get("melt"), dict) else {}
        mp = me.get("payload") if isinstance(me.get("payload"), dict) else {}
        key = me.get("source_utc_ns")
        if me and (key is None or key != self.last_src["melt"]):
            self.last_src["melt"] = key
            v = num(mp.get("temperature_c")) if me.get("valid") is True and mp.get("valid") is True else None
            self.melt_t.append(t)
            self.melt_v.append(math.nan if v is None else v)
        pr = src.get("process") if isinstance(src.get("process"), dict) else {}
        key = pr.get("source_utc_ns")
        if pr and (key is None or key != self.last_src["process"]):
            self.last_src["process"] = key
            pp = pr.get("payload") if isinstance(pr.get("payload"), dict) else {}
            sub = dig(pp, "measurement", "substrate")
            v = num(dig(sub, "temperature_c")) if pr.get("valid") is True and dig(sub, "valid") is True else None
            self.sub_t.append(t)
            self.sub_v.append(math.nan if v is None else v)
            pid = pp.get("pid_state") if pr.get("valid") is True else None
            if isinstance(pid, dict):
                S = self.series
                S["pid_cooling"].offer(t, dig(pid, "cooling", "enabled"), None)
                S["pid_speed"].offer(t, dig(pid, "speed_substrate", "enabled"), None)
                S["pid_speed_mode"].offer(t, pid.get("speed_mode"), None)

    def _gateway(self, t, dtc, gw):
        p = gw.get("payload") if isinstance(gw, dict) and isinstance(gw.get("payload"), dict) else None
        if gw is None or p is None or gw.get("valid") is not True:
            self.gw_prev = None
            return
        state = p.get("state")
        if self.gw_prev is not None:
            self.gw_time[self.gw_prev] += dtc
        self.gw_prev = state
        key = (state, p.get("permits_outputs"), p.get("command_gate_closed"), p.get("output_epoch"))
        if key != self.gw_key:
            if len(self.gw_events) < MAX_LIST:
                self.gw_events.append({"t_ms": t, "from": self.gw_key[0] if self.gw_key else None, "state": state,
                                       "reason": p.get("reason"), "permits_outputs": p.get("permits_outputs"),
                                       "command_gate_closed": p.get("command_gate_closed"),
                                       "output_epoch": p.get("output_epoch"),
                                       "error_class": dig(p, "error", "error_class"), "initial": self.gw_key is None})
            self.gw_key = key

    # ---- print start / end detection ---------------------------------------------------------------------------------
    @staticmethod
    def _streak(r, key, cond, t):
        if not cond:
            r[key] = None
            return 0
        if r[key] is None:
            r[key] = t
        return t - r[key]

    def _new_run(self, t, name, already):
        r = {"start_ms": t, "already_running": already, "program": name, "programs": [name] if name else [], "ex_s": 0.0,
             "last_ex": t, "last_mv": None, "nz_max": 0.0, "end": None, "confirmed": None,
             "_idle": None, "_gone": None, "_chg": None, "_chg_ex": None, "_chg_exs": 0, "_heat": None}
        self.runs.append(r)
        self.run = r
        return r

    def _detect(self, t, dtc, brk, ex, mv, name):
        r = self.run
        if r is None:
            if not ex:
                return
            r = self._new_run(t, name, self.n_state == 0)
        if brk:
            for k in ("_idle", "_gone", "_chg", "_chg_ex", "_heat"):
                r[k] = None
            r["_chg_exs"] = 0
        if not r["program"] and name:
            r["program"] = name
        prog = r["program"]
        changed = bool(prog) and bool(name) and name != prog
        if changed:      # activity under another program name does not belong to this run
            r["_chg_exs"] += dtc if ex else 0
            if ex and r["_chg_ex"] is None:
                r["_chg_ex"] = t
        else:
            r["_chg_ex"], r["_chg_exs"] = None, 0
            if ex:
                r["ex_s"] += dtc / 1000
                r["last_ex"] = t
            if mv:
                r["last_mv"] = t
            if name and name not in r["programs"]:
                r["programs"].append(name)
        nz = self.nz_last if self.nz_last and t - self.nz_last[0] <= 30_000 else None
        if nz:
            r["nz_max"] = max(r["nz_max"], nz[1])
        why = None
        if self._streak(r, "_idle", not ex and not mv, t) >= IDLE_S * 1000:
            why = ("idle", f"no extrusion and no motion for {IDLE_S:.0f} s")
        if prog:
            if self._streak(r, "_gone", not name and not ex, t) >= PROGRAM_GONE_S * 1000 and not why:
                why = ("program_cleared", f"program name empty for {PROGRAM_GONE_S:.0f} s (was {prog})")
            if self._streak(r, "_chg", changed, t) >= PROGRAM_CHANGED_S * 1000 and not why:
                why = ("program_changed", f"program name {name} for {PROGRAM_CHANGED_S:.0f} s (was {prog})")
        off = bool(nz) and r["nz_max"] >= HEATER_ON_C and not ex and (nz[1] < HEATER_OFF_C or nz[2] == "off")
        if self._streak(r, "_heat", off, t) >= HEATER_OFF_S * 1000 and not why:
            why = ("heater_off", f"nozzle target {nz[1]:g} C / state {nz[2]} for {HEATER_OFF_S:.0f} s, no extrusion")
        if why:
            r["end"] = {"rule": why[0], "detail": why[1], "detected_ms": t, "end_ms": r["last_ex"], "last_motion_ms": r["last_mv"]}
            r["confirmed"] = r["ex_s"] >= MIN_PRINT_EXTRUSION_S
            self.run = None
            if r["confirmed"]:
                self.ended.append(r)
            if why[0] == "program_changed" and r["_chg_ex"]:
                nr = self._new_run(r["_chg_ex"], name, False)
                nr["ex_s"], nr["last_ex"] = r["_chg_exs"] / 1000, t if ex else r["_chg_ex"]

    # ---- writer ------------------------------------------------------------------------------------------
    def feed_writer(self, raw):
        w = self.writer
        try:
            e = json.loads(raw)
            t, topic, msg = int(e["recv_ms"]), str(e["topic"]), e["msg"]
        except (ValueError, KeyError, TypeError):
            w["bad"] += 1
            return
        if e.get("retain"):
            w["retained"] += 1
        if topic.endswith("/status") and isinstance(msg, dict):
            self._status(t, msg, bool(e.get("retain")))
        elif topic.endswith("/result"):
            self._result(t, msg, bool(e.get("retain")))
        else:
            w["other"][topic] += 1

    def _status(self, t, msg, retained):
        w = self.writer
        if w["n"] == 0:
            w["retained_first"] = retained
        w["n"] += 1
        prev = w["prev"]
        if w["last"] is not None:
            gap = (t - w["last"]) / 1000
            if gap > STATUS_GAP_S and len(w["gaps"]) < MAX_LIST:
                w["gaps"].append({"start_ms": w["last"], "end_ms": t, "seconds": round(gap, 2)})
            w["auth_s"][str(prev.get("normal_authorized"))] += min(gap, STATUS_GAP_S)
        w["first"] = t if w["first"] is None else w["first"]
        w["last"] = t
        skip = ("published_utc_ms", "expires_utc_ms")
        if prev is not None:
            ch = {k: [prev.get(k), msg.get(k)] for k in set(prev) | set(msg) if k not in skip and prev.get(k) != msg.get(k)}
            if ch and len(w["changes"]) < MAX_LIST:
                w["changes"].append({"t_ms": t, "changes": ch})
            a, b = prev.get("highest_admitted_sequence"), msg.get("highest_admitted_sequence")
            if isinstance(a, int) and isinstance(b, int) and prev.get("gateway_session_id") == msg.get("gateway_session_id") \
                    and b != a and len(w["admitted"]) < MAX_LIST:
                w["admitted"].append({"t_ms": t, "from": a, "to": b, "delta": b - a})
        w["prev"] = msg

    def _result(self, t, msg, retained):
        d = msg if isinstance(msg, dict) else {}
        obs = d.get("observation") if isinstance(d.get("observation"), dict) else {}
        op = next((v for v in (d.get("operation"), dig(d, "correction", "operation"), dig(d, "request", "correction", "operation"),
                               obs.get("operation")) if v is not None), None)
        req = next((v for v in (d.get("requested_value"), dig(d, "correction", "value"), obs.get("requested_value"),
                                d.get("value")) if v is not None), None)
        row = {"t_ms": t, "state": d.get("state"), "reason": d.get("reason"), "sequence": d.get("sequence"),
               "request_id": d.get("request_id"), "operation": op, "requested": req,
               "observed_value": obs.get("observed_value"), "setting_confirmed": obs.get("setting_confirmed"),
               "gateway_session_id": d.get("gateway_session_id"), "retain": retained, "raw": msg}
        if len(self.writer["results"]) < MAX_LIST:
            self.writer["results"].append(row)

    # ---- summary ---------------------------------------------------------------------------------------------------
    def last_ms(self):
        return self.t[-1] if self.t else None

    def primary_run(self):
        return next((r for r in self.runs if r["confirmed"] or (r["confirmed"] is None and r["ex_s"] >= MIN_PRINT_EXTRUSION_S)), None)

    def z_at(self, t):
        i = bisect.bisect_right(self.t, t) - 1
        for j in range(i, max(i - 60, -1), -1):
            if self.z[j] == self.z[j]:
                return self.z[j]
        return None

    def layers(self, i0, i1):
        segs, ref = [], None
        for i in range(i0, i1):
            if not self.fl[i] & 2 or self.z[i] != self.z[i]:
                continue
            z = self.z[i]
            if ref is None or abs(z - ref) >= LAYER_HYSTERESIS_MM:
                segs.append({"start_ms": self.t[i], "_sum": 0.0, "n": 0, "_ex": 0})
                ref = z
            segs[-1]["_sum"] += z
            segs[-1]["n"] += 1
            segs[-1]["_ex"] += self.dt[i]
        out = []
        for sg in segs:
            if sg["_ex"] < MIN_LAYER_EXTRUDE_S * 1000:
                continue
            if out and abs(sg["_sum"] / sg["n"] - out[-1]["_sum"] / out[-1]["n"]) < LAYER_HYSTERESIS_MM:
                out[-1]["_sum"] += sg["_sum"]
                out[-1]["n"] += sg["n"]
                continue
            out.append(sg)
        for k, la in enumerate(out):
            la["z_mean"] = round(la["_sum"] / la["n"], 3)
            nxt = out[k + 1]["start_ms"] if k + 1 < len(out) else self.t[i1 - 1]
            la["seconds"] = round((nxt - la["start_ms"]) / 1000, 1)
        return out, len(segs) - len(out)

    def window(self, a, b):
        i0, i1 = bisect.bisect_left(self.t, a), bisect.bisect_right(self.t, b)
        w = {"messages": i1 - i0, "observed_s": round(sum(self.dt[i0:i1]) / 1000, 1)}
        use = [i for i in range(i0, i1) if self.fl[i]]
        ext = [i for i in use if self.fl[i] & 2]
        mov = [i for i in use if self.fl[i] & 4 and not self.fl[i] & 2]
        tm = lambda idx: round(sum(self.dt[i] for i in idx) / 1000, 1)
        w["extruding_s"], w["moving_not_extruding_s"] = tm(ext), tm(mov)
        w["stationary_s"] = round(max(tm(use) - w["extruding_s"] - w["moving_not_extruding_s"], 0), 1)
        zs = [self.z[i] for i in use if self.z[i] == self.z[i]]
        zx = [self.z[i] for i in ext if self.z[i] == self.z[i]]
        w["z_all_mm"] = [round(min(zs), 3), round(max(zs), 3)] if zs else None
        w["z_extruding_mm"] = [round(min(zx), 3), round(max(zx), 3)] if zx else None
        xs = [self.x[i] for i in ext if self.x[i] == self.x[i]]
        ys = [self.y[i] for i in ext if self.y[i] == self.y[i]]
        w["xy_extruding_bbox_mm"] = {"x": [round(min(xs), 2), round(max(xs), 2)], "y": [round(min(ys), 2), round(max(ys), 2)]} if xs and ys else None
        w["commanded_speed_extruding_mm_s"] = describe([self.vel[i] for i in ext])
        lay, transit = self.layers(i0, i1)
        diffs = [lay[k + 1]["z_mean"] - lay[k]["z_mean"] for k in range(len(lay) - 1)]
        up = sorted(d for d in diffs if d > 0)
        w["layers"] = {"estimated_count": len(lay), "layer_height_mm_median": round(up[len(up) // 2], 3) if up else None,
                       "z_revisits": sum(1 for k, la in enumerate(lay) if any(abs(la["z_mean"] - o["z_mean"]) < LAYER_HYSTERESIS_MM for o in lay[:k])) if len(lay) <= 3000 else None,
                       "transit_levels_ignored": transit,
                       "note": f"estimate: Z of extruding samples bucketed with {LAYER_HYSTERESIS_MM} mm hysteresis; levels with under "
                               f"{MIN_LAYER_EXTRUDE_S:g} s of extrusion count as Z transit and are ignored; travel Z-hops are ignored; "
                               "a recording that starts mid-print misses the earlier layers", "list": lay[:MAX_LIST]}
        w["heaters"] = {}
        for tag, ts, cur, tgt in (("nozzle", self.nz_t, self.nz_cur, self.nz_tgt), ("bed", self.bd_t, self.bd_cur, self.bd_tgt)):
            j0, j1 = bisect.bisect_left(ts, a), bisect.bisect_right(ts, b)
            dev = [abs(cur[j] - tgt[j]) for j in range(j0, j1) if tgt[j] == tgt[j]]
            w["heaters"][tag] = {"current_c": describe(cur[j0:j1]), "max_abs_deviation_from_target_c": round(max(dev), 2) if dev else None}
        w["ir"] = {}
        for tag, ts, vs in (("melt_temperature_c", self.melt_t, self.melt_v), ("previous_layer_temperature_c", self.sub_t, self.sub_v)):
            j0, j1 = bisect.bisect_left(ts, a), bisect.bisect_right(ts, b)
            n = j1 - j0
            ok = [v for v in vs[j0:j1] if v == v]
            w["ir"][tag] = {"updates": n, "valid": len(ok), "valid_pct": round(100 * len(ok) / n, 1) if n else None, "valid_values": describe(ok)}
        return w

    def summarize(self, done=False, files=None):
        first, last = (self.t[0], self.t[-1]) if self.t else (None, None)
        run = self.primary_run()
        win = None
        if run:
            ended = run["end"] is not None
            a, b = run["start_ms"], run["end"]["end_ms"] if ended else last
            win = self.window(a, b)
        status = "no_print_detected" if not run else "ended" if run["end"] else "in_progress"
        runs = []
        for r in self.runs:
            e = r["end"]
            b = e["end_ms"] if e else last
            i0, i1 = bisect.bisect_left(self.t, r["start_ms"]), bisect.bisect_right(self.t, b)
            zs = [self.z[i] for i in range(i0, i1) if self.fl[i] & 2 and self.z[i] == self.z[i]]
            runs.append({"start_ms": r["start_ms"], "start_local": iso(r["start_ms"]), "start_is_lower_bound": r["already_running"],
                         "end_ms": e["end_ms"] if e else None, "end_local": iso(e["end_ms"]) if e else None,
                         "status": "ended" if e else "in_progress", "end_rule": e["rule"] if e else None,
                         "end_detail": e["detail"] if e else None, "detected_ms": e["detected_ms"] if e else None,
                         "detected_local": iso(e["detected_ms"]) if e else None,
                         "last_motion_ms": e["last_motion_ms"] if e else None,
                         "unobserved_s_between_last_extrusion_and_detection": round(sum(
                             min(g["end_ms"], e["detected_ms"]) - max(g["start_ms"], e["end_ms"]) for g in self.gaps
                             if g["end_ms"] > e["end_ms"] and g["start_ms"] < e["detected_ms"]) / 1000, 1) if e else None,
                         "last_motion_local": iso(e["last_motion_ms"]) if e else None,
                         "duration_s": round((b - r["start_ms"]) / 1000, 1), "extruding_s": round(r["ex_s"], 1),
                         "is_print": bool(r["confirmed"]) if r["end"] else r["ex_s"] >= MIN_PRINT_EXTRUSION_S, "is_primary": r is run,
                         "programs": r["programs"], "z_extruding_mm": [round(min(zs), 3), round(max(zs), 3)] if zs else None})
        S = self.series
        stats = {k: {"label": s.label, "unit": s.unit, "first": s.first, "last": s.cur, "min": s.lo, "max": s.hi,
                     "changes": len(s.events) + s.dropped, "used_samples": s.used, "rejected": dict(s.rejected),
                     "stale_excluded": self.stale.get(k, 0)} for k, s in S.items() if s.used or s.rejected}
        lay = win["layers"]["list"] if win else []
        lay_t = [la["start_ms"] for la in lay]
        changes = []
        for k, s in S.items():
            if k.startswith("pid") and not s.events:
                continue
            for ev in s.events:
                t = ev["t_ms"]
                row = dict(ev, key=k, label=s.label, unit=s.unit, z_mm=self.z_at(t))
                if run:
                    end = run["end"]["end_ms"] if run["end"] else last
                    row["phase"] = "before print" if t < run["start_ms"] else "during print" if t <= end else "after print"
                    row["since_print_start_s"] = round((t - run["start_ms"]) / 1000, 1)
                    row["layer_est"] = bisect.bisect_right(lay_t, t) if row["phase"] == "during print" and lay_t else None
                changes.append(row)
        changes.sort(key=lambda c: c["t_ms"])
        w = self.writer
        link_writer(changes, w["results"], bool(w["n"] or w["results"]))
        for c in changes:
            c["t_local"] = iso(c["t_ms"])
        for lst in (self.gw_events, self.sessions, self.programs):
            for ev in lst:
                ev["t_local"] = iso(ev["t_ms"])
        for lst in (w["changes"], w["admitted"], w["results"]):
            for ev in lst:
                ev["t_local"] = iso(ev["t_ms"])
        for g in list(self.gaps) + list(w["gaps"]):
            g["start_local"], g["end_local"] = iso(g["start_ms"]), iso(g["end_ms"])
        gw_obs = sum(self.gw_time.values())
        by_req = {}
        for r_ in w["results"]:
            by_req.setdefault(r_["request_id"], []).append(r_)
        n_tel = len(self.t)
        return clean({
            "generator": "tools/mqtt/print_watch.py", "generated_ms": int(time.time() * 1000), "generated_local": iso(int(time.time() * 1000)),
            "status": status, "recording_finished": done, "files": files or {},
            "recording": {"first_ms": first, "last_ms": last, "first_local": iso(first), "last_local": iso(last),
                          "duration_s": round((last - first) / 1000, 1) if n_tel else None, "telemetry_messages": n_tel,
                          "parse_errors": self.bad_tel, "time_regressions": self.time_regressions,
                          "sequence_regressions": self.seq_regressions, "observed_s": round(sum(self.dt) / 1000, 1),
                          "recv_minus_published_ms": {"min": self.lag[1], "max": self.lag[2],
                                                     "mean": round(self.lag[3] / self.lag[0], 1) if self.lag[0] else None},
                          "source_valid_pct": {k: round(100 * self.src_ok[k] / n, 1) for k, n in sorted(self.src_n.items())},
                          "motion_samples_unusable": self.motion_unusable},
            "print": None if not run else {
                **{k: v for k, v in next(x for x in runs if x["is_primary"]).items() if k not in ("is_print", "is_primary")},
                "program": run["program"], "already_running_at_recording_start": run["already_running"],
                "other_runs": len(self.runs) - 1, "window": win},
            "runs": runs,
            "telemetry_sessions": {"changes": max(len(self.sessions) - 1, 0), "list": self.sessions},
            "telemetry_gaps": {"threshold_s": GAP_S, "count": self.gap_count, "total_s": round(self.gap_total_ms / 1000, 1),
                               "longest_s": max((g["seconds"] for g in self.gaps), default=None), "list": self.gaps},
            "program_names": self.programs,
            "setpoints": stats,
            "setpoint_changes": changes,
            "setpoint_changes_dropped": {k: s.dropped for k, s in S.items() if s.dropped},
            "gateway": {"time_in_state_s": {k: round(v / 1000, 1) for k, v in self.gw_time.items()},
                        "observed_s": round(gw_obs / 1000, 1), "changes": self.gw_events},
            "writer": {"present": bool(w["n"] or w["results"] or w["other"]), "status_messages": w["n"],
                       "first_local": iso(w["first"]), "last_local": iso(w["last"]), "retained_messages": w["retained"],
                       "first_status_was_retained": w["retained_first"], "parse_errors": w["bad"],
                       "other_topics": dict(w["other"]), "status_gaps_over_s": STATUS_GAP_S, "status_gaps": w["gaps"],
                       "seconds_by_normal_authorized": {k: round(v, 1) for k, v in w["auth_s"].items()},
                       "status_changes": w["changes"], "admitted_sequence_increments": w["admitted"],
                       "admitted_commands_inferred": sum(a["delta"] for a in w["admitted"] if a["delta"] > 0),
                       "results": w["results"],
                       "request_outcomes": [{"request_id": k, "sequence": v[0]["sequence"], "operation": next((x["operation"] for x in v if x["operation"]), None),
                                             "first_local": v[0]["t_local"], "states": [x["state"] for x in v], "final_state": v[-1]["state"],
                                             "reason": next((x["reason"] for x in reversed(v) if x["reason"]), None),
                                             "observed_value": next((x["observed_value"] for x in reversed(v) if x["observed_value"] is not None), None)}
                                            for k, v in by_req.items()]},
            "previews": self.frames["previews"].summary(),
            "ir_frames": self.frames["ir"].summary(),
        })


OP_KEYS = {"flow_percent": ("flow",), "nozzle_temperature_c": ("nozzle_target",), "speed_override_percent": ("speed_override",),
           "fan_speed_percent": ("fan0", "fan1")}


def link_writer(changes, results, have_writer):
    """Pair telemetry setpoint changes with writer results (result 5 s before to 15 s after the change; the observed value
    must equal the new value, except for fans). Results do not name their operation, so this is the only way to tell what a
    request changed; it is an inference from timing and value."""
    matched = {}
    for c in changes:
        c["writer_result"] = None
        if not have_writer or c["key"] not in ("flow", "nozzle_target", "speed_override", "fan0", "fan1"):
            continue

        def fits(r):
            if not -5_000 <= r["t_ms"] - c["t_ms"] <= 15_000:
                return False
            if r["operation"] is not None and c["key"] not in OP_KEYS.get(r["operation"], ()):
                return False
            ov = num(r["observed_value"])
            return ov is None or c["key"].startswith("fan") or abs(ov - c["to"]) < 0.51
        near = [r for r in results if fits(r)]
        if near:
            r = min(near, key=lambda r: (r["observed_value"] is None, abs(r["t_ms"] - c["t_ms"])))
            c["writer_result"] = {"request_id": r["request_id"], "sequence": r["sequence"], "state": r["state"]}
            matched[r["request_id"]] = f"{c['label']} {c['from']} to {c['to']} at {iso(c['t_ms'])[11:19]}"
    for r in results:
        r["telemetry_change_seen"] = matched.get(r["request_id"])


# ---- markdown ----------------------------------------------------------------------------------------------------------
def table(head, rows):
    esc = lambda v: "-" if v is None else str(v).replace("|", "\\|").replace("\n", " ")
    return "\n".join(["| " + " | ".join(head) + " |", "|" + "---|" * len(head)] + ["| " + " | ".join(esc(c) for c in r) + " |" for r in rows]) + "\n"


def fv(v, unit=""):
    if v is None:
        return "-"
    return f"{v:g}{' ' + unit if unit else ''}" if isinstance(v, (int, float)) and not isinstance(v, bool) else str(v)


def fd(d):
    return "-" if not d else ", ".join(f"{k} {v}" for k, v in d.items())


def render_md(s, name):
    L, rec, pr = [], s["recording"], s["print"]
    L.append(f"# Print summary: {name}\n")
    part = "" if s["recording_finished"] else " **Partial: the recording was still running when this was written.**"
    L.append(f"Written {stamp(s['generated_ms'])}.{part}\n")
    # overview
    L.append("## Overview\n")
    o = []
    if not rec["telemetry_messages"]:
        o.append("No telemetry has been read yet.")
    else:
        o.append(f"Recording window: {stamp(rec['first_ms'])} to {hms(rec['last_ms'])} ({dur(rec['duration_s'])}, "
                 f"{rec['telemetry_messages']:,} telemetry messages).")
    if pr is None and rec["telemetry_messages"]:
        if s["runs"]:
            r0 = s["runs"][-1]
            o.append(f"No print yet: extrusion started at {hms(r0['start_ms'])} but has totalled only {dur(r0['extruding_s'])} (a print needs 60 s).")
        else:
            o.append("No print was detected: no extrusion was seen.")
    elif pr:
        w = pr["window"]
        o.append(f"Program: {', '.join(f'`{p}`' for p in pr['programs']) or 'name not reported'}.")
        if pr["already_running_at_recording_start"]:
            o.append("The print was **already running when the recording started**, so the start time below is only the start of our data "
                     "and the duration and layer count are lower bounds.")
        if pr["status"] == "ended":
            o.append(f"Printed from {hms(pr['start_ms'])}{' (recording start)' if pr['start_is_lower_bound'] else ''} to {hms(pr['end_ms'])}: "
                     f"{dur(pr['duration_s'])}. The end was detected at {hms(pr['detected_ms'])} by rule `{pr['end_rule']}` ({pr['end_detail']}).")
        else:
            tail = "the recording ended while it was still printing, so no end was seen" if s["recording_finished"] else "still running at the last data, no end detected yet"
            o.append(f"Printing since {hms(pr['start_ms'])}{' (recording start)' if pr['start_is_lower_bound'] else ''} until {hms(rec['last_ms'])} "
                     f"({dur(pr['duration_s'])}): {tail}.")
        ly = w["layers"]
        if ly["estimated_count"]:
            o.append(f"Layers (estimate): about {ly['estimated_count']}"
                     + (f" at roughly {ly['layer_height_mm_median']} mm each" if ly["layer_height_mm_median"] else "")
                     + f"; extruding Z {w['z_extruding_mm'][0]} to {w['z_extruding_mm'][1]} mm.")
        ex_pct = 100 * w["extruding_s"] / max(w["observed_s"], 1)
        o.append(f"Extruding {dur(w['extruding_s'])} of {dur(w['observed_s'])} observed ({ex_pct:.0f} %); the rest was travel or standing still.")
        # what changed
        ch = [c for c in s["setpoint_changes"] if c.get("phase") == "during print" and not c["key"].startswith("pid")]
        const = [f"{st['label']} {fv(st['last'], st['unit'])}" for k, st in s["setpoints"].items()
                 if not k.startswith("pid") and st["changes"] == 0 and st["last"] is not None]
        if ch:
            o.append(f"Setpoint changes during the print: {len(ch)}. " + "; ".join(
                f"{hms(c['t_ms'])} {c['label']} {fv(c['from'])} to {fv(c['to'], c['unit'])}" for c in ch[:12])
                + ("; more in the change log below." if len(ch) > 12 else "."))
        else:
            o.append("No setpoint changes were seen during the print.")
        if const:
            o.append("Unchanged for the whole recording: " + "; ".join(const) + ".")
        for tag in ("nozzle", "bed"):
            h = w["heaters"][tag]["current_c"]
            if h:
                o.append(f"{tag.capitalize()} temperature while printing: {h['min']} to {h['max']} C, mean {h['mean']} C "
                         f"(largest gap to target {w['heaters'][tag]['max_abs_deviation_from_target_c']} C).")
    # problems
    prob = []
    g = s["telemetry_gaps"]
    if g["count"]:
        prob.append(f"{g['count']} telemetry gaps over {g['threshold_s']:g} s (longest {g['longest_s']} s, {g['total_s']} s in total).")
    if s["telemetry_sessions"]["changes"]:
        prob.append(f"The telemetry session id changed {s['telemetry_sessions']['changes']} time(s).")
    gwt = s["gateway"]["time_in_state_s"]
    off = [e for e in s["gateway"]["changes"] if not e["initial"] and e["state"] != "RUN"]
    if off:
        prob.append(f"The gateway left RUN {len(off)} time(s): " + "; ".join(f"{hms(e['t_ms'])} {e['state']} ({e['reason']})" for e in off[:6])
                    + (" (first 6 shown)" if len(off) > 6 else "") + f". Time in states (s): {', '.join(f'{k} {v:g}' for k, v in gwt.items())}.")
    elif gwt and set(gwt) != {"RUN"}:
        prob.append(f"The gateway was not in RUN for the whole recording (time in states, s: {', '.join(f'{k} {v:g}' for k, v in gwt.items())}).")
    wr = s["writer"]
    if wr["present"]:
        unconfirmed = [r for r in wr["request_outcomes"] if r["final_state"] != "setting_confirmed"]
        o.append(f"Command channel: {len(wr['results'])} result message(s) from {len(wr['request_outcomes'])} request(s); "
                 f"{wr['admitted_commands_inferred']} admitted command(s) inferred from the sequence counter (any sender).")
        if unconfirmed:
            prob.append(f"{len(unconfirmed)} command request(s) did not end as setting_confirmed: "
                        + "; ".join(f"seq {r['sequence']} {r['final_state']}" + (f" ({r['reason']})" if r["reason"] else "") for r in unconfirmed[:6])
                        + (" (first 6 shown)" if len(unconfirmed) > 6 else "") + ". Not confirmed means the outcome is unknown, not that nothing changed.")
        if wr["status_gaps"]:
            prob.append(f"{len(wr['status_gaps'])} gap(s) over {wr['status_gaps_over_s']:g} s in the command-gateway status feed.")
        bad_auth = wr["seconds_by_normal_authorized"].get("False", 0)
        if bad_auth:
            prob.append(f"The command gate was not authorised for {bad_auth:g} s.")
    else:
        o.append("No writer.jsonl in this capture, so command traffic is unknown.")
    if pr and pr["status"] == "ended":
        later = [r for r in s["runs"] if r["start_ms"] > pr["end_ms"]]
        if later:
            prob.append(f"Extrusion started again at {hms(later[0]['start_ms'])}, after the detected end: either the end rule fired during a pause "
                        "or a second print began. See 'All extrusion runs seen'.")
    for fname, info in s["files"].items():
        if info.get("lines_skipped_after_error"):
            prob.append(f"{info['lines_skipped_after_error']} line(s) of {fname} raised errors and were skipped.")
    if rec["parse_errors"] or rec["time_regressions"] or rec["sequence_regressions"]:
        prob.append(f"Data quality: {rec['parse_errors']} unreadable telemetry lines, {rec['time_regressions']} time regressions, {rec['sequence_regressions']} sequence regressions.")
    inv = {k: v for k, v in rec["source_valid_pct"].items() if v < 99.5}
    if inv:
        prob.append("Sources flagged valid less than 99.5 % of the time: " + ", ".join(f"{k} {v} %" for k, v in inv.items()) + ".")
    for k, st in s["setpoints"].items():
        bad = {r: n for r, n in st["rejected"].items() if r != "absent"}
        if bad and not k.startswith("pid"):
            prob.append(f"{st['label']}: readings left out ({', '.join(f'{r} {n}' for r, n in bad.items())}); only valid, fresh readings are used.")
        if st["rejected"].get("absent") and not st["used_samples"]:
            prob.append(f"{st['label']}: not present in this recording's telemetry.")
    if pr and pr["window"]["ir"]["melt_temperature_c"]["valid_pct"] is not None:
        ir = pr["window"]["ir"]
        o.append(f"IR temperatures are valid only part of the time (melt {ir['melt_temperature_c']['valid_pct']} %, "
                 f"previous layer {ir['previous_layer_temperature_c']['valid_pct']} % of updates while printing), so they describe those moments only.")
    L.append("\n".join(f"- {x}" for x in o) + "\n")
    L.append("**Problems / things to know**\n")
    L.append(("\n".join(f"- {x}" for x in prob) if prob else "- None found in the data.") + "\n")
    L.append("Times are Europe/Berlin and are when our recorder received a message. The heater and flow readback refresh about every 1.4 s, "
             "so a change really happened up to ~1.5 s before the time shown. Layer counts are estimates; everything else is read directly from the data.\n")

    # recording / print window
    L.append("## Recording and print window\n")
    L.append(table(["Item", "Value"], [
        ["Recording first / last message", f"{stamp(rec['first_ms'])} / {stamp(rec['last_ms'])}"],
        ["Recording length (wall) / observed (gaps excluded)", f"{dur(rec['duration_s'])} / {dur(rec['observed_s'])}"],
        ["Telemetry messages / unreadable lines", f"{rec['telemetry_messages']:,} / {rec['parse_errors']}"],
        ["Receive minus publish, ms (min / mean / max)", f"{rec['recv_minus_published_ms']['min']} / {rec['recv_minus_published_ms']['mean']} / {rec['recv_minus_published_ms']['max']}"],
        ["Source valid % (all messages)", ", ".join(f"{k} {v}" for k, v in rec["source_valid_pct"].items())],
        ["Recorder finished (DONE file)", "yes" if s["recording_finished"] else "no, still recording or stopped early"]]))
    if pr:
        w = pr["window"]
        L.append("\n**Print (detected)**\n")
        L.append(table(["Item", "Value"], [
            ["Status", pr["status"] + (" (recording ended while printing)" if pr["status"] == "in_progress" and s["recording_finished"] else "")],
            ["Start", f"{stamp(pr['start_ms'])}" + (" (recording start; print was already running, so a lower bound)" if pr["start_is_lower_bound"] else " (first extruding message)")],
            ["End (last extruding message)", stamp(pr["end_ms"]) if pr["end_ms"] else "not ended"],
            ["Head stopped moving", stamp(pr["last_motion_ms"]) if pr.get("last_motion_ms") else "-"],
            ["End detected / rule", f"{stamp(pr['detected_ms'])} / {pr['end_rule']}: {pr['end_detail']}" if pr["detected_ms"] else "-"],
            ["Telemetry gap time between last extrusion and detection", f"{pr['unobserved_s_between_last_extrusion_and_detection']} s (the true end may lie inside it)"
             if pr["unobserved_s_between_last_extrusion_and_detection"] else "none"],
            ["Duration", dur(pr["duration_s"])],
            ["Program(s)", ", ".join(pr["programs"]) or "-"],
            ["Extruding / moving without extruding / stationary", f"{dur(w['extruding_s'])} / {dur(w['moving_not_extruding_s'])} / {dur(w['stationary_s'])}"],
            ["Z, all samples in window (mm)", "-" if not w["z_all_mm"] else "{} to {}".format(*w["z_all_mm"])],
            ["Z, extruding samples (mm)", "-" if not w["z_extruding_mm"] else "{} to {}".format(*w["z_extruding_mm"])],
            ["Layers (estimate)", f"{w['layers']['estimated_count']} (median step {w['layers']['layer_height_mm_median']} mm; revisited Z levels {w['layers']['z_revisits']})"],
            ["X/Y extent while extruding (mm)", "-" if not w["xy_extruding_bbox_mm"] else
             "x {} to {}, y {} to {}".format(*w["xy_extruding_bbox_mm"]["x"], *w["xy_extruding_bbox_mm"]["y"])],
            ["Commanded speed while extruding, mm/s", fd(w["commanded_speed_extruding_mm_s"])]]))
        ly = w["layers"]["list"]
        if ly:
            L.append("\n**Layers (estimate)** " + w["layers"]["note"] + "\n")
            num_ly = list(enumerate(ly, 1))
            show = num_ly if len(ly) <= 30 else num_ly[:10] + [None] + num_ly[-10:]
            L.append(table(["Layer", "Z mean (mm)", "Start", "Duration"],
                           [["...", "", "", ""] if x is None else [x[0], x[1]["z_mean"], hms(x[1]["start_ms"]), dur(x[1]["seconds"])] for x in show]))
        if len(s["runs"]) > 1:
            L.append("\n**All extrusion runs seen**\n")
            L.append("A run needs at least 60 s of extrusion to count as a print; the first such run is the one summarised above.\n")
            L.append(table(["#", "Start", "End", "Status", "Rule", "Extruding", "Counts as print", "Summarised above"],
                           [[i + 1, stamp(r["start_ms"]), stamp(r["end_ms"]) if r["end_ms"] else "-", r["status"], r["end_rule"] or "-",
                             dur(r["extruding_s"]), "yes" if r["is_print"] else "no", "yes" if r["is_primary"] else "no"] for i, r in enumerate(s["runs"])]))
    # setpoints
    L.append("\n## Setpoints and readbacks\n")
    L.append("Only valid, fresh readings are used (flow also needs extrusion_valid). Whole recording.\n")
    L.append(table(["Signal", "First", "Last", "Min", "Max", "Changes", "Samples used", "Rejected"],
                   [[st["label"], fv(st["first"], st["unit"]), fv(st["last"], st["unit"]), fv(st["min"]), fv(st["max"]), st["changes"], st["used_samples"],
                     ", ".join(f"{r} {n}" for r, n in st["rejected"].items()) or "-"] for st in s["setpoints"].values()]))
    L.append("\n**Change log** (every change; time = first message carrying the new value)\n")
    ch = s["setpoint_changes"]
    if ch:
        L.append(table(["Time", "Phase", "Since print start", "Layer (est)", "Z mm", "Signal", "From", "To", "Writer result nearby", "Note"],
                       [[hms(c["t_ms"]), c.get("phase", "-"), dur(c["since_print_start_s"]) if c.get("since_print_start_s") is not None and c.get("phase") == "during print" else "-",
                         c.get("layer_est") or "-", c["z_mm"] and round(c["z_mm"], 2), c["label"], fv(c["from"]), fv(c["to"], c["unit"]),
                         ("seq %s %s" % (c["writer_result"]["sequence"], c["writer_result"]["state"])) if c.get("writer_result") else
                         ("none (inference: panel, G-code or another path)" if wr["present"] and c["key"] in ("flow", "nozzle_target", "speed_override", "fan0", "fan1") else "-"),
                         f"no valid reading for {c['unobserved_s']} s before; change time uncertain" if c.get("unobserved_s") else ""] for c in ch]))
    else:
        L.append("No changes: every setpoint kept its first value for the whole recording.\n")
    # temps
    if pr:
        w = pr["window"]
        L.append("\n## Temperatures while printing\n")
        L.append("Heaters: one reading per distinct source timestamp (valid and fresh only). IR: one value per distinct source update; "
                 "percentages are of updates, and statistics use valid values only.\n")
        rows = []
        for tag in ("nozzle", "bed"):
            d = w["heaters"][tag]["current_c"] or {}
            rows.append([f"{tag} current C", d.get("n"), d.get("min"), d.get("p5"), d.get("median"), d.get("mean"), d.get("p95"), d.get("max"), "-"])
        for key, lab in (("melt_temperature_c", "melt (IR) C"), ("previous_layer_temperature_c", "previous layer (IR) C")):
            ir = w["ir"][key]
            d = ir["valid_values"] or {}
            rows.append([lab, d.get("n"), d.get("min"), d.get("p5"), d.get("median"), d.get("mean"), d.get("p95"), d.get("max"),
                         f"{ir['valid_pct']} % of {ir['updates']}"])
        L.append(table(["Signal", "n", "min", "p5", "median", "mean", "p95", "max", "valid"], rows))
    # gateway
    L.append("\n## Gateway state\n")
    gt = s["gateway"]
    L.append("Time in state (s): " + (", ".join(f"{k} {v:g}" for k, v in gt["time_in_state_s"].items()) or "none") + f" of {gt['observed_s']:g} s observed.\n")
    L.append(table(["Time", "State", "Reason", "Permits outputs", "Command gate closed", "Output epoch", "Error class"],
                   [[hms(e["t_ms"]) + (" (first seen)" if e["initial"] else ""), e["state"], e["reason"], e["permits_outputs"], e["command_gate_closed"],
                     e["output_epoch"], e["error_class"]] for e in gt["changes"]]))
    # gaps / sessions
    L.append("\n## Telemetry sessions and gaps\n")
    ss = s["telemetry_sessions"]["list"]
    L.append(f"Session id changes: {s['telemetry_sessions']['changes']}.\n")
    L.append(table(["Time", "Session id", "From"], [[hms(e["t_ms"]) + (" (first seen)" if e["initial"] else ""), e["to"], e["from"]] for e in ss]))
    L.append(f"\nGaps over {g['threshold_s']:g} s (receive times): {g['count']}, {g['total_s']} s in total.\n")
    if g["list"]:
        L.append(table(["From", "To", "Seconds"], [[hms(x["start_ms"]), hms(x["end_ms"]), x["seconds"]] for x in g["list"][:60]]))
        if g["count"] > 60:
            L.append(f"\n(first 60 of {g['count']}; all in summary.json)\n")
    # writer
    L.append("\n## Command gateway (writer.jsonl)\n")
    if not wr["present"]:
        L.append("Not present in this capture.\n")
    else:
        L.append(f"Status messages {wr['status_messages']} ({wr['first_local']} to {wr['last_local']}); retained {wr['retained_messages']}; "
                 f"seconds by normal_authorized: {wr['seconds_by_normal_authorized']}. Admitted-sequence increments reveal commands from any sender: "
                 f"{wr['admitted_commands_inferred']} inferred.\n")
        L.append("\n**Results** (every result message)\n")
        if wr["results"]:
            has_op = any(r["operation"] is not None or r["requested"] is not None for r in wr["results"])
            L.append(("" if has_op else "Results carry no operation name or requested value; ") + "the last column is our inference from timing and value.\n")
            head = ["Time", "Sequence", "Request", "State", "Reason"] + (["Operation", "Requested"] if has_op else []) + ["Observed", "Confirmed", "Telemetry change seen (inference)"]
            L.append(table(head, [[hms(r["t_ms"]), r["sequence"], (r["request_id"] or "-")[:8], r["state"], r["reason"]]
                                  + ([r["operation"], r["requested"]] if has_op else [])
                                  + [r["observed_value"], r["setting_confirmed"], r.get("telemetry_change_seen") or ("none seen" if r["observed_value"] is not None else "-")]
                                  for r in wr["results"][:300]]))
        else:
            L.append("No result messages: nobody sent a command through the gateway during this capture (as far as the results show).\n")
        L.append("\n**Gate and status changes** (any field except the publish/expiry stamps)\n")
        L.append(table(["Time", "Field", "From", "To"],
                       [[hms(c["t_ms"]), k, v[0], v[1]] for c in wr["status_changes"][:300] for k, v in c["changes"].items()]))
        L.append("\n**highest_admitted_sequence increments**\n")
        L.append(table(["Time", "From", "To", "Delta"], [[hms(a["t_ms"]), a["from"], a["to"], a["delta"]] for a in wr["admitted_sequence_increments"]]))
        if wr["status_gaps"]:
            L.append(f"\nStatus gaps over {wr['status_gaps_over_s']:g} s: " + "; ".join(f"{hms(x['start_ms'])}-{hms(x['end_ms'])} ({x['seconds']} s)" for x in wr["status_gaps"][:20]) + "\n")
    # frame metadata
    for key, fname in (("previews", "previews.jsonl"), ("ir_frames", "ir.jsonl")):
        pv = s[key]
        L.append(f"\n## Camera frame metadata ({fname})\n")
        if not pv["messages"] and not pv["parse_errors"]:
            L.append("No frames recorded in this file (or the file is absent).\n")
            continue
        L.append(f"{pv['messages']} frame lines ({pv['first_local']} to {pv['last_local']}), {pv['with_file_reference']} with an image file reference. "
                 "Only the metadata is counted; image files are not read. Camera index is zero-based.\n")
        L.append(table(["Kind", "Camera index", "Frames", "First", "Last"],
                       [[c["camera_kind"], c["camera_index"], c["n"], (c["first_local"] or "-")[11:19], (c["last_local"] or "-")[11:19]] for c in pv["by_kind_and_camera"]]))
        L.append("\n" + table(["Kind", "Camera index", "Role (topic)", "Frames"], [[c["camera_kind"], c["camera_index"], c["role"], c["n"]] for c in pv["by_kind_camera_role"]]))
        L.append(f"\nGaps over {pv['gap_threshold_s']:g} s per camera kind (shorter RGB silences of 7-9 s are normal): {pv['gap_count']}.\n")
        if pv["gaps"]:
            L.append(table(["Kind", "From", "To", "Seconds"], [[g["camera_kind"], g["start_local"][11:19], g["end_local"][11:19], g["seconds"]] for g in pv["gaps"][:25]]))
            if pv["gap_count"] > 25:
                L.append(f"\n(first 25 of {pv['gap_count']}; all in summary.json)\n")
    if s["previews"]["messages"] and s["ir_frames"]["messages"]:
        L.append("\nIR frame metadata can appear in both files (previews.jsonl keeps metadata only); do not add the two IR counts together.\n")
    return "\n".join(L)


# ---- driver -----------------------------------------------------------------------------------------------------------
def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


class Feeder:
    def __init__(self, cap_dir):
        self.dir, self.cap, self.errors = cap_dir, Capture(), Counter()
        self.fns = {"telemetry.jsonl": self.cap.feed_telemetry, "previews.jsonl": self.cap.frames["previews"].feed,
                    "ir.jsonl": self.cap.frames["ir"].feed, "writer.jsonl": self.cap.feed_writer}
        self.tails = {n: Tail(os.path.join(cap_dir, n)) for n in self.fns}

    def pump(self):
        n = Counter()
        for name, tail in self.tails.items():
            fn = self.fns[name]
            for line in tail.lines():
                try:
                    fn(line)
                except Exception as e:  # one odd record must not stop the run; counted and shown in the summary
                    self.errors[name] += 1
                    if self.errors[name] <= 3:
                        log(f"WARNING {name}: skipped a line that raised {type(e).__name__}: {e}")
                n[name] += 1
        return n

    def done(self):
        return os.path.exists(os.path.join(self.dir, "DONE"))

    def write_summary(self):
        files = {n: {"bytes": os.path.getsize(t.path), "pending_partial_line_bytes": t.pending, "lines_skipped_after_error": self.errors[n]}
                 for n, t in self.tails.items() if os.path.exists(t.path)}
        s = self.cap.summarize(self.done(), files)
        write_atomic(os.path.join(self.dir, "summary.json"), json.dumps(s, indent=1))
        write_atomic(os.path.join(self.dir, "SUMMARY.md"), render_md(s, os.path.basename(os.path.abspath(self.dir))))
        return s

    def write_marker(self, run, n):
        path = os.path.join(self.dir, "PRINT_END" if n == 1 else f"PRINT_END_{n}")
        e = run["end"]
        body = {"reason": e["rule"], "detail": e["detail"], "detected_at": iso(e["detected_ms"]), "marker_written_at": iso(int(time.time() * 1000)),
                "print_start": iso(run["start_ms"]), "start_is_lower_bound": run["already_running"],
                "print_end_estimate_last_extrusion": iso(e["end_ms"]), "last_motion": iso(e["last_motion_ms"]),
                "duration_s": round((e["end_ms"] - run["start_ms"]) / 1000, 1), "extruding_s": round(run["ex_s"], 1), "programs": run["programs"],
                "note": "times are Europe/Berlin receive times of our recorder; full summary in SUMMARY.md / summary.json"}
        if os.path.exists(path):
            log(f"{os.path.basename(path)} already exists, not overwritten")
        else:
            write_atomic(path, json.dumps(body, indent=1) + "\n")
        return body


def headline(s):
    r, p = s["recording"], s["print"]
    if not p:
        return f"{r['telemetry_messages']} telemetry messages, no print detected"
    w = p["window"]
    return (f"{p['status']}: {p['program']} {hms(p['start_ms'])}-{hms(p['end_ms'] or r['last_ms'])} ({dur(p['duration_s'])}), "
            f"~{w['layers']['estimated_count']} layers, {len([c for c in s['setpoint_changes'] if c.get('phase') == 'during print'])} setpoint changes")


def run_summary(path):
    f = Feeder(path)
    t0 = time.time()
    f.pump()
    s = f.write_summary()
    log(f"read {s['recording']['telemetry_messages']} telemetry lines in {time.time() - t0:.1f} s; wrote summary.json and SUMMARY.md in {path}")
    log(headline(s))


def run_watch(path, poll, refresh):
    f = Feeder(path)
    stop = {"now": False}

    def on_term(*_):
        stop["now"] = True
    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGINT, on_term)
    log(f"watching {path} every {poll:g} s, summary every {refresh:g} s; end rules: idle {IDLE_S:g} s, heater off, program cleared/changed")

    def summary(label):
        try:
            s = f.write_summary()
            log(f"{label}: " + headline(s))
        except Exception:
            log("ERROR writing summary, will retry:\n" + traceback.format_exc())

    last_summary, last_growth, markers, warned, runs_seen = 0.0, time.time(), 0, False, 0
    while True:
        done = f.done()
        n = f.pump()
        now = time.time()
        if n["telemetry.jsonl"]:
            last_growth, warned = now, False
        c = f.cap
        for run in c.ended:
            markers += 1
            m = f.write_marker(run, markers)
            log(f"PRINT END: rule {m['reason']} ({m['detail']}); last extrusion {m['print_end_estimate_last_extrusion']}, detected {m['detected_at']}")
            last_summary = 0.0
        c.ended.clear()
        if len(c.runs) > runs_seen:
            runs_seen = len(c.runs)
            if markers and c.run:
                log(f"NOTE extrusion started again at {hms(c.run['start_ms'])} after a PRINT_END marker: a pause, or a second print (see runs in SUMMARY.md)")
        if now - last_summary >= refresh:
            summary("summary written")
            last_summary = now
        r = c.run
        log(f"telemetry {len(c.t)} msgs, last {hms(c.last_ms())}, +{n['telemetry.jsonl']} lines (+{sum(n.values()) - n['telemetry.jsonl']} other); "
            + (f"printing since {hms(r['start_ms'])}, extruded {r['ex_s']:.0f} s, z {c.z_at(c.t[-1])}" if r else f"no print running ({len(c.runs)} run(s) seen)")
            + f"; gateway {c.gw_key[0] if c.gw_key else '-'}")
        if stop["now"]:
            summary("stopped by signal; final summary")
            return
        if done and not sum(n.values()):
            summary("recorder DONE file present and all lines read; final summary")
            return
        stalled = now - last_growth
        if stalled > 600 and (r or markers) and not warned:
            warned = True
            log(f"WARNING no new telemetry for {stalled:.0f} s" + (" while a print is running; is the recorder alive?" if r else ""))
            summary("summary written")
        if stalled > (900 if markers else 3600):
            summary(f"no new telemetry for {stalled:.0f} s, giving up; final summary")
            return
        for _ in range(max(int(poll), 1)):
            if stop["now"]:
                break
            time.sleep(1)


def main():
    global IDLE_S
    p = argparse.ArgumentParser(description="Print summary and print-end watcher for a record.py capture (offline).")
    p.add_argument("capture", help="capture folder, e.g. data/captures/NAME")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--summary", action="store_true", help="read the whole capture once and write summary.json + SUMMARY.md")
    g.add_argument("--watch", action="store_true", help="poll the growing capture, write PRINT_END when the print ends, refresh the summary")
    p.add_argument("--poll", type=float, default=30, help="seconds between polls (watch)")
    p.add_argument("--refresh", type=float, default=600, help="seconds between partial summaries (watch)")
    p.add_argument("--idle", type=float, default=IDLE_S, help="seconds without extrusion and motion that end a print")
    args = p.parse_args()
    IDLE_S = args.idle
    if not os.path.exists(os.path.join(args.capture, "telemetry.jsonl")):
        raise SystemExit(f"no telemetry.jsonl in {args.capture}")
    if args.summary:
        run_summary(args.capture)
    else:
        run_watch(args.capture, args.poll, args.refresh)


if __name__ == "__main__":
    main()
