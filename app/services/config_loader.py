from __future__ import annotations

import yaml
from pathlib import Path

_CONFIG_DIR = Path("config")


def load_cameras() -> list[dict]:  # type: ignore[type-arg]
    path = _CONFIG_DIR / "cameras.yaml"
    with open(path) as f:
        data = yaml.safe_load(f)
    return data.get("cameras", [])


def load_whitelist() -> dict[str, list[dict]]:  # type: ignore[type-arg]
    path = _CONFIG_DIR / "whitelist.yaml"
    with open(path) as f:
        data = yaml.safe_load(f)
    return {
        "whitelist": data.get("whitelist", []),
        "blacklist": data.get("blacklist", []),
    }


def load_models() -> dict:  # type: ignore[type-arg]
    path = _CONFIG_DIR / "models.yaml"
    with open(path) as f:
        return yaml.safe_load(f)
