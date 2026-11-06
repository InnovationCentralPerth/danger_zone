"""Ingest manager — turns configured sources into bus events.

One asyncio task per configured camera. Each task pulls frames from its
:class:`~app.ingest.base.FrameSource`, keeps the most recent frames in a
bounded ring buffer, and:

* publishes ``camera.frame`` for every frame (metadata only — cheap, and the
  hook a future detector service will subscribe to), and
* persists a JPEG and publishes ``camera.snapshot`` at the camera's
  ``vlm_fps`` rate, which is what the existing VLM agents already consume.

Rate-limiting the snapshot events matters: a 235B VLM takes seconds per call,
so publishing every decoded frame would just build an unbounded backlog. The
agents coalesce anyway, but not publishing is cheaper still.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.ingest.base import Frame, FrameRing, FrameSource, RateGate
from app.ingest.file_source import FileSource
from app.ingest.rtsp_source import RtspSource
from app.ingest.synthetic_source import SyntheticSource, phases_from_config
from app.runtime.bus import bus
from app.services.config_loader import load_cameras

logger = logging.getLogger(__name__)

DEFAULT_SAMPLE_FPS = 5.0
DEFAULT_VLM_FPS = 0.2  # one snapshot every 5 seconds
DEFAULT_RING_SIZE = 16


def build_source(camera: dict[str, Any]) -> FrameSource:
    """Instantiate the backend described by ``camera["source"]``.

    Raises :class:`ValueError` for an unknown type or missing URI; the manager
    logs and skips that camera rather than failing the whole app.
    """
    serial = str(camera.get("serial") or "").strip()
    if not serial:
        raise ValueError("camera config is missing 'serial'")

    src = camera.get("source") or {}
    if not isinstance(src, dict):
        raise ValueError(f"camera {serial}: 'source' must be a mapping")
    kind = str(src.get("type", "")).strip().lower()
    fps = float(src.get("fps", camera.get("sample_fps", DEFAULT_SAMPLE_FPS)))
    width = int(src.get("width", 1280))

    if kind == "synthetic":
        return SyntheticSource(
            serial,
            fps=fps,
            width=width,
            height=int(src.get("height", 720)),
            script=phases_from_config(src.get("script")),
        )
    if kind == "file":
        uri = src.get("uri")
        if not uri:
            raise ValueError(f"camera {serial}: file source requires 'uri'")
        return FileSource(
            serial,
            str(uri),
            fps=fps,
            width=width,
            loop=bool(src.get("loop", True)),
            speed=float(src.get("speed", 1.0)),
        )
    if kind == "rtsp":
        uri = src.get("uri")
        if not uri:
            raise ValueError(f"camera {serial}: rtsp source requires 'uri'")
        return RtspSource(serial, str(uri), fps=fps, width=width)

    raise ValueError(f"camera {serial}: unsupported source type {kind!r}")


def _snapshot_path(serial: str) -> Path:
    settings = get_settings()
    ts = datetime.now(UTC)
    folder = settings.snapshots_dir / ts.strftime("%Y/%m/%d")
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"{serial}_{ts.strftime('%H%M%S_%f')}.jpg"


class IngestManager:
    """Owns the per-camera ingest tasks and their frame rings."""

    def __init__(self) -> None:
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._sources: dict[str, FrameSource] = {}
        self._rings: dict[str, FrameRing] = {}
        self._started = False

    # ---- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        """Spawn ingest tasks for every camera that declares a ``source``."""
        if self._started:
            return
        settings = get_settings()
        if not settings.ingest_enabled:
            logger.info("Ingest disabled (INGEST_ENABLED=false)")
            return

        cameras = [c for c in load_cameras() if isinstance(c, dict) and c.get("source")]
        if not cameras:
            logger.info("No cameras with a 'source' block; ingest idle")
            return

        for camera in cameras:
            serial = str(camera.get("serial") or "").strip()
            if not serial:
                logger.warning("Skipping camera with no serial: %s", camera)
                continue
            if serial in self._tasks:
                logger.warning("Duplicate camera serial %s; skipping", serial)
                continue
            try:
                source = build_source(camera)
            except Exception as exc:  # noqa: BLE001 - one bad camera shouldn't kill the app
                logger.error("Failed to start ingest for %s: %s", serial, exc)
                continue
            self._sources[serial] = source
            self._rings[serial] = FrameRing(int(camera.get("ring_size", DEFAULT_RING_SIZE)))
            self._tasks[serial] = asyncio.create_task(
                self._run(camera, source), name=f"ingest:{serial}"
            )
            logger.info(
                "Ingest started for %s (%s, %.1f fps -> %.2f vlm fps)",
                serial,
                camera["source"].get("type"),
                float(camera["source"].get("fps", DEFAULT_SAMPLE_FPS)),
                float(camera.get("vlm_fps", DEFAULT_VLM_FPS)),
            )
        self._started = True

    async def stop(self) -> None:
        """Close sources, cancel tasks, and clear state."""
        for source in self._sources.values():
            with contextlib.suppress(Exception):
                await source.aclose()
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        self._sources.clear()
        self._rings.clear()
        self._started = False

    # ---- accessors ---------------------------------------------------------

    def ring(self, serial: str) -> FrameRing | None:
        return self._rings.get(serial)

    def latest_frame(self, serial: str) -> Frame | None:
        ring = self._rings.get(serial)
        return ring.latest() if ring else None

    def capture_latest(self, serial: str) -> Path | None:
        """Persist the newest buffered frame for ``serial`` (if any).

        Lets the dashboard's "capture now" button work for file/RTSP/synthetic
        cameras that have no Meraki snapshot API behind them.
        """
        frame = self.latest_frame(serial)
        if frame is None:
            return None
        return self._persist(serial, frame)

    def source(self, serial: str) -> FrameSource | None:
        return self._sources.get(serial)

    def playback(self, serial: str) -> dict[str, Any] | None:
        """Playback state for a seekable (file) camera; None if not ingest-backed."""
        src = self._sources.get(serial)
        if src is None:
            return None
        if not getattr(src, "seekable", False):
            return {"supported": False}
        return {
            "supported": True,
            "position": round(float(src.position()), 2),  # type: ignore[attr-defined]
            "duration": getattr(src, "duration", None),
            "paused": bool(src.paused),  # type: ignore[attr-defined]
        }

    def set_playback(
        self, serial: str, *, position: float | None = None, paused: bool | None = None
    ) -> dict[str, Any] | None:
        """Seek and/or pause/resume a file camera. None if the camera is unknown."""
        src = self._sources.get(serial)
        if src is None:
            return None
        if not getattr(src, "seekable", False):
            raise ValueError(f"camera {serial} is not seekable")
        if paused is True:
            src.pause()  # type: ignore[attr-defined]
        if position is not None:
            src.seek(position)  # type: ignore[attr-defined]
        if paused is False:
            src.resume()  # type: ignore[attr-defined]
        return self.playback(serial)

    def status(self) -> list[dict[str, Any]]:
        """Small snapshot for the dashboard / ops endpoints."""
        out: list[dict[str, Any]] = []
        for serial, task in self._tasks.items():
            ring = self._rings.get(serial)
            latest = ring.latest() if ring else None
            out.append(
                {
                    "serial": serial,
                    "source": type(self._sources[serial]).__name__,
                    "running": not task.done(),
                    "buffered": len(ring) if ring else 0,
                    "last_seq": latest.seq if latest else None,
                    "last_ingest_ts": latest.ingest_ts if latest else None,
                }
            )
        return out

    # ---- worker ------------------------------------------------------------

    async def _run(self, camera: dict[str, Any], source: FrameSource) -> None:
        serial = source.camera_id
        ring = self._rings[serial]
        gate = RateGate(float(camera.get("vlm_fps", DEFAULT_VLM_FPS)))
        source_type = str((camera.get("source") or {}).get("type", "unknown"))
        last_hold_id = -1

        try:
            async for frame in source.stream():
                ring.push(frame)
                await bus.publish(
                    "camera.frame",
                    {
                        "serial": serial,
                        "seq": frame.seq,
                        "source": source_type,
                        "width": frame.width,
                        "height": frame.height,
                        "source_ts": frame.source_ts,
                        "ingest_ts": frame.ingest_ts,
                    },
                )
                if getattr(source, "paused", False):
                    # A frozen frame is analysed once per hold, not once per tick.
                    hold_id = getattr(source, "hold_id", 0)
                    if hold_id == last_hold_id:
                        continue
                    last_hold_id = hold_id
                elif not gate.ready(frame.ingest_ts):
                    continue
                path = await asyncio.to_thread(self._persist, serial, frame)
                await bus.publish(
                    "camera.snapshot",
                    {
                        "serial": serial,
                        "path": str(path),
                        "trigger": "ingest",
                        "source": source_type,
                        "seq": frame.seq,
                    },
                )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - keep the other cameras alive
            logger.exception("Ingest stream for %s crashed", serial)

    @staticmethod
    def _persist(serial: str, frame: Frame) -> Path:
        path = _snapshot_path(serial)
        path.write_bytes(frame.jpeg)
        logger.debug("Ingest wrote %s (%d bytes)", path, len(frame.jpeg))
        return path


ingest_manager = IngestManager()
