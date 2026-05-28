from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import _init_db, app


@pytest.fixture(autouse=True)
def init_test_db(tmp_path, monkeypatch):
    """Initialise an in-memory-ish DB for each test."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DEBUG", "false")
    # Reset settings cache so env override takes effect
    from app import config

    config.get_settings.cache_clear()
    _init_db()


async def test_healthz():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        r = await ac.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


async def test_readyz():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        r = await ac.get("/readyz")
    assert r.status_code == 200


async def test_list_incidents_empty():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        r = await ac.get("/incidents/")
    assert r.status_code == 200
    assert r.json() == []


async def test_dashboard_renders_zone_overlay_markup():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        r = await ac.get("/")
    assert r.status_code == 200
    body = r.text
    assert 'id="zone-overlay"' in body
    assert 'id="zone-legend"' in body
    assert 'id="camera-image-wrap"' in body
