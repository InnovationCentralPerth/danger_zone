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
        patch("app.services.config_loader.load_cameras", return_value=_camera_cfg()),
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
        patch("app.services.config_loader.load_cameras", return_value=_camera_cfg()),
        patch("app.agents.ppe_compliance.get_provider", return_value=mock_provider),
        patch("app.agents.ppe_compliance.create_incident", new=AsyncMock()) as ci,
    ):
        ci.return_value = MagicMock(id=1)
        await ppe_compliance.run("TEST-SERIAL", snap, trigger="test")

    assert ci.await_args.kwargs["severity"].value == "MEDIUM"


async def test_files_ppe_ok_when_compliant(tmp_path):
    """Person present, wearing PPE, high confidence -> PPE_OK / INFO."""
    snap = _write_snapshot(tmp_path)
    mock_provider = MagicMock()
    mock_provider.analyse_image = AsyncMock(
        return_value=_vlm_result(False, 0.95, in_area=2, without=0)
    )

    with (
        patch("app.services.config_loader.load_cameras", return_value=_camera_cfg()),
        patch("app.agents.ppe_compliance.get_provider", return_value=mock_provider),
        patch("app.agents.ppe_compliance.create_incident", new=AsyncMock()) as ci,
    ):
        ci.return_value = MagicMock(id=11)
        await ppe_compliance.run("TEST-SERIAL", snap, trigger="test")

    ci.assert_awaited_once()
    kwargs = ci.await_args.kwargs
    assert kwargs["type"] == IncidentType.PPE_OK
    assert kwargs["severity"].value == "INFO"
    assert kwargs["extra"]["people_in_area"] == 2


async def test_silent_when_nobody_in_area(tmp_path):
    """No people in the PPE area -> no incident, no PPE_OK note."""
    snap = _write_snapshot(tmp_path)
    mock_provider = MagicMock()
    mock_provider.analyse_image = AsyncMock(
        return_value=_vlm_result(False, 0.95, in_area=0, without=0)
    )

    with (
        patch("app.services.config_loader.load_cameras", return_value=_camera_cfg()),
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
        patch("app.services.config_loader.load_cameras", return_value=_camera_cfg()),
        patch("app.agents.ppe_compliance.get_provider", return_value=mock_provider),
        patch("app.agents.ppe_compliance.create_incident", new=AsyncMock()) as ci,
    ):
        await ppe_compliance.run("TEST-SERIAL", snap, trigger="test")

    ci.assert_not_awaited()


async def test_files_ppe_ok_when_violation_flag_wrong_but_zero_missing(tmp_path):
    """Model says violation=true but reports 0 missing -> treat as compliant PPE_OK."""
    snap = _write_snapshot(tmp_path)
    mock_provider = MagicMock()
    mock_provider.analyse_image = AsyncMock(
        return_value=_vlm_result(True, 0.9, in_area=3, without=0)
    )

    with (
        patch("app.services.config_loader.load_cameras", return_value=_camera_cfg()),
        patch("app.agents.ppe_compliance.get_provider", return_value=mock_provider),
        patch("app.agents.ppe_compliance.create_incident", new=AsyncMock()) as ci,
    ):
        ci.return_value = MagicMock(id=12)
        await ppe_compliance.run("TEST-SERIAL", snap, trigger="test")

    ci.assert_awaited_once()
    assert ci.await_args.kwargs["type"] == IncidentType.PPE_OK


async def test_cooldown_blocks_second_run(tmp_path):
    snap = _write_snapshot(tmp_path)
    mock_provider = MagicMock()
    mock_provider.analyse_image = AsyncMock(return_value=_vlm_result(True, 0.9))

    cfg = _camera_cfg()
    cfg[0]["cooldown_seconds"] = 60

    with (
        patch("app.services.config_loader.load_cameras", return_value=cfg),
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
        patch("app.services.config_loader.load_cameras", return_value=cfg),
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
        patch("app.services.config_loader.load_cameras", return_value=cfg),
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
        patch("app.services.config_loader.load_cameras", return_value=cfg),
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
        patch("app.services.config_loader.load_cameras", return_value=_camera_cfg()),
        patch("app.agents.ppe_compliance.get_provider", return_value=mock_provider),
        patch("app.agents.ppe_compliance.create_incident", new=AsyncMock()) as ci,
    ):
        await ppe_compliance.run("TEST-SERIAL", snap, trigger="t")

    mock_provider.analyse_image.assert_not_awaited()
    ci.assert_not_awaited()


# ---- multi-item path (mission context) -----------------------------------


def _multi_vlm_result(
    items: list[dict],
    *,
    in_area: int = 1,
    overall_conf: float = 0.9,
    summary: str = "stub multi summary",
):
    return vlm.VLMResult(
        raw="{}",
        parsed={
            "people_in_area": in_area,
            "items_checked": items,
            "overall_confidence": overall_conf,
            "summary": summary,
        },
        model_id="openai/gpt-4o-mini",
        latency_ms=20,
    )


def _multi_ctx(items, *, severity_hint="medium", note=""):
    from app.services.mission_context import AgentContext

    return AgentContext(
        raw_input="test mission",
        required_items=items,
        context_note=note,
        severity_hint=severity_hint,
        parsed_at="2026-06-08T00:00:00",
        model_id="openai/gpt-4o-mini",
    )


async def test_multi_item_violation_fires_with_breakdown(tmp_path):
    snap = _write_snapshot(tmp_path)
    ctx = _multi_ctx(["safety glasses", "anti-static wrist strap"], severity_hint="medium")
    mock_provider = MagicMock()
    mock_provider.analyse_image = AsyncMock(
        return_value=_multi_vlm_result(
            [
                {"item": "safety glasses", "missing_count": 1, "confidence": 0.9},
                {"item": "anti-static wrist strap", "missing_count": 2, "confidence": 0.8},
            ],
            in_area=2,
            overall_conf=0.85,
        )
    )
    with (
        patch("app.services.config_loader.load_cameras", return_value=_camera_cfg()),
        patch("app.agents.ppe_compliance.get_provider", return_value=mock_provider),
        patch("app.agents.ppe_compliance.get_context", return_value=ctx),
        patch("app.agents.ppe_compliance.create_incident", new=AsyncMock()) as ci,
    ):
        ci.return_value = MagicMock(id=101)
        await ppe_compliance.run("TEST-SERIAL", snap, trigger="t")

    ci.assert_awaited_once()
    kwargs = ci.await_args.kwargs
    assert kwargs["type"] == IncidentType.PPE_VIOLATION
    # severity_hint=medium -> always MEDIUM regardless of confidence
    assert kwargs["severity"].value == "MEDIUM"
    assert "safety glasses (1)" in kwargs["summary"]
    assert "anti-static wrist strap (2)" in kwargs["summary"]
    extra = kwargs["extra"]
    assert extra["mission"] == "test mission"
    assert extra["required_items"] == ["safety glasses", "anti-static wrist strap"]
    assert len(extra["violating_items"]) == 2


async def test_multi_item_severity_high_when_hint_high_and_confident(tmp_path):
    snap = _write_snapshot(tmp_path)
    ctx = _multi_ctx(["hard hat", "fall harness"], severity_hint="high")
    mock_provider = MagicMock()
    mock_provider.analyse_image = AsyncMock(
        return_value=_multi_vlm_result(
            [
                {"item": "hard hat", "missing_count": 1, "confidence": 0.9},
                {"item": "fall harness", "missing_count": 0, "confidence": 0.9},
            ],
            in_area=1,
            overall_conf=0.9,
        )
    )
    with (
        patch("app.services.config_loader.load_cameras", return_value=_camera_cfg()),
        patch("app.agents.ppe_compliance.get_provider", return_value=mock_provider),
        patch("app.agents.ppe_compliance.get_context", return_value=ctx),
        patch("app.agents.ppe_compliance.create_incident", new=AsyncMock()) as ci,
    ):
        ci.return_value = MagicMock(id=1)
        await ppe_compliance.run("TEST-SERIAL", snap, trigger="t")

    assert ci.await_args.kwargs["severity"].value == "HIGH"


async def test_multi_item_ppe_ok_when_all_compliant(tmp_path):
    snap = _write_snapshot(tmp_path)
    ctx = _multi_ctx(["safety glasses", "hi-vis vest"])
    mock_provider = MagicMock()
    mock_provider.analyse_image = AsyncMock(
        return_value=_multi_vlm_result(
            [
                {"item": "safety glasses", "missing_count": 0, "confidence": 0.95},
                {"item": "hi-vis vest", "missing_count": 0, "confidence": 0.92},
            ],
            in_area=1,
            overall_conf=0.93,
        )
    )
    with (
        patch("app.services.config_loader.load_cameras", return_value=_camera_cfg()),
        patch("app.agents.ppe_compliance.get_provider", return_value=mock_provider),
        patch("app.agents.ppe_compliance.get_context", return_value=ctx),
        patch("app.agents.ppe_compliance.create_incident", new=AsyncMock()) as ci,
    ):
        ci.return_value = MagicMock(id=2)
        await ppe_compliance.run("TEST-SERIAL", snap, trigger="t")

    ci.assert_awaited_once()
    kwargs = ci.await_args.kwargs
    assert kwargs["type"] == IncidentType.PPE_OK
    assert kwargs["severity"].value == "INFO"


async def test_multi_item_silent_when_nobody_in_area(tmp_path):
    snap = _write_snapshot(tmp_path)
    ctx = _multi_ctx(["hard hat"])
    mock_provider = MagicMock()
    mock_provider.analyse_image = AsyncMock(
        return_value=_multi_vlm_result(
            [{"item": "hard hat", "missing_count": 0, "confidence": 0.95}],
            in_area=0,
            overall_conf=0.95,
        )
    )
    with (
        patch("app.services.config_loader.load_cameras", return_value=_camera_cfg()),
        patch("app.agents.ppe_compliance.get_provider", return_value=mock_provider),
        patch("app.agents.ppe_compliance.get_context", return_value=ctx),
        patch("app.agents.ppe_compliance.create_incident", new=AsyncMock()) as ci,
    ):
        await ppe_compliance.run("TEST-SERIAL", snap, trigger="t")

    ci.assert_not_awaited()


async def test_multi_item_low_confidence_silenced(tmp_path):
    snap = _write_snapshot(tmp_path)
    ctx = _multi_ctx(["safety glasses"])
    mock_provider = MagicMock()
    mock_provider.analyse_image = AsyncMock(
        return_value=_multi_vlm_result(
            [{"item": "safety glasses", "missing_count": 1, "confidence": 0.3}],
            in_area=1,
            overall_conf=0.3,
        )
    )
    with (
        patch("app.services.config_loader.load_cameras", return_value=_camera_cfg()),
        patch("app.agents.ppe_compliance.get_provider", return_value=mock_provider),
        patch("app.agents.ppe_compliance.get_context", return_value=ctx),
        patch("app.agents.ppe_compliance.create_incident", new=AsyncMock()) as ci,
    ):
        await ppe_compliance.run("TEST-SERIAL", snap, trigger="t")

    ci.assert_not_awaited()


async def test_multi_item_per_item_low_confidence_excluded_from_violations(tmp_path):
    """An item with missing>=1 but confidence<0.5 must not count as violating."""
    snap = _write_snapshot(tmp_path)
    ctx = _multi_ctx(["safety glasses", "hard hat"])
    mock_provider = MagicMock()
    mock_provider.analyse_image = AsyncMock(
        return_value=_multi_vlm_result(
            [
                # confident violation
                {"item": "safety glasses", "missing_count": 1, "confidence": 0.9},
                # unconfident — should NOT count toward violations
                {"item": "hard hat", "missing_count": 1, "confidence": 0.3},
            ],
            in_area=1,
            overall_conf=0.7,
        )
    )
    with (
        patch("app.services.config_loader.load_cameras", return_value=_camera_cfg()),
        patch("app.agents.ppe_compliance.get_provider", return_value=mock_provider),
        patch("app.agents.ppe_compliance.get_context", return_value=ctx),
        patch("app.agents.ppe_compliance.create_incident", new=AsyncMock()) as ci,
    ):
        ci.return_value = MagicMock(id=3)
        await ppe_compliance.run("TEST-SERIAL", snap, trigger="t")

    ci.assert_awaited_once()
    extra = ci.await_args.kwargs["extra"]
    # Only safety glasses should be in violating_items
    violating_names = [v["item"] for v in extra["violating_items"]]
    assert violating_names == ["safety glasses"]


async def test_multi_item_context_when_active_takes_priority_over_yaml_default(tmp_path):
    """A mission with no yaml ppe_area.required_item should still use the multi-item path."""
    snap = _write_snapshot(tmp_path)
    cfg = _camera_cfg()
    # Remove required_item — mission context should drive the eval anyway.
    cfg[0]["ppe_area"].pop("required_item", None)
    ctx = _multi_ctx(["hard hat"], severity_hint="medium")
    mock_provider = MagicMock()
    mock_provider.analyse_image = AsyncMock(
        return_value=_multi_vlm_result(
            [{"item": "hard hat", "missing_count": 1, "confidence": 0.9}],
            in_area=1,
            overall_conf=0.9,
        )
    )
    with (
        patch("app.services.config_loader.load_cameras", return_value=cfg),
        patch("app.agents.ppe_compliance.get_provider", return_value=mock_provider),
        patch("app.agents.ppe_compliance.get_context", return_value=ctx),
        patch("app.agents.ppe_compliance.create_incident", new=AsyncMock()) as ci,
    ):
        ci.return_value = MagicMock(id=4)
        await ppe_compliance.run("TEST-SERIAL", snap, trigger="t")

    ci.assert_awaited_once()
    assert ci.await_args.kwargs["type"] == IncidentType.PPE_VIOLATION


# ---- pure helpers ---------------------------------------------------------


def test_parse_multi_item_verdict_normalises_and_filters():
    parsed = {
        "people_in_area": 3,
        "overall_confidence": 0.8,
        "summary": "x",
        "items_checked": [
            {"item": "SAFETY GLASSES", "missing_count": 2, "confidence": 0.9},
            {"item": "wrist strap", "missing_count": 1, "confidence": 0.3},  # low conf
            # missing "hard hat" — should be filled with zeros
        ],
    }
    out = ppe_compliance._parse_multi_item_verdict(
        parsed, ["safety glasses", "wrist strap", "hard hat"]
    )
    assert out["people_in_area"] == 3
    assert out["total_missing"] == 3  # 2 + 1 + 0
    assert [i["item"] for i in out["items"]] == [
        "safety glasses",
        "wrist strap",
        "hard hat",
    ]
    # Only confident violations count
    assert [v["item"] for v in out["violating_items"]] == ["safety glasses"]


def test_severity_from_hint_mapping():
    sev_high_conf = ppe_compliance._severity_from_hint("high", 0.9)
    sev_high_low = ppe_compliance._severity_from_hint("high", 0.6)
    sev_medium = ppe_compliance._severity_from_hint("medium", 0.99)
    sev_low = ppe_compliance._severity_from_hint("low", 0.99)
    assert sev_high_conf.value == "HIGH"
    assert sev_high_low.value == "MEDIUM"
    assert sev_medium.value == "MEDIUM"
    assert sev_low.value == "LOW"
