"""HTTP tests for the auto-capture API."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app.main import _init_db, app
from app.services import auto_capture


@pytest.fixture(autouse=True)
def setup_env(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DEBUG", "false")
    from app import config

    config.get_settings.cache_clear()
    _init_db()
    auto_capture._reset_for_tests()
    yield
    auto_capture._reset_for_tests()


def _cfg():
    return [
        {
            "serial": "AC-CAM-1",
            "name": "AC Test",
            "location": "Lab",
            "network_id": "L_TEST",
        }
    ]


def test_list_returns_one_row_per_camera():
    with patch("app.services.config_loader.load_cameras", return_value=_cfg()):
        client = TestClient(app)
        r = client.get("/cameras/auto-capture")
    assert r.status_code == 200
    data = r.json()
    assert len(data) == 1
    assert data[0]["serial"] == "AC-CAM-1"
    assert data[0]["enabled"] is False
    assert data[0]["running"] is False


def test_get_unknown_serial_404():
    with patch("app.services.config_loader.load_cameras", return_value=_cfg()):
        client = TestClient(app)
        r = client.get("/cameras/NOT-A-CAM/auto-capture")
    assert r.status_code == 404


def test_post_enable_then_disable_roundtrip():
    with (
        patch("app.services.config_loader.load_cameras", return_value=_cfg()),
        patch(
            "app.services.auto_capture.fetch_and_persist_snapshot",
            new=AsyncMock(return_value="/tmp/x.jpg"),
        ),
    ):
        client = TestClient(app)
        r = client.post(
            "/cameras/AC-CAM-1/auto-capture",
            json={"enabled": True, "interval_seconds": 5},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["enabled"] is True
        assert body["running"] is True
        assert body["interval_seconds"] == 5

        r2 = client.post("/cameras/AC-CAM-1/auto-capture", json={"enabled": False})
        assert r2.status_code == 200
        assert r2.json()["enabled"] is False
        assert r2.json()["running"] is False


def test_post_rejects_interval_below_minimum():
    with patch("app.services.config_loader.load_cameras", return_value=_cfg()):
        client = TestClient(app)
        r = client.post(
            "/cameras/AC-CAM-1/auto-capture",
            json={"enabled": True, "interval_seconds": 1},
        )
    assert r.status_code == 422


def test_post_unknown_serial_404():
    with patch("app.services.config_loader.load_cameras", return_value=_cfg()):
        client = TestClient(app)
        r = client.post("/cameras/NOPE/auto-capture", json={"enabled": True})
    assert r.status_code == 404
