"""Subscribers that react to Meraki-sourced bus events.

Imported by `app.main` lifespan so the subscriber tasks start as background
coroutines for the lifetime of the app.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from app.integrations.meraki import MerakiError, fetch_and_persist_snapshot
from app.runtime.bus import bus

logger = logging.getLogger(__name__)


async def _on_camera_motion() -> None:
    """Listen for camera.motion events and persist a snapshot for each."""
    q = bus.subscribe("camera.motion")
    logger.info("Subscriber started: camera.motion")
    while True:
        event = await q.get()
        payload = event.payload
        serial = (
            payload.get("deviceSerial")
            or payload.get("device", {}).get("serial")
            or payload.get("serial")
        )
        if not serial:
            logger.warning("camera.motion event missing serial: %s", payload)
            continue

        try:
            path = await fetch_and_persist_snapshot(serial)
            # Real agents (zone_intrusion, ppe_compliance) subscribe to
            # camera.snapshot — they decide whether anything is worth filing.
            await bus.publish(
                "camera.snapshot",
                {
                    "serial": serial,
                    "path": str(path),
                    "timestamp": datetime.utcnow().isoformat(),
                    "trigger": "motion",
                },
            )
        except MerakiError as e:
            logger.error("Failed to fetch snapshot for %s: %s", serial, e)


async def _on_network_client() -> None:
    """Listen for network.client events and publish enriched details."""
    q = bus.subscribe("network.client")
    logger.info("Subscriber started: network.client")
    while True:
        event = await q.get()
        payload = event.payload
        # Real enrichment + whitelist check lands in M5 (rogue_device agent).
        # For now just echo to the bus so the dashboard sees the activity.
        mac = payload.get("clientMac") or payload.get("client", {}).get("mac", "unknown")
        logger.info("Network client event: mac=%s", mac)


def start_subscribers() -> None:
    """Spawn background tasks for all subscribers. Idempotent per-process.

    Must be called from inside a running event loop (e.g. FastAPI lifespan).
    """
    if getattr(start_subscribers, "_started", False):
        return
    asyncio.create_task(_on_camera_motion())
    asyncio.create_task(_on_network_client())
    start_subscribers._started = True  # type: ignore[attr-defined]
