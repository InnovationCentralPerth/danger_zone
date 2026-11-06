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
    assert 'id="camera-mode"' in body
    assert 'id="camera-mode-overlay"' in body
    assert "mission-preset" in body
    assert "main.js?v=" in body
    assert "main.css?v=" in body


async def test_dashboard_has_replay_and_feed_controls():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        r = await ac.get("/")
    assert r.status_code == 200
    for control in (
        "playback-slider",
        "loop-a",
        "loop-b",
        "loop-clear",
        "feed-clear",
        "scope-camera",
        "scope-all",
    ):
        assert f'id="{control}"' in r.text, control


async def test_dashboard_powered_by_strip_and_status_bar():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        r = await ac.get("/")
    html = r.text
    assert "Powered by" in html
    assert 'src="/static/img/partners/meraki.png"' in html
    assert 'src="/static/img/partners/splunk.png"' in html
    # No NIIN logo file yet -> plain product name, never a broken <img>.
    assert "NIIN Sovereign Cloud" in html
    assert "/static/img/partners/niin." not in html
    assert 'id="ops-clock-perth"' in html and 'id="ops-ai"' in html
    assert "Splunk LLC" in html  # required trademark attribution


def test_partner_logo_is_picked_up_when_file_is_dropped_in(tmp_path, monkeypatch):
    from app.dashboard import routes

    monkeypatch.setattr(routes, "_PARTNER_DIR", tmp_path)
    assert {p["key"]: p["logo"] for p in routes._partners()}["niin"] is None
    (tmp_path / "niin.svg").write_text("<svg xmlns='http://www.w3.org/2000/svg'/>")
    logos = {p["key"]: p["logo"] for p in routes._partners()}
    assert logos["niin"] is not None and logos["niin"].endswith("/niin.svg")
    assert logos["meraki"] is None  # nothing else was dropped in
