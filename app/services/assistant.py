"""Ask Safety Sentry — natural-language questions about what the system has seen.

Design rules (this is shown to customers, so it must never make numbers up):

1. The model **never writes SQL or does arithmetic**. Step 1 turns the question
   into a small structured :class:`QueryPlan` (validated against fixed menus).
2. Our code runs fixed, read-only queries and groups raw incidents into *events*
   (see :mod:`app.services.events`) so a person standing still is one event, not
   eight rows.
3. Step 2 asks the model to phrase 1–2 sentences from the **facts only**. Every
   number in that sentence must appear in the facts, otherwise a deterministic
   template sentence is used instead.

Everything runs against the local model (``get_provider``), so no frame or
question leaves the site.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlmodel import Session, select

from app.config import get_settings
from app.db import get_engine
from app.integrations.vlm import VLMError, get_active_model, get_provider
from app.models.db import Incident
from app.services import mission_context
from app.services.config_loader import load_cameras
from app.services.events import Event, canonical_item, group_events, incident_row

logger = logging.getLogger(__name__)

MAX_QUESTION_CHARS = 300
MAX_ROWS = 60_000
EVIDENCE_LIMIT = 3

INTENTS = ("count", "list", "status", "retask", "unknown")
EVENT_TYPES = ("violation", "compliant", "zone", "any")
WINDOWS = ("mission", "today", "hour", "all")
GROUP_BYS = ("none", "camera", "item")

_TYPE_MAP = {
    "violation": ["PPE_VIOLATION"],
    "compliant": ["PPE_OK"],
    "zone": ["INTRUSION"],
    "any": ["PPE_VIOLATION", "INTRUSION"],  # alerts; compliance notes are not "incidents"
}

# SQL LIKE patterns to find out whether the PPE agent was *checking* an item.
_COVERAGE_LIKE = {
    "hard hat": ["%hard hat%", "%hard_hat%", "%hardhat%", "%helmet%"],
    "hi-vis vest": ["%vest%", "%hi_vis%", "%hi-vis%"],
    "safety goggles": ["%goggle%"],
    "safety glasses": ["%glasses%"],
    "gloves": ["%glove%"],
}


# ---- plan --------------------------------------------------------------------


@dataclass
class QueryPlan:
    intent: str = "unknown"
    event_type: str = "violation"
    item: str | None = None
    camera: str | None = None
    window: str = "mission"
    group_by: str = "none"
    keyword: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "intent": self.intent,
            "event_type": self.event_type,
            "item": self.item,
            "camera": self.camera,
            "window": self.window,
            "group_by": self.group_by,
            "keyword": self.keyword,
        }


def _pick(value: Any, allowed: tuple[str, ...], default: str) -> str:
    v = str(value or "").strip().lower()
    return v if v in allowed else default


def _opt_text(value: Any, limit: int = 60) -> str | None:
    v = str(value or "").strip()
    return v[:limit] if v and v.lower() not in {"null", "none", "n/a"} else None


def validate_plan(raw: dict[str, Any] | None) -> QueryPlan:
    """Coerce whatever the model returned into a safe :class:`QueryPlan`."""
    raw = raw if isinstance(raw, dict) else {}
    item = _opt_text(raw.get("item"))
    return QueryPlan(
        intent=_pick(raw.get("intent"), INTENTS, "unknown"),
        event_type=_pick(raw.get("event_type"), EVENT_TYPES, "violation"),
        item=canonical_item(item) if item else None,
        camera=_opt_text(raw.get("camera")),
        window=_pick(raw.get("window"), WINDOWS, "mission"),
        group_by=_pick(raw.get("group_by"), GROUP_BYS, "none"),
        keyword=(_opt_text(raw.get("keyword"), 40) or "").lower() or None,
    )


_PLAN_SYSTEM = """\
You translate a question about a workplace-safety camera system into a JSON query.
Respond with ONE JSON object and nothing else:

{
  "intent": "count" | "list" | "status" | "retask" | "unknown",
  "event_type": "violation" | "compliant" | "zone" | "any",
  "item": "<one PPE item, e.g. hard hat, hi-vis vest, safety goggles>" | null,
  "camera": "<one of the camera names below>" | null,
  "window": "mission" | "today" | "hour" | "all",
  "group_by": "none" | "camera" | "item",
  "keyword": "<one word from a zone rule, e.g. bench, doorway, walkway>" | null
}

Meaning:
- count: how many / are there any / did anyone ...   list: show / what were the latest ...
- status: what are you watching for / what is the current mission.
- retask: the user asks you to START watching for something ("watch for hard hats").
- event_type: violation = someone missing PPE; compliant = someone wearing the right PPE;
  zone = someone in a restricted area; any = any alert (violation or zone).
- item: normalise ("helmet" -> "hard hat", "hi-vis" -> "hi-vis vest", "goggles" -> "safety goggles").
- window: "mission" is the DEFAULT (since the current mission was set). Use "today" only if the user
  says today, "hour" only for the last hour, and "all" only if they say ever / all time / in total.
- camera: set it ONLY if the user names a camera or area that matches one of the camera names below.
  Objects like "the bench" are NOT cameras; put those in keyword instead.
- keyword: for zone questions, the object or area they mention (bench, doorway, walkway), else null.
- group_by: camera for "which camera ...", item for "which PPE item ...", otherwise none.
- Use "unknown" for anything not about this system's observations.

Cameras: {cameras}
"""

_PLAN_EXAMPLES = """\
Q: how many PPE violations have we seen?
A: {"intent":"count","event_type":"violation","item":null,"camera":null,"window":"mission","group_by":"none","keyword":null}
Q: how many PPE violations did we see today?
A: {"intent":"count","event_type":"violation","item":null,"camera":null,"window":"today","group_by":"none","keyword":null}
Q: are there any people not wearing helmets
A: {"intent":"count","event_type":"violation","item":"hard hat","camera":null,"window":"mission","group_by":"none","keyword":null}
Q: which camera had the most violations in the last hour
A: {"intent":"count","event_type":"violation","item":null,"camera":null,"window":"hour","group_by":"camera","keyword":null}
Q: did anyone go near the bench
A: {"intent":"count","event_type":"zone","item":null,"camera":null,"window":"mission","group_by":"none","keyword":"bench"}
Q: any restricted-area breaches at the workshop
A: {"intent":"count","event_type":"zone","item":null,"camera":"workshop","window":"mission","group_by":"none","keyword":null}
Q: show me the latest goggle problems
A: {"intent":"list","event_type":"violation","item":"safety goggles","camera":null,"window":"mission","group_by":"none","keyword":null}
Q: what are you watching for right now?
A: {"intent":"status","event_type":"any","item":null,"camera":null,"window":"mission","group_by":"none","keyword":null}
Q: start watching for hard hats
A: {"intent":"retask","event_type":"violation","item":"hard hat","camera":null,"window":"mission","group_by":"none","keyword":null}
Q: what's the weather in perth
A: {"intent":"unknown","event_type":"any","item":null,"camera":null,"window":"mission","group_by":"none","keyword":null}
"""


# ---- cameras / time ------------------------------------------------------------


def _cameras() -> list[dict[str, Any]]:
    try:
        return [c for c in load_cameras() if isinstance(c, dict) and c.get("serial")]
    except Exception:  # noqa: BLE001 - assistant must still work without YAML
        return []


def camera_label(serial: str | None, cams: list[dict[str, Any]] | None = None) -> str:
    for c in cams if cams is not None else _cameras():
        if c.get("serial") == serial:
            return str(c.get("name") or serial)
    return serial or "unknown camera"


def resolve_camera(
    fragment: str | None, cams: list[dict[str, Any]]
) -> tuple[list[str] | None, bool]:
    """Camera fragment -> matching serials. Returns ``(serials | None, found)``."""
    if not fragment:
        return None, True
    frag = fragment.lower()
    hits = [
        str(c["serial"])
        for c in cams
        if frag in str(c.get("name", "")).lower()
        or frag in str(c.get("location", "")).lower()
        or frag in str(c["serial"]).lower()
    ]
    return (hits, True) if hits else (None, False)


def _tz() -> ZoneInfo:
    try:
        return ZoneInfo(get_settings().site_timezone)
    except Exception:  # noqa: BLE001
        return ZoneInfo("UTC")


def _local(dt: datetime) -> datetime:
    return dt.replace(tzinfo=UTC).astimezone(_tz())


def _clock(dt: datetime) -> str:
    return _local(dt).strftime("%H:%M")


def window_bounds(
    window: str,
    now: datetime,
    mission_since: datetime | None,
    cleared_since: datetime | None,
) -> tuple[datetime, datetime, str]:
    """Resolve a window name to naive-UTC ``(start, end, label)``."""
    if window == "all":
        return datetime(2000, 1, 1), now, "across everything on record"
    if window == "hour":
        return now - timedelta(hours=1), now, "in the last hour"
    if window == "today":
        local_midnight = _local(now).replace(hour=0, minute=0, second=0, microsecond=0)
        start = local_midnight.astimezone(UTC).replace(tzinfo=None)
        return start, now, f"today (since midnight {get_settings().site_name.split()[-1]} time)"
    # "mission": since the later of the last mission change and the last feed clear
    candidates = [
        (d, kind) for d, kind in ((mission_since, "mission"), (cleared_since, "clear")) if d
    ]
    if not candidates:
        local_midnight = _local(now).replace(hour=0, minute=0, second=0, microsecond=0)
        return local_midnight.astimezone(UTC).replace(tzinfo=None), now, "today"
    start, kind = max(candidates, key=lambda c: c[0])
    why = "you set the current mission" if kind == "mission" else "the feed was cleared"
    return start, now, f"since {why} at {_clock(start)}"


# ---- queries --------------------------------------------------------------------


def _coverage(session: Session, item: str, start: datetime, end: datetime) -> int:
    """How many PPE-agent runs in the window were checking ``item``."""
    likes = _COVERAGE_LIKE.get(item, [f"%{item.replace(' ', '%')}%"])
    clause = " or ".join(f"inputs_json like :p{i}" for i in range(len(likes)))
    params: dict[str, Any] = {f"p{i}": v for i, v in enumerate(likes)}
    params.update(s=start, e=end)
    row = session.execute(
        text(
            "select count(*) from agenttrace where agent = 'ppe_compliance' "
            f"and created_at >= :s and created_at < :e and ({clause})"
        ),
        params,
    ).first()
    return int(row[0]) if row else 0


def _cutoff_time(session: Session, incident_id: int | None) -> datetime | None:
    if not incident_id:
        return None
    row = session.get(Incident, incident_id)
    return row.created_at if row else None


_SERIAL_RE = re.compile(r" on camera [A-Z0-9]{4}(?:-[A-Z0-9]{4}){2}")


def _short(summary: str, limit: int = 140) -> str:
    s = _SERIAL_RE.sub("", summary or "")
    return s if len(s) <= limit else s[: limit - 1] + "…"


def _evidence(events: list[Event], cams: list[dict[str, Any]]) -> list[dict[str, Any]]:
    replay = {str(c["serial"]) for c in cams if (c.get("source") or {}).get("type") == "file"}
    out: list[dict[str, Any]] = []
    for ev in events[:EVIDENCE_LIMIT]:
        inc = ev.representative()
        pos = inc["extra"].get("replay_position")
        out.append(
            {
                "incident_id": inc["id"],
                "camera_id": ev.camera_id,
                "camera": camera_label(ev.camera_id, cams),
                "time": _local(ev.start).strftime("%H:%M:%S"),
                "type": ev.type,
                "summary": _short(inc["summary"]),
                "items": sorted(ev.items),
                "samples": ev.count,
                "seconds": round(ev.duration_s),
                "has_snapshot": bool(inc.get("snapshot_path")),
                "replay_position": pos
                if isinstance(pos, (int, float)) and ev.camera_id in replay
                else None,
            }
        )
    return out


def build_facts(
    plan: QueryPlan, feed_cleared_after_id: int | None = None, now: datetime | None = None
) -> dict[str, Any]:
    """Run the fixed queries for ``plan`` and return the facts (and evidence)."""
    now = now or datetime.utcnow()
    cams = _cameras()
    ctx = mission_context.get_context()
    mission_since: datetime | None = None
    if ctx and ctx.parsed_at:
        try:
            mission_since = datetime.fromisoformat(ctx.parsed_at)
        except ValueError:
            mission_since = None

    facts: dict[str, Any] = {
        "intent": plan.intent,
        "current_mission": ctx.raw_input if ctx else None,
        "current_items": list(ctx.required_items) if ctx else [],
        "current_zone_rules": list(ctx.zone_rules) if ctx else [],
        "replay_footage": any((c.get("source") or {}).get("type") == "file" for c in cams),
    }
    if plan.intent in ("status", "retask", "unknown"):
        facts["item"] = plan.item
        return facts

    serials, camera_found = resolve_camera(plan.camera, cams)
    facts["camera_filter"] = (
        camera_label(serials[0], cams) if serials and len(serials) == 1 else None
    )
    facts["camera_not_found"] = None if camera_found else plan.camera

    with Session(get_engine()) as session:
        start, end, label = window_bounds(
            plan.window, now, mission_since, _cutoff_time(session, feed_cleared_after_id)
        )
        stmt = select(Incident).where(
            Incident.created_at >= start,
            Incident.created_at < end,
            Incident.type.in_(_TYPE_MAP[plan.event_type]),  # type: ignore[attr-defined]
        )
        if plan.window == "mission" and feed_cleared_after_id:
            # Same rule as the dashboard's Clear feed: anything up to that id is "gone".
            stmt = stmt.where(Incident.id > feed_cleared_after_id)  # type: ignore[operator]
        # Talk about the cameras that are configured now, not leftovers from old demos.
        scope = serials or [str(c["serial"]) for c in cams]
        if scope:
            stmt = stmt.where(Incident.camera_id.in_(scope))  # type: ignore[union-attr]
        rows = [incident_row(r) for r in session.exec(stmt.limit(MAX_ROWS)).all()]
        if plan.item:
            want = canonical_item(plan.item)
            from app.services.events import incident_items

            rows = [r for r in rows if want in incident_items(r)]
        if plan.keyword:
            kw = plan.keyword
            rows = [
                r
                for r in rows
                if kw in str(r["extra"].get("zone_rule") or "").lower()
                or kw in r["summary"].lower()
            ]
        checked = _coverage(session, plan.item, start, end) if plan.item else None

    events = group_events(rows)
    by_camera: dict[str, int] = {}
    by_item: dict[str, int] = {}
    by_rule: dict[str, int] = {}
    for ev in events:
        if ev.rule:
            by_rule[ev.rule] = by_rule.get(ev.rule, 0) + 1
        by_camera[camera_label(ev.camera_id, cams)] = (
            by_camera.get(camera_label(ev.camera_id, cams), 0) + 1
        )
        for it in ev.items:
            by_item[it] = by_item.get(it, 0) + 1

    facts.update(
        {
            "event_type": plan.event_type,
            "item": plan.item,
            "group_by": plan.group_by,
            "window": {"label": label, "start": start.isoformat(), "end": end.isoformat()},
            "events": len(events),
            "samples": len(rows),
            "by_camera": dict(sorted(by_camera.items(), key=lambda kv: -kv[1])),
            "by_item": dict(sorted(by_item.items(), key=lambda kv: -kv[1])),
            "by_rule": dict(sorted(by_rule.items(), key=lambda kv: -kv[1])),
            "keyword": plan.keyword,
            "latest": [
                {"camera": e["camera"], "time": e["time"], "summary": e["summary"]}
                for e in _evidence(events, cams)
            ],
            "watching": (
                None
                if plan.item is None
                else {
                    "item": plan.item,
                    "checked_in_window": bool(checked),
                    "checked_runs": checked,
                }
            ),
        }
    )
    facts["_evidence"] = _evidence(events, cams)
    return facts


# ---- answers --------------------------------------------------------------------


def _plural(n: int, one: str, many: str | None = None) -> str:
    return one if n == 1 else (many or one + "s")


def _scope_phrase(facts: dict[str, Any], n: int) -> str:
    item = facts.get("item")
    kind = facts.get("event_type")
    if kind == "violation":
        return f"{item} {_plural(n, 'violation')}" if item else _plural(n, "PPE violation")
    if kind == "compliant":
        return _plural(n, "compliant sighting")
    if kind == "zone":
        return _plural(n, "zone breach", "zone breaches")
    return _plural(n, "alert")


def _watching_phrase(facts: dict[str, Any]) -> str:
    bits = list(facts.get("current_items") or []) + [
        f"“{r}”" for r in facts.get("current_zone_rules") or []
    ]
    return ", ".join(bits) if bits else "the default hard-hat check"


def template_answer(facts: dict[str, Any]) -> str:
    """Deterministic answer built only from the facts (the safe fallback)."""
    intent = facts.get("intent")
    if intent == "status":
        return f"Right now I'm watching for: {_watching_phrase(facts)}."
    if intent == "retask":
        return f"I can start watching for {facts.get('item') or 'that'} — confirm below."
    if intent == "unknown":
        return (
            "I can answer questions about what Safety Sentry has seen — violations, "
            "restricted-area breaches, and what it's watching for."
        )
    if facts.get("camera_not_found"):
        return f"I couldn't find a camera called “{facts['camera_not_found']}”."

    n = int(facts.get("events", 0))
    window = facts["window"]["label"]
    replay = " (replayed footage)" if facts.get("replay_footage") else ""
    watching = facts.get("watching")
    if n == 0:
        if watching and not watching["checked_in_window"]:
            return (
                f"I wasn't checking for {watching['item']} {window}, so I have no record of that. "
                f"Want me to start now?"
            )
        scope = _scope_phrase(facts, 2)
        return f"No {scope} {window}{replay}."
    by_camera = facts.get("by_camera") or {}
    cams = ""
    if len(by_camera) > 1 or facts.get("group_by") == "camera":
        cams = " — " + ", ".join(f"{v} on {k}" for k, v in by_camera.items())
    rules = ""
    by_rule = facts.get("by_rule") or {}
    if facts.get("event_type") == "zone" and by_rule:
        top = list(by_rule.items())[:2]
        if len(by_rule) == 1:
            rules = f" — “{top[0][0]}”"
        else:
            rules = " — " + ", ".join(f"{v} for “{k}”" for k, v in top)
        cams = cams if len(by_camera) > 1 else ""
    return f"{n} {_scope_phrase(facts, n)} {window}{rules or cams}{replay}."


_NARRATE_SYSTEM = """\
You are Safety Sentry, an AI site-safety monitor, answering a colleague's question.
Use ONLY the facts provided. Reply with JSON: {"answer": "<text>"}.

Rules:
- One or two short, plain sentences. No markdown, no lists, no emojis.
- Quote numbers as digits exactly as they appear in the facts. Never invent or compute new numbers.
- Always state the time window using its label.
- If facts.watching.checked_in_window is false and events is 0, say you were not checking for that item in that window and offer to start.
- If facts.replay_footage is true, say it was replayed footage (never imply the video was live).
- If facts.camera_not_found is set, say you could not find that camera.
- If events is greater than 0, never say "no one", "nobody", "none" or "no violations".
- For zone questions, name the rule(s) in by_rule that were broken.
- For intent "status", say what you are watching for (current_items and current_zone_rules); do not mention replay.
"""


_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")
_NEGATION_RE = re.compile(
    r"\b(no one|nobody|none|not a single|no (?:\w+ ){0,2}(?:violations?|breaches|incidents|alerts|events))\b",
    re.IGNORECASE,
)


def numbers_in(value: Any) -> set[str]:
    return set(_NUMBER_RE.findall(json.dumps(value, default=str)))


def answer_is_grounded(answer: str, facts: dict[str, Any]) -> bool:
    """Every number must exist in the facts, and "none" must not contradict a positive count."""
    events = facts.get("events")
    if isinstance(events, int) and events > 0 and _NEGATION_RE.search(answer):
        return False
    allowed = numbers_in({k: v for k, v in facts.items() if not k.startswith("_")})
    allowed |= {n.lstrip("0") or "0" for n in allowed}
    return all(n in allowed or n.lstrip("0") in allowed for n in _NUMBER_RE.findall(answer))


def _actions(plan: QueryPlan, facts: dict[str, Any]) -> list[dict[str, Any]]:
    """Offer to start watching for the asked-about item, unless it already is watched."""
    if not plan.item or plan.intent not in ("count", "list", "retask"):
        return []
    items = list(facts.get("current_items") or [])
    if canonical_item(plan.item) in {canonical_item(i) for i in items}:
        return []
    items.append(plan.item)
    mission = "Workers must wear " + ", ".join(items) + "."
    for rule in facts.get("current_zone_rules") or []:
        mission += " " + str(rule).strip().rstrip(".").capitalize() + "."
    return [{"type": "set_mission", "label": f"Start watching for {plan.item}", "mission": mission}]


async def ask(question: str, feed_cleared_after_id: int | None = None) -> dict[str, Any]:
    """Answer ``question``. Never raises for model trouble — returns a polite answer."""
    started = time.monotonic()
    question = (question or "").strip()
    if not question:
        raise ValueError("question is empty")
    if len(question) > MAX_QUESTION_CHARS:
        raise ValueError(f"question exceeds {MAX_QUESTION_CHARS} characters")

    cams = _cameras()
    names = ", ".join(str(c.get("name") or c["serial"]) for c in cams) or "none configured"
    provider = get_provider()
    model = get_active_model()

    try:
        planned = await provider.complete(
            system_prompt=_PLAN_SYSTEM.replace("{cameras}", names),
            user_prompt=f"{_PLAN_EXAMPLES}\nQ: {question}\nA:",
            model_id=model,
            max_tokens=200,
        )
    except VLMError as e:
        logger.warning("assistant planner failed: %s", e)
        return _failure("I couldn't reach the AI model just now — try again in a moment.", started)

    plan = validate_plan(planned.parsed)
    facts = build_facts(plan, feed_cleared_after_id)
    evidence = facts.pop("_evidence", [])
    answer, source = template_answer(facts), "template"

    if plan.intent != "unknown":
        try:
            narrated = await provider.complete(
                system_prompt=_NARRATE_SYSTEM,
                user_prompt=f"Question: {question}\nFacts: {json.dumps(facts, default=str)}",
                model_id=model,
                max_tokens=160,
            )
            candidate = str((narrated.parsed or {}).get("answer") or "").strip()
            if candidate and answer_is_grounded(candidate, facts):
                answer, source = candidate, "llm"
            elif candidate:
                logger.info("assistant answer rejected (ungrounded numbers): %r", candidate)
        except VLMError as e:
            logger.warning("assistant narrator failed, using template: %s", e)

    return {
        "answer": answer,
        "source": source,
        "plan": plan.as_dict(),
        "facts": {
            k: facts[k]
            for k in ("window", "events", "samples", "by_camera", "by_item", "by_rule", "watching")
            if k in facts
        },
        "evidence": evidence if plan.intent in ("count", "list") else [],
        "actions": _actions(plan, facts),
        "took_ms": int((time.monotonic() - started) * 1000),
    }


def _failure(message: str, started: float) -> dict[str, Any]:
    return {
        "answer": message,
        "source": "error",
        "plan": QueryPlan().as_dict(),
        "facts": {},
        "evidence": [],
        "actions": [],
        "took_ms": int((time.monotonic() - started) * 1000),
    }
