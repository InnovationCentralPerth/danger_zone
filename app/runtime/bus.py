from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class BusEvent:
    topic: str
    payload: dict[str, Any]
    timestamp: datetime = field(default_factory=datetime.utcnow)


class EventBus:
    """Simple in-process pub/sub using asyncio queues."""

    def __init__(self) -> None:
        self._subscribers: dict[str, list[asyncio.Queue[BusEvent]]] = {}

    def subscribe(self, topic: str) -> asyncio.Queue[BusEvent]:
        q: asyncio.Queue[BusEvent] = asyncio.Queue()
        self._subscribers.setdefault(topic, []).append(q)
        return q

    def unsubscribe(self, topic: str, q: asyncio.Queue[BusEvent]) -> None:
        subs = self._subscribers.get(topic, [])
        if q in subs:
            subs.remove(q)

    async def publish(self, topic: str, payload: dict[str, Any]) -> None:
        event = BusEvent(topic=topic, payload=payload)
        for q in self._subscribers.get(topic, []):
            await q.put(event)
        # Also publish to wildcard subscribers
        for q in self._subscribers.get("*", []):
            await q.put(event)


# Singleton — imported by agents, webhooks, and the SSE endpoint
bus = EventBus()
