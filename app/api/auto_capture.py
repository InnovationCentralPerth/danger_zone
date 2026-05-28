"""REST API for the auto-capture loop.

Endpoints (mounted under ``/cameras``):
    GET  /cameras/auto-capture
    GET  /cameras/{serial}/auto-capture
    POST /cameras/{serial}/auto-capture   body: {"enabled": bool, "interval_seconds"?: int}

Designed to be driven by both the dashboard and external AI agents.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.services import auto_capture

router = APIRouter()
logger = logging.getLogger(__name__)


class AutoCaptureDTO(BaseModel):
    serial: str
    enabled: bool
    interval_seconds: int
    running: bool
    running_for_seconds: int
    last_status: str
    last_run: str | None = None
    max_runtime_seconds: int


class SetAutoCaptureRequest(BaseModel):
    enabled: bool
    interval_seconds: int | None = Field(
        default=None,
        ge=auto_capture.MIN_INTERVAL_SECONDS,
        description=(
            f"How often to grab a snapshot, in seconds. "
            f"Minimum {auto_capture.MIN_INTERVAL_SECONDS}; "
            f"omit to keep current / use default "
            f"({auto_capture.DEFAULT_INTERVAL_SECONDS})."
        ),
    )


def _to_dto(s: auto_capture.AutoCaptureState) -> AutoCaptureDTO:
    return AutoCaptureDTO(**s.as_dict())


def _known_serial(serial: str) -> bool:
    from app.services.config_loader import load_cameras

    return any(c.get("serial") == serial for c in load_cameras())


@router.get("/auto-capture", summary="List auto-capture state for all configured cameras")
async def list_auto_capture() -> list[AutoCaptureDTO]:
    return [_to_dto(s) for s in auto_capture.all_states()]


@router.get("/{serial}/auto-capture", summary="Get auto-capture state for one camera")
async def get_auto_capture(serial: str) -> AutoCaptureDTO:
    if not _known_serial(serial):
        raise HTTPException(status_code=404, detail=f"Unknown camera serial: {serial}")
    return _to_dto(auto_capture.get_state(serial))


@router.post("/{serial}/auto-capture", summary="Enable / disable auto-capture for one camera")
async def set_auto_capture(serial: str, body: SetAutoCaptureRequest) -> AutoCaptureDTO:
    if not _known_serial(serial):
        raise HTTPException(status_code=404, detail=f"Unknown camera serial: {serial}")
    if body.enabled:
        state = await auto_capture.enable(serial, body.interval_seconds)
    else:
        state = await auto_capture.disable(serial)
    return _to_dto(state)
