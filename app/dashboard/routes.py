from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sse_starlette.sse import EventSourceResponse

from app.runtime.bus import bus

router = APIRouter()
templates = Jinja2Templates(directory="app/dashboard/templates")
logger = logging.getLogger(__name__)


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
async def dashboard(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "index.html")


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
