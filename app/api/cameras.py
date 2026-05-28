from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app.config import get_settings
from app.integrations.meraki import MerakiError, fetch_and_persist_snapshot, get_client

router = APIRouter()
logger = logging.getLogger(__name__)


class CameraInfo(BaseModel):
    serial: str
    name: str
    location: str


@router.get("/", summary="List configured cameras")
async def list_cameras() -> list[CameraInfo]:
    """List cameras from local config (config/cameras.yaml)."""
    from app.services.config_loader import load_cameras

    cameras = load_cameras()
    return [CameraInfo(serial=c["serial"], name=c["name"], location=c["location"]) for c in cameras]


@router.get("/devices", summary="List MV cameras from Meraki org (live)")
async def list_meraki_cameras() -> list[dict]:
    """Hit the Meraki API to enumerate all MV cameras in the configured org."""
    try:
        return await get_client().list_camera_devices()
    except MerakiError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e


@router.post("/{serial}/snapshot", summary="Force a fresh snapshot from a camera")
async def trigger_snapshot(serial: str) -> dict[str, str]:
    """Generate, fetch, and persist a new snapshot from the camera."""
    try:
        path = await fetch_and_persist_snapshot(serial)
    except MerakiError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    return {"status": "ok", "path": str(path)}


@router.get("/{serial}/snapshot/latest", summary="Get the most recent snapshot file")
async def get_latest_snapshot(serial: str) -> FileResponse:
    """Return the newest snapshot on disk for a given camera serial."""
    settings = get_settings()
    matches = sorted(
        settings.snapshots_dir.rglob(f"{serial}_*.jpg"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not matches:
        raise HTTPException(status_code=404, detail=f"No snapshots on disk for {serial}")
    return FileResponse(matches[0], media_type="image/jpeg")
