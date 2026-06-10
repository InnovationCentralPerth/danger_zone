"""HTTP tests for the analytics-watcher API."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app.integrations.meraki import MerakiError
from app.main import _init_db, app
from app.services import analytics_watcher


@pytest.fixture(autouse=True)
def setup_env(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DEBUG", "false")
    from app import config

    config.get_settings.cache_clear()
    _init_db()
    analytics_watcher._reset_for_tests()
    yield
    analytics_watcher._reset_for_tests()


def _cfg():
    return [
        {
            "serial": "AW-CAM-1",
            "name": "AW Test",
            "location": "Lab",
            "network_id": "L_TEST",
        }
    ]


def _meraki_devices(serials):
    """Build a fake `list_camera_devices()` response."""
    return [{"serial": s, "name": s, "model": "MV12"} for s in serials]


def test_list_returns_one_row_per_camera():
    """YAML cameras and Meraki cameras are merged into one list."""
    mock_client = AsyncMock()
    mock_client.list_camera_devices = AsyncMock(return_value=_meraki_devices(["AW-CAM-1"]))
    with (
        patch("app.services.config_loader.load_cameras", return_value=_cfg()),
        patch("app.api.analytics.get_client", return_value=mock_client),
    ):
        client = TestClient(app)
        r = client.get("/cameras/analytics-watcher")
    assert r.status_code == 200
    data = r.json()
    assert len(data) == 1
    assert data[0]["serial"] == "AW-CAM-1"
    assert data[0]["enabled"] is False
    assert data[0]["running"] is False


def test_list_includes_meraki_only_cameras():
    """Cameras discovered via Meraki but absent from YAML appear in the list."""
    mock_client = AsyncMock()
    mock_client.list_camera_devices = AsyncMock(
        return_value=_meraki_devices(["AW-CAM-1", "MERAKI-ONLY"])
    )
    with (
        patch("app.services.config_loader.load_cameras", return_value=_cfg()),
        patch("app.api.analytics.get_client", return_value=mock_client),
    ):
        client = TestClient(app)
        r = client.get("/cameras/analytics-watcher")
    assert r.status_code == 200
    serials = {row["serial"] for row in r.json()}
    assert serials == {"AW-CAM-1", "MERAKI-ONLY"}


def test_list_falls_back_to_yaml_when_meraki_unavailable():
    """A Meraki outage must not break the dashboard's camera list."""
    mock_client = AsyncMock()
    mock_client.list_camera_devices = AsyncMock(side_effect=MerakiError("boom"))
    with (
        patch("app.services.config_loader.load_cameras", return_value=_cfg()),
        patch("app.api.analytics.get_client", return_value=mock_client),
    ):
        client = TestClient(app)
        r = client.get("/cameras/analytics-watcher")
    assert r.status_code == 200
    serials = {row["serial"] for row in r.json()}
    assert serials == {"AW-CAM-1"}


def test_get_unknown_serial_returns_default_state():
    """Any serial is queryable; unknown serials get a default disabled state."""
    with patch("app.services.config_loader.load_cameras", return_value=_cfg()):
        client = TestClient(app)
        r = client.get("/cameras/MERAKI-ONLY/analytics-watcher")
    assert r.status_code == 200
    body = r.json()
    assert body["serial"] == "MERAKI-ONLY"
    assert body["enabled"] is False
    assert body["running"] is False


def test_post_enable_then_disable_roundtrip():
    mock_client = AsyncMock()
    mock_client.get_camera_analytics_recent = AsyncMock(return_value=[])
    with (
        patch("app.services.config_loader.load_cameras", return_value=_cfg()),
        patch("app.services.analytics_watcher.get_client", return_value=mock_client),
    ):
        client = TestClient(app)
        r = client.post(
            "/cameras/AW-CAM-1/analytics-watcher",
            json={"enabled": True, "interval_seconds": 5},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["enabled"] is True
        assert body["running"] is True
        assert body["interval_seconds"] == 5

        r2 = client.post("/cameras/AW-CAM-1/analytics-watcher", json={"enabled": False})
        assert r2.status_code == 200
        assert r2.json()["enabled"] is False
        assert r2.json()["running"] is False


def test_post_enable_works_for_meraki_only_serial():
    """Cameras not in YAML can still be enabled from the dashboard."""
    mock_client = AsyncMock()
    mock_client.get_camera_analytics_recent = AsyncMock(return_value=[])
    with (
        patch("app.services.config_loader.load_cameras", return_value=_cfg()),
        patch("app.services.analytics_watcher.get_client", return_value=mock_client),
    ):
        client = TestClient(app)
        r = client.post(
            "/cameras/MERAKI-ONLY/analytics-watcher",
            json={"enabled": True, "interval_seconds": 5},
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["serial"] == "MERAKI-ONLY"
    assert body["enabled"] is True
    assert body["running"] is True


def test_post_rejects_interval_below_minimum():
    with patch("app.services.config_loader.load_cameras", return_value=_cfg()):
        client = TestClient(app)
        r = client.post(
            "/cameras/AW-CAM-1/analytics-watcher",
            json={"enabled": True, "interval_seconds": 1},
        )
    assert r.status_code == 422


def test_post_rejects_interval_above_maximum():
    with patch("app.services.config_loader.load_cameras", return_value=_cfg()):
        client = TestClient(app)
        r = client.post(
            "/cameras/AW-CAM-1/analytics-watcher",
            json={"enabled": True, "interval_seconds": 99999},
        )
    assert r.status_code == 422


def test_get_analytics_recent_passthrough():
    payload = [{"zoneId": "0", "averageCount": 0.0, "entrances": 0, "endTs": "T"}]
    mock_client = AsyncMock()
    mock_client.get_camera_analytics_recent = AsyncMock(return_value=payload)
    with (
        patch("app.services.config_loader.load_cameras", return_value=_cfg()),
        patch("app.api.analytics.get_client", return_value=mock_client),
    ):
        client = TestClient(app)
        r = client.get("/cameras/AW-CAM-1/analytics/recent")
    assert r.status_code == 200
    assert r.json() == payload


def test_get_analytics_recent_502_on_meraki_error():
    mock_client = AsyncMock()
    mock_client.get_camera_analytics_recent = AsyncMock(side_effect=MerakiError("boom"))
    with (
        patch("app.services.config_loader.load_cameras", return_value=_cfg()),
        patch("app.api.analytics.get_client", return_value=mock_client),
    ):
        client = TestClient(app)
        r = client.get("/cameras/AW-CAM-1/analytics/recent")
    assert r.status_code == 502
    assert "boom" in r.json()["detail"]
