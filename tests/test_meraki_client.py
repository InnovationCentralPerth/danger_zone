from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock

import pytest

from app.integrations import meraki as mod
from app.integrations.meraki import MerakiClient, MerakiError, WriteScopedKeyError


def _make_client(monkeypatch, me: dict, admins: list[dict]) -> MerakiClient:
    """Construct a MerakiClient with the SDK calls mocked."""
    fake_sdk = MagicMock()
    fake_sdk.administered.getAdministeredIdentitiesMe.return_value = me
    fake_sdk.organizations.getOrganizationAdmins.return_value = admins

    monkeypatch.setattr(mod.meraki, "DashboardAPI", MagicMock(return_value=fake_sdk))
    client = MerakiClient(api_key="x" * 40, org_id="O_123")
    return client


async def test_assert_read_only_passes(monkeypatch):
    client = _make_client(
        monkeypatch,
        me={"email": "bot@example.com"},
        admins=[{"email": "bot@example.com", "orgAccess": "read-only"}],
    )
    await client.assert_read_only()  # no exception


async def test_assert_read_only_rejects_full_access(monkeypatch):
    client = _make_client(
        monkeypatch,
        me={"email": "bot@example.com"},
        admins=[{"email": "bot@example.com", "orgAccess": "full"}],
    )
    with pytest.raises(WriteScopedKeyError):
        await client.assert_read_only()


async def test_assert_read_only_rejects_unknown_admin(monkeypatch):
    client = _make_client(
        monkeypatch,
        me={"email": "bot@example.com"},
        admins=[{"email": "someone-else@example.com", "orgAccess": "read-only"}],
    )
    with pytest.raises(MerakiError, match="not found"):
        await client.assert_read_only()


def test_constructor_requires_keys():
    with pytest.raises(MerakiError):
        MerakiClient(api_key="", org_id="O_1")
    with pytest.raises(MerakiError):
        MerakiClient(api_key="x", org_id="")


def test_snapshot_path_layout(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    from app import config

    config.get_settings.cache_clear()
    ts = datetime(2026, 5, 28, 10, 30, 0)
    p = mod.snapshot_path_for("AAAA-BBBB", ts=ts)
    assert p.parent == tmp_path / "snapshots/2026/05/28"
    assert p.name.startswith("AAAA-BBBB_103000")


async def test_fetch_snapshot_tolerates_400_during_polling(monkeypatch):
    """Older MV cameras return 400 (not 404) while the snapshot warms up.

    fetch_snapshot must keep polling on 400/403/404 and only succeed when
    the CDN serves a real 200.
    """
    import httpx

    client = _make_client(
        monkeypatch,
        me={"email": "bot@example.com"},
        admins=[{"email": "bot@example.com", "orgAccess": "read-only"}],
    )
    client._sdk.camera.generateDeviceCameraSnapshot.return_value = {
        "url": "https://spn34.meraki.com/stream/jpeg/snapshot/xyz"
    }

    # First two polls return 400, third returns 200 with image bytes.
    call = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call["n"] += 1
        if call["n"] < 3:
            return httpx.Response(400, text="<html>400 Bad Request</html>")
        return httpx.Response(200, content=b"\xff\xd8\xff\xe0jpegbytes")

    transport = httpx.MockTransport(handler)
    real_client_cls = httpx.AsyncClient

    def make_client(*_a, **kwargs):
        kwargs.pop("transport", None)
        return real_client_cls(transport=transport, **kwargs)

    monkeypatch.setattr(mod.httpx, "AsyncClient", make_client)

    data = await client.fetch_snapshot(
        "AAAA-BBBB",
        max_wait_seconds=5,
        poll_interval=0.05,
        initial_delay=0.0,
    )
    assert data.startswith(b"\xff\xd8")
    assert call["n"] == 3


async def test_fetch_snapshot_raises_on_persistent_4xx(monkeypatch):
    """If the CDN never serves a 200 within the deadline, raise MerakiError."""
    import httpx

    client = _make_client(
        monkeypatch,
        me={"email": "bot@example.com"},
        admins=[{"email": "bot@example.com", "orgAccess": "read-only"}],
    )
    client._sdk.camera.generateDeviceCameraSnapshot.return_value = {
        "url": "https://spn34.meraki.com/stream/jpeg/snapshot/xyz"
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text="<html>still warming up</html>")

    transport = httpx.MockTransport(handler)
    real_client_cls = httpx.AsyncClient

    def make_client(*_a, **kwargs):
        kwargs.pop("transport", None)
        return real_client_cls(transport=transport, **kwargs)

    monkeypatch.setattr(mod.httpx, "AsyncClient", make_client)

    with pytest.raises(MerakiError, match="not ready after"):
        await client.fetch_snapshot(
            "AAAA-BBBB",
            max_wait_seconds=0.2,
            poll_interval=0.05,
            initial_delay=0.0,
        )
