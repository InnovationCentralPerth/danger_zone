from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlmodel import select

from app.db import get_session
from app.models.db import AgentTrace
from app.runtime.bus import bus

router = APIRouter()

KNOWN_AGENTS = {"zone_intrusion", "ppe_compliance", "rogue_device", "heartbeat", "demo"}


class InvokeRequest(BaseModel):
    payload: dict[str, Any] = {}


@router.post("/{agent_name}/invoke", summary="Manually trigger an agent")
async def invoke_agent(agent_name: str, body: InvokeRequest) -> dict[str, str]:
    if agent_name not in KNOWN_AGENTS:
        raise HTTPException(status_code=404, detail=f"Unknown agent: {agent_name}")
    await bus.publish(f"agent.invoke.{agent_name}", body.payload)
    return {"status": "queued", "agent": agent_name}


@router.get("/traces", summary="Agent reasoning traces")
async def list_traces(
    since: datetime | None = None,
    agent: str | None = None,
    limit: int = 50,
) -> list[AgentTrace]:
    with get_session() as session:
        q = select(AgentTrace)
        if since:
            q = q.where(AgentTrace.created_at >= since)
        if agent:
            q = q.where(AgentTrace.agent == agent)
        q = q.order_by(AgentTrace.created_at.desc()).limit(limit)  # type: ignore[arg-type]
        return list(session.exec(q).all())
