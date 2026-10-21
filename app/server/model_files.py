"""Where the fitted parameter files live and whether they are usable; never raises, so a missing model is a state."""
import json
import os
from pathlib import Path

MODELS_DIR = Path(os.environ.get("MODELS_DIR", Path(__file__).resolve().parent.parent / "models"))
MODEL_FILES = {"geometry": "geometry.json", "camera_model": "camera_model.json", "printer_model": "printer_model.json"}


def status(models_dir=None):
    """Per model: present (file parsed as a JSON object), its "version" field, and why it is unusable if it exists."""
    base = Path(models_dir) if models_dir is not None else MODELS_DIR
    return {name: _file_status(base / filename) for name, filename in MODEL_FILES.items()}


def _file_status(path):
    try:
        if not path.is_file():
            return {"present": False, "version": None, "error": None}
        data = json.loads(path.read_text())
    except (OSError, ValueError) as e:
        return {"present": False, "version": None, "error": f"unreadable: {e}"}
    if not isinstance(data, dict):
        return {"present": False, "version": None, "error": "not a JSON object"}
    version = data.get("version")
    return {"present": True, "version": None if version is None else str(version), "error": None}
