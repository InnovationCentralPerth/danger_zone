from __future__ import annotations

import logging
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

_CONFIG_DIR = Path("config")


def _load_yaml(name: str) -> dict:  # type: ignore[type-arg]
    """Load ``config/<name>.yaml``, falling back to ``<name>.example.yaml``.

    Site-specific files (``cameras.yaml``, ``whitelist.yaml``) are gitignored;
    the example variants ship with the repo as templates.
    """
    real = _CONFIG_DIR / f"{name}.yaml"
    example = _CONFIG_DIR / f"{name}.example.yaml"
    path = real if real.exists() else example
    with open(path) as f:
        return yaml.safe_load(f) or {}


def _camera_config_path() -> Path:
    """Resolve the camera config, honouring ``DEMO_PROFILE``.

    ``DEMO_PROFILE=replay`` loads ``config/cameras.replay.yaml`` when present.
    This lets the expo switch between live cameras and recorded footage with an
    env var and a restart, instead of editing the live config in front of an
    audience. A missing profile file falls back to the normal config with a
    warning so a typo cannot take the demo down.
    """
    from app.config import get_settings

    profile = (get_settings().demo_profile or "").strip()
    if profile:
        candidate = _CONFIG_DIR / f"cameras.{profile}.yaml"
        if candidate.exists():
            return candidate
        logger.warning(
            "DEMO_PROFILE=%s requested but %s does not exist; using default camera config",
            profile,
            candidate,
        )
    real = _CONFIG_DIR / "cameras.yaml"
    example = _CONFIG_DIR / "cameras.example.yaml"
    return real if real.exists() else example


def load_cameras() -> list[dict]:  # type: ignore[type-arg]
    path = _camera_config_path()
    with open(path) as f:
        data = yaml.safe_load(f) or {}
    return data.get("cameras", [])


# Full-frame polygon used when a camera has no YAML entry. Coordinates are
# normalised 0..1 relative to the snapshot resolution; this rectangle covers
# the entire frame. Counterclockwise starting top-left.
_FULL_FRAME_POLYGON: list[list[float]] = [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0]]


def _default_camera_config(serial: str) -> dict:  # type: ignore[type-arg]
    """Synthetic config for a Meraki-discovered camera with no YAML override.

    Returns full-frame zones for both exclusion and PPE areas, the default
    ``required_item`` (``hard_hat``), and the standard cooldown. Callers can
    treat this identically to a YAML-defined camera.
    """
    return {
        "serial": serial,
        "name": serial,
        "location": "",
        "network_id": "",
        "exclusion_zone": {
            "name": "Full Frame",
            "polygon": [p.copy() for p in _FULL_FRAME_POLYGON],
        },
        "ppe_area": {
            "name": "Full Frame",
            "polygon": [p.copy() for p in _FULL_FRAME_POLYGON],
            "required_item": "hard_hat",
        },
        "cooldown_seconds": 10,
    }


def get_camera_config(serial: str) -> dict:  # type: ignore[type-arg]
    """Return per-camera config for ``serial``, falling back to defaults.

    YAML entries from ``config/cameras.yaml`` win when present. Otherwise the
    camera is treated as Meraki-discovered with no overrides and gets
    full-frame zones plus the default PPE item.
    """
    for cam in load_cameras():
        if cam.get("serial") == serial:
            return cam
    return _default_camera_config(serial)


def load_whitelist() -> dict[str, list[dict]]:  # type: ignore[type-arg]
    data = _load_yaml("whitelist")
    return {
        "whitelist": data.get("whitelist", []),
        "blacklist": data.get("blacklist", []),
    }


def load_models() -> dict:  # type: ignore[type-arg]
    return _load_yaml("models")
