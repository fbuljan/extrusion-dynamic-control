"""Rolling robust estimate of the REAL bead width from IR width samples, through the camera model.

Clocks: everything is on the local clock. The window and staleness use the sample's receive time (`recv_ms`); `since_ms`
gates on deposit time (`t_dep_ms`, which the caller converts to the local clock; without it the receive time stands in),
so after a change only beads laid down after it are judged. The device clock (`t_ms`) is never compared with ours.
Views (groups): each camera/side has its own offset the camera model doesn't know exactly; their relative offsets are
learned slowly (zero mean, so the absolute level stays the camera model's) and removed before combining. A follower view
(anchored, not fitted) learns its offset against the fitted views and is left out of the mean; until it has been seen
next to one, an estimate made of followers alone is only half as confident.
Per view: a median-guarded trimmed mean whose sigma is the single-sample noise scale learned over the last
`noise_samples` samples (a MAD of the handful in one window is too noisy to gate on) over sqrt(n); then inverse-variance
across views. The camera is blind above its knee: at_ceiling says the estimate is within knee_margin_mm of the
camera model's ceiling, where a wider bead would read the same. sigma_mm is the random part (plus the views'
disagreement while their offsets are still being learned): the camera model's sigma is a calibration error shared by all samples of
a view, which doesn't shrink with n or make the trend noisy, so it is reported apart as model_sigma_mm. Readings beyond
the model's valid range lower the confidence instead. add() and estimate() may run on different threads.
"""
import math
import statistics
import threading
from collections import deque

DEFAULTS = {"window_s": 25.0, "min_samples": 5, "max_age_ms": 5000, "full_n": 10, "min_spread_mm": 0.02,
            "noise_samples": 120, "group_memory_s": 60.0, "harmonize_s": 60.0, "trim_sigmas": 2.5,
            "knee_margin_mm": 0.05, "view_spread_cap": 1.0, "sigma_bucket_s": 1.0}
MAD_TO_SIGMA = 1.4826
MEDIAN_SE = 1.2533  # a median's standard error relative to the mean's, for normal noise
HISTORY_MS = 60_000  # samples kept for comparing views


def _field(sample, name):
    return sample.get(name) if isinstance(sample, dict) else getattr(sample, name, None)


def _finite(*values):
    return all(isinstance(v, (int, float)) and math.isfinite(v) for v in values)


class WidthEstimator:
    def __init__(self, camera_model, config=None):
        self.camera_model = camera_model
        self.cfg = {**DEFAULTS, **(config or {})}
        self._lock = threading.Lock()
        self._samples = deque(maxlen=4000)  # (recv_ms, t_dep_ms, group, mm, model_sigma, in_range, confidence)
        self._residuals = deque(maxlen=self.cfg["noise_samples"])  # |sample - median of its view's window|
        self._seen = {}  # group -> last recv_ms
        self._offsets, self._offsets_ms = {}, None  # group -> mm above the views' mean
        self._learning = {}  # group -> when its offset started being learned
        self.skipped = {}
        self.reason = "no samples"  # why the last estimate() returned None

    def add(self, sample, nozzle_c, speed_mm_s):
        """nozzle_c and speed_mm_s at the frame's time; samples that can't be converted are counted, not kept.
        Returns the sample in real mm less its view's learned offset (what the estimate averages), or None."""
        with self._lock:
            why = self._add(sample, nozzle_c, speed_mm_s)
            if why:
                self.skipped[why] = self.skipped.get(why, 0) + 1
                return None
            s = self._samples[-1]
            return s[3] - self._offsets.get(s[2], 0.0)

    def _add(self, sample, nozzle_c, speed_mm_s):
        px, confidence = _field(sample, "width_px"), _field(sample, "confidence")
        group = _field(sample, "group")
        group = _field(sample, "camera") if group in (None, "") else group
        if _field(sample, "reason") != "ok" or px is None:
            return "rejected"
        if not _finite(px, confidence):
            return "non-finite"
        if self.camera_model is None:
            return "no camera model"
        if not _finite(nozzle_c, speed_mm_s):
            return "no settings"
        converted = self.camera_model.real_mm(px, group, nozzle_c, speed_mm_s)
        if converted is None:
            return "group unsupported"
        mm, model_sigma = converted
        if not _finite(mm, model_sigma):
            return "non-finite"
        lo, hi = self.camera_model.valid_px_range(group)
        t_ms, recv, dep = _field(sample, "t_ms"), _field(sample, "recv_ms"), _field(sample, "t_dep_ms")
        recv = t_ms if recv is None else recv
        peers = [s[3] for s in self._samples if s[2] == group and s[0] >= recv - self.cfg["window_s"] * 1000]
        if len(peers) >= 3:
            self._residuals.append(abs(mm - statistics.median(peers)))
        self._samples.append((recv, recv if dep is None else dep, group, mm, model_sigma, lo <= px <= hi, confidence))
        self._seen[group] = max(recv, self._seen.get(group, recv))
        return None

    def _independent(self, members):
        """How many samples count towards a view's sigma: one per sigma_bucket_s at most (closer frames correlate)."""
        bucket = self.cfg["sigma_bucket_s"] * 1000
        return len({s[0] // bucket for s in members}) if bucket > 0 else sum(1 for _ in members)

    def noise_mm(self):
        """Single-sample noise scale (robust sigma), or None until enough samples."""
        with self._lock:
            return self._noise()

    def _noise(self):
        if len(self._residuals) < 10:
            return None
        return max(MAD_TO_SIGMA * statistics.median(self._residuals), self.cfg["min_spread_mm"])

    def _follower(self, group):
        fn = getattr(self.camera_model, "follower", None)
        return bool(fn(group)) if callable(fn) else False

    def _harmonize(self, now_ms):
        """Move each view's offset towards its last-minute median minus the fitted views' mean, by an EMA."""
        medians = {}
        for s in self._samples:
            if s[0] >= now_ms - HISTORY_MS:
                medians.setdefault(s[2], []).append(s[3])
        medians = {g: statistics.median(v) for g, v in medians.items() if len(v) >= 3}
        leaders = [m for g, m in medians.items() if not self._follower(g)]
        if len(medians) >= 2 and leaders:
            center = statistics.fmean(leaders)
            dt = 0.0 if self._offsets_ms is None else (now_ms - self._offsets_ms) / 1000
            a = 1 - math.exp(-max(dt, 0.0) / self.cfg["harmonize_s"])
            for g, m in medians.items():
                self._offsets[g] = self._offsets.get(g, 0.0) + (m - center - self._offsets.get(g, 0.0)) * a
                self._learning.setdefault(g, now_ms)
            mean = statistics.fmean(o for g, o in self._offsets.items() if not self._follower(g))
            self._offsets = {g: o - mean for g, o in self._offsets.items()}
        self._offsets_ms = now_ms

    def estimate(self, now_ms, since_ms=None):
        with self._lock:
            return self._estimate(now_ms, since_ms)

    def _estimate(self, now_ms, since_ms):
        cfg = self.cfg
        oldest = now_ms - cfg["window_s"] * 1000
        while self._samples and self._samples[0][0] < min(oldest, now_ms - HISTORY_MS):
            self._samples.popleft()
        harmonize = cfg["harmonize_s"] > 0
        if harmonize:
            self._harmonize(now_ms)
        rows = [s for s in self._samples if oldest <= s[0] <= now_ms and (since_ms is None or s[1] >= since_ms)]
        if not rows:
            self.reason = "no samples since the last change" if since_ms is not None and self._samples else "no samples"
            return None
        age = now_ms - max(s[0] for s in rows)
        if age > cfg["max_age_ms"]:
            self.reason = "stale"
            return None
        if len(rows) < cfg["min_samples"]:
            self.reason = f"{len(rows)}/{cfg['min_samples']} samples"
            return None
        groups = {}
        for s in rows:
            groups.setdefault(s[2], []).append(s)
        noise = self._noise()
        per = {}
        for group, members in groups.items():
            values = [s[3] - self._offsets.get(group, 0.0) for s in members]
            med = statistics.median(values)
            model = statistics.median(s[4] for s in members)
            if noise:
                kept = [(v, s) for v, s in zip(values, members) if abs(v - med) <= cfg["trim_sigmas"] * noise]
                loc = statistics.fmean(v for v, _ in kept) if kept else med
                se = noise / math.sqrt(self._independent(s for _, s in kept) if kept else 1)
            else:
                spread = max(MAD_TO_SIGMA * statistics.median(abs(v - med) for v in values), cfg["min_spread_mm"])
                loc, se = med, MEDIAN_SE * spread / math.sqrt(self._independent(members))
            per[str(group)] = {"mm": loc, "n": len(values), "sigma_mm": se, "model_sigma_mm": model}
        weights = {g: 1 / p["sigma_mm"] ** 2 for g, p in per.items()}
        total = sum(weights.values())
        mm = sum(w * per[g]["mm"] for g, w in weights.items()) / total
        sigma = 1 / math.sqrt(total)
        if len(per) > 1:
            # Views disagreeing beyond their noise widen sigma in full until their offsets are learned (at 2 frames/s
            # the random part alone no longer covers them), and by at most view_spread_cap after.
            learned = harmonize and all(now_ms - self._learning.get(g, now_ms) >= cfg["harmonize_s"] * 1000
                                        for g in groups)
            chi2 = sum(w * (per[g]["mm"] - mm) ** 2 for g, w in weights.items())
            sigma *= min(cfg["view_spread_cap"] if learned else math.inf, max(1.0, math.sqrt(chi2 / (len(per) - 1))))
        # A median-like estimate is in the camera's valid range when at least half its samples are.
        in_range = sum(s[5] for s in rows) / len(rows)
        confidence = statistics.fmean(s[6] for s in rows) * min(1.0, len(rows) / cfg["full_n"]) * min(1.0, 2 * in_range)
        if noise is None:  # sigma from a few samples' MAD is unreliable until the noise scale is learned
            confidence *= 0.5
        if all(self._follower(g) and g not in self._learning for g in groups):  # level never anchored on this print
            confidence *= 0.5
        active = sum(1 for t in self._seen.values() if t >= now_ms - cfg["group_memory_s"] * 1000)
        ceiling = getattr(self.camera_model, "ceiling_mm", None)
        ceiling = ceiling() if callable(ceiling) else None
        ceiling = ceiling if _finite(ceiling) else None
        self.reason = None
        return {"mm": mm, "sigma_mm": sigma, "n": len(rows), "age_ms": age, "confidence": confidence,
                "per_group": per, "groups_active": active, "since_ms": since_ms, "in_range": in_range,
                "model_sigma_mm": max(p["model_sigma_mm"] for p in per.values()),
                "view_offsets_mm": {str(g): o for g, o in self._offsets.items()},
                "ceiling_mm": ceiling, "at_ceiling": ceiling is not None and mm >= ceiling - cfg["knee_margin_mm"],
                "relative": bool(getattr(self.camera_model, "relative", False))}
