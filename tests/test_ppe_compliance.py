"""Tests for the ppe_compliance agent with a mocked VLM."""

from __future__ import annotations

import io
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from PIL import Image

from app.agents import ppe_compliance
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
    set_mode("ppe_compliance", AgentMode.ACTIVE)
    ppe_compliance._last_fired.clear()
    yield
    ppe_compliance._last_fired.clear()


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
            "ppe_area": {
                "name": "Hard Hat Area",
                "polygon": [[0.0, 0.4], [0.5, 0.4], [0.5, 1.0], [0.0, 1.0]],
            },
            "cooldown_seconds": 0,
        }
    ]


def _vlm_result(violation: bool, conf: float, in_area: int = 1, without: int = 1):
    return vlm.VLMResult(
        raw="{}",
        parsed={
            "people_in_area": in_area,
            "people_without_hardhat": without,
            "violation": violation,
            "confidence": conf,
            "reasoning": "stub",
        },
        model_id="openai/gpt-4o-mini",
        latency_ms=12,
    )


async def test_creates_incident_on_violation(tmp_path):
    snap = _write_snapshot(tmp_path)
    mock_provider = MagicMock()
    mock_provider.analyse_image = AsyncMock(return_value=_vlm_result(True, 0.9, 2, 1))

    with (
        patch("app.agents.ppe_compliance.load_cameras", return_value=_camera_cfg()),
        patch("app.agents.ppe_compliance.get_provider", return_value=mock_provider),
        patch("app.agents.ppe_compliance.create_incident", new=AsyncMock()) as ci,
    ):
        ci.return_value = MagicMock(id=42)
        await ppe_compliance.run("TEST-SERIAL", snap, trigger="test")

    ci.assert_awaited_once()
    kwargs = ci.await_args.kwargs
    assert kwargs["type"] == IncidentType.PPE_VIOLATION
    assert kwargs["severity"].value == "HIGH"  # 0.9 >= 0.85
    assert kwargs["camera_id"] == "TEST-SERIAL"
    assert kwargs["extra"]["people_missing_item"] == 1
    assert kwargs["extra"]["required_item"] == "hard_hat"


async def test_medium_severity_when_lower_confidence(tmp_path):
    snap = _write_snapshot(tmp_path)
    mock_provider = MagicMock()
    mock_provider.analyse_image = AsyncMock(return_value=_vlm_result(True, 0.6))

    with (
        patch("app.agents.ppe_compliance.load_cameras", return_value=_camera_cfg()),
        patch("app.agents.ppe_compliance.get_provider", return_value=mock_provider),
        patch("app.agents.ppe_compliance.create_incident", new=AsyncMock()) as ci,
    ):
        ci.return_value = MagicMock(id=1)
        await ppe_compliance.run("TEST-SERIAL", snap, trigger="test")

    assert ci.await_args.kwargs["severity"].value == "MEDIUM"


async def test_no_incident_when_violation_false(tmp_path):
    snap = _write_snapshot(tmp_path)
    mock_provider = MagicMock()
    mock_provider.analyse_image = AsyncMock(
        return_value=_vlm_result(False, 0.95, in_area=2, without=0)
    )

    with (
        patch("app.agents.ppe_compliance.load_cameras", return_value=_camera_cfg()),
        patch("app.agents.ppe_compliance.get_provider", return_value=mock_provider),
        patch("app.agents.ppe_compliance.create_incident", new=AsyncMock()) as ci,
    ):
        await ppe_compliance.run("TEST-SERIAL", snap, trigger="test")

    ci.assert_not_awaited()


async def test_no_incident_when_low_confidence(tmp_path):
    snap = _write_snapshot(tmp_path)
    mock_provider = MagicMock()
    mock_provider.analyse_image = AsyncMock(return_value=_vlm_result(True, 0.3))

    with (
        patch("app.agents.ppe_compliance.load_cameras", return_value=_camera_cfg()),
        patch("app.agents.ppe_compliance.get_provider", return_value=mock_provider),
        patch("app.agents.ppe_compliance.create_incident", new=AsyncMock()) as ci,
    ):
        await ppe_compliance.run("TEST-SERIAL", snap, trigger="test")

    ci.assert_not_awaited()


async def test_no_incident_when_nobody_without_hardhat(tmp_path):
    """Guards against a model that says violation=true but reports 0 missing hats."""
    snap = _write_snapshot(tmp_path)
    mock_provider = MagicMock()
    mock_provider.analyse_image = AsyncMock(
        return_value=_vlm_result(True, 0.9, in_area=3, without=0)
    )

    with (
        patch("app.agents.ppe_compliance.load_cameras", return_value=_camera_cfg()),
        patch("app.agents.ppe_compliance.get_provider", return_value=mock_provider),
        patch("app.agents.ppe_compliance.create_incident", new=AsyncMock()) as ci,
    ):
        await ppe_compliance.run("TEST-SERIAL", snap, trigger="test")

    ci.assert_not_awaited()


async def test_cooldown_blocks_second_run(tmp_path):
    snap = _write_snapshot(tmp_path)
    mock_provider = MagicMock()
    mock_provider.analyse_image = AsyncMock(return_value=_vlm_result(True, 0.9))

    cfg = _camera_cfg()
    cfg[0]["cooldown_seconds"] = 60

    with (
        patch("app.agents.ppe_compliance.load_cameras", return_value=cfg),
        patch("app.agents.ppe_compliance.get_provider", return_value=mock_provider),
        patch("app.agents.ppe_compliance.create_incident", new=AsyncMock()) as ci,
    ):
        ci.return_value = MagicMock(id=1)
        await ppe_compliance.run("TEST-SERIAL", snap, trigger="t1")
        await ppe_compliance.run("TEST-SERIAL", snap, trigger="t2")

    assert mock_provider.analyse_image.await_count == 1
    assert ci.await_count == 1


async def test_no_ppe_area_skips(tmp_path):
    snap = _write_snapshot(tmp_path)
    cfg = _camera_cfg()
    del cfg[0]["ppe_area"]
    mock_provider = MagicMock()
    mock_provider.analyse_image = AsyncMock(return_value=_vlm_result(True, 0.9))

    with (
        patch("app.agents.ppe_compliance.load_cameras", return_value=cfg),
        patch("app.agents.ppe_compliance.get_provider", return_value=mock_provider),
    ):
        await ppe_compliance.run("TEST-SERIAL", snap, trigger="t")

    mock_provider.analyse_image.assert_not_awaited()


async def test_hi_vis_vest_required_item(tmp_path):
    """When required_item=hi_vis_vest the agent reads people_without_hivis."""
    snap = _write_snapshot(tmp_path)
    cfg = _camera_cfg()
    cfg[0]["ppe_area"]["required_item"] = "hi_vis_vest"
    cfg[0]["ppe_area"]["name"] = "Lab Floor"

    hivis_result = vlm.VLMResult(
        raw="{}",
        parsed={
            "people_in_area": 2,
            "people_without_hivis": 1,
            "violation": True,
            "confidence": 0.9,
            "reasoning": "one person in plain shirt",
        },
        model_id="openai/gpt-4o-mini",
        latency_ms=10,
    )
    mock_provider = MagicMock()
    mock_provider.analyse_image = AsyncMock(return_value=hivis_result)

    with (
        patch("app.agents.ppe_compliance.load_cameras", return_value=cfg),
        patch("app.agents.ppe_compliance.get_provider", return_value=mock_provider),
        patch("app.agents.ppe_compliance.create_incident", new=AsyncMock()) as ci,
    ):
        ci.return_value = MagicMock(id=7)
        await ppe_compliance.run("TEST-SERIAL", snap, trigger="test")

    ci.assert_awaited_once()
    kwargs = ci.await_args.kwargs
    assert kwargs["extra"]["required_item"] == "hi_vis_vest"
    assert kwargs["extra"]["people_missing_item"] == 1
    # Verify the prompt sent to the VLM actually mentions hi-vis.
    prompt = mock_provider.analyse_image.await_args.args[1]
    assert "hi-vis vest" in prompt.lower()
    assert "hard hat" not in prompt.lower()


async def test_unknown_required_item_falls_back_to_hard_hat(tmp_path):
    snap = _write_snapshot(tmp_path)
    cfg = _camera_cfg()
    cfg[0]["ppe_area"]["required_item"] = "safety_glasses"
    mock_provider = MagicMock()
    mock_provider.analyse_image = AsyncMock(return_value=_vlm_result(False, 0.95, 1, 0))

    with (
        patch("app.agents.ppe_compliance.load_cameras", return_value=cfg),
        patch("app.agents.ppe_compliance.get_provider", return_value=mock_provider),
    ):
        await ppe_compliance.run("TEST-SERIAL", snap, trigger="t")

    prompt = mock_provider.analyse_image.await_args.args[1]
    assert "hard hat" in prompt.lower()


async def test_passive_mode_skips_vlm_call(tmp_path):
    """When the agent is passive, the VLM should never be called."""
    from app.services.agent_mode import AgentMode, set_mode

    set_mode("ppe_compliance", AgentMode.PASSIVE)

    snap = _write_snapshot(tmp_path)
    mock_provider = MagicMock()
    mock_provider.analyse_image = AsyncMock(return_value=_vlm_result(True, 0.9))

    with (
        patch("app.agents.ppe_compliance.load_cameras", return_value=_camera_cfg()),
        patch("app.agents.ppe_compliance.get_provider", return_value=mock_provider),
        patch("app.agents.ppe_compliance.create_incident", new=AsyncMock()) as ci,
    ):
        await ppe_compliance.run("TEST-SERIAL", snap, trigger="t")

    mock_provider.analyse_image.assert_not_awaited()
    ci.assert_not_awaited()
