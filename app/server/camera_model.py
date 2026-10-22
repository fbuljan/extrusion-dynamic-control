"""Camera model runtime: IR bead width in px <-> bead width in mm, per camera-heading group.

Parameters from app/models/camera_model.json (tools/model/fit_camera.py), the groups of the file's active profile:
    px = k * min(mm, knee_mm) + g_T * (nozzle_c - 220) + s * (speed_mm_s - 20) + offset_px
Above the knee the IR stops reading wider (print 2: beads wider than the 1.5 mm wall pitch merge with the neighbour
wall): the camera is blind there (`ceiling_mm`). real_mm stays linear up to knee + 2 ramps (a frame SD each), so
a window of baseline frames scattered around the knee averages to the knee instead of below it, and clips beyond. Its
sigma ramps up to the above-knee value over knee +- 1 ramp, so a window's median sigma doesn't flip frame to frame.
Groups the file doesn't hold return None. A group marked "follower" was anchored to the fitted views of one print (its
offset moves from print to print): the estimator learns its offset against the others and never lets it set the level.
A "relative" file holds IR widths in geometric mm (px / k_nom), not real
widths: fit only for holding the current width. The file records the geometry and extractor settings its widths came
from (`geometry_version`, `width_config_hash`); widths made otherwise don't belong to this model.
"""
import json
import math
import numbers
from pathlib import Path

from .model_files import MODELS_DIR

CAMERA_MODEL_FILE = "camera_model.json"


def _num(x):
    return float(x) if isinstance(x, numbers.Real) and not isinstance(x, bool) and math.isfinite(x) else None


class CameraModel:
    def __init__(self, data: dict, profile: str | None = None):
        self.data = data
        self.version = str(data["version"])
        self.relative = bool(data.get("relative", False))
        self.choice = data.get("choice")
        self.geometry_version = data.get("geometry_version")
        self.width_config_hash = data.get("width_config_hash")
        self.profile = profile or data["profile"]
        ref = data.get("ref") or {}
        self.ref_c, self.ref_speed = float(ref.get("nozzle_c", 220.0)), float(ref.get("speed_mm_s", 20.0))
        self.g_T, self.s = float(data["g_T_px_per_c"]), float(data["s_px_per_mm_s"])
        self.above_knee_sigma = float(data["above_knee_sigma_mm"])
        self.groups = {str(g): self._group(e) for g, e in data["profiles"][self.profile]["groups"].items()}
        if not self.groups:
            raise ValueError(f"camera model profile {self.profile!r} has no groups")

    @staticmethod
    def _group(e):
        k, knee, (lo, hi) = float(e["k_px_per_mm"]), e.get("knee_mm"), e["valid_px"]
        if not k > 0:
            raise ValueError(f"camera model: k must be positive, got {k}")
        return {"k": k, "offset": float(e["offset_px"]), "knee": math.inf if knee is None else float(knee),
                "lo": float(lo), "hi": math.inf if hi is None else float(hi), "sigma": float(e["sigma_mm"]),
                "k_rel_sd": float(e.get("k_rel_sd") or 0.0), "ramp": float(e.get("ramp_mm") or 0.0),
                "follower": e.get("follower") is True}

    @classmethod
    def load(cls, path=None, profile=None):
        path = Path(path) if path else MODELS_DIR / CAMERA_MODEL_FILE
        if not path.exists():
            raise FileNotFoundError(f"camera model not found at {path} (local fit, see models/README.md)")
        return cls(json.loads(path.read_text()), profile)

    def follower(self, group):
        """A view anchored to the others rather than fitted: it may not set the absolute level."""
        gp = self.groups.get(str(group))
        return bool(gp and gp["follower"])

    def _terms(self, gp, nozzle_c, speed_mm_s):
        return self.g_T * (nozzle_c - self.ref_c) + self.s * (speed_mm_s - self.ref_speed) + gp["offset"]

    def real_mm(self, width_px, group, nozzle_c, speed_mm_s):
        """(mm, sigma_mm), or None for a group without a fit or a missing / non-finite input."""
        gp = self.groups.get(str(group))
        px, t, v = _num(width_px), _num(nozzle_c), _num(speed_mm_s)
        if gp is None or px is None or t is None or v is None:
            return None
        mm = (px - self._terms(gp, t, v)) / gp["k"]
        knee = gp["knee"]
        if not math.isfinite(knee):
            return mm, gp["sigma"]
        # offsets are pinned at the baseline, which sits on the knee: k's uncertainty grows with the distance from it
        sigma = math.hypot(gp["sigma"], (knee - min(mm, knee)) * gp["k_rel_sd"])
        if gp["ramp"] > 0:
            above = min(1.0, max(0.0, (mm - knee) / (2 * gp["ramp"]) + 0.5))
        else:
            above = 1.0 if mm > knee else 0.0
        return min(mm, knee + 2 * gp["ramp"]), math.hypot(sigma, above * self.above_knee_sigma)

    def ir_px(self, real_mm, group, nozzle_c, speed_mm_s):
        gp = self.groups.get(str(group))
        mm, t, v = _num(real_mm), _num(nozzle_c), _num(speed_mm_s)
        if gp is None or mm is None or t is None or v is None:
            return None
        return gp["k"] * min(mm, gp["knee"]) + self._terms(gp, t, v)

    def valid_px_range(self, group):
        """(lo, hi) px: below lo the blur floor (narrower beads read too wide); above hi wider than any baseline frame
        reads, i.e. not a normal bead (a gap beside it, extra glow). Empty for unfitted groups."""
        gp = self.groups.get(str(group))
        return (gp["lo"], gp["hi"]) if gp else (math.inf, -math.inf)

    def knee_mm(self, group):
        """Width above which this view can't see a bead get wider; None if unfitted or it has no knee."""
        gp = self.groups.get(str(group))
        return gp["knee"] if gp and math.isfinite(gp["knee"]) else None

    def ceiling_mm(self):
        """Lowest knee over the profile's views: no estimate shows a real width above it."""
        knees = [gp["knee"] for gp in self.groups.values() if math.isfinite(gp["knee"])]
        return min(knees) if knees else None


class MarginCameraModel:
    """A camera model whose widths all read `margin_mm` narrower (D55): the operator's numbers stay the nominal ones while
    the real bead is kept that much wider, a safety margin against under-reading. Everything else is the wrapped model's."""

    def __init__(self, model, margin_mm):
        self._model, self.margin_mm = model, float(margin_mm)

    def __getattr__(self, name):
        return getattr(self._model, name)

    def real_mm(self, width_px, group, nozzle_c, speed_mm_s):
        out = self._model.real_mm(width_px, group, nozzle_c, speed_mm_s)
        return None if out is None else (out[0] - self.margin_mm, out[1])

    def ir_px(self, real_mm, group, nozzle_c, speed_mm_s):
        return self._model.ir_px(None if real_mm is None else real_mm + self.margin_mm, group, nozzle_c, speed_mm_s)

    def knee_mm(self, group):
        knee = self._model.knee_mm(group)
        return None if knee is None else knee - self.margin_mm

    def ceiling_mm(self):
        ceiling = self._model.ceiling_mm()
        return None if ceiling is None else ceiling - self.margin_mm
