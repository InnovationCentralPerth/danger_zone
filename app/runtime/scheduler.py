from __future__ import annotations

import asyncio
import logging
from typing import Callable, Coroutine, Any

logger = logging.getLogger(__name__)


class Scheduler:
    """Runs coroutine tasks on a fixed interval."""

    def __init__(self) -> None:
        self._tasks: list[tuple[int, Callable[[], Coroutine[Any, Any, None]]]] = []
        self._running = False

    def register(self, interval_seconds: int, fn: Callable[[], Coroutine[Any, Any, None]]) -> None:
        self._tasks.append((interval_seconds, fn))

    async def start(self) -> None:
        self._running = True
        for interval, fn in self._tasks:
            asyncio.create_task(self._loop(interval, fn))

    async def _loop(self, interval: int, fn: Callable[[], Coroutine[Any, Any, None]]) -> None:
        while self._running:
            try:
                await fn()
            except Exception:
                logger.exception("Scheduler task %s failed", fn.__name__)
            await asyncio.sleep(interval)

    def stop(self) -> None:
        self._running = False


scheduler = Scheduler()
