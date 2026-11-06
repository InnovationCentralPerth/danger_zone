"""On-screen model naming policy.

The UI describes the model by what matters (open weights, on-site) and keeps the exact
identity one tap away. Nothing is hidden: the model card is in the API, the gear menu
and the poster's Model card button, and the repo/docs name the model plainly.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app

ROOT = Path(__file__).resolve().parent.parent


def test_api_has_neutral_label_and_the_exact_model_card():
    models = TestClient(app).get("/models/").json()
    local = next(m for m in models if m["id"] == "qwen3-vl-235b-awq")
    assert "qwen" not in local["label"].lower()
    assert "open-weight" in local["label"].lower()
    # Honest and complete: name, quantisation, maker, licence.
    for fact in ("Qwen3-VL-235B-A22B-Instruct", "AWQ", "Alibaba", "Apache-2.0"):
        assert fact in local["card"], fact


def test_dashboard_code_does_not_hard_code_the_model_name():
    for path in (
        "app/dashboard/templates/index.html",
        "static/js/main.js",
        "static/css/main.css",
    ):
        assert "qwen" not in (ROOT / path).read_text().lower(), path


def test_dashboard_page_shows_the_card_slot_in_the_gear_menu():
    html = TestClient(app).get("/").text
    assert 'id="model-card"' in html and "qwen" not in html.lower()


def test_mission_card_no_longer_prints_the_raw_model_id():
    js = (ROOT / "static/js/main.js").read_text()
    assert "parsed by the on-site model" in js
    assert "parsed by <code>" not in js


def test_poster_names_the_model_only_inside_its_model_card():
    pytest.importorskip("segno")
    from poster import build_poster

    build_poster.main()
    html = (build_poster.ROOT / "dist" / "safety-sentry-poster.html").read_text()
    assert html.count("Qwen") == 1  # exactly one place: the model card string
    card = re.search(r"card: '([^']+)'", html).group(1)
    assert card.startswith("Qwen3-VL-235B-A22B-Instruct") and "Apache-2.0" in card
    assert "Model card" in html
    assert "An open-weight vision-language model" in html
