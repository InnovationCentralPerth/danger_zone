"""Agent A — Zone Intrusion.

Triggered by ``camera.snapshot`` bus events (which fire after motion-driven
or manually-requested snapshots). Loads the camera's exclusion polygon from
config, draws it on the snapshot, asks the VLM whether a person is inside,
and files an INTRUSION incident if confirmed.

Cooldown is enforced per-camera so a burst of motion only generates one
VLM call and one incident.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from app.integrations.vlm import VLMError, get_active_model, get_provider
from app.models.db import IncidentSeverity, IncidentType
from app.runtime.bus import bus
from app.runtime.trace import AgentRun
from app.services.config_loader import load_cameras
from app.services.incidents import create_incident
from app.services.zone import draw_zone_overlay

logger = logging.getLogger(__name__)

AGENT_NAME = "zone_intrusion"

# Per-camera last-fired timestamps for cooldown enforcement.
_last_fired: dict[str, float] = {}


_PROMPT = """\
You are reviewing a still frame from a security camera.
The image has a red polygon drawn on it labelled "{label}" — this is a
RESTRICTED EXCLUSION ZONE.

Answer ONLY with a JSON object of this exact shape:

{{
  "person_in_zone": <true|false>,
  "confidence": <0.0..1.0>,
  "people_visible": <integer>,
  "reasoning": "<one short sentence describing what you see>"
}}

Rules:
- Set person_in_zone = true ONLY if any human body part (head, torso, limbs)
  visibly overlaps the red polygon.
- A person standing next to the polygon but not inside it -> false.
- If you are unsure, lower the confidence; do not guess true.
"""


def _camera_config(serial: str) -> dict[str, Any] | None:
    for c in load_cameras():
        if c.get("serial") == serial:
            return c
    return None


async def run(serial: str, snapshot_path: str, trigger: str = "camera.snapshot") -> None:
    """Process a single snapshot."""
    cfg = _camera_config(serial)
    if not cfg:
        logger.debug("No camera config for %s; skipping zone_intrusion", serial)
        return

    zone = cfg.get("exclusion_zone")
    if not zone or not zone.get("polygon"):
        logger.debug("No exclusion_zone polygon for %s; skipping", serial)
        return

    cooldown = int(cfg.get("cooldown_seconds", 10))
    now = time.monotonic()
    last = _last_fired.get(serial, 0)
    if now - last < cooldown:
        logger.debug(
            "zone_intrusion cooldown active for %s (%.1fs remaining)",
            serial,
            cooldown - (now - last),
        )
        return
    _last_fired[serial] = now

    async with AgentRun(
        AGENT_NAME,
        trigger=trigger,
        inputs={"serial": serial, "snapshot_path": snapshot_path},
    ) as run_ctx:
        try:
            from pathlib import Path

            raw = Path(snapshot_path).read_bytes()
        except OSError as e:
            run_ctx.decision = f"failed to read snapshot: {e}"
            logger.error("zone_intrusion read failed: %s", e)
            return

        polygon = [tuple(p) for p in zone["polygon"]]
        label = zone.get("name", "Restricted")
        annotated = draw_zone_overlay(raw, polygon, label=label)  # type: ignore[arg-type]
        run_ctx.add_tool_call(
            "draw_zone_overlay",
            {"polygon_points": len(polygon), "label": label},
            {"bytes": len(annotated)},
        )

        model = get_active_model()
        try:
            result = await get_provider().analyse_image(
                annotated,
                _PROMPT.format(label=label),
                model_id=model,
            )
        except VLMError as e:
            run_ctx.decision = f"VLM error: {e}"
            logger.error("zone_intrusion VLM call failed: %s", e)
            return

        run_ctx.add_tool_call(
            "vlm.analyse_image",
            {"model": model, "prompt_len": len(_PROMPT)},
            {
                "latency_ms": result.latency_ms,
                "tokens": (result.prompt_tokens, result.completion_tokens),
                "parsed": result.parsed,
            },
        )

        verdict = result.parsed
        in_zone = bool(verdict.get("person_in_zone"))
        confidence = float(verdict.get("confidence", 0))
        reasoning = str(verdict.get("reasoning", ""))[:300]

        if in_zone and confidence >= 0.5:
            severity = IncidentSeverity.CRITICAL if confidence >= 0.85 else IncidentSeverity.HIGH
            incident = await create_incident(
                type=IncidentType.INTRUSION,
                severity=severity,
                summary=(f"Person detected inside '{label}' on camera {serial}. {reasoning}"),
                camera_id=serial,
                snapshot_path=snapshot_path,
                extra={
                    "vlm_model": model,
                    "vlm_confidence": confidence,
                    "vlm_raw": verdict,
                },
            )
            run_ctx.incident_id = incident.id
            run_ctx.decision = (
                f"INTRUSION confirmed (conf={confidence:.2f}) -> incident #{incident.id}"
            )
        else:
            run_ctx.decision = f"cleared: person_in_zone={in_zone}, conf={confidence:.2f}"


# ---- bus subscriber --------------------------------------------------------


async def _subscriber() -> None:
    q = bus.subscribe("camera.snapshot")
    logger.info("zone_intrusion subscriber started")
    while True:
        event = await q.get()
        serial = event.payload.get("serial")
        path = event.payload.get("path")
        if not serial or not path:
            continue
        try:
            await run(serial, path, trigger=event.payload.get("trigger", "camera.snapshot"))
        except Exception:  # noqa: BLE001
            logger.exception("zone_intrusion run failed for %s", serial)


async def _invoke_subscriber() -> None:
    """Allow manual triggering via POST /agents/zone_intrusion/invoke."""
    q = bus.subscribe("agent.invoke.zone_intrusion")
    while True:
        event = await q.get()
        serial = event.payload.get("serial")
        path = event.payload.get("snapshot_path")
        if not (serial and path):
            logger.warning("zone_intrusion invoke missing serial/snapshot_path: %s", event.payload)
            continue
        await run(serial, path, trigger="manual_invoke")


def start() -> None:
    """Spawn background subscribers. Call from FastAPI lifespan."""
    import asyncio

    if getattr(start, "_started", False):
        return
    asyncio.create_task(_subscriber())
    asyncio.create_task(_invoke_subscriber())
    start._started = True  # type: ignore[attr-defined]
