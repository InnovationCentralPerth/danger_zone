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


def _known_serial(serial: str) -> bool:
    from app.services.config_loader import load_cameras

    return any(c.get("serial") == serial for c in load_cameras())


@router.get(
    "/analytics-watcher",
    summary="List analytics-watcher state for all configured cameras",
)
async def list_watchers() -> list[WatcherDTO]:
    return [_to_dto(s) for s in analytics_watcher.all_states()]


@router.get(
    "/{serial}/analytics-watcher",
    summary="Get analytics-watcher state for one camera",
)
async def get_watcher(serial: str) -> WatcherDTO:
    if not _known_serial(serial):
        raise HTTPException(status_code=404, detail=f"Unknown camera serial: {serial}")
    return _to_dto(analytics_watcher.get_state(serial))


@router.post(
    "/{serial}/analytics-watcher",
    summary="Enable / disable the analytics-gated capture watcher for one camera",
)
async def set_watcher(serial: str, body: SetWatcherRequest) -> WatcherDTO:
    if not _known_serial(serial):
        raise HTTPException(status_code=404, detail=f"Unknown camera serial: {serial}")
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
    without enabling the watcher loop.
    """
    if not _known_serial(serial):
        raise HTTPException(status_code=404, detail=f"Unknown camera serial: {serial}")
    try:
        return await get_client().get_camera_analytics_recent(serial)
    except MerakiError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
