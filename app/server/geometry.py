"""IR pixel <-> print-plane mm for the four IR cameras (raw 160x120 previews). Loads models/geometry.json.

Pixels: x right, y down, integer = pixel centre; camera = camera_index 0-3 of the preview. The plane is the top of the
layer being printed, in camera-aligned mm: a along the camera's view azimuth (the way a trailing bead runs toward the
camera), c 90 deg CCW from a seen from above. Image angles are atan2(dy, dx) in y-down axes, 180 deg ambiguous.
Model: pinhole with one radial distortion term per camera; a bead's width in mm = w_px * |det A| / |A t|, with A the
local d(plane)/d(pixel) and t the bead direction in the image.
"""
import json
import os
from pathlib import Path

import numpy as np

MODELS_DIR = Path(os.environ.get("MODELS_DIR", Path(__file__).resolve().parent.parent / "models"))
GEOMETRY_FILE = "geometry.json"


class Geometry:
    def __init__(self, model):
        self.model = model
        self.cams = {int(k): v for k, v in model["cameras"].items()}
        for cam, cm in self.cams.items():
            if cm.get("type") != "pinhole_radial":
                raise ValueError(f"camera {cam}: unsupported geometry type {cm.get('type')!r}")
        self.image_size = tuple(model.get("image_size", (160, 120)))

    @classmethod
    def load(cls, path=None):
        path = Path(path) if path else MODELS_DIR / GEOMETRY_FILE
        if not path.exists():
            raise FileNotFoundError(f"IR geometry model not found at {path} (local calibration file, see models/README.md)")
        return cls(json.loads(path.read_text()))

    def _cam(self, cam):
        try:
            return self.cams[int(cam)]
        except KeyError:
            raise ValueError(f"no geometry for camera {cam}") from None

    def plane_to_pixel(self, cam, ac):
        """(N,2) plane mm (a, c) -> (N,2) raw pixels."""
        cm = self._cam(cam)
        ac = np.atleast_2d(np.asarray(ac, float))
        X = np.c_[ac, np.zeros(len(ac))]
        Xc = (X - np.asarray(cm["C_mm"], float)) @ np.asarray(cm["R_world_to_cam"], float).T
        u = Xc[:, :2] / Xc[:, 2:3]
        u = u * (1 + cm["k1"] * np.sum(u ** 2, 1, keepdims=True))
        return u * cm["f_px"] + np.asarray(cm["principal_point_px"], float)

    def pixel_to_plane(self, cam, xy):
        """(N,2) raw pixels -> (N,2) plane mm (a, c)."""
        cm = self._cam(cam)
        xy = np.atleast_2d(np.asarray(xy, float))
        d = (xy - np.asarray(cm["principal_point_px"], float)) / cm["f_px"]
        u = d.copy()
        for _ in range(20):  # invert d = u (1 + k1 |u|^2); beyond ~75 px from the centre the model folds over anyway
            u = d / (1 + cm["k1"] * np.sum(u ** 2, axis=-1, keepdims=True))
        ray = np.c_[u, np.ones(len(u))] @ np.asarray(cm["R_world_to_cam"], float)
        C = np.asarray(cm["C_mm"], float)
        t = -C[2] / ray[:, 2]
        return C[None, :2] + t[:, None] * ray[:, :2]

    def jacobian(self, cam, x, y, eps=0.05):
        """2x2 d(a, c)/d(x, y) at a pixel."""
        p = np.array([[x + eps, y], [x - eps, y], [x, y + eps], [x, y - eps]], float)
        w = self.pixel_to_plane(cam, p)
        return np.c_[(w[0] - w[1]) / (2 * eps), (w[2] - w[3]) / (2 * eps)]

    def mm_per_px_across(self, cam, x, y, angle_img_deg):
        """mm per image px measured perpendicular to a bead running at angle_img_deg through (x, y)."""
        A = self.jacobian(cam, x, y)
        t = np.array([np.cos(np.radians(angle_img_deg)), np.sin(np.radians(angle_img_deg))])
        return float(abs(np.linalg.det(A)) / np.linalg.norm(A @ t))

    def image_angle_of_plane_direction(self, cam, x, y, plane_angle_deg):
        """Image angle (0-180) of a bead running at plane_angle_deg (0 = along a) through pixel (x, y)."""
        w = self.pixel_to_plane(cam, [[x, y]])[0]
        t = np.array([np.cos(np.radians(plane_angle_deg)), np.sin(np.radians(plane_angle_deg))])
        q = self.plane_to_pixel(cam, np.vstack([w - 0.25 * t, w + 0.25 * t]))
        return float(np.degrees(np.arctan2(q[1, 1] - q[0, 1], q[1, 0] - q[0, 0])) % 180)

    def world_heading_to_plane_angle(self, cam, heading_deg):
        """Machine heading (deg CCW from +X) -> angle in this camera's plane frame (0 = along its view azimuth)."""
        return (heading_deg - self._cam(cam)["view_azimuth_deg"]) % 360

    def nozzle_px(self, cam):
        """Pixel where the fresh bead leaves the nozzle (the nozzle tip on the print plane)."""
        cm = self._cam(cam)
        if "nozzle_px" in cm:
            return tuple(float(v) for v in cm["nozzle_px"])
        return tuple(float(v) for v in self.plane_to_pixel(cam, [[0.0, 0.0]])[0])

    def hotspot_px(self, cam):
        """Centre of the nozzle's glow: distances behind the nozzle are counted from its foot on the bead axis."""
        return tuple(float(v) for v in self._cam(cam)["nozzle_hotspot_px"])


_default = None


def default():
    global _default
    if _default is None:
        _default = Geometry.load()
    return _default


def plane_to_pixel(cam, ac):
    return default().plane_to_pixel(cam, ac)


def pixel_to_plane(cam, xy):
    return default().pixel_to_plane(cam, xy)


def mm_per_px_across(cam, x, y, angle_img_deg):
    return default().mm_per_px_across(cam, x, y, angle_img_deg)


def image_angle_of_plane_direction(cam, x, y, plane_angle_deg):
    return default().image_angle_of_plane_direction(cam, x, y, plane_angle_deg)


def world_heading_to_plane_angle(cam, heading_deg):
    return default().world_heading_to_plane_angle(cam, heading_deg)


def nozzle_px(cam):
    return default().nozzle_px(cam)
