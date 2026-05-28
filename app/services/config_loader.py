from __future__ import annotations

from pathlib import Path

import yaml

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


def load_cameras() -> list[dict]:  # type: ignore[type-arg]
    return _load_yaml("cameras").get("cameras", [])


def load_whitelist() -> dict[str, list[dict]]:  # type: ignore[type-arg]
    data = _load_yaml("whitelist")
    return {
        "whitelist": data.get("whitelist", []),
        "blacklist": data.get("blacklist", []),
    }


def load_models() -> dict:  # type: ignore[type-arg]
    return _load_yaml("models")
