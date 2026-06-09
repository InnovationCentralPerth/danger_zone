"""Tests for app.services.mission_context."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.integrations import vlm
from app.main import _init_db
from app.runtime.bus import bus
from app.services import mission_context


@pytest.fixture(autouse=True)
def setup_env(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DEBUG", "false")
    from app import config

    config.get_settings.cache_clear()
    _init_db()
    vlm.reset_provider()
    yield


def _vlm_parse_result(parsed: dict) -> vlm.VLMResult:
    return vlm.VLMResult(
        raw="{}",
        parsed=parsed,
        model_id="openai/gpt-4o-mini",
        latency_ms=42,
    )


# ---- pure helpers ---------------------------------------------------------


def test_sanitise_parsed_clamps_items_and_lowercases():
    parsed = {
        "required_items": [
            "Safety Glasses",
            "  HARD HAT  ",
            "x" * 200,
            "hard hat",  # duplicate
            "gloves",
            "boots",
            "vest",
            "respirator",  # exceeds MAX_ITEMS
        ],
        "context_note": "Lab area",
        "severity_hint": "HIGH",
    }
    items, note, sev = mission_context._sanitise_parsed(parsed)
    assert len(items) <= mission_context.MAX_ITEMS
    assert items[0] == "safety glasses"
    assert items[1] == "hard hat"
    assert "hard hat" in items and items.count("hard hat") == 1
    assert all(len(i) <= mission_context.MAX_ITEM_CHARS for i in items)
    assert note == "Lab area"
    assert sev == "high"


def test_sanitise_parsed_defaults_when_missing():
    items, note, sev = mission_context._sanitise_parsed({})
    assert items == []
    assert note == ""
    assert sev == "medium"


def test_sanitise_parsed_rejects_unknown_severity():
    items, note, sev = mission_context._sanitise_parsed({"severity_hint": "extreme"})
    assert sev == "medium"


def test_parse_user_input_rejects_empty():
    with pytest.raises(ValueError):
        mission_context._parse_user_input("   ")


def test_parse_user_input_rejects_too_long():
    with pytest.raises(ValueError):
        mission_context._parse_user_input("x" * (mission_context.MAX_INPUT_CHARS + 1))


# ---- persistence + lifecycle ---------------------------------------------


async def test_set_context_persists_and_publishes_event():
    mock_provider = MagicMock()
    mock_provider.complete = AsyncMock(
        return_value=_vlm_parse_result(
            {
                "required_items": ["safety glasses", "anti-static wrist strap"],
                "context_note": "Electronics lab",
                "severity_hint": "medium",
            }
        )
    )

    q = bus.subscribe("mission_context.changed")
    with patch("app.services.mission_context.get_provider", return_value=mock_provider):
        ctx = await mission_context.set_context("electronics lab safety gear")

    assert ctx.required_items == ["safety glasses", "anti-static wrist strap"]
    assert ctx.context_note == "Electronics lab"
    assert ctx.severity_hint == "medium"
    assert ctx.model_id == "openai/gpt-4o-mini"
    assert ctx.parsed_at  # ISO timestamp populated

    # Survives reload
    loaded = mission_context.get_context()
    assert loaded is not None
    assert loaded.required_items == ctx.required_items

    event = await q.get()
    assert event.payload["active"] is True
    assert event.payload["context"]["required_items"] == ctx.required_items
    bus.unsubscribe("mission_context.changed", q)


async def test_set_context_rejects_empty_input():
    with pytest.raises(ValueError):
        await mission_context.set_context("")


async def test_set_context_raises_when_no_items_parsed():
    mock_provider = MagicMock()
    mock_provider.complete = AsyncMock(return_value=_vlm_parse_result({"required_items": []}))
    with (
        patch("app.services.mission_context.get_provider", return_value=mock_provider),
        pytest.raises(vlm.VLMError),
    ):
        await mission_context.set_context("just chatting, nothing specific")


async def test_set_context_wraps_unexpected_exception_as_vlmerror():
    mock_provider = MagicMock()
    mock_provider.complete = AsyncMock(side_effect=RuntimeError("boom"))
    with (
        patch("app.services.mission_context.get_provider", return_value=mock_provider),
        pytest.raises(vlm.VLMError),
    ):
        await mission_context.set_context("hard hats")


async def test_clear_context_removes_setting_and_publishes_event():
    mock_provider = MagicMock()
    mock_provider.complete = AsyncMock(
        return_value=_vlm_parse_result(
            {"required_items": ["hard hat"], "context_note": "", "severity_hint": "high"}
        )
    )
    with patch("app.services.mission_context.get_provider", return_value=mock_provider):
        await mission_context.set_context("hard hats required")

    assert mission_context.get_context() is not None

    q = bus.subscribe("mission_context.changed")
    await mission_context.clear_context()
    assert mission_context.get_context() is None
    event = await q.get()
    assert event.payload["active"] is False
    assert event.payload["context"] is None
    bus.unsubscribe("mission_context.changed", q)


def test_get_context_returns_none_when_blob_malformed():
    mission_context._write_setting("not-valid-json{")
    assert mission_context.get_context() is None
