"""End-to-end-ish test for the zone_intrusion agent with a mocked VLM."""

from __future__ import annotations

import io
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from PIL import Image

from app.agents import zone_intrusion
from app.integrations import vlm
from app.main import _init_db
from app.models.db import IncidentType
from app.services.agent_mode import AgentMode, set_mode


@pytest.fixture(autouse=True)
def setup_env(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DEBUG", "false")
    from app import config

    config.get_settings.cache_clear()
    _init_db()
    vlm.reset_provider()
    # Default mode is passive; flip to active for these tests.
    set_mode("zone_intrusion", AgentMode.ACTIVE)
    # Reset cooldown state between tests.
    zone_intrusion._last_fired.clear()
    yield
    zone_intrusion._last_fired.clear()


def _write_snapshot(tmp_path) -> str:
    img = Image.new("RGB", (320, 240), (50, 50, 50))
    p = tmp_path / "snap.jpg"
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    p.write_bytes(buf.getvalue())
    return str(p)


def _camera_cfg(serial: str = "TEST-SERIAL"):
    return [
        {
            "serial": serial,
            "name": "Test",
            "location": "Test",
            "network_id": "L_TEST",
            "exclusion_zone": {
                "name": "Restricted",
                "polygon": [[0.1, 0.1], [0.9, 0.1], [0.9, 0.9], [0.1, 0.9]],
            },
            "cooldown_seconds": 0,
        }
    ]


def _vlm_result(person: bool, conf: float):
    return vlm.VLMResult(
        raw="{}",
        parsed={
            "person_in_zone": person,
            "confidence": conf,
            "people_visible": 1 if person else 0,
            "reasoning": "stub",
        },
        model_id="openai/gpt-4o-mini",
        latency_ms=12,
    )


async def test_creates_incident_on_high_confidence(tmp_path):
    snap = _write_snapshot(tmp_path)
    mock_provider = MagicMock()
    mock_provider.analyse_image = AsyncMock(return_value=_vlm_result(True, 0.92))

    with (
        patch("app.services.config_loader.load_cameras", return_value=_camera_cfg()),
        patch("app.agents.zone_intrusion.get_provider", return_value=mock_provider),
        patch("app.agents.zone_intrusion.create_incident", new=AsyncMock()) as ci,
    ):
        ci.return_value = MagicMock(id=99)
        await zone_intrusion.run("TEST-SERIAL", snap, trigger="test")

    ci.assert_awaited_once()
    kwargs = ci.await_args.kwargs
    assert kwargs["type"] == IncidentType.INTRUSION
    # 0.92 >= 0.85 -> CRITICAL
    assert kwargs["severity"].value == "CRITICAL"
    assert kwargs["camera_id"] == "TEST-SERIAL"


async def test_no_incident_when_no_person(tmp_path):
    snap = _write_snapshot(tmp_path)
    mock_provider = MagicMock()
    mock_provider.analyse_image = AsyncMock(return_value=_vlm_result(False, 0.9))

    with (
        patch("app.services.config_loader.load_cameras", return_value=_camera_cfg()),
        patch("app.agents.zone_intrusion.get_provider", return_value=mock_provider),
        patch("app.agents.zone_intrusion.create_incident", new=AsyncMock()) as ci,
    ):
        await zone_intrusion.run("TEST-SERIAL", snap, trigger="test")

    ci.assert_not_awaited()


async def test_no_incident_when_low_confidence(tmp_path):
    snap = _write_snapshot(tmp_path)
    mock_provider = MagicMock()
    mock_provider.analyse_image = AsyncMock(return_value=_vlm_result(True, 0.3))

    with (
        patch("app.services.config_loader.load_cameras", return_value=_camera_cfg()),
        patch("app.agents.zone_intrusion.get_provider", return_value=mock_provider),
        patch("app.agents.zone_intrusion.create_incident", new=AsyncMock()) as ci,
    ):
        await zone_intrusion.run("TEST-SERIAL", snap, trigger="test")

    ci.assert_not_awaited()


async def test_cooldown_blocks_second_run(tmp_path):
    snap = _write_snapshot(tmp_path)
    mock_provider = MagicMock()
    mock_provider.analyse_image = AsyncMock(return_value=_vlm_result(True, 0.9))

    cfg = _camera_cfg()
    cfg[0]["cooldown_seconds"] = 60

    with (
        patch("app.services.config_loader.load_cameras", return_value=cfg),
        patch("app.agents.zone_intrusion.get_provider", return_value=mock_provider),
        patch("app.agents.zone_intrusion.create_incident", new=AsyncMock()) as ci,
    ):
        ci.return_value = MagicMock(id=1)
        await zone_intrusion.run("TEST-SERIAL", snap, trigger="t1")
        await zone_intrusion.run("TEST-SERIAL", snap, trigger="t2")

    # Second call should be suppressed by cooldown.
    assert mock_provider.analyse_image.await_count == 1
    assert ci.await_count == 1


async def test_unknown_camera_uses_full_frame_defaults(tmp_path):
    """Cameras not in cameras.yaml must still be processed.

    Meraki-discovered cameras (those without a YAML override) get full-frame
    polygons and the default cooldown. The agent should run end-to-end on
    them without skipping.
    """
    snap = _write_snapshot(tmp_path)
    mock_provider = MagicMock()
    mock_provider.analyse_image = AsyncMock(return_value=_vlm_result(True, 0.9))
    with (
        patch("app.services.config_loader.load_cameras", return_value=_camera_cfg()),
        patch("app.agents.zone_intrusion.get_provider", return_value=mock_provider),
        patch("app.agents.zone_intrusion.create_incident", new=AsyncMock()) as ci,
    ):
        await zone_intrusion.run("MERAKI-ONLY", snap, trigger="t")
    mock_provider.analyse_image.assert_awaited_once()
    ci.assert_awaited_once()


async def test_passive_mode_skips_vlm_call(tmp_path):
    set_mode("zone_intrusion", AgentMode.PASSIVE)
    snap = _write_snapshot(tmp_path)
    mock_provider = MagicMock()
    mock_provider.analyse_image = AsyncMock(return_value=_vlm_result(True, 0.9))
    with (
        patch("app.services.config_loader.load_cameras", return_value=_camera_cfg()),
        patch("app.agents.zone_intrusion.get_provider", return_value=mock_provider),
    ):
        await zone_intrusion.run("TEST-SERIAL", snap, trigger="t")
    mock_provider.analyse_image.assert_not_awaited()
