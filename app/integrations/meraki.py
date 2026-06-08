"""Meraki Dashboard API integration.

Wraps the official `meraki` SDK with:
- A startup read-only key assertion (hard fail if the key has write scope)
- Async-safe snapshot fetching (SDK calls run in a threadpool)
- Helpers for camera snapshot + persistence
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import meraki
from meraki.exceptions import APIError as MerakiSDKAPIError

from app.config import get_settings

logger = logging.getLogger(__name__)


class MerakiError(RuntimeError):
    pass


class WriteScopedKeyError(MerakiError):
    """Raised when a Meraki API key has write privileges — we require read-only."""


class MerakiClient:
    """Thin async-friendly wrapper around the official Meraki Python SDK."""

    def __init__(self, api_key: str, org_id: str) -> None:
        if not api_key:
            raise MerakiError("MERAKI_API_KEY is empty — set it in .env")
        if not org_id:
            raise MerakiError("MERAKI_ORG_ID is empty — set it in .env")
        self.api_key = api_key
        self.org_id = org_id
        # suppress_logging=True keeps our log output clean; we add our own
        self._sdk = meraki.DashboardAPI(
            api_key=api_key,
            suppress_logging=True,
            output_log=False,
            print_console=False,
            maximum_retries=3,
            wait_on_rate_limit=True,
        )

    # ---- async wrappers around blocking SDK calls -------------------------

    async def _run(self, fn, *args, **kwargs):  # type: ignore[no-untyped-def]
        """Run a blocking SDK call in a thread, normalising SDK errors.

        Meraki's SDK raises ``meraki.exceptions.APIError`` for 4xx/5xx; we
        convert those to ``MerakiError`` so callers (and FastAPI handlers)
        only need to catch one type.
        """
        try:
            return await asyncio.to_thread(fn, *args, **kwargs)
        except MerakiSDKAPIError as e:
            # The SDK's APIError stringifies to e.g.
            # "camera, generateDeviceCameraSnapshot - 404 Not Found, ..."
            raise MerakiError(str(e)) from e

    # ---- startup safety check --------------------------------------------

    async def assert_read_only(self) -> None:
        """Hard-fail if the API key has anything other than read-only org access.

        Strategy: get the identity associated with this key, then look up that
        identity in the org's admin list and inspect ``orgAccess``.
        """
        try:
            me = await self._run(self._sdk.administered.getAdministeredIdentitiesMe)
            admins = await self._run(self._sdk.organizations.getOrganizationAdmins, self.org_id)
        except Exception as e:  # pragma: no cover — network errors
            raise MerakiError(f"Failed to verify Meraki API key scope: {e}") from e

        my_email = me.get("email", "").lower()
        matching = [a for a in admins if a.get("email", "").lower() == my_email]

        if not matching:
            raise MerakiError(
                f"API key identity {my_email!r} not found in org {self.org_id!r} admin list. "
                "Check MERAKI_ORG_ID."
            )

        org_access = matching[0].get("orgAccess", "unknown")
        if org_access != "read-only":
            raise WriteScopedKeyError(
                f"Meraki API key for {my_email!r} has orgAccess={org_access!r}. "
                "danger_zone requires a read-only key. "
                "Create a separate read-only admin in Meraki Dashboard."
            )
        logger.info("Meraki API key verified: %s (read-only)", my_email)

    # ---- cameras ----------------------------------------------------------

    async def list_camera_devices(self) -> list[dict[str, Any]]:
        """Return all MV cameras in the org."""
        devices = await self._run(
            self._sdk.organizations.getOrganizationDevices,
            self.org_id,
            total_pages="all",
        )
        return [d for d in devices if d.get("model", "").startswith("MV")]

    async def generate_camera_snapshot_url(self, serial: str) -> str:
        """Request a snapshot from a camera and return the signed URL.

        Meraki returns a URL that becomes valid after a short delay (~3-5s).
        Caller must poll the URL until it returns 200.
        """
        result = await self._run(
            self._sdk.camera.generateDeviceCameraSnapshot,
            serial,
        )
        url = result.get("url")
        if not url:
            raise MerakiError(f"No snapshot URL returned for camera {serial}: {result}")
        return url  # type: ignore[no-any-return]

    async def fetch_snapshot(
        self,
        serial: str,
        *,
        max_wait_seconds: int = 25,
        poll_interval: float = 1.5,
        initial_delay: float = 3.0,
    ) -> bytes:
        """Generate a snapshot URL and poll it until the image is ready.

        Meraki signed snapshot URLs return errors until the camera has
        captured + uploaded the frame (typically 3-8 s, sometimes longer on
        older MV models). The "not ready" status varies by camera firmware:
        newer MVs return 404, older ones (MV12, MV21, MV71) return 400 with
        a generic HTML error page. We treat 400/403/404 as transient during
        the polling window and only raise on persistent failure past the
        timeout, or on other status codes (5xx, 401, etc).
        """
        url = await self.generate_camera_snapshot_url(serial)
        logger.debug("Meraki snapshot URL for %s: %s", serial, url)

        # Give the camera a head-start before the first poll; cuts down on
        # noise in the logs and matches Meraki's own guidance.
        await asyncio.sleep(initial_delay)

        deadline = asyncio.get_event_loop().time() + max_wait_seconds
        last_status: int | None = None
        last_body_preview: str = ""

        async with httpx.AsyncClient(timeout=10.0) as client:
            while asyncio.get_event_loop().time() < deadline:
                resp = await client.get(url)
                if resp.status_code == 200:
                    return resp.content
                last_status = resp.status_code
                last_body_preview = resp.text[:200].replace("\n", " ")
                if resp.status_code not in (400, 403, 404):
                    raise MerakiError(
                        f"Unexpected status {resp.status_code} fetching snapshot for "
                        f"{serial}: {last_body_preview!r} (url={url})"
                    )
                await asyncio.sleep(poll_interval)

        raise MerakiError(
            f"Snapshot for camera {serial} not ready after {max_wait_seconds}s "
            f"(last_status={last_status}, body={last_body_preview!r})"
        )

    # ---- analytics --------------------------------------------------------

    async def get_camera_analytics_recent(self, serial: str) -> list[dict[str, Any]]:
        """Return Meraki's recent on-device analytics windows for a camera.

        Each entry looks like::

            {"zoneId": "0", "startTs": "...", "endTs": "...",
             "entrances": 0, "averageCount": 0.023}

        Zone id ``"0"`` is the always-present "Full Frame" zone. Other ids
        correspond to Dashboard-configured analytics zones (which we ignore in
        favour of YAML zones for VLM reasoning).

        Zones with no recent updates return ``{"zoneId": ..., "error": "..."}``;
        we pass them through unchanged so the caller can inspect.
        """
        return await self._run(  # type: ignore[no-any-return]
            self._sdk.camera.getDeviceCameraAnalyticsRecent,
            serial,
        )

    # ---- network clients --------------------------------------------------

    async def list_network_clients(self, network_id: str, timespan: int = 3600) -> list[dict]:
        """List recent clients on a Meraki network (defaults to last hour)."""
        return await self._run(  # type: ignore[no-any-return]
            self._sdk.networks.getNetworkClients,
            network_id,
            timespan=timespan,
            total_pages="all",
        )


# ---- module-level singleton -------------------------------------------------

_client: MerakiClient | None = None


def get_client() -> MerakiClient:
    global _client
    if _client is None:
        settings = get_settings()
        _client = MerakiClient(api_key=settings.meraki_api_key, org_id=settings.meraki_org_id)
    return _client


def reset_client() -> None:
    """Test hook to clear the singleton between tests."""
    global _client
    _client = None


# ---- snapshot persistence ---------------------------------------------------


def snapshot_path_for(serial: str, ts: datetime | None = None) -> Path:
    """Compute the on-disk path for a snapshot. Creates parent dirs."""
    settings = get_settings()
    ts = ts or datetime.utcnow()
    folder = settings.snapshots_dir / ts.strftime("%Y/%m/%d")
    folder.mkdir(parents=True, exist_ok=True)
    fname = f"{serial}_{ts.strftime('%H%M%S_%f')}.jpg"
    return folder / fname


async def fetch_and_persist_snapshot(serial: str) -> Path:
    """Fetch a snapshot from Meraki and write it to data/snapshots/."""
    client = get_client()
    data = await client.fetch_snapshot(serial)
    path = snapshot_path_for(serial)
    path.write_bytes(data)
    logger.info("Saved snapshot for %s -> %s (%d bytes)", serial, path, len(data))
    return path
