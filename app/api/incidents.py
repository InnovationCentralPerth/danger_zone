from __future__ import annotations

from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse
from sqlmodel import select

from app.db import get_session
from app.models.db import Incident, IncidentStatus, IncidentType

router = APIRouter()


@router.get("/", summary="List incidents")
async def list_incidents(
    since: datetime | None = None,
    type: IncidentType | None = None,
    status: IncidentStatus | None = None,
    camera_id: str | None = None,
    limit: int = Query(50, le=200),
) -> list[Incident]:
    with get_session() as session:
        q = select(Incident)
        if since:
            q = q.where(Incident.created_at >= since)
        if type:
            q = q.where(Incident.type == type)
        if status:
            q = q.where(Incident.status == status)
        if camera_id:
            q = q.where(Incident.camera_id == camera_id)
        q = q.order_by(Incident.created_at.desc()).limit(limit)  # type: ignore[arg-type]
        return list(session.exec(q).all())


@router.get("/{incident_id}", summary="Get incident by ID")
async def get_incident(incident_id: int) -> Incident:
    with get_session() as session:
        incident = session.get(Incident, incident_id)
        if not incident:
            raise HTTPException(status_code=404, detail="Incident not found")
        return incident


@router.get("/{incident_id}/snapshot", summary="Get the snapshot image attached to an incident")
async def get_incident_snapshot(incident_id: int) -> FileResponse:
    """Serve the JPEG snapshot recorded with an incident, if any."""
    with get_session() as session:
        incident = session.get(Incident, incident_id)
    if not incident:
        raise HTTPException(status_code=404, detail="Incident not found")
    if not incident.snapshot_path:
        raise HTTPException(status_code=404, detail="No snapshot attached to this incident")
    path = Path(incident.snapshot_path)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Snapshot file missing on disk")
    return FileResponse(path, media_type="image/jpeg")
