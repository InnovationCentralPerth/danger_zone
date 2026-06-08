"""Tests for the mission-context API endpoints under /agents/context."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from app.integrations import vlm
from app.main import _init_db, app
from app.services import mission_context


@pytest.fixture(autouse=True)
def init_test_db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DEBUG", "false")
    from app import config

    config.get_settings.cache_clear()
    _init_db()
    vlm.reset_provider()


def _vlm_parse_result(parsed: dict) -> vlm.VLMResult:
    return vlm.VLMResult(
        raw="{}",
        parsed=parsed,
        model_id="openai/gpt-4o-mini",
        latency_ms=50,
    )


async def test_get_returns_inactive_when_unset():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        r = await ac.get("/agents/context")
    assert r.status_code == 200
    body = r.json()
    assert body["active"] is False
    assert body["context"] is None


async def test_post_parses_and_persists():
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
    with patch("app.services.mission_context.get_provider", return_value=mock_provider):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            r = await ac.post(
                "/agents/context",
                json={"mission": "Electronics lab — eye and static protection"},
            )

    assert r.status_code == 200
    body = r.json()
    assert body["active"] is True
    assert body["context"]["required_items"] == ["safety glasses", "anti-static wrist strap"]
    assert body["context"]["context_note"] == "Electronics lab"
    assert body["context"]["severity_hint"] == "medium"

    # Verify persistence via service-level read
    ctx = mission_context.get_context()
    assert ctx is not None
    assert ctx.required_items == ["safety glasses", "anti-static wrist strap"]


async def test_post_rejects_empty_mission():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        r = await ac.post("/agents/context", json={"mission": ""})
    assert r.status_code == 422  # pydantic min_length validation


async def test_post_rejects_too_long_mission():
    too_long = "x" * (mission_context.MAX_INPUT_CHARS + 1)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        r = await ac.post("/agents/context", json={"mission": too_long})
    assert r.status_code == 422


async def test_post_returns_502_when_llm_returns_no_items():
    mock_provider = MagicMock()
    mock_provider.complete = AsyncMock(return_value=_vlm_parse_result({"required_items": []}))
    with patch("app.services.mission_context.get_provider", return_value=mock_provider):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            r = await ac.post(
                "/agents/context",
                json={"mission": "just generally be safe please"},
            )
    assert r.status_code == 502
    assert "PPE items" in r.json()["detail"]


async def test_delete_clears_active_context():
    # First set it
    mock_provider = MagicMock()
    mock_provider.complete = AsyncMock(
        return_value=_vlm_parse_result(
            {"required_items": ["hard hat"], "context_note": "", "severity_hint": "high"}
        )
    )
    with patch("app.services.mission_context.get_provider", return_value=mock_provider):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            await ac.post("/agents/context", json={"mission": "hard hats required"})
            assert (await ac.get("/agents/context")).json()["active"] is True

            r = await ac.delete("/agents/context")
            assert r.status_code == 200
            assert r.json()["active"] is False

            assert (await ac.get("/agents/context")).json()["active"] is False
