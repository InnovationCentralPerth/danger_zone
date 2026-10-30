"""Probe the active VLM with a synthetic compliant / violating PPE frame.

Useful as a smoke test before a demo: it exercises the same overlay + prompt
the ppe_compliance agent uses, without needing a camera or the DB.

    . .venv/bin/activate
    python scripts/probe_ppe.py
"""

from __future__ import annotations

import asyncio
import json

from app.agents.ppe_compliance import _build_prompt
from app.ingest.synthetic_source import Phase, render_frame
from app.integrations.vlm import get_active_model, get_provider
from app.services.zone import draw_zone_overlay

FULL_FRAME = [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)]
YELLOW = (255, 210, 0, 230)
YELLOW_FILL = (255, 210, 0, 50)


async def main() -> None:
    prompt, _ = _build_prompt("hard_hat", "Workshop Floor")
    model = get_active_model()
    provider = get_provider()
    print(f"model: {model}")

    cases = {
        "compliant (hard hat)": Phase(1, person=True, hard_hat=True, hi_vis=True),
        "violation (no hard hat)": Phase(1, person=True, hard_hat=False, hi_vis=True),
    }
    for label, phase in cases.items():
        jpeg = render_frame(phase, 1.0, 1280, 720)
        annotated = draw_zone_overlay(
            jpeg, FULL_FRAME, label="Workshop Floor", outline=YELLOW, fill=YELLOW_FILL
        )
        result = await provider.analyse_image(annotated, prompt, model_id=model)
        print(f"\n{label} — {result.latency_ms} ms")
        print(json.dumps(result.parsed, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
