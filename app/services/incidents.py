"""Incident persistence + bus broadcast."""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from sqlmodel import Session

from app.db import get_engine
from app.models.db import Incident, IncidentSeverity, IncidentStatus, IncidentType
from app.runtime.bus import bus

logger = logging.getLogger(__name__)


async def create_incident(
    *,
    type: IncidentType,
    severity: IncidentSeverity,
    summary: str,
    camera_id: str | None = None,
    client_mac: str | None = None,
    snapshot_path: str | None = None,
    extra: dict[str, Any] | None = None,
    status: IncidentStatus = IncidentStatus.OPEN,
) -> Incident:
    """Persist an incident, return it, and publish to the bus."""
    with Session(get_engine()) as session:
        row = Incident(
            type=type,
            severity=severity,
            status=status,
            summary=summary,
            camera_id=camera_id,
            client_mac=client_mac,
            snapshot_path=snapshot_path,
            extra_json=json.dumps(extra, default=str) if extra else None,
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        incident_id = row.id
        incident_dict = {
            "id": row.id,
            "type": row.type.value,
            "severity": row.severity.value,
            "status": row.status.value,
            "summary": row.summary,
            "camera_id": row.camera_id,
            "client_mac": row.client_mac,
            "snapshot_path": row.snapshot_path,
            "extra": extra or {},
            "timestamp": (row.created_at or datetime.utcnow()).isoformat(),
        }

    logger.info("Incident #%s created: %s", incident_id, summary)
    await bus.publish("incident.new", incident_dict)
    return row
