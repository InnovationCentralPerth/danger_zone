from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter()


class CameraInfo(BaseModel):
    serial: str
    name: str
    location: str


@router.get("/", summary="List configured cameras")
async def list_cameras() -> list[CameraInfo]:
    # Reads from cameras.yaml — real impl in milestone 2
    from app.services.config_loader import load_cameras
    cameras = load_cameras()
    return [CameraInfo(serial=c["serial"], name=c["name"], location=c["location"]) for c in cameras]


@router.get("/{serial}/snapshot", summary="Get latest snapshot for a camera")
async def get_snapshot(serial: str) -> dict[str, str]:
    # Stub — real Meraki snapshot fetch in milestone 2
    return {"status": "not_implemented", "serial": serial}
