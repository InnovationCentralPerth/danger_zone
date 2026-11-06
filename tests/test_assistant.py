"""Tests for the 'Ask Safety Sentry' assistant (all offline, model mocked)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from app.db import get_engine
from app.integrations import vlm
from app.main import _init_db, app
from app.models.db import AgentTrace, Incident, IncidentSeverity, IncidentType
from app.services import assistant
from app.services.mission_context import AgentContext

NOW = datetime(2026, 11, 6, 6, 0, 0)  # naive UTC == 14:00 Perth

CAMERAS = [
    {"serial": "CAM-A", "name": "REPLAY — Demo Area", "source": {"type": "file"}},
    {"serial": "CAM-B", "name": "REPLAY — Workshop", "source": {"type": "file"}},
]


@pytest.fixture(autouse=True)
def setup_env(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DEBUG", "false")
    from app import config

    config.get_settings.cache_clear()
    _init_db()
    vlm.reset_provider()
    with patch("app.services.assistant.load_cameras", return_value=CAMERAS):
        yield


def _add(type_, camera, minutes_ago, *, items=("hard hat",), **extra):
    e = dict(extra)
    if type_ == IncidentType.PPE_VIOLATION:
        e["violating_items"] = [{"item": i, "missing": 1} for i in items]
    with Session(get_engine()) as s:
        row = Incident(
            type=type_,
            severity=IncidentSeverity.MEDIUM,
            summary=f"{type_.value} on camera ABCD-1234-WXYZ",
            camera_id=camera,
            snapshot_path=f"/snap/{camera}.jpg",
            extra_json=json.dumps(e),
            created_at=NOW - timedelta(minutes=minutes_ago),
        )
        s.add(row)
        s.commit()
        s.refresh(row)
        return row.id


def _trace(minutes_ago, required_items):
    with Session(get_engine()) as s:
        s.add(
            AgentTrace(
                agent="ppe_compliance",
                trigger="ingest",
                inputs_json=json.dumps({"mode": "multi_item", "required_items": required_items}),
                created_at=NOW - timedelta(minutes=minutes_ago),
            )
        )
        s.commit()


def _ctx(items, rules=(), parsed_minutes_ago=30):
    return AgentContext(
        raw_input="test mission",
        required_items=list(items),
        zone_rules=list(rules),
        parsed_at=(NOW - timedelta(minutes=parsed_minutes_ago)).isoformat(),
    )


def _facts(plan, ctx=None, cleared=None):
    with patch("app.services.assistant.mission_context.get_context", return_value=ctx):
        return assistant.build_facts(plan, cleared, now=NOW)


# ---- plan validation ---------------------------------------------------------


def test_validate_plan_coerces_garbage_to_safe_defaults():
    p = assistant.validate_plan({"intent": "DROP TABLE", "window": "someday", "item": "null"})
    assert (p.intent, p.window, p.item, p.event_type, p.group_by) == (
        "unknown", "mission", None, "violation", "none",
    )  # fmt: skip
    assert assistant.validate_plan(None).intent == "unknown"
    assert assistant.validate_plan("not a dict").intent == "unknown"


def test_validate_plan_normalises_items_and_keeps_good_values():
    p = assistant.validate_plan(
        {"intent": "count", "event_type": "zone", "item": "Helmets", "window": "today",
         "group_by": "camera", "camera": "workshop"}
    )  # fmt: skip
    assert (p.intent, p.event_type, p.item, p.window, p.group_by, p.camera) == (
        "count", "zone", "hard hat", "today", "camera", "workshop",
    )  # fmt: skip


# ---- windows -----------------------------------------------------------------


def test_window_bounds_variants():
    start, end, label = assistant.window_bounds("hour", NOW, None, None)
    assert end - start == timedelta(hours=1) and "hour" in label

    start, _, label = assistant.window_bounds("today", NOW, None, None)
    assert start == datetime(2026, 11, 5, 16, 0, 0)  # Perth midnight = 16:00 UTC the day before
    assert "today" in label

    start, _, label = assistant.window_bounds("all", NOW, None, None)
    assert start.year == 2000

    mission = NOW - timedelta(minutes=30)
    start, _, label = assistant.window_bounds("mission", NOW, mission, None)
    assert start == mission and "mission" in label and "13:30" in label  # Perth clock

    cleared = NOW - timedelta(minutes=5)
    start, _, label = assistant.window_bounds("mission", NOW, mission, cleared)
    assert start == cleared and "cleared" in label

    start, _, _ = assistant.window_bounds("mission", NOW, None, None)  # no mission: today
    assert start == datetime(2026, 11, 5, 16, 0, 0)


# ---- facts -------------------------------------------------------------------


def test_repeats_are_counted_as_one_event_and_windowed():
    for i in range(8):  # one person standing there: 8 incidents 5 s apart
        _add(IncidentType.PPE_VIOLATION, "CAM-A", 10 - i * 5 / 60)
    _add(IncidentType.PPE_VIOLATION, "CAM-B", 3)
    _add(IncidentType.PPE_VIOLATION, "CAM-A", 120)  # before the mission started -> excluded

    f = _facts(assistant.QueryPlan(intent="count"), _ctx(["hard hat"]))
    assert f["samples"] == 9 and f["events"] == 2
    assert f["by_camera"] == {"REPLAY — Demo Area": 1, "REPLAY — Workshop": 1}
    assert f["replay_footage"] is True


def test_item_filter_uses_synonyms_and_event_type():
    _add(IncidentType.PPE_VIOLATION, "CAM-A", 10, items=("hard hat",))
    _add(IncidentType.PPE_VIOLATION, "CAM-A", 5, items=("safety goggles",))
    _add(IncidentType.PPE_OK, "CAM-A", 4, required_items=["hard hat"])
    _add(IncidentType.INTRUSION, "CAM-B", 3, zone_rule="no one at the bench")

    f = _facts(assistant.QueryPlan(intent="count", item="hard hat"), _ctx(["hard hat"]))
    assert f["events"] == 1 and f["by_item"] == {"hard hat": 1}

    zone = _facts(assistant.QueryPlan(intent="count", event_type="zone"), _ctx([]))
    assert zone["events"] == 1
    assert (
        _facts(assistant.QueryPlan(intent="count", event_type="compliant"), _ctx([]))["events"] == 1
    )
    assert _facts(assistant.QueryPlan(intent="count", event_type="any"), _ctx([]))["events"] == 3


def test_camera_filter_and_unknown_camera():
    _add(IncidentType.PPE_VIOLATION, "CAM-A", 10)
    _add(IncidentType.PPE_VIOLATION, "CAM-B", 10)
    f = _facts(assistant.QueryPlan(intent="count", camera="workshop"), _ctx(["hard hat"]))
    assert f["events"] == 1 and f["camera_filter"] == "REPLAY — Workshop"
    nf = _facts(assistant.QueryPlan(intent="count", camera="submarine"), _ctx(["hard hat"]))
    assert nf["camera_not_found"] == "submarine"
    assert "couldn't find a camera called “submarine”" in assistant.template_answer(nf)


def test_feed_cleared_cutoff_narrows_the_window():
    old = _add(IncidentType.PPE_VIOLATION, "CAM-A", 20)
    _add(IncidentType.PPE_VIOLATION, "CAM-A", 2)
    f = _facts(assistant.QueryPlan(intent="count"), _ctx(["hard hat"]), cleared=old)
    assert f["events"] == 1 and "cleared" in f["window"]["label"]


def test_coverage_says_whether_the_item_was_being_checked():
    _trace(10, ["safety goggles"])
    _trace(9, ["safety goggles"])
    no = _facts(assistant.QueryPlan(intent="count", item="hard hat"), _ctx(["safety goggles"]))
    assert no["events"] == 0 and no["watching"]["checked_in_window"] is False
    ans = assistant.template_answer(no)
    assert "wasn't checking for hard hat" in ans and "Want me to start" in ans

    _trace(8, ["hard hat", "hi-vis vest"])
    yes = _facts(assistant.QueryPlan(intent="count", item="hard hat"), _ctx(["hard hat"]))
    assert yes["watching"]["checked_in_window"] is True
    assert assistant.template_answer(yes).startswith("No hard hat violations")


def test_evidence_carries_replay_position_only_for_replay_cameras():
    _add(IncidentType.PPE_VIOLATION, "CAM-A", 5, replay_position=139.5, vlm_confidence=0.9)
    f = _facts(assistant.QueryPlan(intent="count"), _ctx(["hard hat"]))
    ev = assistant.build_facts  # noqa: F841 (documented below)
    with patch(
        "app.services.assistant.mission_context.get_context", return_value=_ctx(["hard hat"])
    ):
        facts = assistant.build_facts(assistant.QueryPlan(intent="list"), None, now=NOW)
    (e,) = facts["_evidence"]
    assert e["replay_position"] == 139.5 and e["camera"] == "REPLAY — Demo Area"
    assert "ABCD-1234-WXYZ" not in e["summary"]  # serial stripped from the sentence
    assert f["events"] == 1


# ---- answers -----------------------------------------------------------------


def test_template_answers():
    base = {
        "window": {"label": "since you set the current mission at 13:30"},
        "replay_footage": True,
    }
    one = {**base, "intent": "count", "event_type": "violation", "item": None, "events": 1}
    assert assistant.template_answer(one) == (
        "1 PPE violation since you set the current mission at 13:30 (replayed footage)."
    )
    many = {**base, "intent": "count", "event_type": "violation", "item": "hard hat", "events": 3,
            "by_camera": {"Workshop": 2, "Demo": 1}, "group_by": "camera"}  # fmt: skip
    assert "3 hard hat violations" in assistant.template_answer(many)
    assert "2 on Workshop, 1 on Demo" in assistant.template_answer(many)
    status = {
        "intent": "status",
        "current_items": ["hard hat"],
        "current_zone_rules": ["no one at the bench"],
    }
    assert assistant.template_answer(status) == (
        "Right now I'm watching for: hard hat, “no one at the bench”."
    )
    assert "answer questions" in assistant.template_answer({"intent": "unknown"})


def test_grounding_guard_rejects_invented_numbers():
    facts = {"events": 3, "window": {"label": "since you set the current mission at 13:30"}}
    assert assistant.answer_is_grounded("3 violations since you set the mission at 13:30.", facts)
    assert not assistant.answer_is_grounded("47 violations since 13:30.", facts)
    assert not assistant.answer_is_grounded("About 3 or 4 violations.", facts)
    # Saying "none" while events exist is a contradiction, even with no digits in it.
    assert not assistant.answer_is_grounded("No one went near the bench.", facts)
    assert not assistant.answer_is_grounded("There were no violations.", facts)
    none = {"events": 0, "window": {"label": "today"}}
    assert assistant.answer_is_grounded("No violations today.", none)


# ---- ask() end to end (model mocked) ---------------------------------------------


def _provider(plan, narration):
    p = MagicMock()
    results = [
        vlm.VLMResult(raw="", parsed=plan, model_id="m", latency_ms=1),
        vlm.VLMResult(raw="", parsed=narration, model_id="m", latency_ms=1),
    ]
    p.complete = AsyncMock(side_effect=results)
    return p


async def _ask(q, provider, ctx=None, cleared=None):
    with (
        patch("app.services.assistant.get_provider", return_value=provider),
        patch("app.services.assistant.mission_context.get_context", return_value=ctx),
        patch("app.services.assistant.datetime") as dt,
    ):
        dt.utcnow.return_value = NOW
        dt.fromisoformat = datetime.fromisoformat
        return await assistant.ask(q, cleared)


async def test_ask_uses_grounded_llm_narration():
    for i in range(3):
        _add(IncidentType.PPE_VIOLATION, "CAM-A", 10 - i * 0.1)
    plan = {"intent": "count", "event_type": "violation", "item": None, "window": "mission"}
    prov = _provider(plan, {"answer": "I saw 1 PPE violation since 13:30 in the replayed footage."})
    out = await _ask("how many violations?", prov, _ctx(["hard hat"]))
    assert out["source"] == "llm" and "1 PPE violation" in out["answer"]
    assert out["facts"]["events"] == 1 and out["evidence"][0]["camera"] == "REPLAY — Demo Area"


async def test_ask_falls_back_to_template_when_the_model_invents_a_number():
    _add(IncidentType.PPE_VIOLATION, "CAM-A", 10)
    plan = {"intent": "count", "event_type": "violation", "window": "mission"}
    prov = _provider(plan, {"answer": "There were 99 violations."})
    out = await _ask("how many?", prov, _ctx(["hard hat"]))
    assert out["source"] == "template" and "99" not in out["answer"]
    assert out["answer"].startswith("1 PPE violation")


async def test_ask_offers_to_start_watching_when_item_was_never_checked():
    _trace(10, ["safety goggles"])
    plan = {"intent": "count", "event_type": "violation", "item": "helmet", "window": "mission"}
    prov = _provider(
        plan, {"answer": "I wasn't checking for hard hat since 13:30, so I have no record."}
    )
    out = await _ask(
        "any people without helmets?", prov, _ctx(["safety goggles"], ["nobody near the bench"])
    )
    assert out["facts"]["watching"]["checked_in_window"] is False
    (action,) = out["actions"]
    assert action["type"] == "set_mission" and action["label"] == "Start watching for hard hat"
    assert action["mission"] == "Workers must wear safety goggles, hard hat. Nobody near the bench."


async def test_ask_no_action_when_already_watching_and_unknown_intent_has_no_evidence():
    plan = {"intent": "count", "event_type": "violation", "item": "hard hat", "window": "mission"}
    out = await _ask("hard hats?", _provider(plan, {"answer": "None."}), _ctx(["hard hat"]))
    assert out["actions"] == []
    unknown = MagicMock()
    unknown.complete = AsyncMock(
        return_value=vlm.VLMResult(raw="", parsed={"intent": "unknown"}, model_id="m", latency_ms=1)
    )
    out = await _ask("weather?", unknown, _ctx(["hard hat"]))
    assert (
        out["plan"]["intent"] == "unknown" and out["evidence"] == [] and out["source"] == "template"
    )


async def test_ask_survives_model_failure():
    prov = MagicMock()
    prov.complete = AsyncMock(side_effect=vlm.VLMError("boom"))
    out = await _ask("how many?", prov)
    assert out["source"] == "error" and "couldn't reach" in out["answer"]


async def test_ask_validates_the_question():
    with pytest.raises(ValueError):
        await assistant.ask("   ")
    with pytest.raises(ValueError):
        await assistant.ask("x" * (assistant.MAX_QUESTION_CHARS + 1))


def test_api_ask_endpoint_validation_and_shape():
    client = TestClient(app)
    assert client.post("/assistant/ask", json={"question": ""}).status_code == 422
    assert client.post("/assistant/ask", json={"question": "x" * 400}).status_code == 422
    fake = {"answer": "ok", "source": "llm", "plan": {}, "facts": {}, "evidence": [], "actions": [],
            "took_ms": 5}  # fmt: skip
    with patch("app.api.assistant.assistant.ask", new=AsyncMock(return_value=fake)) as m:
        r = client.post("/assistant/ask", json={"question": "hi", "feed_cleared_after_id": 7})
    assert r.status_code == 200 and r.json()["answer"] == "ok"
    m.assert_awaited_once_with("hi", 7)


# ---- replay position on incidents --------------------------------------------------


async def test_incident_records_the_replay_position_of_its_snapshot():
    from app.ingest import positions
    from app.services.incidents import create_incident

    positions.clear()
    positions.register("/snap/x.jpg", 139.456)
    row = await create_incident(
        type=IncidentType.PPE_VIOLATION,
        severity=IncidentSeverity.HIGH,
        summary="s",
        camera_id="CAM-A",
        snapshot_path="/snap/x.jpg",
        extra={"vlm_confidence": 0.9},
    )
    assert row.extra()["replay_position"] == 139.46 and row.extra()["vlm_confidence"] == 0.9

    plain = await create_incident(
        type=IncidentType.PPE_VIOLATION,
        severity=IncidentSeverity.HIGH,
        summary="s",
        snapshot_path="/snap/unknown.jpg",
    )
    assert "replay_position" not in plain.extra()
    positions.clear()


def test_positions_registry_is_bounded():
    from app.ingest import positions

    positions.clear()
    for i in range(positions._MAX_ENTRIES + 50):
        positions.register(f"/p/{i}", float(i))
    assert (
        positions.lookup("/p/0") is None
        and positions.lookup(f"/p/{positions._MAX_ENTRIES + 49}") is not None
    )
    assert positions.lookup(None) is None
    positions.clear()


# ---- configured cameras, keyword, per-rule breakdown ------------------------------


def test_only_configured_cameras_are_counted():
    _add(IncidentType.PPE_VIOLATION, "CAM-A", 10)
    _add(IncidentType.PPE_VIOLATION, "OLD-SYNTHETIC-CAMERA", 10)  # leftover from an old demo
    f = _facts(assistant.QueryPlan(intent="count", window="all"), _ctx(["hard hat"]))
    assert f["events"] == 1 and list(f["by_camera"]) == ["REPLAY — Demo Area"]


def test_keyword_filters_zone_events_and_reports_the_rules():
    _add(
        IncidentType.INTRUSION, "CAM-A", 10, zone_rule="nobody may stand at or near the demo bench"
    )
    _add(IncidentType.INTRUSION, "CAM-B", 8, zone_rule="no one in the narrow walkway")
    f = _facts(assistant.QueryPlan(intent="count", event_type="zone", keyword="bench"), _ctx([]))
    assert f["events"] == 1
    assert f["by_rule"] == {"nobody may stand at or near the demo bench": 1}
    both = _facts(assistant.QueryPlan(intent="count", event_type="zone"), _ctx([]))
    assert both["events"] == 2 and len(both["by_rule"]) == 2
    ans = assistant.template_answer(both)
    assert "for “" in ans and "bench" in ans


def test_plan_keyword_is_lowercased_and_optional():
    assert assistant.validate_plan({"intent": "count", "keyword": " Bench "}).keyword == "bench"
    assert assistant.validate_plan({"intent": "count", "keyword": "null"}).keyword is None
    assert assistant.validate_plan({"intent": "count"}).keyword is None
