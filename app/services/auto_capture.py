"""Auto-capture loop — drives continuous snapshot polling per camera.

A long-running background task per enabled camera fetches a fresh snapshot at
a configurable interval and publishes it onto the ``camera.snapshot`` bus
topic, where the VLM agents (zone_intrusion, ppe_compliance) pick it up just
as they would a motion-triggered or manually-triggered snapshot.

Settings are persisted in the ``Setting`` table (keys
``auto_capture.<serial>.enabled`` and ``auto_capture.<serial>.interval_seconds``)
so a configured loop survives a process restart — :func:`restore_on_startup`
re-spawns the tasks during the FastAPI lifespan.

Safety rails baked in:
* **Default off** on first boot — never burns Meraki quota or VLM tokens unbidden.
* **Minimum interval clamp** of :data:`MIN_INTERVAL_SECONDS`.
* **Idle skip**: when every gated VLM agent is passive, the loop publishes an
  ``auto_capture.tick`` with ``status="idle"`` and skips the Meraki call so
  no quota is spent watching paint dry.
* **Exponential back-off** on consecutive Meraki errors, capped at
  :data:`MAX_BACKOFF_SECONDS`.
* **Auto-shutoff** after :data:`MAX_RUNTIME_SECONDS` of continuous running, to
  prevent a forgotten demo session burning quota over a weekend.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlmodel import Session, select

from app.db import get_engine
from app.integrations.meraki import MerakiError, fetch_and_persist_snapshot
from app.models.db import Setting
from app.runtime.bus import bus
from app.services.agent_mode import GATED_AGENTS, is_active

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL_SECONDS = 5
MIN_INTERVAL_SECONDS = 2
MAX_BACKOFF_SECONDS = 60
MAX_RUNTIME_SECONDS = 30 * 60  # 30-minute safety auto-shutoff

_KEY_ENABLED = "auto_capture.{serial}.enabled"
_KEY_INTERVAL = "auto_capture.{serial}.interval_seconds"


@dataclass
class _RuntimeState:
    """In-memory state for a running loop (not persisted)."""

    started_at: float = 0.0
    last_status: str = "pending"
    last_run: datetime | None = None
    backoff: int = DEFAULT_INTERVAL_SECONDS


@dataclass
class AutoCaptureState:
    """Combined persisted + runtime view of one camera's loop."""

    serial: str
    enabled: bool
    interval_seconds: int
    running: bool = False
    running_for_seconds: int = 0
    last_status: str = "idle"
    last_run: str | None = None
    max_runtime_seconds: int = MAX_RUNTIME_SECONDS

    def as_dict(self) -> dict[str, Any]:
        d = self.__dict__.copy()
        return d


# Registry of in-flight loops, keyed by serial.
_tasks: dict[str, asyncio.Task[None]] = {}
_runtime: dict[str, _RuntimeState] = {}


# ---- persistence helpers ---------------------------------------------------


def _get_setting(session: Session, key: str) -> str | None:
    row = session.exec(select(Setting).where(Setting.key == key)).first()
    return row.value if row else None


def _set_setting(session: Session, key: str, value: str) -> None:
    row = session.exec(select(Setting).where(Setting.key == key)).first()
    if row is None:
        row = Setting(key=key, value=value)
    else:
        row.value = value
    session.add(row)


def _read_state(serial: str) -> tuple[bool, int]:
    """Read the persisted (enabled, interval) for ``serial``."""
    with Session(get_engine()) as session:
        enabled = _get_setting(session, _KEY_ENABLED.format(serial=serial)) == "true"
        raw_interval = _get_setting(session, _KEY_INTERVAL.format(serial=serial))
    try:
        interval = int(raw_interval) if raw_interval else DEFAULT_INTERVAL_SECONDS
    except ValueError:
        interval = DEFAULT_INTERVAL_SECONDS
    return enabled, max(MIN_INTERVAL_SECONDS, interval)


def _write_state(serial: str, enabled: bool, interval: int) -> None:
    with Session(get_engine()) as session:
        _set_setting(session, _KEY_ENABLED.format(serial=serial), "true" if enabled else "false")
        _set_setting(session, _KEY_INTERVAL.format(serial=serial), str(interval))
        session.commit()


# ---- public API ------------------------------------------------------------


def get_state(serial: str) -> AutoCaptureState:
    """Snapshot of persisted + runtime state for ``serial``."""
    enabled, interval = _read_state(serial)
    rt = _runtime.get(serial)
    running = serial in _tasks and not _tasks[serial].done()
    return AutoCaptureState(
        serial=serial,
        enabled=enabled,
        interval_seconds=interval,
        running=running,
        running_for_seconds=(int(time.monotonic() - rt.started_at) if rt and running else 0),
        last_status=rt.last_status if rt else "idle",
        last_run=rt.last_run.isoformat() if rt and rt.last_run else None,
    )


def all_states() -> list[AutoCaptureState]:
    """One AutoCaptureState per camera in the local config."""
    from app.services.config_loader import load_cameras

    return [get_state(c["serial"]) for c in load_cameras()]


async def enable(serial: str, interval_seconds: int | None = None) -> AutoCaptureState:
    """Persist enabled=true and spawn the loop if not already running."""
    raw = DEFAULT_INTERVAL_SECONDS if interval_seconds is None else int(interval_seconds)
    interval = max(MIN_INTERVAL_SECONDS, raw)
    _write_state(serial, enabled=True, interval=interval)
    _spawn(serial, interval)
    logger.info("auto_capture: enabled %s @ %ds", serial, interval)
    return get_state(serial)


async def disable(serial: str) -> AutoCaptureState:
    """Persist enabled=false and cancel the loop if running."""
    _write_state(serial, enabled=False, interval=_read_state(serial)[1])
    await _cancel(serial)
    logger.info("auto_capture: disabled %s", serial)
    return get_state(serial)


def restore_on_startup() -> None:
    """Re-spawn loops for every serial persisted as enabled. Idempotent."""
    from app.services.config_loader import load_cameras

    for cam in load_cameras():
        serial = cam.get("serial")
        if not serial:
            continue
        enabled, interval = _read_state(serial)
        if enabled:
            _spawn(serial, interval)
            logger.info("auto_capture: restored %s @ %ds", serial, interval)


# ---- task lifecycle --------------------------------------------------------


def _spawn(serial: str, interval: int) -> None:
    existing = _tasks.get(serial)
    if existing and not existing.done():
        # Already running — fresh enable() resets the runtime clock + interval.
        _runtime[serial] = _RuntimeState(
            started_at=time.monotonic(),
            backoff=interval,
            last_status="restarted",
        )
        return
    _runtime[serial] = _RuntimeState(started_at=time.monotonic(), backoff=interval)
    _tasks[serial] = asyncio.create_task(_run(serial, interval))


async def _cancel(serial: str) -> None:
    task = _tasks.pop(serial, None)
    _runtime.pop(serial, None)
    if task and not task.done():
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task


async def _publish_tick(serial: str, status: str, **extra: Any) -> None:
    await bus.publish(
        "auto_capture.tick",
        {
            "serial": serial,
            "status": status,
            "timestamp": datetime.utcnow().isoformat(),
            **extra,
        },
    )


async def _run(serial: str, base_interval: int) -> None:
    """Main loop body for one camera. Exits on cancel or auto-shutoff."""
    rt = _runtime[serial]
    try:
        while True:
            # 30-minute auto-shutoff
            if time.monotonic() - rt.started_at > MAX_RUNTIME_SECONDS:
                rt.last_status = "auto-shutoff"
                await _publish_tick(serial, "auto-shutoff", reason="max-runtime")
                _write_state(serial, enabled=False, interval=base_interval)
                logger.info("auto_capture: auto-shutoff for %s after 30min", serial)
                _tasks.pop(serial, None)
                return

            # Idle skip when every gated agent is passive
            if not any(is_active(a) for a in GATED_AGENTS):
                rt.last_status = "idle"
                rt.last_run = datetime.utcnow()
                await _publish_tick(serial, "idle")
                await asyncio.sleep(base_interval)
                continue

            # Fetch + publish
            try:
                path = await fetch_and_persist_snapshot(serial)
                await bus.publish(
                    "camera.snapshot",
                    {
                        "serial": serial,
                        "path": str(path),
                        "timestamp": datetime.utcnow().isoformat(),
                        "trigger": "auto",
                    },
                )
                rt.last_status = "ok"
                rt.last_run = datetime.utcnow()
                rt.backoff = base_interval
                await _publish_tick(serial, "ok")
                await asyncio.sleep(base_interval)
            except MerakiError as e:
                rt.last_status = f"error: {e}"
                rt.last_run = datetime.utcnow()
                rt.backoff = min(max(rt.backoff * 2, base_interval), MAX_BACKOFF_SECONDS)
                await _publish_tick(serial, "error", error=str(e), next_in=rt.backoff)
                logger.warning(
                    "auto_capture: meraki error for %s; backing off to %ds: %s",
                    serial,
                    rt.backoff,
                    e,
                )
                await asyncio.sleep(rt.backoff)
    except asyncio.CancelledError:
        logger.debug("auto_capture: cancelled for %s", serial)
        raise


# ---- test helpers ----------------------------------------------------------


def _reset_for_tests() -> None:
    """Clear in-memory state. Call from test fixtures."""
    _tasks.clear()
    _runtime.clear()
