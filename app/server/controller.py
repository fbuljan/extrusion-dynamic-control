"""Width controller: holds, effect gating, lever choice by soft cost through the printer model, pinning.

Pure logic: no I/O, no clock reads, not thread-safe (make every call from the control loop's thread). Per tick:
    est = estimator.estimate(now, since_ms=controller.estimate_since())
    decision = controller.step(now, est, target, setpoints, measured, context, gate_open, auto)
and only in Auto send decision.action, then report note_sent / note_result; foreign writer results and manual sends
go to note_external. Readbacks are the truth about what is set, gateway results only hint.

Works in increments: it asks the printer model which setpoint changes the width by kappa * (target - estimate)
from the current setpoints, so a constant model bias cancels. At most one command in flight: after a command nothing
else is sent until beads deposited after its effect delay have been measured (`settle_samples` of them). A rolling
median wanders, so the error must also stay outside the band for `persist_ticks` ticks before anything is proposed.
The camera is blind above its knee, and a drifted IR baseline can put that blind plateau anywhere below it: no
target above the ceiling, no widening at it, and every widening step goes from a settled width and must show its effect
(each view against itself) before the next one, or widening latches until Auto is re-armed.
"""
import math
import statistics
from collections import deque
from dataclasses import asdict, dataclass, field

from . import control_config
from .estimator import DEFAULTS as ESTIMATOR, MEDIAN_SE

NAMES = {"flow_percent": ("flow", "%"), "speed_override_percent": ("speed", "%"),
         "nozzle_temperature_c": ("nozzle", "°C")}
MODEL_KEYS = {"flow_percent": "flow", "speed_override_percent": "speed"}
FAILED_STATES = ("rejected", "dispatch_unknown", "queued_unconfirmed")
NOT_APPLIED = "confirmed by the gateway but not applied by the printer"
RE_ARM = "; switch Auto off and on to re-arm"
RE_ARM_ADVISE = "; switch Auto on and off again to re-arm (Auto on sends commands if sending is enabled)"
NO_EFFECT = ("alarm: widening had no visible effect (camera saturated or lever ineffective): check the bead; lower the "
             "target")
SATURATED = NO_EFFECT + ", or" + RE_ARM[1:]
SATURATED_ADVISE = NO_EFFECT + ", or" + RE_ARM_ADVISE[1:]
LEVEL_S = 30  # a widening step's reference: the estimates over this long a settled stretch before it
HEATER_SETTLED_C = 1.0  # the nozzle still this far from its target keeps changing the bead
SETTLED_MM = 0.015  # the last third of LEVEL_S this close to the rest (or within a sigma) is settled
WINDOW_S = ESTIMATOR["window_s"]  # estimates this far apart share no samples
ALL = "*"  # the estimate over all views, next to the views' own
RESPONSE_AFTER_S, RESPONSE_MAX_S = 30, 180  # a widening step's rise is judged on beads laid this long after its effect
RESPONSE_Z = 1.5  # ... and before the max only once it is this many sigmas clear of the threshold either way
RESPONSE_ROWS = 20  # ... and on at least this many estimates


def _name(op):
    return NAMES.get(op, (op, ""))[0]


def _fmt(op, value):
    name, unit = NAMES.get(op, (op, ""))
    return f"{name} {value:g} {unit}".rstrip()


def _finite(value):
    return isinstance(value, (int, float)) and math.isfinite(value)


class ModelFailure(Exception):
    pass


@dataclass
class Decision:
    t_ms: int
    mode: str  # "advise" | "auto"
    action: dict | None  # {"operation", "value"}
    reason: str  # "alarm: ..." when an operator should look
    error_mm: float | None  # target - estimate
    predicted_mm: float | None  # estimate after the action settles, by the model
    candidates: list = field(default_factory=list)
    hold: bool = True

    def to_dict(self):
        return asdict(self)


class Controller:
    def __init__(self, printer_model, config=None):
        self.model = printer_model
        self.cfg = control_config.load(config)
        self.levers = (self.cfg["levers"] if self.cfg["mode"] == "model"
                       else {"flow_percent": self.cfg["levers"]["flow_percent"]})
        self._rb = {}  # op -> (value, seen_ms): last finite readback and when it was last seen
        self._uncertain = {}  # op -> [(value, sent_ms, until_ms, predicted, before)] sent values that may still land
        self._own = {}  # op -> our value the readback last confirmed
        self._advice = []  # [(op, value, t_ms, predicted, before)] recent advice-mode proposals
        self._cmd = None  # the one command in flight
        self._proposal = None
        self._pins = {}
        self._foreign_until, self._foreign_reason = -math.inf, ""
        self._since = None
        self._auto = False
        self._alarm = None
        self._failures = 0
        self._disabled = {}
        self._refused = set()  # (op, value) of failed commands: LEAM forbids automatic retries
        self._saturated = False  # latched: a widening step showed too little response
        self._widen = None  # the last widening step taken from a settled width, until its response is judged
        self._recent = deque()  # (t_ms, first sample's ms, {view or ALL: (mm, sigma_mm)}) of recent usable estimates
        self._heating_ms = -math.inf  # last time the nozzle was off its target
        self._target = None
        self._streak = None  # the current run of same-direction steps on one lever, for the ineffective check
        self._last_action_ms = None
        self._unreachable_steps = 0
        self._outside = 0  # consecutive ticks the error was outside the band
        self._feed, self._feed_ms = 1.0, None  # anchor: estimate / model width, smoothed

    # caller notes ---------------------------------------------------------------------------------------------------
    def estimate_since(self):
        """Deposit-time cutoff (local clock) the estimate passed to step() must respect (None: no cutoff)."""
        return self._since

    def limits(self):
        return control_config.limits(self.cfg)

    def note_sent(self, operation, value, request_id, now_ms):
        p = self._proposal if self._proposal and self._proposal["action"] == {"operation": operation, "value": value} \
            else {}
        current = self._rb.get(operation)
        self._cmd = {"op": operation, "value": value, "id": request_id, "sent_ms": now_ms, "state": "sent",
                     "from": current[0] if current else None, "result": None, "result_ms": None,
                     "predicted": p.get("predicted"), "before": p.get("before"), "cutoff": None, "why": None}
        self._uncertain.setdefault(operation, []).append(
            (value, now_ms, now_ms + self.cfg["uncertain_memory_s"] * 1000, p.get("predicted"), p.get("before")))
        self._last_action_ms = now_ms
        self._outside = 0  # the next command needs its own run of ticks off target
        self._widen = None
        if p.get("unreachable"):
            self._unreachable_steps += 1

    def note_unsent(self, operation, value, now_ms):
        """An Auto proposal that wasn't published (sends disabled, refused): if someone applies it, it is ours."""
        p = self._proposal if self._proposal and self._proposal["action"] == {"operation": operation, "value": value} \
            else {}
        self._advice.append((operation, value, now_ms, p.get("predicted"), p.get("before")))

    def note_result(self, request_id, state, now_ms):
        c = self._cmd
        if c is None or c["id"] != request_id or c["state"] != "sent":
            return
        if state == "setting_confirmed":
            c["result"], c["result_ms"] = state, now_ms
        elif state in FAILED_STATES:
            if state == "rejected":
                self._forget(c["op"], c["value"])
            self._fail(c, state.replace("_", " "), now_ms)

    def note_external(self, lever, value, now_ms):
        """Someone else's accepted command. A result that names no operation or value pins nothing but still holds."""
        if lever in (None, "unknown") or not _finite(value):
            self._foreign_until = max(self._foreign_until, now_ms + self.cfg["foreign_hold_s"] * 1000)
            self._foreign_reason = "alarm: another sender's command was accepted"
        else:
            self._external(lever, value, now_ms, "another sender set")

    def pinned(self, now_ms):
        self._pins = {op: p for op, p in self._pins.items() if p["until_ms"] > now_ms}
        return {op: dict(p) for op, p in self._pins.items()}

    # bookkeeping ----------------------------------------------------------------------------------------------------
    def _delay_ms(self, op):
        return self.cfg["levers"].get(op, {}).get("effect_delay_s", 3) * 1000

    def _gate_after(self, t_ms, change=True):
        self._since = t_ms if self._since is None else max(self._since, t_ms)
        if change:  # estimates of beads laid before a change say nothing about the width after it
            self._recent.clear()

    def _forget(self, op, value):
        tol = self.cfg["levers"][op]["tolerance"]
        self._uncertain[op] = [e for e in self._uncertain.get(op, []) if abs(e[0] - value) > tol]

    def _is_ours(self, op, value, now_ms):
        tol = self.cfg["levers"][op]["tolerance"]
        own = [v for v, _, until, *_ in self._uncertain.get(op, []) if until > now_ms]
        if op in self._own:
            own.append(self._own[op])
        return any(abs(v - value) <= tol for v in own)

    def _applied_advice(self, op, value, now_ms):
        tol = self.cfg["levers"][op]["tolerance"]
        self._advice = [a for a in self._advice if now_ms - a[2] <= self.cfg["advice_memory_s"] * 1000]
        return next((a for a in reversed(self._advice) if a[0] == op and abs(a[1] - value) <= tol), None)

    def _external(self, op, value, now_ms, who):
        self._pins[op] = {"value": value, "until_ms": now_ms + self.cfg["pin_s"] * 1000}
        self._foreign_until = now_ms + self.cfg["foreign_hold_s"] * 1000
        self._foreign_reason = f"alarm: {who} {_fmt(op, value)}"
        self._gate_after(now_ms + self._delay_ms(op))
        self._streak = self._widen = None
        self._own.pop(op, None)  # an operator may set a value we used before: that is theirs now
        self._uncertain.pop(op, None)
        if self._cmd and self._cmd["op"] == op:
            self._cmd = None

    def _fail(self, c, why, now_ms):
        c["state"], c["why"], c["failed_ms"] = "failed", why, now_ms
        self._refused.add((c["op"], c["value"]))
        self._failures += 1
        if self._failures >= self.cfg["max_failures"]:
            self._alarm = (f"alarm: {self._failures} commands in a row failed (last: {_fmt(c['op'], c['value'])} {why})"
                           f"{RE_ARM}")

    def _confirm(self, c, now_ms):
        late = c["state"] == "failed"
        c["state"] = "confirmed"
        # A command that landed after it was given up on took effect now, not at the old result time.
        start = c["result_ms"] if c["result_ms"] is not None and not late else now_ms
        c["cutoff"] = start + self._delay_ms(c["op"])
        self._gate_after(c["cutoff"])
        self._own[c["op"]] = c["value"]
        self._uncertain[c["op"]] = [e for e in self._uncertain.get(c["op"], []) if e[1] > c["sent_ms"]]
        if not late:
            self._failures = 0

    def _synthetic(self, op, value, now_ms, predicted=None, before=None):
        """A change of ours seen only in the readback (late landing, advice applied by hand): gate its effect."""
        self._widen = None
        self._cmd = {"op": op, "value": value, "id": None, "sent_ms": now_ms, "state": "sent", "result": None,
                     "result_ms": None, "predicted": predicted, "before": before, "cutoff": None, "why": None}
        self._confirm(self._cmd, now_ms)

    def _readbacks(self, now_ms, setpoints):
        for op, spec in self.cfg["levers"].items():
            r = setpoints.get(op)
            if not _finite(r):
                continue
            prev = self._rb.get(op)
            self._rb[op] = (r, now_ms)
            c, tol = self._cmd, spec["tolerance"]
            if c and c["op"] == op and c["state"] in ("sent", "failed") and abs(r - c["value"]) <= tol:
                self._confirm(c, now_ms)
            elif prev is not None and abs(r - prev[0]) > tol:
                advice = self._applied_advice(op, r, now_ms)
                if advice and self._cmd is None:
                    self._synthetic(op, r, now_ms, advice[3], advice[4])
                elif not (advice or self._is_ours(op, r, now_ms)):
                    self._external(op, r, now_ms, "readback shows someone else set")
                elif self._cmd is None:  # ours landing late: judged like the command it was
                    late = [e for e in self._uncertain.get(op, []) if e[2] > now_ms and abs(e[0] - r) <= tol]
                    self._synthetic(op, r, now_ms, *(late[-1][3:] if late else ()))
                else:
                    self._gate_after(now_ms + self._delay_ms(op))
        c = self._cmd
        if c is None:
            return
        if c["state"] == "sent":
            if c["result"] is None and now_ms - c["sent_ms"] >= self.cfg["result_timeout_ms"]:
                self._fail(c, "unanswered", now_ms)
            elif c["result"] and now_ms - c["result_ms"] >= self.cfg["readback_timeout_ms"]:
                self._fail(c, NOT_APPLIED, now_ms)
                # The printer ignores what the gateway confirms: no further command can be trusted until re-armed.
                self._disabled[c["op"]] = f"alarm: {_fmt(c['op'], c['value'])} {NOT_APPLIED}"
                self._alarm = self._disabled[c["op"]] + RE_ARM
        elif c["state"] == "failed":
            rb = self._rb.get(c["op"])
            if rb and rb[1] >= c["failed_ms"] + self.cfg["explain_wait_ms"]:
                self._cmd = None  # a readback taken well after the failure still shows the old value: not applied

    def _settle(self, now_ms, estimate):
        c = self._cmd
        if c is None or c["state"] != "confirmed" or estimate is None or now_ms < c["cutoff"]:
            return
        if (estimate.get("since_ms") or -math.inf) < c["cutoff"] or estimate["n"] < self.cfg["settle_samples"] \
                or self._needs_views(estimate):
            return
        self._cmd = None
        if not (c["predicted"] and c["before"]):
            self._streak = None
            return
        mm, _, at_ceiling, ceiling, level = c["before"]
        cfg = self.cfg
        if c["predicted"] > 0 and level:
            self._widen = {"before": level, "predicted": c["predicted"], "cutoff": c["cutoff"],
                           "what": _fmt(c["op"], c["value"])}
        if at_ceiling:  # from the ceiling the first steps show nothing
            self._streak = None
            return
        # Judged over the whole run of same-direction steps on this lever, from the estimate before the first one:
        # the noise of a difference of two estimates does not grow with the number of steps, the predicted sum does.
        # A later step's estimate replaces it when further back: a step taken while the rolling estimate still lagged
        # a disturbance would otherwise make the lever look ineffective.
        direction, s = (1 if c["predicted"] > 0 else -1), self._streak
        if s and s["op"] == c["op"] and s["dir"] == direction and \
                c["sent_ms"] - s["sent_ms"] <= cfg["streak_gap_s"] * 1000:
            s["predicted"] += c["predicted"]
            s["steps"] += 1
            s["sent_ms"] = c["sent_ms"]
            if (c["before"][0] - s["before"][0]) * direction < 0:
                s["before"] = c["before"]
        else:
            s = self._streak = {"op": c["op"], "dir": direction, "predicted": c["predicted"], "steps": 1,
                                "before": c["before"], "sent_ms": c["sent_ms"]}
        observed = (estimate["mm"] - s["before"][0]) * direction
        sigma = math.hypot(s["before"][1], estimate["sigma_mm"])
        if s["steps"] >= cfg["ineffective_steps"] and \
                observed + cfg["ineffective_z"] * sigma < cfg["ineffective_ratio"] * abs(s["predicted"]):
            self._disabled[c["op"]] = (f"alarm: {_name(c['op'])} moved the width less than "
                                       f"{cfg['ineffective_ratio']:.0%} of the predicted change")

    def _needs_views(self, estimate):
        """The print shows several views (each with its own camera offset) but this estimate holds only one."""
        return self.cfg["group_rule"] != "none" and estimate.get("groups_active", 1) >= 2 and \
            len(estimate.get("per_group", {})) < 2

    def _arm(self, now_ms):
        self._alarm, self._failures, self._disabled, self._streak = None, 0, {}, None
        self._refused.clear()
        self._saturated = False  # a pending judgement stays: the step it judges was taken all the same
        self._unreachable_steps = 0
        self._gate_after(now_ms, change=False)

    # decision -------------------------------------------------------------------------------------------------------
    def step(self, now_ms, estimate, target_mm, setpoints, measured, context, gate_open, auto):
        cfg = self.cfg
        mode = "auto" if auto else "advise"
        # A gap in the data (no, stale or weak estimate) pauses the persistence count; anything else restarts it.
        outside, self._outside = self._outside, 0
        if auto and not self._auto:
            self._arm(now_ms)
        self._auto = auto
        setpoints, measured, context = setpoints or {}, measured or {}, context or {}
        self._readbacks(now_ms, setpoints)
        self._proposal = None
        bad = self._non_finite(estimate, target_mm, setpoints, measured)
        if bad:
            return Decision(now_ms, mode, None, f"non-finite {bad}", None, None, [], True)
        if target_mm is not None and target_mm != self._target:
            self._refused.clear()
            self._target = target_mm
        self._settle(now_ms, estimate)
        error = target_mm - estimate["mm"] if target_mm is not None and estimate is not None else None

        def hold(reason, candidates=(), predicted=None):
            return Decision(now_ms, mode, None, reason, error, predicted, list(candidates), True)

        if target_mm is None:
            return hold("no target")
        if cfg["mode"] == "model" and self.model is None:
            return hold("no printer model")
        if auto and self._alarm:
            return hold(self._alarm)
        current = {op: rb[0] for op, rb in self._rb.items()
                   if op in self.levers and now_ms - rb[1] <= cfg["readback_max_age_ms"]}
        missing = [_name(op) for op in self.levers if op not in current]
        if missing:
            return hold(f"no readback for {', '.join(missing)}")
        if measured.get("extruding") is not True:
            return hold("not extruding")
        layer = context.get("layer")
        if layer is None or layer < cfg["min_layer"]:
            return hold(f"first layers ({layer if layer is not None else '?'} of {cfg['min_layer']})")
        gap = None
        if estimate is None:
            gap = "no width estimate"
        elif estimate["age_ms"] > cfg["max_estimate_age_ms"]:
            gap = "width estimate stale"
        elif estimate["confidence"] < cfg["min_confidence"]:
            gap = f"low confidence ({estimate['confidence']:.2f})"
        if gap:
            self._outside = outside
            return hold(gap)
        nozzle, nozzle_target = measured.get("nozzle_c"), setpoints.get("nozzle_temperature_c")
        if _finite(nozzle) and _finite(nozzle_target) and abs(nozzle - nozzle_target) > HEATER_SETTLED_C:
            self._heating_ms = now_ms
        # An estimate made before the latest change (this tick's readback may show it) is of beads laid before it.
        first = estimate.get("since_ms") or -math.inf
        if self._since is None or first >= self._since:
            offsets = estimate.get("view_offsets_mm") or {}
            views = {str(g): (v["mm"] + offsets.get(str(g), 0.0), v["sigma_mm"])
                     for g, v in (estimate.get("per_group") or {}).items()
                     if _finite(v.get("mm")) and _finite(v.get("sigma_mm")) and v["sigma_mm"] > 0}
            views[ALL] = (estimate["mm"], estimate["sigma_mm"])
            self._recent.append((now_ms, max(now_ms - WINDOW_S * 1000, first), views))
        while self._recent and self._recent[0][0] < now_ms - max(LEVEL_S, RESPONSE_MAX_S) * 1000:
            self._recent.popleft()
        self._judge_widening(now_ms)
        ceiling = estimate.get("ceiling_mm")
        if ceiling is not None and target_mm > ceiling:
            return hold(f"alarm: target {target_mm:.2f} mm is above what the camera can see ({ceiling:.2f} mm)")
        if now_ms < self._foreign_until:
            return hold(f"{self._foreign_reason}; holding {math.ceil((self._foreign_until - now_ms) / 1000)} s")
        if self._cmd:
            return hold(self._waiting(estimate))
        if self._since is not None and (estimate.get("since_ms") or -math.inf) < self._since:
            return hold("waiting for a fresh estimate")
        if self._needs_views(estimate):
            self._outside = outside
            return hold("waiting for a second view")
        try:
            self._anchor(now_ms, current, measured, context, estimate)
        except ModelFailure as e:
            return hold(f"alarm: printer model {e}")
        if self._last_action_ms is not None and now_ms - self._last_action_ms < cfg["min_spacing_ms"]:
            return hold("spacing")
        band = max(cfg["deadband_mm"], cfg["sigma_factor"] * estimate["sigma_mm"])
        if abs(error) <= band:
            self._unreachable_steps, self._streak = 0, None
            if estimate.get("at_ceiling"):
                seen = min(estimate["mm"], ceiling)  # the bead is at least this wide, maybe wider
                return hold(f"at the camera's ceiling (≥ {seen:.2f} mm): can't confirm the width")
            return hold(f"on target (|error| {abs(error):.3f} <= {band:.3f} mm)")
        if error > 0 and estimate.get("at_ceiling"):  # a wider bead would read the same: never widen blind
            return hold(f"at the camera's ceiling: can't see wider than {ceiling:.2f} mm")
        if error > 0 and self._saturated:
            return hold(SATURATED if auto else SATURATED_ADVISE)
        self._outside = outside + 1
        if self._outside < cfg["persist_ticks"]:
            return hold(f"off target {error:+.3f} mm, confirming ({self._outside}/{cfg['persist_ticks']})")
        if error > 0 and ceiling is not None:
            # With a camera that has a ceiling, widening goes from settled widths only and each step must lift the
            # width before the next: a blind plateau, at the knee or anywhere below it with a drifted IR, latches
            # after one step.
            if self._widen:
                return hold(self._judging(now_ms))
            if not self._settled(now_ms, estimate):
                return hold("waiting for the width to settle before widening")
        try:
            best, candidates, reach = (self._plan_model if cfg["mode"] == "model" else self._plan_fallback)(
                now_ms, current, measured, estimate, context, error)
        except ModelFailure as e:
            return hold(f"alarm: printer model {e}")
        direction = 1 if error > 0 else -1
        reachable = reach * direction >= abs(error) - band
        out_of_reach = f"target out of reach (needs {error:+.2f} mm, usable levers at their limits give {reach:+.2f})"
        if reachable:
            self._unreachable_steps = 0
        elif self._unreachable_steps >= 1:
            return hold(f"alarm: {out_of_reach}", candidates)
        if best is None or best["predicted_change_mm"] * direction < cfg["min_effect_fraction"] * cfg["kappa"] * abs(
                error):
            disabled = [r for op, r in self._disabled.items() if op in self.levers]
            why = "alarm: no usable lever can move the width"
            if disabled:
                why = f"{disabled[0]}; no usable lever can move the width{RE_ARM if auto else RE_ARM_ADVISE}"
            return hold(why, candidates)
        action = {"operation": best["operation"], "value": best["value"]}
        if not self._within_limits(action, current):
            return hold(f"alarm: internal limit check refused {action}", candidates)
        predicted = estimate["mm"] + best["predicted_change_mm"]
        what = f"{_fmt(best['operation'], best['from'])} -> {best['value']:g}"
        if not reachable:
            what = f"{out_of_reach}; one step: {what}"
        if auto and not gate_open:
            return hold(f"gate closed (would set {what})", candidates, predicted)
        before = (estimate["mm"], estimate["sigma_mm"], bool(estimate.get("at_ceiling")), ceiling,
                  self._level(now_ms - LEVEL_S * 1000) if error > 0 and ceiling is not None else None)
        self._proposal = {"action": action, "predicted": best["predicted_change_mm"], "before": before,
                          "unreachable": not reachable}
        if auto:
            self._last_action_ms = now_ms
        else:
            self._advice.append((action["operation"], action["value"], now_ms, best["predicted_change_mm"], before))
        reason = what if auto or gate_open else f"{what} (gate closed)"
        return Decision(now_ms, mode, action, reason, error, predicted, candidates, False)

    def _settled(self, now_ms, estimate):
        """The heater has been on target and LEVEL_S of estimates since the last change show no trend: their last
        third agrees with the rest."""
        if now_ms - self._heating_ms < LEVEL_S * 1000 or not self._recent or \
                self._recent[0][0] > now_ms - LEVEL_S * 1000:
            return False
        rows = [(t, views[ALL][0]) for t, _, views in self._recent if t >= now_ms - LEVEL_S * 1000]
        newer = [mm for t, mm in rows if t > now_ms - LEVEL_S * 1000 / 3]
        older = [mm for t, mm in rows if t <= now_ms - LEVEL_S * 1000 / 3]
        return bool(newer and older) and \
            abs(statistics.median(newer) - statistics.median(older)) <= max(estimate["sigma_mm"], SETTLED_MM)

    def _level(self, from_ms):
        """Per view, without the estimator's offsets, and over ALL views as estimated: the median of the estimates
        since from_ms (the start of a disturbance still in their windows moves it less than a mean) and its sigma.
        Estimates overlap, so only the time a view's samples span counts."""
        rows = [r for r in self._recent if r[0] >= from_ms]
        level = {}
        for view in {v for r in rows for v in r[2]}:
            mine = [(t, first, r[view]) for t, first, r in rows if view in r and r[view][1] > 0]
            span, end = 0.0, -math.inf
            for t, first, _ in mine:
                span += max(0.0, t - max(first, end))
                end = max(end, t)
            if mine:
                shrink = MEDIAN_SE * math.sqrt(WINDOW_S * 1000 / max(span, WINDOW_S * 1000))
                level[view] = (statistics.median(mm for _, _, (mm, _) in mine),
                               shrink * math.sqrt(len(mine) / sum(1 / sigma ** 2 for _, _, (_, sigma) in mine)))
        return level or None

    def _judge_start(self, w):
        """Beads laid from here on show the step: after its effect delay, with the heater settled."""
        return max(w["cutoff"], self._heating_ms + LEVEL_S * 1000)

    def _judging(self, now_ms):
        w = self._widen
        if now_ms - self._heating_ms < LEVEL_S * 1000:
            return f"waiting for the heater to settle before judging {w['what']}"
        if now_ms >= self._judge_start(w) + RESPONSE_MAX_S * 1000:  # past the deadline only without the estimates
            return f"waiting for enough width estimates to judge {w['what']}"
        return f"waiting for {w['what']} to show its effect before widening again"

    def _judge_widening(self, now_ms):
        """Latch the alarm when the last widening step lifted the width less than widen_response_ratio of its
        prediction. Judged once the rise since its effect is RESPONSE_Z sigmas clear of that either way, or on what
        there is at RESPONSE_MAX_S; on RESPONSE_ROWS estimates at least. Each view is compared with itself: the views'
        offsets, and the estimator's slow corrections of them, would otherwise move the width by about as much as a
        small step."""
        w = self._widen
        if w is None:
            return
        start = self._judge_start(w)
        after, before = self._level(start), w["before"]
        if now_ms < start + RESPONSE_AFTER_S * 1000 or after is None or \
                sum(1 for r in self._recent if r[0] >= start) < RESPONSE_ROWS:
            return
        views = (after.keys() & before.keys()) - {ALL} or (after.keys() & before.keys())
        weights = {v: 1 / (after[v][1] ** 2 + before[v][1] ** 2) for v in views}
        rise, sigma = -math.inf, math.inf  # nothing to compare is no sign of an effect
        if weights:
            rise = sum(weights[v] * (after[v][0] - before[v][0]) for v in views) / sum(weights.values())
            sigma = 1 / math.sqrt(sum(weights.values()))
        threshold = self.cfg["widen_response_ratio"] * w["predicted"]
        if now_ms < start + RESPONSE_MAX_S * 1000 and abs(rise - threshold) < RESPONSE_Z * sigma:
            return
        self._widen = None
        if rise < threshold:
            self._saturated = True

    def _non_finite(self, estimate, target_mm, setpoints, measured):
        """What is NaN or infinite among the inputs, or None."""
        if target_mm is not None and not _finite(target_mm):
            return "target"
        if estimate is not None:
            for key in ("mm", "sigma_mm", "confidence", "age_ms", "n"):
                if not _finite(estimate.get(key)):
                    return f"width estimate {key}"
            if estimate.get("ceiling_mm") is not None and not _finite(estimate["ceiling_mm"]):
                return "width estimate ceiling_mm"
        for op in self.cfg["levers"]:
            if setpoints.get(op) is not None and not _finite(setpoints[op]):
                return f"readback for {_name(op)}"
        for key, value in measured.items():
            if isinstance(value, (int, float)) and not _finite(value):
                return f"measured {key}"
        return None

    def _waiting(self, estimate):
        c = self._cmd
        what = _fmt(c["op"], c["value"])
        if c["state"] == "failed":
            prefix = "alarm: " if c["why"] == NOT_APPLIED else ""
            return f"{prefix}{what} {c['why']}; waiting for the readback"
        if c["state"] == "sent":
            return f"waiting for {what} to be confirmed"
        n = estimate["n"] if (estimate.get("since_ms") or -math.inf) >= c["cutoff"] else 0
        views = ", waiting for a second view" if n >= self.cfg["settle_samples"] and self._needs_views(estimate) else ""
        return f"waiting for {what} to take effect ({n}/{self.cfg['settle_samples']} samples{views})"

    def _excluded(self, op, now_ms):
        if op in self._disabled:
            return self._disabled[op]
        if op in self._pins and self._pins[op]["until_ms"] > now_ms:
            return "pinned"
        return None

    def _window(self, op, value):
        spec = self.levers[op]
        lo, hi = spec["bounds"]
        return max(lo, value - spec["max_step"]), min(hi, value + spec["max_step"])

    def _within_limits(self, action, current):
        """Last line of defence: an operation, value or step outside the configured limits is never returned."""
        op, value = action["operation"], action["value"]
        if op not in control_config.LEVERS or op not in self.levers or not _finite(value) or value != round(value):
            return False
        lo, hi = self._window(op, current[op])
        return lo - 1e-9 <= value <= hi + 1e-9

    def _width(self, settings, context):
        try:
            w = self.model.steady_width(settings, context)
        except Exception as e:  # a fitted model must never take the loop down; the operator sees why it holds
            raise ModelFailure(f"failed ({type(e).__name__}: {e})") from e
        if not _finite(w):
            raise ModelFailure(f"returned a non-finite width ({w})")
        return w

    def _settings(self, current, measured, op=None, value=None, flow_scale=1.0):
        nozzle = measured.get("nozzle_c")
        s = {"flow": current["flow_percent"] * flow_scale, "speed": current["speed_override_percent"],
             "nozzle_c": nozzle if _finite(nozzle) else current["nozzle_temperature_c"],
             "pa": current.get("pressure_advance_seconds", 0.0)}
        if op == "flow_percent":
            s["flow"] = value * flow_scale
        elif op in MODEL_KEYS:
            s[MODEL_KEYS[op]] = value
        elif op == "nozzle_temperature_c":  # the actual nozzle follows the target by the same step
            s["nozzle_c"] += value - current[op]
        return s

    def _anchor(self, now_ms, current, measured, context, estimate):
        """Smoothed ratio of measured to modelled width, when anchoring is on (a clog moves the operating point)."""
        cfg = self.cfg
        if not cfg["anchor_operating_point"] or cfg["mode"] != "model":
            return
        lo, hi = cfg["anchor_range"]
        ratio = min(hi, max(lo, estimate["mm"] / self._width(self._settings(current, measured), context)))
        dt = 0.0 if self._feed_ms is None else (now_ms - self._feed_ms) / 1000
        self._feed += (ratio - self._feed) * (1 - math.exp(-dt / cfg["anchor_tau_s"])) if self._feed_ms else 0.0
        self._feed_ms = now_ms

    def _flow_scale(self, current, measured, context, w0):
        """The flow multiplier at which the model reproduces the anchored width (1.0 when anchoring is off)."""
        if not self.cfg["anchor_operating_point"] or abs(self._feed - 1.0) < 1e-6:
            return 1.0
        want, (lo, hi) = self._feed * w0, (r ** 2 for r in self.cfg["anchor_range"])
        for _ in range(30):
            mid = (lo + hi) / 2
            lo, hi = (mid, hi) if self._width(self._settings(current, measured, flow_scale=mid), context) < want \
                else (lo, mid)
        return (lo + hi) / 2

    def _plan_model(self, now_ms, current, measured, estimate, context, error):
        cfg = self.cfg
        goal = cfg["kappa"] * error
        scale = self._flow_scale(current, measured, context, self._width(self._settings(current, measured), context))

        def width(op=None, v=None):
            return self._width(self._settings(current, measured, op, v, scale), context)

        w0 = width()
        candidates, best, combo = [], None, {}
        for op, spec in self.levers.items():
            why = self._excluded(op, now_ms)
            c = current[op]
            lo, hi = self._window(op, c)
            values = [v for v in range(math.ceil(lo - 1e-9), math.floor(hi + 1e-9) + 1)
                      if abs(v - c) >= 0.5 and (op, v) not in self._refused]
            if why or not values:
                continue
            top = None
            for v in values:
                change = width(op, v) - w0
                residual = abs(goal - change) / abs(goal)
                score = spec["cost"] + cfg["residual_weight"] * residual + cfg["step_penalty"] * abs(v - c) / spec[
                    "max_step"]
                if top is None or score < top["score"]:
                    top = {"operation": op, "value": v, "from": c, "predicted_change_mm": change,
                           "residual": residual, "score": score}
            candidates.append(top)
            if best is None or top["score"] < best["score"]:
                best = top
            change, v = max(((width(op, v) - w0, v) for v in spec["bounds"]), key=lambda e: e[0] * error)
            if change * error > 0:
                combo[op] = v
        # Reach: every usable lever at its helpful bound at once; walking levers to bounds for less is pointless.
        settings = self._settings(current, measured, flow_scale=scale)
        for op, v in combo.items():
            if op == "flow_percent":
                settings["flow"] = v * scale
            elif op in MODEL_KEYS:
                settings[MODEL_KEYS[op]] = v
            else:
                settings["nozzle_c"] += v - current[op]
        return best, candidates, self._width(settings, context) - w0

    def _plan_fallback(self, now_ms, current, measured, estimate, context, error):
        """Flow only: width proportional to flow below the melt limit, so flow scales by the asked width ratio."""
        op, cfg = "flow_percent", self.cfg
        c, mm = current[op], max(estimate["mm"], cfg["fallback_gain_floor_mm"])
        why = self._excluded(op, now_ms)
        lo, hi = self._window(op, c)
        b_lo, b_hi = self.levers[op]["bounds"]
        reach = mm * ((b_hi if error > 0 else b_lo) / c - 1) if not why else 0.0
        step = 1 if error > 0 else -1
        value = min(hi, max(lo, round(c * (1 + cfg["kappa"] * error / mm))))
        if abs(value - c) < 0.5:
            value = min(hi, max(lo, round(c) + step))
        while (op, value) in self._refused and abs(value - step - c) >= 0.5:
            value -= step  # a smaller step rather than a retry
        if why or abs(value - c) < 0.5 or (op, value) in self._refused:
            return None, [], reach
        change = mm * (value - c) / c
        best = {"operation": op, "value": value, "from": c, "predicted_change_mm": change,
                "residual": abs(cfg["kappa"] * error - change) / abs(cfg["kappa"] * error), "score": 1.0}
        return best, [best], reach
