from __future__ import annotations

import json
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlmodel import Field, SQLModel


class IncidentType(StrEnum):
    INTRUSION = "INTRUSION"
    PPE_VIOLATION = "PPE_VIOLATION"
    PPE_OK = "PPE_OK"
    ROGUE_DEVICE = "ROGUE_DEVICE"
    DEMO = "DEMO"


class IncidentSeverity(StrEnum):
    INFO = "INFO"
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class IncidentStatus(StrEnum):
    OPEN = "OPEN"
    REVIEWING = "REVIEWING"
    RESOLVED = "RESOLVED"


class Incident(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    type: IncidentType
    severity: IncidentSeverity = IncidentSeverity.MEDIUM
    status: IncidentStatus = IncidentStatus.OPEN
    summary: str
    camera_id: str | None = None
    client_mac: str | None = None
    snapshot_path: str | None = None
    extra_json: str | None = None  # JSON blob for type-specific fields
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

    def extra(self) -> dict[str, Any]:
        return json.loads(self.extra_json) if self.extra_json else {}


class AgentTrace(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    agent: str
    trigger: str
    inputs_json: str = "{}"
    tool_calls_json: str = "[]"
    decision: str = ""
    latency_ms: int = 0
    cost_estimate_usd: float = 0.0
    created_at: datetime = Field(default_factory=datetime.utcnow)
    incident_id: int | None = Field(default=None, foreign_key="incident.id")


class Device(SQLModel, table=True):
    mac: str = Field(primary_key=True)
    vendor: str = ""
    label: str = ""
    status: str = "unknown"  # whitelist | blacklist | unknown
    first_seen: datetime = Field(default_factory=datetime.utcnow)
    last_seen: datetime = Field(default_factory=datetime.utcnow)


class Setting(SQLModel, table=True):
    key: str = Field(primary_key=True)
    value: str = ""
