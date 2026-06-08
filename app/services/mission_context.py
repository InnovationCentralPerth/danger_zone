"""Mission context — natural-language PPE requirements for the PPE agent.

The user (or an external agent) describes in plain English what PPE workers
must wear; an LLM call parses that into a structured :class:`AgentContext`
which is then injected into the PPE compliance agent's VLM prompt at run time.

This is what makes danger_zone *agentic* rather than just a fancy YOLO wrapper:
the detection target is set at run time in natural language. No code change,
no model retraining, no YAML edit — and the system adapts immediately.

Scope is intentionally narrow:

* Global (one active context for all cameras).
* PPE compliance only — zone intrusion remains untouched.
* Multi-item supported (capped at :data:`MAX_ITEMS`).

State lives in a single :class:`~app.models.db.Setting` row under
:data:`SETTING_KEY` as a JSON blob. ``set_context`` publishes
``mission_context.changed`` on the bus so the dashboard refreshes live.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

from sqlmodel import Session, select

from app.db import get_engine
from app.integrations.vlm import VLMError, get_active_model, get_provider
from app.models.db import Setting
from app.runtime.bus import bus

logger = logging.getLogger(__name__)

SETTING_KEY = "mission_context.active"
MAX_ITEMS = 5
MAX_INPUT_CHARS = 500
MAX_ITEM_CHARS = 60
MAX_NOTE_CHARS = 120
VALID_SEVERITIES = ("low", "medium", "high")


# ---- data model ------------------------------------------------------------


@dataclass
class AgentContext:
    """Structured PPE requirements parsed from a natural-language instruction."""

    raw_input: str
    required_items: list[str] = field(default_factory=list)
    context_note: str = ""
    severity_hint: str = "medium"
    parsed_at: str = ""
    model_id: str = ""

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, raw: str) -> AgentContext:
        data = json.loads(raw)
        return cls(**data)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---- intent parsing --------------------------------------------------------


_SYSTEM_PROMPT = """\
You are a workplace safety analyst. Extract the required Personal Protective
Equipment (PPE) items from the user's instruction.

Respond ONLY with a single JSON object of this exact shape:

{
  "required_items": ["<item>", ...],
  "context_note": "<one short sentence, max 80 characters>",
  "severity_hint": "low" | "medium" | "low"
}

Rules:
- Each item in required_items must be a short, lowercase, visually-identifiable
  PPE item (e.g. "hard hat", "safety glasses", "hi-vis vest", "steel-toe boots",
  "anti-static wrist strap", "respirator mask", "cut-resistant gloves").
- Maximum 5 items.
- If the user describes a setting but names no specific items, infer the most
  common PPE for that setting (e.g. electronics lab -> safety glasses,
  anti-static wrist strap).
- If the instruction is empty, irrelevant, or unsafe, return required_items: [].
- severity_hint: "high" for life-safety gear (hard hat, fall arrest, respirator),
  "medium" for standard PPE (glasses, gloves, vest), "low" for advisory only.
"""


def _parse_user_input(text: str) -> str:
    text = (text or "").strip()
    if not text:
        raise ValueError("mission text is empty")
    if len(text) > MAX_INPUT_CHARS:
        raise ValueError(f"mission text exceeds {MAX_INPUT_CHARS} characters")
    return text


def _sanitise_parsed(parsed: dict[str, Any]) -> tuple[list[str], str, str]:
    """Validate + clamp the LLM output before persisting."""
    items_raw = parsed.get("required_items", [])
    if not isinstance(items_raw, list):
        items_raw = []
    items: list[str] = []
    seen: set[str] = set()
    for entry in items_raw:
        if not isinstance(entry, str):
            continue
        item = entry.strip().lower()[:MAX_ITEM_CHARS]
        if item and item not in seen:
            seen.add(item)
            items.append(item)
        if len(items) >= MAX_ITEMS:
            break

    note = str(parsed.get("context_note", "") or "").strip()[:MAX_NOTE_CHARS]

    sev = str(parsed.get("severity_hint", "medium") or "medium").strip().lower()
    if sev not in VALID_SEVERITIES:
        sev = "medium"

    return items, note, sev


# ---- persistence -----------------------------------------------------------


def _read_setting() -> str | None:
    with Session(get_engine()) as session:
        row = session.exec(select(Setting).where(Setting.key == SETTING_KEY)).first()
        return row.value if row and row.value else None


def _write_setting(value: str | None) -> None:
    with Session(get_engine()) as session:
        row = session.exec(select(Setting).where(Setting.key == SETTING_KEY)).first()
        if value is None:
            if row is not None:
                session.delete(row)
                session.commit()
            return
        if row is None:
            row = Setting(key=SETTING_KEY, value=value)
        else:
            row.value = value
        session.add(row)
        session.commit()


# ---- public API ------------------------------------------------------------


def get_context() -> AgentContext | None:
    """Return the active mission context, or None if unset."""
    raw = _read_setting()
    if not raw:
        return None
    try:
        return AgentContext.from_json(raw)
    except (json.JSONDecodeError, TypeError, ValueError) as e:
        logger.warning("mission_context: failed to decode persisted blob: %s", e)
        return None


async def set_context(natural_language: str) -> AgentContext:
    """Parse ``natural_language`` via the LLM and persist as the active context.

    Publishes ``mission_context.changed`` on the bus on success.
    Raises ``ValueError`` for invalid input, ``VLMError`` if the LLM call or
    JSON parsing fails.
    """
    raw_input = _parse_user_input(natural_language)
    model = get_active_model()

    try:
        result = await get_provider().complete(
            system_prompt=_SYSTEM_PROMPT,
            user_prompt=raw_input,
            model_id=model,
        )
    except VLMError:
        raise
    except Exception as e:  # noqa: BLE001 — defensive
        raise VLMError(f"mission_context parse failed: {e}") from e

    items, note, sev = _sanitise_parsed(result.parsed)
    if not items:
        raise VLMError(
            "Could not extract any PPE items from the instruction. "
            "Try naming the items explicitly (e.g. 'safety glasses and gloves')."
        )

    ctx = AgentContext(
        raw_input=raw_input,
        required_items=items,
        context_note=note,
        severity_hint=sev,
        parsed_at=datetime.utcnow().isoformat(),
        model_id=result.model_id,
    )
    _write_setting(ctx.to_json())

    await bus.publish(
        "mission_context.changed",
        {"active": True, "context": ctx.as_dict()},
    )
    logger.info(
        "mission_context: set %d item(s) via %s (%dms) — %s",
        len(items),
        result.model_id,
        result.latency_ms,
        ", ".join(items),
    )
    return ctx


async def clear_context() -> None:
    """Drop the active mission context (PPE agent reverts to YAML default)."""
    _write_setting(None)
    await bus.publish("mission_context.changed", {"active": False, "context": None})
    logger.info("mission_context: cleared")
