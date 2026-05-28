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
