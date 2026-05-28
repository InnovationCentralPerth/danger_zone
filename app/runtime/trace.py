"""Agent tracing — records what each agent did, how long it took, and why.

Usage::

    from app.runtime.trace import AgentRun

    async def my_agent(payload):
        async with AgentRun("zone_intrusion", trigger="camera.motion",
                            inputs=payload) as run:
            run.add_tool_call("vlm", {"model": "gpt-4o-mini"}, {"hit": True})
            run.decision = "INTRUSION confirmed"
            run.incident_id = some_id

Traces are persisted to the AgentTrace table and broadcast on the
`agent.trace` bus topic so the dashboard's reasoning panel updates live.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlmodel import Session

from app.db import get_engine
from app.models.db import AgentTrace
from app.runtime.bus import bus

logger = logging.getLogger(__name__)


@dataclass
class AgentRun:
    agent: str
    trigger: str = "unknown"
    inputs: dict[str, Any] = field(default_factory=dict)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    decision: str = ""
    incident_id: int | None = None
    cost_estimate_usd: float = 0.0
    _started: float = field(default=0.0, init=False)

    async def __aenter__(self) -> AgentRun:
        self._started = time.monotonic()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:  # type: ignore[no-untyped-def]
        elapsed_ms = int((time.monotonic() - self._started) * 1000)
        if exc:
            self.decision = self.decision or f"ERROR: {exc!r}"

        try:
            with Session(get_engine()) as session:
                row = AgentTrace(
                    agent=self.agent,
                    trigger=self.trigger,
                    inputs_json=json.dumps(self.inputs, default=str),
                    tool_calls_json=json.dumps(self.tool_calls, default=str),
                    decision=self.decision,
                    latency_ms=elapsed_ms,
                    cost_estimate_usd=self.cost_estimate_usd,
                    incident_id=self.incident_id,
                )
                session.add(row)
                session.commit()
        except Exception:  # noqa: BLE001
            # Never let trace persistence break the agent itself.
            logger.exception("Failed to persist AgentTrace for %s", self.agent)

        await bus.publish(
            "agent.trace",
            {
                "agent": self.agent,
                "trigger": self.trigger,
                "decision": self.decision,
                "latency_ms": elapsed_ms,
                "incident_id": self.incident_id,
                "tool_calls": self.tool_calls,
                "timestamp": datetime.utcnow().isoformat(),
            },
        )

    def add_tool_call(self, name: str, args: dict[str, Any], result: Any) -> None:
        self.tool_calls.append(
            {
                "tool": name,
                "args": args,
                "result": result,
                "at": datetime.utcnow().isoformat(),
            }
        )
