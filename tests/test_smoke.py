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
    assert "Partners" in html
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


def test_dashboard_feeds_are_capped_so_a_long_running_tab_stays_fast():
    # An all-day expo tab got sluggish: every agent decision was prepended to the reasoning
    # feed forever, and each one re-scanned every entry. Keep both feeds bounded.
    from pathlib import Path

    js = (Path(__file__).resolve().parent.parent / "static/js/main.js").read_text()
    assert "const MAX_TRACES = 60;" in js and "const MAX_INCIDENT_CARDS = 80;" in js
    assert "lastElementChild.remove()" in js
    assert "MAX_TRACES);" in js and "MAX_INCIDENT_CARDS);" in js


def test_latest_snapshot_uses_the_live_ring_and_never_scans_the_whole_tree(tmp_path, monkeypatch):
    # The dashboard polls /snapshot/latest constantly; it used to rglob every snapshot ever
    # taken (1M+ files) on the event loop, which locked up the whole app.
    import os

    from fastapi.testclient import TestClient

    from app.api import cameras
    from app.ingest.base import Frame
    from app.ingest.manager import ingest_manager
    from app.main import app

    jpeg = b"\xff\xd8\xff\xe0fakejpeg\xff\xd9"
    frame = Frame(
        camera_id="CAM-1", seq=1, source_ts=0.0, ingest_ts=0.0, width=1, height=1, jpeg=jpeg
    )
    monkeypatch.setattr(
        ingest_manager, "latest_frame", lambda serial: frame if serial == "CAM-1" else None
    )
    client = TestClient(app)
    r = client.get("/cameras/CAM-1/snapshot/latest")
    assert r.status_code == 200 and r.content == jpeg and r.headers["content-type"] == "image/jpeg"

    # No ring: newest day folder wins, older days are not listed once a match is found.
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    from app import config

    config.get_settings.cache_clear()
    cameras._LATEST_CACHE.clear()
    old = tmp_path / "snapshots/2026/11/09"
    new = tmp_path / "snapshots/2026/11/10"
    old.mkdir(parents=True)
    new.mkdir(parents=True)
    (old / "CAM-2_000001_1.jpg").write_bytes(b"old")
    (new / "CAM-2_000002_2.jpg").write_bytes(b"new")
    listed: list[str] = []
    real_scandir = os.scandir
    monkeypatch.setattr(os, "scandir", lambda p: (listed.append(str(p)), real_scandir(p))[1])
    r = client.get("/cameras/CAM-2/snapshot/latest")
    assert r.status_code == 200 and r.content == b"new"
    assert not any(p.endswith("/09") for p in listed)
    config.get_settings.cache_clear()


def test_hot_query_columns_are_indexed(tmp_path, monkeypatch):
    # /incidents/ sorts by created_at on every page load; without an index that is a full-table
    # sort (400+ ms at 175k rows) on the event loop.
    import sqlite3

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    from app import config
    from app.main import _init_db

    config.get_settings.cache_clear()
    _init_db()
    con = sqlite3.connect(tmp_path / "app.db")
    names = {r[0] for r in con.execute("select name from sqlite_master where type='index'")}
    assert {
        "ix_incident_created_at",
        "ix_incident_camera_created",
        "ix_agenttrace_agent_created",
    } <= names
    plan = con.execute(
        "explain query plan select * from incident order by created_at desc limit 60"
    ).fetchall()
    assert "TEMP B-TREE" not in str(plan)
    config.get_settings.cache_clear()
