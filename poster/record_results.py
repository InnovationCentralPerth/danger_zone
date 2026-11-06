"""Record REAL results from the running system for the interactive poster.

Nothing on the poster is mocked: for each source frame and each mission this runs the
same prompts, parsers and local model the live agents use, and saves what came back
(plus the real mission parser and a few real "Ask Safety Sentry" answers).

Run from the repo root with the vLLM server up:

    python poster/record_results.py

Writes poster/data/results.json and poster/assets/ask/*.jpg. Re-run it after changing
frames, missions or prompts, then rebuild the poster with poster/build_poster.py.
"""

from __future__ import annotations

import asyncio
import io
import json
import time
from datetime import UTC, datetime
from pathlib import Path

from PIL import Image
from sqlmodel import Session

ROOT = Path(__file__).resolve().parent
FRAMES = ROOT / "assets" / "frames"
ASK_DIR = ROOT / "assets" / "ask"
OUT = ROOT / "data" / "results.json"

PPE_PRESETS = [
    {"id": "hat", "label": "Hard hat", "items": ["hard hat"]},
    {"id": "hat-vest", "label": "Hard hat + vest", "items": ["hard hat", "hi-vis vest"]},
    {
        "id": "all3",
        "label": "All three",
        "items": ["hard hat", "hi-vis vest", "safety goggles"],
    },
    {"id": "goggles", "label": "Goggles", "items": ["safety goggles"]},
]

MOMENTS = [
    {
        "id": "a",
        "title": "One person, almost fully kitted",
        "image": "moment-a.jpg",
        "area": "Demo Area",
        "zones": [
            {
                "id": "bench",
                "label": "Keep off the bench",
                "rule": "Nobody may stand at or near the demo bench.",
            },
            {
                "id": "left",
                "label": "Left half off limits",
                "rule": "Nobody may be in the left half of the picture.",
            },
        ],
    },
    {
        "id": "b",
        "title": "Two people, different kit",
        "image": "moment-b.jpg",
        "area": "Demo Area",
        "zones": [
            {
                "id": "bench",
                "label": "Keep off the bench",
                "rule": "Nobody may stand at or near the demo bench.",
            },
            {
                "id": "left",
                "label": "Left half off limits",
                "rule": "Nobody may be in the left half of the picture.",
            },
        ],
    },
    {
        "id": "c",
        "title": "The wider workshop",
        "image": "moment-c.jpg",
        "area": "Workshop Floor",
        # People are small at this distance, so goggles are not something it can judge
        # reliably here; the poster says so rather than showing a flaky result.
        "skip": ["goggles"],
        "zones": [
            {
                "id": "plants",
                "label": "Keep clear of the plants",
                "rule": "Nobody may stand near the plants on the far side of the room.",
            },
            {
                "id": "corner",
                "label": "Bottom-left corner off limits",
                "rule": "Nobody may be at the desks in the bottom-left corner.",
            },
        ],
    },
]

MISSION_EXAMPLES = [
    "Everyone must wear a hard hat and a hi-vis vest, and nobody may stand at the bench.",
    "This is an electronics lab — workers need safety glasses and anti-static wrist straps.",
    "Nobody may be in the left half of the picture. Everyone must wear safety goggles.",
]

ASK_QUESTIONS = [
    "Did anyone go near the bench in the Demo Area today?",
    "Is anyone not wearing a respirator?",
    "What are you watching for right now?",
]


async def record_moments(provider, model: str) -> list[dict]:
    from app.agents.ppe_compliance import (
        _PPE_FILL,
        _PPE_OUTLINE,
        _build_multi_item_prompt,
        _parse_multi_item_verdict,
    )
    from app.agents.zone_intrusion import _RULES_PROMPT, _format_rules, _parse_rule_verdicts
    from app.services.zone import draw_zone_overlay

    full = [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)]
    out = []
    for m in MOMENTS:
        raw = (FRAMES / m["image"]).read_bytes()
        w, h = Image.open(io.BytesIO(raw)).size
        record = {
            "id": m["id"],
            "title": m["title"],
            "image": m["image"],
            "size": [w, h],
            "missions": [],
        }
        # PPE missions: exactly what ppe_compliance sends (frame with the PPE-area overlay)
        overlaid = draw_zone_overlay(
            raw, full, label=m["area"], outline=_PPE_OUTLINE, fill=_PPE_FILL
        )  # type: ignore[arg-type]
        for p in PPE_PRESETS:
            if p["id"] in m.get("skip", []):
                continue
            prompt = _build_multi_item_prompt(p["items"], m["area"], "")
            t0 = time.monotonic()
            res = await provider.analyse_image(overlaid, prompt, model_id=model)
            parsed = _parse_multi_item_verdict(res.parsed, p["items"])
            record["missions"].append(
                {
                    "id": p["id"],
                    "kind": "ppe",
                    "label": p["label"],
                    "items": p["items"],
                    "people": parsed["people_in_area"],
                    "confidence": parsed["overall_confidence"],
                    "summary": parsed["summary"],
                    "checked": parsed["items"],
                    "latency_ms": res.latency_ms or int((time.monotonic() - t0) * 1000),
                }
            )
            print(
                f"  {m['id']} / {p['label']}: people={parsed['people_in_area']} -> {parsed['summary'][:90]}"
            )
        # Zone rules: clean frame + the rule, as zone_intrusion does
        for z in m["zones"]:
            prompt = _RULES_PROMPT.format(rules=_format_rules([z["rule"]]))
            res = await provider.analyse_image(raw, prompt, model_id=model)
            v = _parse_rule_verdicts(res.parsed, [z["rule"]])[0]
            record["missions"].append(
                {
                    "id": f"zone-{z['id']}",
                    "kind": "zone",
                    "label": z["label"],
                    "rule": z["rule"],
                    "violated": bool(v["violated"]),
                    "confidence": v["confidence"],
                    "summary": v["reasoning"],
                    "people": int(res.parsed.get("people_visible", 0) or 0),
                    "latency_ms": res.latency_ms,
                }
            )
            print(
                f"  {m['id']} / {z['label']}: violated={v['violated']} ({v['confidence']:.2f}) {v['reasoning'][:70]}"
            )
        out.append(record)
    return out


async def record_parses(provider, model: str) -> list[dict]:
    from app.services import mission_context as mc

    out = []
    for text in MISSION_EXAMPLES:
        res = await provider.complete(
            system_prompt=mc._SYSTEM_PROMPT, user_prompt=text, model_id=model
        )
        items, note, sev = mc._sanitise_parsed(res.parsed)
        rules = mc._sanitise_zone_rules(res.parsed)
        out.append(
            {
                "text": text,
                "items": items,
                "zone_rules": rules,
                "severity": sev,
                "latency_ms": res.latency_ms,
            }
        )
        print(f"  parse: {items} {rules}")
    return out


async def record_asks() -> list[dict]:
    from app.db import get_engine
    from app.models.db import Incident
    from app.services import assistant

    ASK_DIR.mkdir(parents=True, exist_ok=True)
    for stale in ASK_DIR.glob("*.jpg"):  # don't keep thumbnails from earlier recordings
        stale.unlink()
    out = []
    for q in ASK_QUESTIONS:
        res = await assistant.ask(q)
        evidence = []
        for i, ev in enumerate(res.get("evidence", [])):
            thumb = None
            with Session(get_engine()) as s:
                row = s.get(Incident, ev["incident_id"])
            if row and row.snapshot_path and Path(row.snapshot_path).is_file():
                img = Image.open(row.snapshot_path).convert("RGB")
                img.thumbnail((520, 520))
                name = f"ask-{len(out)}-{i}.jpg"
                img.save(ASK_DIR / name, quality=78)
                thumb = name
            evidence.append({**ev, "thumb": thumb})
        out.append(
            {
                "question": q,
                **{k: res[k] for k in ("answer", "source", "facts", "actions", "took_ms")},
                "evidence": evidence,
            }
        )
        print(f"  ask: {q!r} -> {res['answer'][:90]}")
    return out


async def main() -> None:
    from app.integrations.vlm import get_active_model, get_provider
    from app.main import _init_db

    _init_db()
    provider, model = get_provider(), get_active_model()
    print("recording with model:", model)
    results = {
        "recorded_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "model": model,
        "moments": await record_moments(provider, model),
        "parses": await record_parses(provider, model),
        "asks": await record_asks(),
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(results, indent=2, ensure_ascii=False))
    print("wrote", OUT, f"({OUT.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    asyncio.run(main())
