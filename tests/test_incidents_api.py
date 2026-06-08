from __future__ import annotations

import asyncio

import pytest
from httpx import ASGITransport, AsyncClient

from app.agents.zone_intrusion import _coalesce_latest_by_serial
from app.main import _init_db, app
from app.models.db import IncidentSeverity, IncidentType
from app.runtime.bus import BusEvent
from app.services.incidents import create_incident


@pytest.fixture(autouse=True)
def init_test_db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DEBUG", "false")
    from app import config

    config.get_settings.cache_clear()
    _init_db()


async def test_incident_snapshot_endpoint_returns_jpeg(tmp_path):
    snap = tmp_path / "snap.jpg"
    snap.write_bytes(b"\xff\xd8\xff\xd9")  # minimal "JPEG" bytes
    incident = await create_incident(
        type=IncidentType.INTRUSION,
        severity=IncidentSeverity.HIGH,
        summary="t",
        camera_id="CAM-1",
        snapshot_path=str(snap),
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        r = await ac.get(f"/incidents/{incident.id}/snapshot")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/jpeg")
    assert r.content == b"\xff\xd8\xff\xd9"


async def test_incident_snapshot_404_when_no_path():
    incident = await create_incident(
        type=IncidentType.INTRUSION,
        severity=IncidentSeverity.HIGH,
        summary="t",
        camera_id="CAM-1",
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        r = await ac.get(f"/incidents/{incident.id}/snapshot")
    assert r.status_code == 404


async def test_incident_snapshot_404_when_file_missing(tmp_path):
    incident = await create_incident(
        type=IncidentType.INTRUSION,
        severity=IncidentSeverity.HIGH,
        summary="t",
        camera_id="CAM-1",
        snapshot_path=str(tmp_path / "does-not-exist.jpg"),
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        r = await ac.get(f"/incidents/{incident.id}/snapshot")
    assert r.status_code == 404


async def test_incident_snapshot_404_for_unknown_incident():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        r = await ac.get("/incidents/9999/snapshot")
    assert r.status_code == 404


async def test_coalesce_latest_by_serial_collapses_backlog():
    q: asyncio.Queue[BusEvent] = asyncio.Queue()
    first = BusEvent(topic="t", payload={"serial": "A", "path": "p1"})
    await q.put(BusEvent(topic="t", payload={"serial": "A", "path": "p2"}))
    await q.put(BusEvent(topic="t", payload={"serial": "A", "path": "p3"}))
    await q.put(BusEvent(topic="t", payload={"serial": "B", "path": "pB"}))

    latest = await _coalesce_latest_by_serial(q, first)

    # One entry per serial; the latest path wins for serial A
    assert set(latest.keys()) == {"A", "B"}
    assert latest["A"].payload["path"] == "p3"
    assert latest["B"].payload["path"] == "pB"
    # Queue fully drained
    assert q.empty()


async def test_coalesce_returns_first_when_queue_empty():
    q: asyncio.Queue[BusEvent] = asyncio.Queue()
    first = BusEvent(topic="t", payload={"serial": "A", "path": "p1"})
    latest = await _coalesce_latest_by_serial(q, first)
    assert latest == {"A": first}
