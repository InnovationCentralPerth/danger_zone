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
async def trigger_snapshot(serial: str, analyse: bool = True) -> dict[str, str]:
    """Generate, fetch, and persist a new snapshot from the camera.

    When ``analyse=true`` (default) the snapshot is published to the bus so
    subscribed agents (zone_intrusion, ppe_compliance) can react to it.
    """
    from datetime import datetime

    from app.runtime.bus import bus

    try:
        path = await fetch_and_persist_snapshot(serial)
    except MerakiError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e

    if analyse:
        await bus.publish(
            "camera.snapshot",
            {
                "serial": serial,
                "path": str(path),
                "timestamp": datetime.utcnow().isoformat(),
                "trigger": "manual",
            },
        )
    return {"status": "ok", "path": str(path)}


@router.get("/{serial}/zones", summary="Get monitored zones for a camera")
async def get_camera_zones(serial: str) -> dict:
    """Return polygon definitions so the dashboard can draw an overlay.

    Coordinates are normalised (0..1) (x, y) from the snapshot's top-left.
    Suitable for direct rendering as SVG polygons with a viewBox of 0..1
    (or scaled to 100 for percentage convenience).

    Cameras with no entry in ``config/cameras.yaml`` fall back to full-frame
    polygons so Meraki-discovered cameras work out of the box without YAML
    edits. Override polygons by adding an entry to ``cameras.yaml``.
    """
    from app.services.config_loader import get_camera_config

    cam = get_camera_config(serial)

    zones: list[dict] = []
    excl = cam.get("exclusion_zone") or {}
    if excl.get("polygon"):
        zones.append(
            {
                "id": "exclusion_zone",
                "label": excl.get("name", "Restricted Area"),
                "polygon": excl["polygon"],
                "colour": "#ff3c3c",
                "kind": "intrusion",
            }
        )
    ppe = cam.get("ppe_area") or {}
    if ppe.get("polygon"):
        zones.append(
            {
                "id": "ppe_area",
                "label": ppe.get("name", "PPE Area"),
                "polygon": ppe["polygon"],
                "colour": "#ffd200",
                "kind": "ppe",
                "required_item": ppe.get("required_item", "hard_hat"),
            }
        )
    return {"serial": serial, "zones": zones}


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
