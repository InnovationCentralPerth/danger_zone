"""ffmpeg-backed frame sources.

A single subprocess pipeline decodes any input ffmpeg understands (files,
RTSP, HTTP/HLS, ...) and writes a paced MJPEG stream to stdout. We split that
stream with :class:`~app.ingest.base.JpegStreamParser` and hand back
:class:`~app.ingest.base.Frame` objects.

Using ffmpeg rather than a Python decoder keeps the dependency footprint
small, gets hardware-friendly decoding for free, and means the exact same
code path works for a recorded clip and a live camera.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncIterator, Sequence

from app.ingest.base import Frame, JpegStreamParser, jpeg_dimensions

logger = logging.getLogger(__name__)

_CHUNK_SIZE = 256 * 1024


class FfmpegFrameSource:
    """Base class for sources decoded by an ffmpeg subprocess."""

    def __init__(
        self,
        camera_id: str,
        uri: str,
        *,
        fps: float = 5.0,
        width: int = 1280,
        loop: bool = False,
        speed: float = 1.0,
        jpeg_quality: int = 5,
        extra_input_args: Sequence[str] = (),
        restart_delay: float = 2.0,
    ) -> None:
        if fps <= 0:
            raise ValueError("fps must be > 0")
        if width < 2:
            raise ValueError("width must be >= 2")
        self.camera_id = camera_id
        self.uri = uri
        self.fps = fps
        self.width = width
        self.loop = loop
        self.speed = speed
        self.jpeg_quality = jpeg_quality
        self.extra_input_args = list(extra_input_args)
        self.restart_delay = restart_delay
        self._closed = False

        # Playback control (only used by seekable sources such as FileSource).
        self.duration: float | None = None
        self._start_offset = 0.0
        self._proc: asyncio.subprocess.Process | None = None
        self._proc_started: float | None = None
        self._paused = False
        self._pause_pos = 0.0
        self._hold: bytes | None = None
        self._hold_id = 0
        self._refresh_hold = False
        self._restart_now = False
        self._wake: asyncio.Event | None = None

    # ---- playback control ---------------------------------------------------

    seekable = False

    @property
    def paused(self) -> bool:
        return self._paused

    @property
    def hold_id(self) -> int:
        """Bumps each time a new frozen frame is produced while paused."""
        return self._hold_id

    def position(self) -> float:
        """Current playback position in seconds (approximate while playing)."""
        if self._paused:
            return self._pause_pos
        if self._proc_started is None:
            return self._start_offset
        elapsed = (time.monotonic() - self._proc_started) * max(self.speed, 0.0)
        pos = self._start_offset + elapsed
        return pos % self.duration if self.duration else pos

    def seek(self, position: float) -> float:
        """Jump to ``position`` seconds. Returns the clamped position."""
        if not self.seekable:
            raise RuntimeError("source is not seekable")
        position = max(0.0, float(position))
        if self.duration:
            position = min(position, max(self.duration - 0.2, 0.0))
        if self._paused:
            self._pause_pos = position
            self._refresh_hold = True
        else:
            self._start_offset = position
            self._proc_started = None
            self._restart_now = True
            self._kill_current()
        self._wake_up()
        return position

    def pause(self) -> None:
        if not self.seekable or self._paused:
            return
        self._pause_pos = self.position()
        self._paused = True
        self._hold = None
        self._refresh_hold = True
        self._kill_current()
        self._wake_up()

    def resume(self) -> None:
        if not self.seekable or not self._paused:
            return
        self._paused = False
        self._start_offset = self._pause_pos
        self._proc_started = None
        self._hold = None
        self._wake_up()

    def _kill_current(self) -> None:
        proc = self._proc
        if proc is not None and proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.terminate()

    def _wake_up(self) -> None:
        if self._wake is not None:
            self._wake.set()

    async def _decode_one(self, position: float) -> bytes | None:
        """Decode a single frame at ``position`` (used while paused)."""
        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
            "-ss", f"{position:.3f}", "-i", self.uri, "-an",
            "-vf", f"scale={self.width}:-2", "-frames:v", "1",
            "-f", "image2pipe", "-vcodec", "mjpeg", "-q:v", str(self.jpeg_quality), "-",
        ]  # fmt: skip
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=20.0)
        except (TimeoutError, OSError):
            return None
        return out or None

    # ---- FrameSource protocol ---------------------------------------------

    def stream(self) -> AsyncIterator[Frame]:
        return self._stream()

    async def aclose(self) -> None:
        self._closed = True

    # ---- internals ---------------------------------------------------------

    def _build_command(self) -> list[str]:
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin"]
        if self.loop:
            cmd += ["-stream_loop", "-1"]
        if self.speed > 0:
            # -readrate paces input in real time (like -re) at a multiplier.
            cmd += ["-readrate", f"{self.speed:g}"]
        if self._start_offset > 0:
            cmd += ["-ss", f"{self._start_offset:.3f}"]
        cmd += self.extra_input_args
        cmd += [
            "-i",
            self.uri,
            "-an",
            "-vf",
            f"fps={self.fps:g},scale={self.width}:-2",
            "-f",
            "image2pipe",
            "-vcodec",
            "mjpeg",
            "-q:v",
            str(self.jpeg_quality),
            "-",
        ]
        return cmd

    async def _spawn(self) -> asyncio.subprocess.Process:
        cmd = self._build_command()
        logger.info("Starting ingest for %s: %s", self.camera_id, " ".join(cmd))
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._proc = proc
        # Playback position is anchored to the first decoded frame (see _stream),
        # not process start, so a slow seek does not make the slider run ahead.
        self._proc_started = None
        return proc

    async def _stream(self) -> AsyncIterator[Frame]:
        seq = 0
        started = time.monotonic()
        delay = self.restart_delay
        self._wake = asyncio.Event()
        while not self._closed:
            if self._paused:
                if self._refresh_hold or self._hold is None:
                    self._refresh_hold = False
                    jpeg = await self._decode_one(self._pause_pos)
                    if jpeg is not None and self._paused:
                        self._hold = jpeg
                        self._hold_id += 1
                if self._hold is not None:
                    try:
                        width, height = jpeg_dimensions(self._hold)
                    except ValueError:
                        self._hold = None
                    else:
                        now = time.monotonic()
                        yield Frame(
                            camera_id=self.camera_id,
                            seq=seq,
                            source_ts=now - started,
                            ingest_ts=now,
                            width=width,
                            height=height,
                            jpeg=self._hold,
                        )
                        seq += 1
                self._wake.clear()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._wake.wait(), timeout=0.5)
                continue

            proc = await self._spawn()
            stderr_task = asyncio.create_task(self._drain_stderr(proc))
            try:
                stdout = proc.stdout
                if stdout is None:  # pragma: no cover - defensive
                    return
                parser = JpegStreamParser()
                async for chunk in _read_chunks(stdout):
                    if self._restart_now or self._paused:
                        break  # seek/pause requested: drop frames from the old position
                    for jpeg in parser.feed(chunk):
                        if self._restart_now or self._paused:
                            break
                        try:
                            width, height = jpeg_dimensions(jpeg)
                        except ValueError:
                            logger.warning("Dropping malformed frame from %s", self.camera_id)
                            continue
                        now = time.monotonic()
                        if self._proc_started is None:
                            self._proc_started = now
                        yield Frame(
                            camera_id=self.camera_id,
                            seq=seq,
                            source_ts=now - started,
                            ingest_ts=now,
                            width=width,
                            height=height,
                            jpeg=jpeg,
                        )
                        seq += 1
                        delay = self.restart_delay
            finally:
                await self._terminate(proc)
                stderr_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await stderr_task

            if self._restart_now or self._paused:
                self._restart_now = False
                continue
            if self._closed or not self.loop:
                break

            self._start_offset = 0.0
            logger.warning("Ingest stream for %s ended; restarting in %.1fs", self.camera_id, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30.0)

    async def _drain_stderr(self, proc: asyncio.subprocess.Process) -> None:
        if proc.stderr is None:  # pragma: no cover - defensive
            return
        while True:
            line = await proc.stderr.readline()
            if not line:
                return
            logger.warning("ffmpeg[%s]: %s", self.camera_id, line.decode(errors="replace").rstrip())

    @staticmethod
    async def _terminate(proc: asyncio.subprocess.Process) -> None:
        if proc.returncode is not None:
            return
        with contextlib.suppress(ProcessLookupError):
            proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            with contextlib.suppress(Exception):
                await proc.wait()


async def _read_chunks(stream: asyncio.StreamReader) -> AsyncIterator[bytes]:
    while True:
        chunk = await stream.read(_CHUNK_SIZE)
        if not chunk:
            return
        yield chunk
