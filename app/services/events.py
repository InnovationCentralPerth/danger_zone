"""Turn raw incident rows into *events*.

An incident is written every time an agent judges a frame (every few seconds),
so one person standing at the bench for 40 s is ~8 incidents. For counting and
for answering questions that is one **event**: consecutive incidents for the same
camera, type and rule with no gap longer than ``GAP_SECONDS``.

Pure functions only (no DB, no network) so they are easy to test.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

GAP_SECONDS = 20.0

# Canonical PPE vocabulary so "helmet", "hardhat" and "hard hat" are one thing.
_CANON_RULES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("hat", "helmet"), "hard hat"),
    (("vest", "vis"), "hi-vis vest"),
    (("goggle",), "safety goggles"),
    (("glass",), "safety glasses"),
    (("glove",), "gloves"),
    (("boot",), "safety boots"),
    (("respirator", "mask"), "respirator"),
    (("wrist", "strap"), "wrist strap"),
)


def canonical_item(name: str | None) -> str:
    """Normalise a PPE item name (``"Helmet"`` -> ``"hard hat"``)."""
    text = re.sub(r"[_\-]+", " ", (name or "").strip().lower())
    for needles, canon in _CANON_RULES:
        if any(n in text for n in needles):
            return canon
    return text


@dataclass
class Event:
    type: str
    camera_id: str | None
    rule: str  # zone rule text for INTRUSION, "" otherwise
    start: datetime
    end: datetime
    incidents: list[dict[str, Any]] = field(default_factory=list)
    items: set[str] = field(default_factory=set)

    @property
    def count(self) -> int:
        return len(self.incidents)

    @property
    def duration_s(self) -> float:
        return max((self.end - self.start).total_seconds(), 0.0)

    def representative(self) -> dict[str, Any]:
        """The most convincing incident: has a snapshot, highest confidence."""

        def score(inc: dict[str, Any]) -> tuple[int, float]:
            conf = inc["extra"].get("vlm_confidence")
            return (
                1 if inc.get("snapshot_path") else 0,
                float(conf) if isinstance(conf, (int, float)) else 0.0,
            )

        return max(self.incidents, key=score)


def incident_row(row: Any) -> dict[str, Any]:
    """Normalise an ORM ``Incident`` (or a mapping) to the dict shape used here."""
    get = row.get if isinstance(row, dict) else lambda k, d=None: getattr(row, k, d)
    extra = get("extra")
    if callable(extra):  # ORM Incident.extra() parses extra_json for us
        extra = extra()
    if extra is None:
        raw = get("extra_json")
        try:
            extra = json.loads(raw) if raw else {}
        except (TypeError, ValueError):
            extra = {}
    type_ = get("type")
    return {
        "id": get("id"),
        "type": getattr(type_, "value", type_),
        "camera_id": get("camera_id"),
        "created_at": get("created_at"),
        "summary": get("summary") or "",
        "snapshot_path": get("snapshot_path"),
        "extra": extra if isinstance(extra, dict) else {},
    }


def incident_items(inc: dict[str, Any]) -> set[str]:
    """Canonical PPE items an incident is about (what was missing, or checked OK)."""
    extra = inc["extra"]
    names: list[str] = []
    for entry in extra.get("violating_items") or []:
        if isinstance(entry, dict) and entry.get("item"):
            names.append(str(entry["item"]))
    if not names and inc["type"] == "PPE_OK":
        names = [str(i) for i in extra.get("required_items") or []]
    if not names:
        # Legacy single-item incidents only carry the item in their summary.
        low = inc["summary"].lower()
        for needles, canon in _CANON_RULES:
            if any(n in low for n in needles):
                names.append(canon)
    return {canonical_item(n) for n in names if n}


def _rule_of(inc: dict[str, Any]) -> str:
    if inc["type"] != "INTRUSION":
        return ""
    return str(inc["extra"].get("zone_rule") or "polygon zone")


def group_events(incidents: list[dict[str, Any]], gap_seconds: float = GAP_SECONDS) -> list[Event]:
    """Group incidents into events, newest event first."""
    buckets: dict[tuple[str | None, str, str], list[dict[str, Any]]] = defaultdict(list)
    for inc in incidents:
        buckets[(inc["camera_id"], inc["type"], _rule_of(inc))].append(inc)

    events: list[Event] = []
    for (camera, type_, rule), rows in buckets.items():
        rows.sort(key=lambda r: r["created_at"])
        current: Event | None = None
        for inc in rows:
            ts = inc["created_at"]
            if current is None or (ts - current.end).total_seconds() > gap_seconds:
                current = Event(type_, camera, rule, ts, ts)
                events.append(current)
            current.end = ts
            current.incidents.append(inc)
            current.items |= incident_items(inc)
    events.sort(key=lambda e: e.end, reverse=True)
    return events
