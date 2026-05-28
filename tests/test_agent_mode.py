"""Tests for per-agent active/passive mode."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import _init_db, app
from app.services.agent_mode import AgentMode, get_mode, set_mode


@pytest.fixture(autouse=True)
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DEBUG", "false")
    from app import config

    config.get_settings.cache_clear()
    _init_db()
    yield


def test_default_mode_is_passive():
    assert get_mode("zone_intrusion") is AgentMode.PASSIVE
    assert get_mode("ppe_compliance") is AgentMode.PASSIVE


def test_set_mode_persists():
    set_mode("zone_intrusion", AgentMode.ACTIVE)
    assert get_mode("zone_intrusion") is AgentMode.ACTIVE
    set_mode("zone_intrusion", AgentMode.PASSIVE)
    assert get_mode("zone_intrusion") is AgentMode.PASSIVE


async def test_modes_endpoint_lists_all_gated_agents():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        r = await ac.get("/agents/modes")
    assert r.status_code == 200
    names = {m["agent"] for m in r.json()}
    assert {"zone_intrusion", "ppe_compliance"}.issubset(names)
    assert all(m["mode"] == "passive" for m in r.json())


async def test_set_mode_endpoint():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        r = await ac.post("/agents/zone_intrusion/mode", json={"mode": "active"})
        assert r.status_code == 200
        assert r.json() == {"agent": "zone_intrusion", "mode": "active"}

        r = await ac.get("/agents/zone_intrusion/mode")
        assert r.json()["mode"] == "active"


async def test_set_mode_rejects_ungated_agent():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        r = await ac.post("/agents/heartbeat/mode", json={"mode": "active"})
    assert r.status_code == 404


async def test_set_mode_rejects_bad_value():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        r = await ac.post("/agents/zone_intrusion/mode", json={"mode": "nope"})
    assert r.status_code == 422  # pydantic enum validation
