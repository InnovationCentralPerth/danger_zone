"""Assemble the standalone interactive poster (one HTML file, no network needed).

    python poster/build_poster.py

Reads poster/template.html + poster/data/results.json (made by record_results.py) and
the images under poster/assets, static/img/partners and static/fonts, and writes
poster/dist/safety-sentry-poster.html. A copy goes to static/poster/ so the tablet can
also fetch it from the dashboard server when it is reachable.
"""

from __future__ import annotations

import base64
import json
import shutil
from pathlib import Path

import segno

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parent
LINKEDIN = "https://www.linkedin.com/company/18071579/"
MIME = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}


def data_uri(path: Path) -> str:
    return (
        f"data:{MIME[path.suffix.lower()]};base64," + base64.b64encode(path.read_bytes()).decode()
    )


def main() -> None:
    results = json.loads((ROOT / "data" / "results.json").read_text())

    for m in results["moments"]:
        m["imageData"] = data_uri(ROOT / "assets" / "frames" / m["image"])
    for a in results["asks"]:
        for e in a["evidence"]:
            thumb = e.get("thumb")
            e["thumbData"] = data_uri(ROOT / "assets" / "ask" / thumb) if thumb else None
            e.pop("thumb", None)
    results["dashboard"] = data_uri(ROOT / "assets" / "dashboard.webp")
    results["linkedin"] = LINKEDIN

    qr = segno.make(LINKEDIN, error="m").svg_inline(
        omitsize=True, dark="#0b1220", light="#ffffff", border=0
    )

    html = (ROOT / "template.html").read_text()
    partners = REPO / "static" / "img" / "partners"
    replacements = {
        "__FONT_B64__": base64.b64encode(
            (REPO / "static" / "fonts" / "Inter-var.woff2").read_bytes()
        ).decode(),
        "__LOGO_ICP__": data_uri(partners / "icp.png"),
        "__LOGO_MERAKI__": data_uri(partners / "meraki.png"),
        "__LOGO_SPLUNK__": data_uri(partners / "splunk.png"),
        "__LOGO_NIIN__": data_uri(partners / "niin.png"),
        "__QR_SVG__": qr,
        "__POSTER_JSON__": json.dumps(results, ensure_ascii=False).replace("</", "<\\/"),
    }
    for key, value in replacements.items():
        if key not in html:
            raise SystemExit(f"template is missing placeholder {key}")
        html = html.replace(key, value)

    out = ROOT / "dist" / "safety-sentry-poster.html"
    out.parent.mkdir(exist_ok=True)
    out.write_text(html)
    served = REPO / "static" / "poster"
    served.mkdir(exist_ok=True)
    shutil.copy(out, served / out.name)
    print(f"wrote {out} ({out.stat().st_size / 1024 / 1024:.2f} MB); copy at {served / out.name}")


if __name__ == "__main__":
    main()
