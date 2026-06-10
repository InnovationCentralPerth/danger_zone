"""Tests for GET /cameras/{serial}/zones."""

from __future__ import annotations

from unittest.mock import patch

from fastapi.testclient import TestClient

from app.main import app


def _cfg():
    return [
        {
            "serial": "ZONE-CAM-1",
            "name": "Test Cam",
            "location": "Lab",
            "network_id": "L_TEST",
            "exclusion_zone": {
                "name": "Restricted",
                "polygon": [[0.0, 0.0], [0.5, 0.0], [0.5, 0.5], [0.0, 0.5]],
            },
            "ppe_area": {
                "name": "Lab PPE Area",
                "polygon": [[0.5, 0.5], [1.0, 0.5], [1.0, 1.0], [0.5, 1.0]],
                "required_item": "hi_vis_vest",
            },
        }
    ]


def test_returns_both_zones():
    with patch("app.services.config_loader.load_cameras", return_value=_cfg()):
        client = TestClient(app)
        r = client.get("/cameras/ZONE-CAM-1/zones")
    assert r.status_code == 200
    data = r.json()
    assert data["serial"] == "ZONE-CAM-1"
    ids = {z["id"] for z in data["zones"]}
    assert ids == {"exclusion_zone", "ppe_area"}
    ppe = next(z for z in data["zones"] if z["id"] == "ppe_area")
    assert ppe["kind"] == "ppe"
    assert ppe["required_item"] == "hi_vis_vest"
    assert ppe["label"] == "Lab PPE Area"
    excl = next(z for z in data["zones"] if z["id"] == "exclusion_zone")
    assert excl["kind"] == "intrusion"
    assert excl["colour"].startswith("#")
    assert len(excl["polygon"]) == 4


def test_unknown_serial_returns_full_frame_defaults():
    """Cameras with no YAML entry must still get usable zones.

    A Meraki-discovered camera (or any serial not in cameras.yaml) should
    receive full-frame polygons so the dashboard renders and the agents have
    something to reason about.
    """
    with patch("app.services.config_loader.load_cameras", return_value=_cfg()):
        client = TestClient(app)
        r = client.get("/cameras/NOT-IN-YAML/zones")
    assert r.status_code == 200
    data = r.json()
    assert data["serial"] == "NOT-IN-YAML"
    ids = {z["id"] for z in data["zones"]}
    assert ids == {"exclusion_zone", "ppe_area"}
    # Both zones should be the full-frame rectangle.
    for z in data["zones"]:
        polygon = z["polygon"]
        assert polygon == [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0]]
    ppe = next(z for z in data["zones"] if z["id"] == "ppe_area")
    assert ppe["required_item"] == "hard_hat"


def test_omits_missing_polygons():
    cfg = _cfg()
    del cfg[0]["exclusion_zone"]
    with patch("app.services.config_loader.load_cameras", return_value=cfg):
        client = TestClient(app)
        r = client.get("/cameras/ZONE-CAM-1/zones")
    assert r.status_code == 200
    ids = {z["id"] for z in r.json()["zones"]}
    assert ids == {"ppe_area"}
