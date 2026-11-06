"""Screenshot the live dashboard for the poster (needs Playwright + a Chromium build).

uv pip install --target /tmp/opencode/pw playwright
PLAYWRIGHT_BROWSERS_PATH=/tmp/opencode/pwbrowsers python -m playwright install chromium
PLAYWRIGHT_BROWSERS_PATH=/tmp/opencode/pwbrowsers python poster/capture_dashboard.py
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, "/tmp/opencode/pw")
from PIL import Image  # noqa: E402
from playwright.async_api import async_playwright  # noqa: E402

OUT = Path(__file__).resolve().parent / "assets" / "dashboard.webp"


async def main() -> None:
    async with async_playwright() as p:
        b = await p.chromium.launch()
        pg = await b.new_page(viewport={"width": 1920, "height": 1080})
        await pg.goto("http://localhost:8001/", wait_until="domcontentloaded")
        await pg.wait_for_timeout(9000)
        await pg.screenshot(path="/tmp/opencode/dashboard.png")
        await b.close()
    img = Image.open("/tmp/opencode/dashboard.png").convert("RGB")
    img.thumbnail((1600, 1600))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    img.save(OUT, quality=82, method=6)
    print("wrote", OUT, OUT.stat().st_size // 1024, "KB", img.size)


asyncio.run(main())
