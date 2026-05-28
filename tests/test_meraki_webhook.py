from __future__ import annotations

import hashlib
import hmac
import json

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import _init_db, app


@pytest.fixture(autouse=True)
def init_test_db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DEBUG", "false")
    monkeypatch.setenv("MERAKI_WEBHOOK_SECRET", "")
    from app import config

    config.get_settings.cache_clear()
    _init_db()


def _sig(body: bytes, secret: str) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


async def test_webhook_accepts_unsigned_when_secret_unset():
    payload = {"alertType": "motion_alert", "deviceSerial": "TEST-1234"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        r = await ac.post("/webhooks/meraki", json=payload)
    assert r.status_code == 200
    assert r.json() == {"status": "accepted"}


async def test_webhook_rejects_bad_signature(monkeypatch):
    monkeypatch.setenv("MERAKI_WEBHOOK_SECRET", "supersecret")
    from app import config

    config.get_settings.cache_clear()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        r = await ac.post(
            "/webhooks/meraki",
            json={"alertType": "motion_alert"},
            headers={"X-Cisco-Meraki-Signature": "deadbeef"},
        )
    assert r.status_code == 401


async def test_webhook_accepts_valid_signature(monkeypatch):
    secret = "supersecret"
    monkeypatch.setenv("MERAKI_WEBHOOK_SECRET", secret)
    from app import config

    config.get_settings.cache_clear()

    body = json.dumps({"alertType": "motion_alert", "deviceSerial": "X"}).encode()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        r = await ac.post(
            "/webhooks/meraki",
            content=body,
            headers={
                "Content-Type": "application/json",
                "X-Cisco-Meraki-Signature": _sig(body, secret),
            },
        )
    assert r.status_code == 200


async def test_webhook_rejects_bad_json():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        r = await ac.post(
            "/webhooks/meraki",
            content=b"not json",
            headers={"Content-Type": "application/json"},
        )
    assert r.status_code == 400


async def test_latest_snapshot_404_when_missing():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        r = await ac.get("/cameras/NOPE-NOPE/snapshot/latest")
    assert r.status_code == 404


async def test_latest_snapshot_returns_newest(tmp_path, monkeypatch):
    """Writing two fake snapshots and asking for latest should return the newer one."""
    from app.config import get_settings

    snaps = get_settings().snapshots_dir / "2026/05/28"
    snaps.mkdir(parents=True, exist_ok=True)
    older = snaps / "AAAA-AAAA-AAAA_010101_000000.jpg"
    newer = snaps / "AAAA-AAAA-AAAA_020202_000000.jpg"
    older.write_bytes(b"\xff\xd8old")
    newer.write_bytes(b"\xff\xd8new")
    import os
    import time

    # Force distinct mtimes
    os.utime(older, (time.time() - 100, time.time() - 100))

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        r = await ac.get("/cameras/AAAA-AAAA-AAAA/snapshot/latest")
    assert r.status_code == 200
    assert r.content == b"\xff\xd8new"
