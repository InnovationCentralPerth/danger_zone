"""REST API for the analytics-gated capture watcher.

Endpoints (mounted under ``/cameras``):
    GET  /cameras/analytics-watcher
    GET  /cameras/{serial}/analytics-watcher
    POST /cameras/{serial}/analytics-watcher   body: {"enabled": bool, "interval_seconds"?: int}
    GET  /cameras/{serial}/analytics/recent   — debug passthrough of Meraki JSON

Designed to be driven by both the dashboard and external AI agents.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.integrations.meraki import MerakiError, get_client
from app.services import analytics_watcher

router = APIRouter()
logger = logging.getLogger(__name__)


class WatcherDTO(BaseModel):
    serial: str
    enabled: bool
    interval_seconds: int
    running: bool
    running_for_seconds: int
    last_status: str
    last_run: str | None = None
    last_average_count: float
    last_entrances: int
    max_runtime_seconds: int


class SetWatcherRequest(BaseModel):
    enabled: bool
    interval_seconds: int | None = Field(
        default=None,
        ge=analytics_watcher.MIN_INTERVAL_SECONDS,
        le=analytics_watcher.MAX_INTERVAL_SECONDS,
        description=(
            f"How often to poll Meraki analytics, in seconds. "
            f"Range {analytics_watcher.MIN_INTERVAL_SECONDS}–"
            f"{analytics_watcher.MAX_INTERVAL_SECONDS}; "
            f"omit to keep current / use default "
            f"({analytics_watcher.DEFAULT_INTERVAL_SECONDS})."
        ),
    )


def _to_dto(s: analytics_watcher.WatcherState) -> WatcherDTO:
    return WatcherDTO(**s.as_dict())


async def _discover_serials() -> list[str]:
    """Return the union of YAML-configured serials and live Meraki serials.

    Falls back to YAML-only if the Meraki API is unreachable so the dashboard
    still works offline. De-duplicates while preserving YAML order first,
    then Meraki order.
    """
    from app.services.config_loader import load_cameras

    seen: set[str] = set()
    serials: list[str] = []
    for cam in load_cameras():
        s = cam.get("serial")
        if s and s not in seen:
            seen.add(s)
            serials.append(s)
    try:
        devices = await get_client().list_camera_devices()
    except MerakiError as e:
        logger.warning("analytics: Meraki camera discovery failed, YAML-only: %s", e)
        return serials
    for d in devices:
        s = d.get("serial")
        if s and s not in seen:
            seen.add(s)
            serials.append(s)
    return serials


@router.get(
    "/analytics-watcher",
    summary="List analytics-watcher state for all known cameras",
)
async def list_watchers() -> list[WatcherDTO]:
    """Return one watcher state per discovered camera.

    Discovery merges ``cameras.yaml`` entries with the live Meraki
    ``GET /organizations/{id}/devices`` list, so cameras added on the Meraki
    dashboard show up here without a config edit. YAML-only fallback when
    Meraki is unreachable.
    """
    serials = await _discover_serials()
    return [_to_dto(analytics_watcher.get_state(s)) for s in serials]


@router.get(
    "/{serial}/analytics-watcher",
    summary="Get analytics-watcher state for one camera",
)
async def get_watcher(serial: str) -> WatcherDTO:
    return _to_dto(analytics_watcher.get_state(serial))


@router.post(
    "/{serial}/analytics-watcher",
    summary="Enable / disable the analytics-gated capture watcher for one camera",
)
async def set_watcher(serial: str, body: SetWatcherRequest) -> WatcherDTO:
    if body.enabled:
        state = await analytics_watcher.enable(serial, body.interval_seconds)
    else:
        state = await analytics_watcher.disable(serial)
    return _to_dto(state)


@router.get(
    "/{serial}/analytics/recent",
    summary="Pass-through of Meraki's /devices/{serial}/camera/analytics/recent",
)
async def get_analytics_recent(serial: str) -> list[dict]:
    """Returns Meraki's raw analytics-recent payload for the camera.

    Useful for external agents that want to inspect zone counts directly
    without enabling the watcher loop. Validates the serial via the Meraki
    API itself — invalid serials return 502 with the upstream error.
    """
    try:
        return await get_client().get_camera_analytics_recent(serial)
    except MerakiError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
