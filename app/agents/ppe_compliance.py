"""Agent B — PPE Compliance.

Triggered by ``camera.snapshot`` bus events. Loads the camera's PPE area
polygon from config, draws it on the snapshot in yellow, and asks the VLM
whether any person standing inside is missing the required PPE item.

The required item is configurable per-camera via
``ppe_area.required_item`` (``hard_hat`` | ``hi_vis_vest``).

Files a PPE_VIOLATION incident when a violation is detected. Cooldown is
enforced per-camera, independently of the zone_intrusion agent. Skipped
entirely when the agent is in passive mode.
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
from app.services.agent_mode import is_active
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


# Per-item config: human label, the JSON key used for the missing-count,
# what the VLM should look for, and what definitely is NOT the item.
_ITEMS: dict[str, dict[str, str]] = {
    "hard_hat": {
        "label": "hard hat",
        "missing_key": "people_without_hardhat",
        "definition": (
            "A 'hard hat' is a rigid industrial safety helmet (any solid colour). "
            "Beanies, caps, hoods, and hair are NOT hard hats."
        ),
        "area_label_default": "Hard Hat Area",
    },
    "hi_vis_vest": {
        "label": "hi-vis vest",
        "missing_key": "people_without_hivis",
        "definition": (
            "A 'hi-vis vest' is a high-visibility safety vest in fluorescent "
            "yellow, orange, or lime — usually with reflective silver stripes. "
            "Regular brightly-coloured clothing without reflective stripes is "
            "NOT a hi-vis vest."
        ),
        "area_label_default": "Hi-Vis Area",
    },
}

_DEFAULT_ITEM = "hard_hat"


def _build_prompt(item_key: str, area_label: str) -> tuple[str, str]:
    """Return (prompt, missing_key) for the configured PPE item."""
    item = _ITEMS.get(item_key, _ITEMS[_DEFAULT_ITEM])
    missing_key = item["missing_key"]
    prompt = f"""\
You are reviewing a still frame from a worksite security camera.
The image has a yellow polygon drawn on it labelled "{area_label}" — this is a
MANDATORY {item["label"].upper()} AREA.

Answer ONLY with a JSON object of this exact shape:

{{
  "people_in_area": <integer>,
  "{missing_key}": <integer>,
  "violation": <true|false>,
  "confidence": <0.0..1.0>,
  "reasoning": "<one short sentence describing what you see>"
}}

Rules:
- Only count people whose body is at least partially INSIDE the yellow polygon.
- {item["definition"]}
- Set violation = true ONLY if {missing_key} >= 1.
- If no people are inside the polygon, set violation = false and confidence high.
- If you are unsure whether someone is wearing a {item["label"]}, lower the
  confidence rather than guessing true.
"""
    return prompt, missing_key


def _camera_config(serial: str) -> dict[str, Any] | None:
    for c in load_cameras():
        if c.get("serial") == serial:
            return c
    return None


async def run(serial: str, snapshot_path: str, trigger: str = "camera.snapshot") -> None:
    """Process a single snapshot."""
    if not is_active(AGENT_NAME):
        logger.debug("ppe_compliance is passive; skipping %s", serial)
        return

    cfg = _camera_config(serial)
    if not cfg:
        logger.debug("No camera config for %s; skipping ppe_compliance", serial)
        return

    area = cfg.get("ppe_area")
    if not area or not area.get("polygon"):
        logger.debug("No ppe_area polygon for %s; skipping", serial)
        return

    item_key = str(area.get("required_item", _DEFAULT_ITEM))
    if item_key not in _ITEMS:
        logger.warning(
            "Unknown ppe_area.required_item=%r for %s; defaulting to %s",
            item_key,
            serial,
            _DEFAULT_ITEM,
        )
        item_key = _DEFAULT_ITEM
    item_label = _ITEMS[item_key]["label"]

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
        inputs={"serial": serial, "snapshot_path": snapshot_path, "required_item": item_key},
    ) as run_ctx:
        try:
            raw = Path(snapshot_path).read_bytes()
        except OSError as e:
            run_ctx.decision = f"failed to read snapshot: {e}"
            logger.error("ppe_compliance read failed: %s", e)
            return

        polygon = [tuple(p) for p in area["polygon"]]
        area_label = area.get("name", _ITEMS[item_key]["area_label_default"])
        annotated = draw_zone_overlay(
            raw,
            polygon,  # type: ignore[arg-type]
            label=area_label,
            outline=_PPE_OUTLINE,
            fill=_PPE_FILL,
        )
        run_ctx.add_tool_call(
            "draw_zone_overlay",
            {"polygon_points": len(polygon), "label": area_label, "colour": "yellow"},
            {"bytes": len(annotated)},
        )

        prompt, missing_key = _build_prompt(item_key, area_label)

        model = get_active_model()
        try:
            result = await get_provider().analyse_image(annotated, prompt, model_id=model)
        except VLMError as e:
            run_ctx.decision = f"VLM error: {e}"
            logger.error("ppe_compliance VLM call failed: %s", e)
            return

        run_ctx.add_tool_call(
            "vlm.analyse_image",
            {"model": model, "required_item": item_key},
            {
                "latency_ms": result.latency_ms,
                "tokens": (result.prompt_tokens, result.completion_tokens),
                "parsed": result.parsed,
            },
        )

        verdict = result.parsed
        violation = bool(verdict.get("violation"))
        confidence = float(verdict.get("confidence", 0))
        missing = int(verdict.get(missing_key, 0))
        in_area = int(verdict.get("people_in_area", 0))
        reasoning = str(verdict.get("reasoning", ""))[:300]

        if violation and confidence >= 0.5 and missing >= 1:
            severity = IncidentSeverity.HIGH if confidence >= 0.85 else IncidentSeverity.MEDIUM
            incident = await create_incident(
                type=IncidentType.PPE_VIOLATION,
                severity=severity,
                summary=(
                    f"PPE violation in '{area_label}' on camera {serial}: "
                    f"{missing}/{in_area} without {item_label}. {reasoning}"
                ),
                camera_id=serial,
                snapshot_path=snapshot_path,
                extra={
                    "vlm_model": model,
                    "vlm_confidence": confidence,
                    "required_item": item_key,
                    "people_in_area": in_area,
                    "people_missing_item": missing,
                    "vlm_raw": verdict,
                },
            )
            run_ctx.incident_id = incident.id
            run_ctx.decision = (
                f"PPE_VIOLATION ({item_label}) confirmed "
                f"(conf={confidence:.2f}, {missing}/{in_area}) -> incident #{incident.id}"
            )
        elif in_area >= 1 and missing == 0 and confidence >= 0.5:
            # Person present + correct PPE -> small compliance note (INFO).
            incident = await create_incident(
                type=IncidentType.PPE_OK,
                severity=IncidentSeverity.INFO,
                summary=(
                    f"PPE OK in '{area_label}' on camera {serial}: "
                    f"{in_area} person(s) wearing {item_label}. {reasoning}"
                ),
                camera_id=serial,
                snapshot_path=snapshot_path,
                extra={
                    "vlm_model": model,
                    "vlm_confidence": confidence,
                    "required_item": item_key,
                    "people_in_area": in_area,
                    "vlm_raw": verdict,
                },
            )
            run_ctx.incident_id = incident.id
            run_ctx.decision = (
                f"PPE_OK ({item_label}): {in_area} person(s) compliant "
                f"(conf={confidence:.2f}) -> incident #{incident.id}"
            )
        else:
            # Either nobody in area, or low confidence — trace only, no incident.
            run_ctx.decision = (
                f"silent ({item_label}): violation={violation}, conf={confidence:.2f}, "
                f"in_area={in_area}, missing={missing}"
            )


# ---- bus subscriber --------------------------------------------------------


async def _subscriber() -> None:
    # Import here to avoid circular import at module-load time.
    from app.agents.zone_intrusion import _coalesce_latest_by_serial

    q = bus.subscribe("camera.snapshot")
    logger.info("ppe_compliance subscriber started")
    while True:
        event = await q.get()
        latest = await _coalesce_latest_by_serial(q, event)
        for ev in latest.values():
            serial = ev.payload.get("serial")
            path = ev.payload.get("path")
            if not serial or not path:
                continue
            try:
                await run(serial, path, trigger=ev.payload.get("trigger", "camera.snapshot"))
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
