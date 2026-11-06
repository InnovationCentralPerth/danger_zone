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


def test_full_frame_zones_are_flagged_and_real_polygons_are_not():
    from app.api.cameras import _is_full_frame

    assert _is_full_frame([[0, 0], [1, 0], [1, 1], [0, 1]])
    assert not _is_full_frame([[0.1, 0.1], [0.9, 0.1], [0.9, 0.9], [0.1, 0.9]])
    assert not _is_full_frame([[0.0, 0.4], [0.5, 0.4], [0.5, 1.0], [0.0, 1.0]])
    assert not _is_full_frame([])
    assert not _is_full_frame([["a", "b"]])


def test_default_zones_carry_full_frame_flag():
    with patch("app.services.config_loader.load_cameras", return_value=_cfg()):
        resp = TestClient(app).get("/cameras/NOT-IN-YAML/zones")
    assert resp.status_code == 200
    zones = resp.json()["zones"]
    assert zones and all(z["full_frame"] for z in zones)


def test_custom_polygon_is_not_flagged_full_frame():
    with patch("app.services.config_loader.load_cameras", return_value=_cfg()):
        zones = TestClient(app).get("/cameras/ZONE-CAM-1/zones").json()["zones"]
    assert zones and not any(z["full_frame"] for z in zones)
