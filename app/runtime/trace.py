from __future__ import annotations

import json
import time
from collections.abc import Callable
from datetime import datetime
from functools import wraps
from typing import Any

from app.models.db import AgentTrace
from app.runtime.bus import bus


def trace(agent_name: str) -> Callable:  # type: ignore[type-arg]
    """Decorator that records an AgentTrace for any async agent method."""

    def decorator(fn: Callable) -> Callable:  # type: ignore[type-arg]
        @wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            start = time.monotonic()
            tool_calls: list[dict[str, Any]] = []
            result = None

            try:
                result = await fn(*args, tool_calls=tool_calls, **kwargs)
            finally:
                elapsed = int((time.monotonic() - start) * 1000)
                record = AgentTrace(
                    agent=agent_name,
                    trigger=kwargs.get("trigger", "unknown"),
                    inputs_json=json.dumps(kwargs.get("inputs", {}), default=str),
                    tool_calls_json=json.dumps(tool_calls, default=str),
                    decision=str(result) if result else "",
                    latency_ms=elapsed,
                )
                # Publish trace event so dashboard SSE picks it up
                await bus.publish(
                    "agent.trace",
                    {
                        "agent": agent_name,
                        "latency_ms": elapsed,
                        "decision": record.decision,
                        "timestamp": datetime.utcnow().isoformat(),
                    },
                )
            return result

        return wrapper

    return decorator
