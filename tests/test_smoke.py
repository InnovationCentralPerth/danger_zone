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
    assert 'src="/static/img/partners/niin.png"' in html
    # Official ICP logo (not the old SVG), once, in the header.
    assert html.count('src="/static/img/partners/icp.png"') == 1
    assert "icp-logo.svg" not in html
    assert 'alt="NIIN Sovereign Cloud"' in html
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


async def test_product_name_comes_from_setting(monkeypatch):
    from app import config

    monkeypatch.setenv("PRODUCT_NAME", "Test Sentinel")
    config.get_settings.cache_clear()
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            html = (await ac.get("/")).text
        assert "<title>Test Sentinel — AI Safety &amp; Security Monitor</title>" in html
        assert '<span class="brand-name">TEST SENTINEL</span>' in html
        assert "NIIN Sovereign Cloud · no external AI" in html
    finally:
        monkeypatch.delenv("PRODUCT_NAME")
        config.get_settings.cache_clear()


async def test_default_product_name_is_safety_sentry():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        html = (await ac.get("/")).text
    assert '<span class="brand-name">SAFETY SENTRY</span>' in html


async def test_partner_logos_are_in_the_header_above_the_fold():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        html = (await ac.get("/")).text
    header_end = html.index("</header>")
    main_start = html.index("<main")
    assert header_end < main_start
    head = html[:header_end]
    for logo in ("meraki", "splunk", "niin", "icp"):
        assert f'src="/static/img/partners/{logo}.png"' in head, logo
    # The footer is only the trademark line now, not a logo strip.
    footer = html[html.index("<footer") : html.index("</footer>")]
    assert "<img" not in footer and "Splunk LLC" in footer


async def test_dashboard_is_self_contained_no_cdn():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        html = (await ac.get("/")).text
    assert "cdn.jsdelivr.net" not in html and "unpkg.com" not in html
    assert "/static/vendor/bootstrap.min.css" in html
    assert 'data-bs-theme="dark"' in html


async def test_dashboard_has_the_ask_bar_and_answer_sheet():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        html = (await ac.get("/")).text
    for el in (
        "ask-form",
        "ask-input",
        "ask-send",
        "ask-sheet",
        "ask-thread",
        "ask-suggestions",
        "ask-close",
    ):
        assert f'id="{el}"' in html, el
    # The bar sits under the incident panel in the right-hand column, not floating over it.
    assert html.index('id="ask-form"') > html.index('id="incident-list"')
    assert html.index('id="ask-form"') < html.index("</aside>")
