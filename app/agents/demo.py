from __future__ import annotations

import logging
import random
from datetime import datetime

from app.models.db import IncidentSeverity, IncidentType
from app.runtime.bus import bus

logger = logging.getLogger(__name__)

_DEMO_SCENARIOS = [
    {
        "type": IncidentType.INTRUSION,
        "severity": IncidentSeverity.HIGH,
        "summary": "[DEMO] Person detected inside exclusion zone at Main Entrance.",
        "camera_id": "DEMO-CAMERA-001",
    },
    {
        "type": IncidentType.PPE_VIOLATION,
        "severity": IncidentSeverity.MEDIUM,
        "summary": "[DEMO] PPE violation: no yellow hard hat detected on worker in workshop area.",
        "camera_id": "DEMO-CAMERA-001",
    },
    {
        "type": IncidentType.ROGUE_DEVICE,
        "severity": IncidentSeverity.HIGH,
        "summary": (
            "[DEMO] Unknown device connected to Corp WiFi. MAC: de:ad:be:ef:00:01 (OUI: Unknown)."
        ),
        "client_mac": "de:ad:be:ef:00:01",
    },
]


async def fire_demo_incident() -> None:
    """Publish a synthetic incident event onto the bus for UI testing."""
    scenario = random.choice(_DEMO_SCENARIOS)
    payload = {
        **scenario,
        "type": scenario["type"].value,
        "severity": scenario["severity"].value,
        "timestamp": datetime.utcnow().isoformat(),
        "demo": True,
    }
    logger.info("Demo agent firing synthetic incident: %s", payload["type"])
    await bus.publish("incident.new", payload)


async def heartbeat_status() -> None:
    """Publish a heartbeat narration event."""
    messages = [
        "All systems nominal. No violations detected in the last 60 seconds.",
        "Zone A clear. Network monitoring active. 0 rogue devices detected.",
        "PPE compliance check running. Camera feed healthy.",
        "Heartbeat OK — danger_zone is watching.",
    ]
    await bus.publish(
        "heartbeat",
        {
            "message": random.choice(messages),
            "timestamp": datetime.utcnow().isoformat(),
        },
    )
