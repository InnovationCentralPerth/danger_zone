"""The interactive poster must build from committed assets and stay fully self-contained."""

from __future__ import annotations

import json
import re

import pytest

pytest.importorskip("segno")

from poster import build_poster  # noqa: E402


@pytest.fixture(scope="module")
def html() -> str:
    build_poster.main()
    return (build_poster.ROOT / "dist" / "safety-sentry-poster.html").read_text()


def test_poster_builds_and_has_no_unfilled_placeholders(html):
    assert len(html) > 200_000
    assert not re.findall(r"__[A-Z0-9_]{3,}__", html)
    assert "<title>Safety Sentry" in html


def test_poster_is_fully_self_contained(html):
    # Nothing may be fetched from the network: no src/href/url() pointing at http(s).
    loads = re.findall(r"""(?:src|href)=["'](https?://[^"']+)""", html) + re.findall(
        r"url\((https?://[^)]+)\)", html
    )
    assert loads == []
    assert "<script src=" not in html and "<link " not in html


def test_poster_has_the_partner_logos_at_the_top_and_the_qr_code(html):
    header = html[html.index("<header") : html.index("</header>")]
    for alt in (
        "Innovation Central Perth",
        "Cisco Meraki",
        "Splunk",
        "National Industry Innovation Network",
    ):
        assert f'alt="{alt}"' in header, alt
    assert '<svg viewBox="' in html and 'class="segno"' in html
    assert "Splunk LLC" in html  # trademark attribution


def test_poster_data_is_real_recorded_output(html):
    m = re.search(r"window\.POSTER = (\{.*?\});\n</script>", html, re.S)
    data = json.loads(m.group(1).replace("<\\/", "</"))
    assert data["model"] and data["recorded_at"]
    assert [x["id"] for x in data["moments"]] == ["a", "b", "c", "w1", "w2", "w3", "w4"]
    for moment in data["moments"]:
        assert moment["imageData"].startswith("data:image/jpeg;base64,")
        kinds = {x["kind"] for x in moment["missions"]}
        # Bench frames have zone rules too; the woodshop ladder is PPE only.
        assert kinds == ({"ppe"} if moment["group"] == "woodshop" else {"ppe", "zone"})
        assert all(x["latency_ms"] > 0 for x in moment["missions"])
    assert len(data["parses"]) == 3 and len(data["asks"]) == 3
    assert data["linkedin"] == "https://www.linkedin.com/company/18071579/"
    # The honest-limits rule: the unreliable workshop goggles result is not offered.
    workshop = next(x for x in data["moments"] if x["id"] == "c")
    assert "goggles" not in {x["id"] for x in workshop["missions"]}


def test_woodshop_results_are_honest_and_measured(html):
    m = re.search(r"window\.POSTER = (\{.*?\});\n</script>", html, re.S)
    data = json.loads(m.group(1).replace("<\\/", "</"))
    # The recorded verdicts on the four poster frames match what is really worn.
    worn = {
        "w1": set(),
        "w2": {"hearing protection"},
        "w3": {"safety glasses", "hearing protection"},
        "w4": {"safety glasses", "hearing protection", "respirator"},
    }
    for moment in (x for x in data["moments"] if x["group"] == "woodshop"):
        for mission in moment["missions"]:
            assert mission["people"] >= 1
            for c in mission["checked"]:
                assert (c["item"] in worn[moment["id"]]) == (c["missing"] == 0), (moment["id"], c)
    # The measured accuracy is on the poster, including how many judgments were wrong.
    ev = data["woodshop_eval"]
    assert 0 < ev["item_correct"] <= ev["item_judgments"] and ev["misses"]
    assert "glasses-only question" in html and "E.item_correct" in html


def test_home_screen_has_the_qr_and_the_new_headline(html):
    # The QR is rendered on the overview (tile 8) from one shared constant, and again in the panel.
    assert "const QR_SVG = `<svg" in html
    assert html.count("${QR_SVG}") == 2
    assert 'class="qrbox"' in html
    assert "Every site has cameras. Not everyone is watching." in html


def test_camera_wall_uses_real_frames(html):
    m = re.search(r"window\.POSTER = (\{.*?\});\n</script>", html, re.S)
    data = json.loads(m.group(1).replace("<\\/", "</"))
    assert len(data["cctv"]) == 6
    assert {c["cam"].split(" · ")[1] for c in data["cctv"]} == {
        "DEMO AREA",
        "WORKSHOP",
        "COFFEE AREA",
    }
    assert all(c["data"].startswith("data:image/jpeg;base64,") for c in data["cctv"])
    # The feeds are dressed as a monitoring wall: REC badge, camera label, scan-line overlay.
    assert 'class="rec"' in html.replace('\\"', '"') or '<span class="rec">' in html
    assert ".feed::before" in html and "repeating-linear-gradient" in html
