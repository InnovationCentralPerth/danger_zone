from __future__ import annotations

from fastapi import APIRouter
from sqlmodel import select

from app.db import get_session
from app.models.db import Device

router = APIRouter()


@router.get("/clients", summary="List all seen network clients")
async def list_clients() -> list[Device]:
    with get_session() as session:
        return list(session.exec(select(Device)).all())


@router.get("/clients/{mac}", summary="Get client by MAC")
async def get_client(mac: str) -> Device | dict[str, str]:
    with get_session() as session:
        device = session.get(Device, mac.lower())
        if not device:
            return {"status": "unknown", "mac": mac}
        return device
