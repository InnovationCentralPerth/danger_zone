"""Record REAL results from the running system for the interactive poster.

Nothing on the poster is mocked: for each source frame and each mission this runs the
same prompts, parsers and local model the live agents use, and saves what came back
(plus the real mission parser and a few real "Ask Safety Sentry" answers).

Run from the repo root with the vLLM server up:

    python poster/record_results.py

Writes poster/data/results.json and poster/assets/ask/*.jpg. Re-run it after changing
frames, missions or prompts, then rebuild the poster with poster/build_poster.py.

    python poster/record_results.py --only woodshop   # just the woodshop set + its accuracy
                                                       # test, merged into the existing file
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import subprocess
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
        "group": "bench",
        "group_label": "Bench & workshop",
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
        "group": "bench",
        "group_label": "Bench & workshop",
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
        "group": "bench",
        "group_label": "Bench & workshop",
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

WOODSHOP_PRESETS = [
    {"id": "glasses", "label": "Safety glasses", "items": ["safety glasses"]},
    {"id": "hearing", "label": "Hearing protection", "items": ["hearing protection"]},
    {"id": "resp", "label": "Respirator", "items": ["respirator"]},
    {
        "id": "all3",
        "label": "All three",
        "items": ["safety glasses", "hearing protection", "respirator"],
    },
]

# Same person, same task, same camera: the PPE ladder (frames from extract_woodshop.py).
WOODSHOP_MOMENTS = [
    {"id": f"w{n}", "group": "woodshop", "group_label": "Woodshop · same task, more PPE",
     "title": title, "image": f"woodshop-{n}.jpg", "area": "Woodshop",
     "presets": WOODSHOP_PRESETS, "zones": [],
     # Clear glasses on a turned head are its weakest spot: in the full-kit frame the AI gets
     # "glasses" wrong when asked about them alone, so that chip is left out (Lessons says why).
     "skip": ["glasses"] if n == 4 else []}
    for n, title in enumerate(
        ["No protection", "Hearing protection only", "Glasses and hearing", "Full kit with respirator"], 1
    )
]  # fmt: skip
MOMENTS += WOODSHOP_MOMENTS

# What is actually worn in each woodshop clip (from the file names, checked against frames).
WOODSHOP_TRUTH = {
    "woodshop-table-1": ("Table saw · none", set()),
    "woodshop-table-2": ("Table saw · hearing only", {"hearing protection"}),
    "woodshop-table-3": ("Table saw · glasses + hearing", {"safety glasses", "hearing protection"}),
    "woodshop-table-4": (
        "Table saw · full kit",
        {"safety glasses", "hearing protection", "respirator"},
    ),
    "woodshop-drop-1": ("Drop saw · none", set()),
    "woodshop-drop-2": ("Drop saw · glasses + hearing", {"safety glasses", "hearing protection"}),
}

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


async def record_moments(provider, model: str, moments: list[dict]) -> list[dict]:
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
    for m in moments:
        raw = (FRAMES / m["image"]).read_bytes()
        w, h = Image.open(io.BytesIO(raw)).size
        record = {
            "id": m["id"],
            "group": m.get("group", "bench"),
            "group_label": m.get("group_label", ""),
            "title": m["title"],
            "image": m["image"],
            "size": [w, h],
            "missions": [],
        }
        # PPE missions: exactly what ppe_compliance sends (frame with the PPE-area overlay)
        overlaid = draw_zone_overlay(
            raw, full, label=m["area"], outline=_PPE_OUTLINE, fill=_PPE_FILL
        )  # type: ignore[arg-type]
        for p in m.get("presets", PPE_PRESETS):
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


async def record_woodshop_eval(provider, model: str) -> dict:
    """Sample 5 frames from each woodshop clip and compare the AI's PPE judgement to the truth."""
    from app.agents.ppe_compliance import (
        _PPE_FILL,
        _PPE_OUTLINE,
        _build_multi_item_prompt,
        _parse_multi_item_verdict,
    )
    from app.services.zone import draw_zone_overlay

    items = ["safety glasses", "hearing protection", "respirator"]
    full = [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)]
    demo = Path("/models/danger_zone/data/demo")
    sem = asyncio.Semaphore(6)

    def grab(stem: str, frac: float) -> bytes:
        clip = demo / f"{stem}.mp4"
        dur = float(subprocess.check_output(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(clip)]
        ))  # fmt: skip
        return subprocess.run(
            ["ffmpeg", "-v", "error", "-ss", f"{dur * frac:.2f}", "-i", str(clip), "-frames:v", "1",
             "-vf", "scale=1280:-2", "-q:v", "3", "-f", "image2pipe", "-vcodec", "mjpeg", "-"],
            capture_output=True, check=True,
        ).stdout  # fmt: skip

    async def one(stem: str, frac: float) -> dict:
        img = draw_zone_overlay(
            grab(stem, frac), full, label="Woodshop", outline=_PPE_OUTLINE, fill=_PPE_FILL
        )  # type: ignore[arg-type]
        async with sem:
            res = await provider.analyse_image(
                img, _build_multi_item_prompt(items, "Woodshop", ""), model_id=model
            )
        v = _parse_multi_item_verdict(res.parsed, items)
        return {
            "clip": stem, "at": frac, "people": v["people_in_area"],
            "missing": sorted(i["item"] for i in v["items"] if i["missing"] > 0), "summary": v["summary"],
        }  # fmt: skip

    fracs = [0.1, 0.3, 0.5, 0.7, 0.9]
    rows = await asyncio.gather(*[one(stem, f) for stem in WOODSHOP_TRUTH for f in fracs])
    frames_with_person = items_judged = items_correct = frames_all_correct = 0
    misses = []
    for r in rows:
        if not r["people"]:
            continue  # nobody in shot yet (a clip's first second): nothing to judge
        worn = WOODSHOP_TRUTH[r["clip"]][1]
        right = [(i in worn) == (i not in r["missing"]) for i in items]
        frames_with_person += 1
        items_judged += len(items)
        items_correct += sum(right)
        frames_all_correct += int(all(right))
        if not all(right):
            misses.append(
                {"clip": WOODSHOP_TRUTH[r["clip"]][0], "at": r["at"], "summary": r["summary"]}
            )
    out = {
        "frames": len(rows), "empty_frames": len(rows) - frames_with_person,
        "frames_judged": frames_with_person, "frames_all_correct": frames_all_correct,
        "item_judgments": items_judged, "item_correct": items_correct, "misses": misses,
    }  # fmt: skip
    print(
        f"  woodshop eval: {items_correct}/{items_judged} item judgments right; {frames_all_correct}/{frames_with_person} frames fully right"
    )
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

    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--only", choices=["woodshop"], help="record just this set and merge it into results.json"
    )
    args = ap.parse_args()

    _init_db()
    provider, model = get_provider(), get_active_model()
    print("recording with model:", model)
    if args.only == "woodshop":
        results = json.loads(OUT.read_text())
        fresh = await record_moments(provider, model, WOODSHOP_MOMENTS)
        results["moments"] = [m for m in results["moments"] if m.get("group") != "woodshop"] + fresh
        results["woodshop_eval"] = await record_woodshop_eval(provider, model)
        results["woodshop_recorded_at"] = datetime.now(UTC).isoformat(timespec="seconds")
    else:
        results = {
            "recorded_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "model": model,
            "moments": await record_moments(provider, model, MOMENTS),
            "parses": await record_parses(provider, model),
            "asks": await record_asks(),
            "woodshop_eval": await record_woodshop_eval(provider, model),
        }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(results, indent=2, ensure_ascii=False))
    print("wrote", OUT, f"({OUT.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    asyncio.run(main())
