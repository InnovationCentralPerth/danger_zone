"""Tests for the DEMO_PROFILE camera-config switch."""

from __future__ import annotations

import pytest
import yaml

from app.services import config_loader


@pytest.fixture
def config_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(config_loader, "_CONFIG_DIR", tmp_path)
    return tmp_path


def _write(path, cameras):
    path.write_text(yaml.safe_dump({"cameras": cameras}))


def test_profile_overrides_default_config(config_dir, monkeypatch):
    _write(config_dir / "cameras.yaml", [{"serial": "LIVE-1"}])
    _write(
        config_dir / "cameras.replay.yaml",
        [{"serial": "REPLAY-1", "demo_label": "REPLAY"}],
    )
    monkeypatch.setenv("DEMO_PROFILE", "replay")
    from app import config

    config.get_settings.cache_clear()

    cameras = config_loader.load_cameras()
    assert [c["serial"] for c in cameras] == ["REPLAY-1"]


def test_missing_profile_falls_back_to_default(config_dir, monkeypatch):
    _write(config_dir / "cameras.yaml", [{"serial": "LIVE-1"}])
    monkeypatch.setenv("DEMO_PROFILE", "nope")
    from app import config

    config.get_settings.cache_clear()

    cameras = config_loader.load_cameras()
    assert [c["serial"] for c in cameras] == ["LIVE-1"]


def test_empty_profile_uses_default(config_dir, monkeypatch):
    _write(config_dir / "cameras.yaml", [{"serial": "LIVE-1"}])
    monkeypatch.setenv("DEMO_PROFILE", "")
    from app import config

    config.get_settings.cache_clear()

    cameras = config_loader.load_cameras()
    assert [c["serial"] for c in cameras] == ["LIVE-1"]
