from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from app.config import get_settings
from app.ingest.base import FrameRing
from app.integrations.meraki import MerakiError, fetch_and_persist_snapshot, get_client

router = APIRouter()
logger = logging.getLogger(__name__)

_MJPEG_BOUNDARY = b"frame"
_MJPEG_STALE_SECONDS = 10.0


class CameraInfo(BaseModel):
    serial: str
    name: str
    location: str
    # `source_type` is null for Meraki-cloud-only cameras (no ingest block).
    # `demo_label` is an explicit operator label (LIVE / REPLAY / SYNTHETIC).
    source_type: str | None = None
    demo_label: str | None = None
    vlm_fps: float | None = None


@router.get("/", summary="List configured cameras")
async def list_cameras() -> list[CameraInfo]:
    """List cameras from local config (config/cameras.yaml or the active profile)."""
    from app.services.config_loader import load_cameras

    cameras = load_cameras()
    out: list[CameraInfo] = []
    for c in cameras:
        src = c.get("source") or {}
        out.append(
            CameraInfo(
                serial=str(c.get("serial", "")),
                name=str(c.get("name") or c.get("serial") or ""),
                location=str(c.get("location") or ""),
                source_type=str(src["type"]) if src.get("type") else None,
                demo_label=str(c["demo_label"]) if c.get("demo_label") else None,
                vlm_fps=float(c["vlm_fps"]) if c.get("vlm_fps") is not None else None,
            )
        )
    return out


@router.get("/devices", summary="List MV cameras from Meraki org (live)")
async def list_meraki_cameras() -> list[dict]:
    """Hit the Meraki API to enumerate all MV cameras in the configured org."""
    try:
        return await get_client().list_camera_devices()
    except MerakiError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e


@router.get("/ingest", summary="Status of source-agnostic ingest streams")
async def ingest_status() -> list[dict]:
    """Report which ingest backends are running and their buffer state."""
    from app.ingest.manager import ingest_manager

    return ingest_manager.status()


@router.post("/{serial}/snapshot", summary="Force a fresh snapshot from a camera")
async def trigger_snapshot(serial: str, analyse: bool = True) -> dict[str, str]:
    """Generate, fetch, and persist a new snapshot from the camera.

    Ingest-backed cameras (file/RTSP/synthetic) return the newest buffered
    frame instead of calling the Meraki snapshot API. When ``analyse=true``
    (default) the snapshot is published to the bus so subscribed agents
    (zone_intrusion, ppe_compliance) can react to it.
    """
    import asyncio
    from datetime import datetime

    from app.ingest.manager import ingest_manager
    from app.runtime.bus import bus

    path = None
    if ingest_manager.latest_frame(serial) is not None:
        path = await asyncio.to_thread(ingest_manager.capture_latest, serial)
    if path is None:
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


def _is_full_frame(polygon: list) -> bool:
    """True when the polygon's bounding box covers (almost) the whole frame.

    These are the placeholder defaults for cameras with no custom zone; the
    dashboard should not paint them over the picture.
    """
    try:
        xs = [float(p[0]) for p in polygon]
        ys = [float(p[1]) for p in polygon]
    except (TypeError, ValueError, IndexError):
        return False
    if not xs or not ys:
        return False
    return (max(xs) - min(xs)) * (max(ys) - min(ys)) >= 0.98


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
                "full_frame": _is_full_frame(excl["polygon"]),
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
                "full_frame": _is_full_frame(ppe["polygon"]),
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


async def _mjpeg_frames(
    ring: FrameRing,
    *,
    stale_seconds: float = _MJPEG_STALE_SECONDS,
    poll_seconds: float = 0.1,
) -> AsyncIterator[bytes]:
    """Yield the newest ring frame as a multipart JPEG stream.

    Frames are only sent when the sequence advances, so a 5 fps source does not
    flood the browser with duplicates. If the source stops producing frames for
    ``stale_seconds`` the generator ends; the dashboard then shows a stalled
    state instead of freezing on an old image forever.
    """
    last_seq: int | None = None
    last_change = time.monotonic()
    while True:
        frame = ring.latest()
        if frame is not None and frame.seq != last_seq:
            last_seq = frame.seq
            last_change = time.monotonic()
            yield (
                b"--"
                + _MJPEG_BOUNDARY
                + b"\r\nContent-Type: image/jpeg\r\nContent-Length: "
                + str(len(frame.jpeg)).encode()
                + b"\r\n\r\n"
                + frame.jpeg
                + b"\r\n"
            )
        elif time.monotonic() - last_change > stale_seconds:
            return
        await asyncio.sleep(poll_seconds)


@router.get(
    "/{serial}/stream.mjpg",
    summary="MJPEG stream from the ingest frame ring",
)
async def stream_camera(serial: str) -> StreamingResponse:
    """Serve a live MJPEG stream for ingest-backed cameras.

    Meraki-cloud-only cameras have no ring and return 404; the dashboard keeps
    using the snapshot endpoint for those. Works for file, RTSP and synthetic
    sources, so the tile looks like video during a replay demo.
    """
    from app.ingest.manager import ingest_manager

    ring = ingest_manager.ring(serial)
    if ring is None:
        raise HTTPException(
            status_code=404,
            detail=f"No ingest stream for {serial} (Meraki-cloud camera or ingest off)",
        )
    return StreamingResponse(
        _mjpeg_frames(ring),
        media_type="multipart/x-mixed-replace; boundary=frame",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )
