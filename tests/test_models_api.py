"""Tests for the /models API."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from app.integrations import vlm
from app.main import _init_db, app


@pytest.fixture(autouse=True)
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DEBUG", "false")
    from app import config

    config.get_settings.cache_clear()
    _init_db()
    vlm.reset_provider()
    yield
    vlm.reset_provider()


async def test_list_models():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        r = await ac.get("/models/")
    assert r.status_code == 200
    ids = [m["id"] for m in r.json()]
    assert "openai/gpt-4o-mini" in ids


async def test_get_and_set_active_model():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        r = await ac.get("/models/active")
        assert r.status_code == 200
        original = r.json()["id"]

        r = await ac.post("/models/active", json={"id": "openai/gpt-4o"})
        assert r.status_code == 200
        assert r.json()["id"] == "openai/gpt-4o"

        # Restore.
        await ac.post("/models/active", json={"id": original})


async def test_set_active_unknown_model_400():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        r = await ac.post("/models/active", json={"id": "nope/never"})
    assert r.status_code == 400
