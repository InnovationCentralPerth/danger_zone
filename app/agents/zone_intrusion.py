"""Agent A — Zone Intrusion.

Triggered by ``camera.snapshot`` bus events (which fire after motion-driven
or manually-requested snapshots). Loads the camera's exclusion polygon from
config, draws it on the snapshot, asks the VLM whether a person is inside,
and files an INTRUSION incident if confirmed.

Cooldown is enforced per-camera so a burst of motion only generates one
VLM call and one incident.

When the active mission context carries natural-language zone rules (e.g.
"nobody within arm's reach of the bench") the agent judges those rules on the
clean frame instead of using the camera's polygon. The rules are the user's
own wording, so they work on any camera without per-camera setup.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from app.integrations.vlm import VLMError, get_active_model, get_provider
from app.models.db import IncidentSeverity, IncidentType
from app.runtime.bus import BusEvent, bus
from app.runtime.trace import AgentRun
from app.services.agent_mode import is_active
from app.services.config_loader import get_camera_config
from app.services.incidents import create_incident
from app.services.mission_context import AgentContext, get_context
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


_RULES_PROMPT = """\
You are reviewing a still frame from a security camera.
A safety officer has set these ZONE RULES in plain English:

{rules}

For each rule decide whether anyone visible in the frame is violating it right now.

Answer ONLY with a JSON object of this exact shape:

{{
  "people_visible": <integer>,
  "rules": [
    {{
      "rule_index": <1-based number of the rule above>,
      "violated": <true|false>,
      "confidence": <0.0..1.0>,
      "people_involved": <integer>,
      "reasoning": "<one short sentence describing what you see>"
    }}
  ]
}}

Include exactly one entry per rule.

Guidance:
- Judge only what is visible. If nobody is visible, every rule is violated = false.
- Only real people standing in the room count. People shown on monitors, screens,
  posters or reflections do NOT count.
- If the rule is ambiguous for what you see, or the person is borderline, lower
  the confidence; do not guess true.
"""


def _format_rules(rules: list[str]) -> str:
    return "\n".join(f"{i}. {rule}" for i, rule in enumerate(rules, start=1))


def _parse_rule_verdicts(parsed: dict[str, Any], rules: list[str]) -> list[dict[str, Any]]:
    """Normalise the VLM output to one verdict per rule, in rule order."""
    by_index: dict[int, dict[str, Any]] = {}
    entries = parsed.get("rules", [])
    if isinstance(entries, list):
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            try:
                idx = int(entry.get("rule_index"))  # type: ignore[arg-type]
            except (TypeError, ValueError):
                continue
            if 1 <= idx <= len(rules) and idx not in by_index:
                by_index[idx] = entry

    verdicts: list[dict[str, Any]] = []
    for i, rule in enumerate(rules, start=1):
        entry = by_index.get(i, {})
        try:
            confidence = max(0.0, min(1.0, float(entry.get("confidence", 0))))
        except (TypeError, ValueError):
            confidence = 0.0
        try:
            involved = max(0, int(entry.get("people_involved", 0)))
        except (TypeError, ValueError):
            involved = 0
        verdicts.append(
            {
                "rule": rule,
                "violated": bool(entry.get("violated", False)),
                "confidence": confidence,
                "people_involved": involved,
                "reasoning": str(entry.get("reasoning", ""))[:300],
            }
        )
    return verdicts


def _cooldown_ready(serial: str, cfg: dict[str, Any]) -> bool:
    cooldown = int(cfg.get("cooldown_seconds", 10))
    now = time.monotonic()
    last = _last_fired.get(serial, 0)
    if now - last < cooldown:
        logger.debug(
            "zone_intrusion cooldown active for %s (%.1fs remaining)",
            serial,
            cooldown - (now - last),
        )
        return False
    _last_fired[serial] = now
    return True


async def _run_rules(
    serial: str, snapshot_path: str, trigger: str, ctx: AgentContext, cfg: dict[str, Any]
) -> None:
    """Judge the mission's natural-language zone rules on a clean frame."""
    if not _cooldown_ready(serial, cfg):
        return

    rules = ctx.zone_rules
    async with AgentRun(
        AGENT_NAME,
        trigger=trigger,
        inputs={
            "serial": serial,
            "snapshot_path": snapshot_path,
            "mode": "zone_rules",
            "zone_rules": rules,
            "mission": ctx.raw_input,
        },
    ) as run_ctx:
        try:
            from pathlib import Path

            raw = Path(snapshot_path).read_bytes()
        except OSError as e:
            run_ctx.decision = f"failed to read snapshot: {e}"
            logger.error("zone_intrusion read failed: %s", e)
            return

        model = get_active_model()
        prompt = _RULES_PROMPT.format(rules=_format_rules(rules))
        try:
            result = await get_provider().analyse_image(raw, prompt, model_id=model)
        except VLMError as e:
            run_ctx.decision = f"VLM error: {e}"
            logger.error("zone_intrusion VLM call failed: %s", e)
            return

        run_ctx.add_tool_call(
            "vlm.analyse_image",
            {"model": model, "zone_rules": rules, "mode": "zone_rules"},
            {
                "latency_ms": result.latency_ms,
                "tokens": (result.prompt_tokens, result.completion_tokens),
                "parsed": result.parsed,
            },
        )

        verdicts = _parse_rule_verdicts(result.parsed, rules)
        violated = [v for v in verdicts if v["violated"] and v["confidence"] >= 0.5]
        if not violated:
            run_ctx.decision = "cleared: " + "; ".join(
                f"rule {i} ok (conf={v['confidence']:.2f})" for i, v in enumerate(verdicts, 1)
            )
            return

        last_id: int | None = None
        for v in violated:
            severity = (
                IncidentSeverity.CRITICAL if v["confidence"] >= 0.85 else IncidentSeverity.HIGH
            )
            incident = await create_incident(
                type=IncidentType.INTRUSION,
                severity=severity,
                summary=(f'Zone rule violated on camera {serial}: "{v["rule"]}". {v["reasoning"]}'),
                camera_id=serial,
                snapshot_path=snapshot_path,
                extra={
                    "vlm_model": model,
                    "vlm_confidence": v["confidence"],
                    "mission": ctx.raw_input,
                    "zone_rule": v["rule"],
                    "people_involved": v["people_involved"],
                    "vlm_raw": result.parsed,
                },
            )
            last_id = incident.id
        run_ctx.incident_id = last_id
        run_ctx.decision = f"ZONE RULE violated ({len(violated)}): " + "; ".join(
            f'"{v["rule"]}" (conf={v["confidence"]:.2f})' for v in violated
        )


def _camera_config(serial: str) -> dict[str, Any]:
    """Resolve per-camera config, falling back to full-frame defaults.

    Meraki-discovered cameras with no entry in ``cameras.yaml`` get a
    synthetic full-frame polygon so the agent still has something to draw
    and reason about.
    """
    return get_camera_config(serial)


async def run(serial: str, snapshot_path: str, trigger: str = "camera.snapshot") -> None:
    """Process a single snapshot."""
    if not is_active(AGENT_NAME):
        logger.debug("zone_intrusion is passive; skipping %s", serial)
        return

    cfg = _camera_config(serial)

    ctx = get_context()
    if ctx is not None and ctx.zone_rules:
        await _run_rules(serial, snapshot_path, trigger, ctx, cfg)
        return

    zone = cfg.get("exclusion_zone")
    if not zone or not zone.get("polygon"):
        logger.debug("No exclusion_zone polygon for %s; skipping", serial)
        return

    if not _cooldown_ready(serial, cfg):
        return

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


async def _coalesce_latest_by_serial(
    q: asyncio.Queue[BusEvent], first: BusEvent
) -> dict[str, BusEvent]:
    """Drain ``q`` and return the most-recent event per ``serial``.

    Used by VLM-backed subscribers to collapse a backlog of snapshots that
    accumulated while a previous call was in flight. Keeps the dashboard
    responsive without firing one VLM call per queued event.
    """
    latest: dict[str, BusEvent] = {}
    s = first.payload.get("serial")
    if s:
        latest[s] = first
    while True:
        try:
            nxt = q.get_nowait()
        except asyncio.QueueEmpty:
            break
        ns = nxt.payload.get("serial")
        if ns:
            latest[ns] = nxt
    return latest


async def _subscriber() -> None:
    q = bus.subscribe("camera.snapshot")
    logger.info("zone_intrusion subscriber started")
    while True:
        event = await q.get()
        latest = await _coalesce_latest_by_serial(q, event)
        if len(latest) == 0:
            continue
        for ev in latest.values():
            serial = ev.payload.get("serial")
            path = ev.payload.get("path")
            if not serial or not path:
                continue
            try:
                await run(serial, path, trigger=ev.payload.get("trigger", "camera.snapshot"))
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
