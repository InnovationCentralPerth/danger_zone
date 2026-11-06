from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sse_starlette.sse import EventSourceResponse

from app.config import get_settings
from app.runtime.bus import bus

router = APIRouter()
templates = Jinja2Templates(directory="app/dashboard/templates")
logger = logging.getLogger(__name__)

_ASSET_FILES = (Path("static/js/main.js"), Path("static/css/main.css"))

_PARTNER_DIR = Path("static/img/partners")
_PARTNER_EXTS = ("svg", "png", "webp", "jpg", "jpeg")

# "Powered by" partners, in display order. A logo is picked up automatically from
# static/img/partners/<key>.<ext>; with no file the tile falls back to the plain
# product name rather than a broken image.
_PARTNERS = (
    {"key": "meraki", "name": "Cisco Meraki", "href": "https://meraki.cisco.com/"},
    {"key": "splunk", "name": "Splunk", "href": "https://www.splunk.com/"},
    {"key": "niin", "name": "NIIN Sovereign Cloud", "href": None},
)


def _partners() -> list[dict[str, str | None]]:
    out: list[dict[str, str | None]] = []
    for partner in _PARTNERS:
        logo = next(
            (
                f"/{_PARTNER_DIR.as_posix()}/{partner['key']}.{ext}"
                for ext in _PARTNER_EXTS
                if (_PARTNER_DIR / f"{partner['key']}.{ext}").is_file()
            ),
            None,
        )
        out.append({**partner, "logo": logo})
    return out


def _asset_version() -> str:
    """Cache-bust dashboard assets when the JS/CSS changes.

    The expo runs on a long-lived browser tab; a stale cached main.js would hide
    the source badges and mission presets after a deploy. Use the newest mtime.
    """
    try:
        return str(int(max(p.stat().st_mtime for p in _ASSET_FILES)))
    except OSError:
        return "1"


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
async def dashboard(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "asset_version": _asset_version(),
            "partners": _partners(),
            "site_name": get_settings().site_name,
            "product_name": get_settings().product_name,
        },
    )


@router.get("/events", include_in_schema=False)
async def sse_events(request: Request) -> EventSourceResponse:
    """Server-Sent Events stream — dashboard subscribes here for live updates."""
    q = bus.subscribe("*")

    async def generator():  # type: ignore[return]
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(q.get(), timeout=30)
                    yield {
                        "event": event.topic.replace(".", "_"),
                        "data": json.dumps(event.payload, default=str),
                    }
                except TimeoutError:
                    # Send keepalive comment
                    yield {"event": "ping", "data": datetime.utcnow().isoformat()}
        finally:
            bus.unsubscribe("*", q)

    return EventSourceResponse(generator())
