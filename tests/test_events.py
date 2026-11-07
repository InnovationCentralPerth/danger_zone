"""Tests for incident -> event grouping (app.services.events)."""

from __future__ import annotations

from datetime import datetime, timedelta

from app.services.events import canonical_item, group_events, incident_items

T0 = datetime(2026, 11, 6, 1, 0, 0)


def _inc(id_, secs, *, type_="PPE_VIOLATION", camera="CAM-A", items=("hard hat",), **extra):
    e = dict(extra)
    if type_ == "PPE_VIOLATION":
        e.setdefault("violating_items", [{"item": i, "missing": 1} for i in items])
    return {
        "id": id_,
        "type": type_,
        "camera_id": camera,
        "created_at": T0 + timedelta(seconds=secs),
        "summary": "s",
        "snapshot_path": f"/snap/{id_}.jpg",
        "extra": e,
    }


def test_canonical_item_merges_synonyms():
    assert canonical_item("Helmet") == "hard hat"
    assert canonical_item("hard_hat") == "hard hat"
    assert canonical_item("hardhat") == "hard hat"
    assert canonical_item("hi-vis vest") == "hi-vis vest"
    assert canonical_item("high visibility safety vest") == "hi-vis vest"
    assert canonical_item("safety goggles") == "safety goggles"
    assert canonical_item("safety glasses") == "safety glasses"
    assert canonical_item("") == ""
    assert canonical_item("lab coat") == "lab coat"


def test_consecutive_incidents_become_one_event():
    events = group_events([_inc(i, i * 5) for i in range(8)])  # 8 incidents, 5 s apart
    assert len(events) == 1
    ev = events[0]
    assert ev.count == 8 and ev.duration_s == 35 and ev.items == {"hard hat"}


def test_a_long_gap_starts_a_new_event():
    rows = [_inc(1, 0), _inc(2, 5), _inc(3, 200), _inc(4, 205)]
    events = group_events(rows)
    assert [e.count for e in events] == [2, 2]
    assert events[0].start > events[1].start  # newest first


def test_cameras_types_and_zone_rules_are_separate_events():
    rows = [
        _inc(1, 0, camera="A"),
        _inc(2, 1, camera="B"),
        _inc(3, 2, type_="PPE_OK", camera="A", required_items=["hard hat"]),
        _inc(4, 3, type_="INTRUSION", camera="A", zone_rule="no one at the bench"),
        _inc(5, 4, type_="INTRUSION", camera="A", zone_rule="no one in the doorway"),
    ]
    assert len(group_events(rows)) == 5


def test_incident_items_handles_all_shapes():
    assert incident_items(_inc(1, 0, items=("Helmet", "hi-vis vest"))) == {
        "hard hat",
        "hi-vis vest",
    }
    ok = _inc(2, 0, type_="PPE_OK", required_items=["safety goggles"])
    assert incident_items(ok) == {"safety goggles"}
    legacy = {"id": 3, "type": "PPE_VIOLATION", "camera_id": "A", "created_at": T0,
              "summary": "2/2 without hard hat", "snapshot_path": None, "extra": {}}  # fmt: skip
    assert incident_items(legacy) == {"hard hat"}


def test_representative_prefers_a_snapshot_and_high_confidence():
    a = _inc(1, 0, vlm_confidence=0.6)
    b = _inc(2, 5, vlm_confidence=0.95)
    c = _inc(3, 10, vlm_confidence=0.99)
    c["snapshot_path"] = None
    (ev,) = group_events([a, b, c])
    assert ev.representative()["id"] == 2


def test_hearing_protection_and_respirator_are_first_class_items():
    for name in ("Hearing protection", "ear muffs", "earplugs", "ear defenders", "Earmuffs"):
        assert canonical_item(name) == "hearing protection", name
    assert canonical_item("respirator mask") == "respirator"
    assert canonical_item("dust mask") == "respirator"
    assert canonical_item("safety glasses") == "safety glasses"  # unchanged
    ok = {"id": 1, "type": "PPE_OK", "camera_id": "A", "created_at": T0, "summary": "",
          "snapshot_path": None, "extra": {"required_items": ["hearing protection"]}}  # fmt: skip
    assert incident_items(ok) == {"hearing protection"}
