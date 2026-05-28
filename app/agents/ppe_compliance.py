"""Agent B — PPE Compliance.

Triggered by ``camera.snapshot`` bus events. Loads the camera's PPE area
polygon from config, draws it on the snapshot in yellow, and asks the VLM
whether any person standing inside the polygon is missing a hard hat.

Files a PPE_VIOLATION incident when a violation is detected. Cooldown is
enforced per-camera, independently of the zone_intrusion agent.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

from app.integrations.vlm import VLMError, get_active_model, get_provider
from app.models.db import IncidentSeverity, IncidentType
from app.runtime.bus import bus
from app.runtime.trace import AgentRun
from app.services.config_loader import load_cameras
from app.services.incidents import create_incident
from app.services.zone import draw_zone_overlay

logger = logging.getLogger(__name__)

AGENT_NAME = "ppe_compliance"

# Per-camera last-fired timestamps for cooldown enforcement.
_last_fired: dict[str, float] = {}

# Yellow overlay so the VLM (and humans reviewing snapshots) can tell the
# PPE area apart from the red exclusion zone used by zone_intrusion.
_PPE_OUTLINE = (255, 210, 0, 230)
_PPE_FILL = (255, 210, 0, 50)


_PROMPT = """\
You are reviewing a still frame from a construction-site security camera.
The image has a yellow polygon drawn on it labelled "{label}" — this is a
MANDATORY HARD HAT AREA.

Answer ONLY with a JSON object of this exact shape:

{{
  "people_in_area": <integer>,
  "people_without_hardhat": <integer>,
  "violation": <true|false>,
  "confidence": <0.0..1.0>,
  "reasoning": "<one short sentence describing what you see>"
}}

Rules:
- Only count people whose body is at least partially INSIDE the yellow polygon.
- A "hard hat" is a rigid industrial helmet (any solid colour). Beanies,
  caps, hoods, and hair are NOT hard hats.
- Set violation = true ONLY if people_without_hardhat >= 1.
- If no people are inside the polygon, set violation = false and confidence high.
- If you are unsure whether someone is wearing a hard hat, lower the confidence
  rather than guessing true.
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
        logger.debug("No camera config for %s; skipping ppe_compliance", serial)
        return

    area = cfg.get("ppe_area")
    if not area or not area.get("polygon"):
        logger.debug("No ppe_area polygon for %s; skipping", serial)
        return

    cooldown = int(cfg.get("cooldown_seconds", 10))
    now = time.monotonic()
    last = _last_fired.get(serial, 0)
    if now - last < cooldown:
        logger.debug(
            "ppe_compliance cooldown active for %s (%.1fs remaining)",
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
            raw = Path(snapshot_path).read_bytes()
        except OSError as e:
            run_ctx.decision = f"failed to read snapshot: {e}"
            logger.error("ppe_compliance read failed: %s", e)
            return

        polygon = [tuple(p) for p in area["polygon"]]
        label = area.get("name", "Hard Hat Area")
        annotated = draw_zone_overlay(
            raw,
            polygon,  # type: ignore[arg-type]
            label=label,
            outline=_PPE_OUTLINE,
            fill=_PPE_FILL,
        )
        run_ctx.add_tool_call(
            "draw_zone_overlay",
            {"polygon_points": len(polygon), "label": label, "colour": "yellow"},
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
            logger.error("ppe_compliance VLM call failed: %s", e)
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
        violation = bool(verdict.get("violation"))
        confidence = float(verdict.get("confidence", 0))
        without_hardhat = int(verdict.get("people_without_hardhat", 0))
        in_area = int(verdict.get("people_in_area", 0))
        reasoning = str(verdict.get("reasoning", ""))[:300]

        if violation and confidence >= 0.5 and without_hardhat >= 1:
            severity = IncidentSeverity.HIGH if confidence >= 0.85 else IncidentSeverity.MEDIUM
            incident = await create_incident(
                type=IncidentType.PPE_VIOLATION,
                severity=severity,
                summary=(
                    f"PPE violation in '{label}' on camera {serial}: "
                    f"{without_hardhat}/{in_area} without hard hat. {reasoning}"
                ),
                camera_id=serial,
                snapshot_path=snapshot_path,
                extra={
                    "vlm_model": model,
                    "vlm_confidence": confidence,
                    "people_in_area": in_area,
                    "people_without_hardhat": without_hardhat,
                    "vlm_raw": verdict,
                },
            )
            run_ctx.incident_id = incident.id
            run_ctx.decision = (
                f"PPE_VIOLATION confirmed (conf={confidence:.2f}, "
                f"{without_hardhat}/{in_area}) -> incident #{incident.id}"
            )
        else:
            run_ctx.decision = (
                f"cleared: violation={violation}, conf={confidence:.2f}, "
                f"in_area={in_area}, without_hardhat={without_hardhat}"
            )


# ---- bus subscriber --------------------------------------------------------


async def _subscriber() -> None:
    q = bus.subscribe("camera.snapshot")
    logger.info("ppe_compliance subscriber started")
    while True:
        event = await q.get()
        serial = event.payload.get("serial")
        path = event.payload.get("path")
        if not serial or not path:
            continue
        try:
            await run(serial, path, trigger=event.payload.get("trigger", "camera.snapshot"))
        except Exception:  # noqa: BLE001
            logger.exception("ppe_compliance run failed for %s", serial)


async def _invoke_subscriber() -> None:
    """Allow manual triggering via POST /agents/ppe_compliance/invoke."""
    q = bus.subscribe("agent.invoke.ppe_compliance")
    while True:
        event = await q.get()
        serial = event.payload.get("serial")
        path = event.payload.get("snapshot_path")
        if not (serial and path):
            logger.warning("ppe_compliance invoke missing serial/snapshot_path: %s", event.payload)
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
