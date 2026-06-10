"""Analytics-gated capture watcher — replaces the dumb auto-capture loop.

The watcher polls Meraki's on-device analytics (``/devices/{serial}/camera/
analytics/recent``) instead of blindly grabbing snapshots. A snapshot is only
fetched, persisted, and published onto the ``camera.snapshot`` bus topic when
the camera reports a person in the Full-Frame zone (``zoneId == "0"``).

Trigger rule (per poll, per camera):

    endTs advanced AND
    (averageCount > 0  OR  entrances > last_entrances)

This catches both "someone is currently in frame" (averageCount > 0) and
"someone just walked in" (entrances rising edge). Otherwise we publish a
quiet tick and skip the Meraki snapshot generation — no VLM cost, no Meraki
snapshot quota burned.

Settings persisted in the ``Setting`` table:
    analytics_watcher.<serial>.enabled         "true" | "false"
    analytics_watcher.<serial>.interval_seconds  integer >= MIN_INTERVAL_SECONDS

Restoration: :func:`restore_on_startup` re-spawns loops for every camera whose
``enabled`` is ``true``. Default is off on first boot.

Safety rails:
* Idle-skip when every gated VLM agent is passive — no analytics call either.
* Exponential back-off on consecutive Meraki errors, capped at
  :data:`MAX_BACKOFF_SECONDS`.
* :data:`MAX_RUNTIME_SECONDS` auto-shutoff guard.
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
from app.integrations.meraki import (
    MerakiError,
    fetch_and_persist_snapshot,
    get_client,
)
from app.models.db import Setting
from app.runtime.bus import bus
from app.services.agent_mode import GATED_AGENTS, is_active

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL_SECONDS = 5
MIN_INTERVAL_SECONDS = 2
MAX_INTERVAL_SECONDS = 60
MAX_BACKOFF_SECONDS = 60
MAX_RUNTIME_SECONDS = 30 * 60  # 30-minute safety auto-shutoff

FULL_FRAME_ZONE_ID = "0"

_KEY_ENABLED = "analytics_watcher.{serial}.enabled"
_KEY_INTERVAL = "analytics_watcher.{serial}.interval_seconds"


@dataclass
class _RuntimeState:
    """In-memory state for a running loop (not persisted)."""

    started_at: float = 0.0
    last_status: str = "pending"
    last_run: datetime | None = None
    backoff: int = DEFAULT_INTERVAL_SECONDS
    # Gate-tracking state for rising-edge detection
    last_end_ts: str | None = None
    last_entrances: int = 0
    last_average_count: float = 0.0


@dataclass
class WatcherState:
    """Combined persisted + runtime view of one camera's watcher."""

    serial: str
    enabled: bool
    interval_seconds: int
    running: bool = False
    running_for_seconds: int = 0
    last_status: str = "idle"
    last_run: str | None = None
    last_average_count: float = 0.0
    last_entrances: int = 0
    max_runtime_seconds: int = MAX_RUNTIME_SECONDS

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


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
    interval = max(MIN_INTERVAL_SECONDS, min(MAX_INTERVAL_SECONDS, interval))
    return enabled, interval


def _write_state(serial: str, enabled: bool, interval: int) -> None:
    with Session(get_engine()) as session:
        _set_setting(session, _KEY_ENABLED.format(serial=serial), "true" if enabled else "false")
        _set_setting(session, _KEY_INTERVAL.format(serial=serial), str(interval))
        session.commit()


# ---- public API ------------------------------------------------------------


def get_state(serial: str) -> WatcherState:
    """Snapshot of persisted + runtime state for ``serial``."""
    enabled, interval = _read_state(serial)
    rt = _runtime.get(serial)
    running = serial in _tasks and not _tasks[serial].done()
    return WatcherState(
        serial=serial,
        enabled=enabled,
        interval_seconds=interval,
        running=running,
        running_for_seconds=(int(time.monotonic() - rt.started_at) if rt and running else 0),
        last_status=rt.last_status if rt else "idle",
        last_run=rt.last_run.isoformat() if rt and rt.last_run else None,
        last_average_count=rt.last_average_count if rt else 0.0,
        last_entrances=rt.last_entrances if rt else 0,
    )


def all_states() -> list[WatcherState]:
    """One WatcherState per camera in the local YAML config.

    Kept for backwards compatibility with callers that only want YAML cameras.
    Use the ``/cameras/analytics-watcher`` API for the YAML + Meraki union.
    """
    from app.services.config_loader import load_cameras

    return [get_state(c["serial"]) for c in load_cameras()]


async def enable(serial: str, interval_seconds: int | None = None) -> WatcherState:
    """Persist enabled=true and spawn the loop if not already running."""
    raw = DEFAULT_INTERVAL_SECONDS if interval_seconds is None else int(interval_seconds)
    interval = max(MIN_INTERVAL_SECONDS, min(MAX_INTERVAL_SECONDS, raw))
    _write_state(serial, enabled=True, interval=interval)
    _spawn(serial, interval)
    logger.info("analytics_watcher: enabled %s @ %ds", serial, interval)
    return get_state(serial)


async def disable(serial: str) -> WatcherState:
    """Persist enabled=false and cancel the loop if running."""
    _write_state(serial, enabled=False, interval=_read_state(serial)[1])
    await _cancel(serial)
    logger.info("analytics_watcher: disabled %s", serial)
    return get_state(serial)


def restore_on_startup() -> None:
    """Re-spawn loops for every serial persisted as enabled. Idempotent.

    Scans the ``Setting`` table for any ``analytics_watcher.<serial>.enabled``
    row set to ``"true"`` and respawns its loop. This lets watchers survive
    a restart even for cameras that aren't in ``cameras.yaml`` (i.e. cameras
    discovered live from Meraki and enabled via the dashboard).
    """
    prefix = "analytics_watcher."
    suffix = ".enabled"
    with Session(get_engine()) as session:
        rows = session.exec(
            select(Setting).where(
                Setting.key.startswith(prefix),  # type: ignore[attr-defined]
                Setting.key.endswith(suffix),  # type: ignore[attr-defined]
            )
        ).all()
    for row in rows:
        if row.value != "true":
            continue
        serial = row.key[len(prefix) : -len(suffix)]
        if not serial:
            continue
        _, interval = _read_state(serial)
        _spawn(serial, interval)
        logger.info("analytics_watcher: restored %s @ %ds", serial, interval)


# ---- task lifecycle --------------------------------------------------------


def _log_task_exception(serial: str, task: asyncio.Task[None]) -> None:
    """Surface task exceptions that asyncio would otherwise swallow.

    Without this, a crash inside ``_run()`` before its first ``await`` (or any
    uncaught exception) leaves the watcher in a "running but dead" state with
    no log line. ``add_done_callback`` runs synchronously when the task ends.
    """
    if task.cancelled():
        return
    exc = task.exception()
    if exc is None:
        return
    logger.error(
        "analytics_watcher: task for %s died with %s: %s",
        serial,
        type(exc).__name__,
        exc,
        exc_info=exc,
    )
    # Clear stale entries so a subsequent enable() can respawn cleanly.
    _tasks.pop(serial, None)
    _runtime.pop(serial, None)


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
    task = asyncio.create_task(_run(serial, interval))
    task.add_done_callback(lambda t: _log_task_exception(serial, t))
    _tasks[serial] = task


async def _cancel(serial: str) -> None:
    task = _tasks.pop(serial, None)
    _runtime.pop(serial, None)
    if task and not task.done():
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task


async def _publish_tick(serial: str, status: str, **extra: Any) -> None:
    await bus.publish(
        "analytics_watcher.tick",
        {
            "serial": serial,
            "status": status,
            "timestamp": datetime.utcnow().isoformat(),
            **extra,
        },
    )


def _extract_full_frame(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Pick the Full Frame zone (id "0") from a /analytics/recent response."""
    for row in rows or []:
        if str(row.get("zoneId")) == FULL_FRAME_ZONE_ID:
            return row
    return None


def _should_trigger(rt: _RuntimeState, ff: dict[str, Any]) -> bool:
    """Apply the gate rule: endTs advanced AND (avgCount>0 OR entrances rose)."""
    end_ts = ff.get("endTs")
    if not end_ts or end_ts == rt.last_end_ts:
        return False
    avg = float(ff.get("averageCount", 0) or 0)
    entrances = int(ff.get("entrances", 0) or 0)
    rising = entrances > rt.last_entrances
    return avg > 0.0 or rising


async def _run(serial: str, base_interval: int) -> None:
    """Main loop body for one camera. Exits on cancel or auto-shutoff."""
    rt = _runtime[serial]
    client = get_client()
    try:
        while True:
            # 30-minute auto-shutoff
            if time.monotonic() - rt.started_at > MAX_RUNTIME_SECONDS:
                rt.last_status = "auto-shutoff"
                await _publish_tick(serial, "auto-shutoff", reason="max-runtime")
                _write_state(serial, enabled=False, interval=base_interval)
                logger.info("analytics_watcher: auto-shutoff for %s after 30min", serial)
                _tasks.pop(serial, None)
                return

            # Idle skip when every gated agent is passive — also skips the
            # Meraki analytics call so we make zero requests in steady state.
            if not any(is_active(a) for a in GATED_AGENTS):
                rt.last_status = "idle"
                rt.last_run = datetime.utcnow()
                await _publish_tick(serial, "idle")
                await asyncio.sleep(base_interval)
                continue

            # Poll Meraki analytics
            try:
                rows = await client.get_camera_analytics_recent(serial)
            except MerakiError as e:
                rt.last_status = f"error: {e}"
                rt.last_run = datetime.utcnow()
                rt.backoff = min(max(rt.backoff * 2, base_interval), MAX_BACKOFF_SECONDS)
                await _publish_tick(serial, "error", error=str(e), next_in=rt.backoff)
                logger.warning(
                    "analytics_watcher: meraki analytics error for %s; backing off to %ds: %s",
                    serial,
                    rt.backoff,
                    e,
                )
                await asyncio.sleep(rt.backoff)
                continue

            # Success — reset backoff
            rt.backoff = base_interval
            ff = _extract_full_frame(rows)
            if ff is None or "error" in ff:
                # No Full Frame analytics yet — quiet tick, no snapshot.
                rt.last_status = "quiet"
                rt.last_run = datetime.utcnow()
                await _publish_tick(
                    serial,
                    "quiet",
                    reason="no-full-frame" if ff is None else ff.get("error", "no-data"),
                )
                await asyncio.sleep(base_interval)
                continue

            avg = float(ff.get("averageCount", 0) or 0)
            entrances = int(ff.get("entrances", 0) or 0)
            triggered = _should_trigger(rt, ff)
            entrances_delta = max(0, entrances - rt.last_entrances)

            # Always advance the window tracker after a successful poll.
            rt.last_end_ts = ff.get("endTs")
            rt.last_entrances = entrances
            rt.last_average_count = avg
            rt.last_run = datetime.utcnow()

            if not triggered:
                rt.last_status = "quiet"
                await _publish_tick(
                    serial,
                    "quiet",
                    average_count=avg,
                    entrances=entrances,
                )
                await asyncio.sleep(base_interval)
                continue

            # Person present — fetch snapshot + publish to VLM agents.
            try:
                path = await fetch_and_persist_snapshot(serial)
            except MerakiError as e:
                rt.last_status = f"snapshot-error: {e}"
                rt.backoff = min(max(rt.backoff * 2, base_interval), MAX_BACKOFF_SECONDS)
                await _publish_tick(
                    serial,
                    "error",
                    error=str(e),
                    next_in=rt.backoff,
                    stage="snapshot",
                )
                logger.warning(
                    "analytics_watcher: snapshot fetch error for %s; backing off to %ds: %s",
                    serial,
                    rt.backoff,
                    e,
                )
                await asyncio.sleep(rt.backoff)
                continue

            await bus.publish(
                "camera.snapshot",
                {
                    "serial": serial,
                    "path": str(path),
                    "timestamp": datetime.utcnow().isoformat(),
                    "trigger": "watcher",
                },
            )
            rt.last_status = "person"
            await _publish_tick(
                serial,
                "person",
                snapshot_path=str(path),
                average_count=avg,
                entrances=entrances,
                entrances_delta=entrances_delta,
            )
            await asyncio.sleep(base_interval)
    except asyncio.CancelledError:
        logger.debug("analytics_watcher: cancelled for %s", serial)
        raise


# ---- test helpers ----------------------------------------------------------


def _reset_for_tests() -> None:
    """Clear in-memory state. Call from test fixtures."""
    _tasks.clear()
    _runtime.clear()
