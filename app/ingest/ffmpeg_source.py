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
from pathlib import Path

from app.ingest.base import Frame, JpegStreamParser, jpeg_dimensions

logger = logging.getLogger(__name__)

_CHUNK_SIZE = 256 * 1024
MIN_LOOP_SECONDS = 1.0
MAX_LOOP_SECONDS = 180.0


def _unlink(path: Path | None) -> None:
    if path is None:
        return
    with contextlib.suppress(OSError):
        path.unlink()


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
        self._single_pass = False
        self._wake: asyncio.Event | None = None

        # Section looping: while active, ``uri`` points at a short cut clip and
        # ``_pos_base`` maps its local time back to whole-reel time.
        self._full_uri = uri
        self._full_duration: float | None = None
        self._pos_base = 0.0
        self._loop_range: tuple[float, float] | None = None
        self._loop_clip: Path | None = None

    # ---- playback control ---------------------------------------------------

    seekable = False

    @property
    def paused(self) -> bool:
        return self._paused

    @property
    def hold_id(self) -> int:
        """Bumps each time a new frozen frame is produced while paused."""
        return self._hold_id

    @property
    def loop_range(self) -> tuple[float, float] | None:
        """``(start, end)`` in whole-reel seconds while a section loop is active."""
        return self._loop_range

    def _local_position(self) -> float:
        if self._paused:
            return self._pause_pos
        if self._proc_started is None:
            return self._start_offset
        elapsed = (time.monotonic() - self._proc_started) * max(self.speed, 0.0)
        pos = self._start_offset + elapsed
        return pos % self.duration if self.duration else pos

    def position(self) -> float:
        """Current position in whole-reel seconds (approximate while playing)."""
        return self._pos_base + self._local_position()

    def seek(self, position: float) -> float:
        """Jump to ``position`` (whole-reel seconds). Returns the clamped position.

        Inside an active loop the seek stays in the loop; outside it ends the loop.
        """
        if not self.seekable:
            raise RuntimeError("source is not seekable")
        position = max(0.0, float(position))
        if self._loop_range is not None:
            start, end = self._loop_range
            if not (start <= position <= end):
                self.clear_loop(resume_at=position)
        local = position - self._pos_base
        if self.duration:
            local = min(local, max(self.duration - 0.2, 0.0))
        local = max(local, 0.0)
        if self._paused:
            self._pause_pos = local
            self._refresh_hold = True
        else:
            self._start_offset = local
            self._proc_started = None
            self._restart_now = True
            self._kill_current()
        self._wake_up()
        return self._pos_base + local

    def pause(self) -> None:
        if not self.seekable or self._paused:
            return
        self._pause_pos = self._local_position()
        self._paused = True
        self._hold = None
        self._refresh_hold = True
        self._kill_current()
        self._wake_up()

    async def set_loop(self, start: float, end: float, clip_dir: Path) -> tuple[float, float]:
        """Loop the section ``[start, end]`` (whole-reel seconds) seamlessly.

        ffmpeg cannot loop a sub-range of one file, so the section is cut into a
        small temporary clip (fast: seconds of video, not the whole reel) and
        that clip is looped instead.
        """
        if not self.seekable:
            raise RuntimeError("source is not seekable")
        full = self._full_duration
        start = max(0.0, float(start))
        end = float(end)
        if full:
            end = min(end, full)
        if end - start < MIN_LOOP_SECONDS:
            raise ValueError(f"loop must be at least {MIN_LOOP_SECONDS:g}s long")
        if end - start > MAX_LOOP_SECONDS:
            raise ValueError(f"loop must be at most {MAX_LOOP_SECONDS:g}s long")

        clip_dir.mkdir(parents=True, exist_ok=True)
        clip = clip_dir / f"{self.camera_id}_{int(time.time() * 1000)}.mp4"
        await self._cut(start, end - start, clip)

        old = self._loop_clip
        self.uri = str(clip)
        self.duration = end - start
        self._pos_base = start
        self._loop_range = (start, end)
        self._loop_clip = clip
        if self._paused:
            self._pause_pos = 0.0
            self._refresh_hold = True
        else:
            self._start_offset = 0.0
            self._proc_started = None
            self._restart_now = True
            self._kill_current()
        self._wake_up()
        _unlink(old)
        return start, end

    def clear_loop(self, resume_at: float | None = None) -> None:
        """Leave the loop and carry on from ``resume_at`` (default: where it is now)."""
        if self._loop_range is None:
            return
        pos = self.position() if resume_at is None else float(resume_at)
        old = self._loop_clip
        self.uri = self._full_uri
        self.duration = self._full_duration
        self._pos_base = 0.0
        self._loop_range = None
        self._loop_clip = None
        if self._paused:
            self._pause_pos = pos
            self._refresh_hold = True
        else:
            self._start_offset = pos
            self._proc_started = None
            self._restart_now = True
            self._kill_current()
        self._wake_up()
        _unlink(old)

    async def _cut(self, start: float, length: float, out: Path) -> None:
        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
            "-ss", f"{start:.3f}", "-t", f"{length:.3f}", "-i", self._full_uri, "-an",
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "22",
            "-g", "10", "-keyint_min", "10", "-sc_threshold", "0",
            "-pix_fmt", "yuv420p", str(out),
        ]  # fmt: skip
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdin=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
        )
        try:
            _, err = await asyncio.wait_for(proc.communicate(), timeout=60.0)
        except TimeoutError as e:
            proc.kill()
            raise RuntimeError("cutting the loop section timed out") from e
        if proc.returncode != 0 or not out.is_file():
            raise RuntimeError(
                f"cutting the loop section failed: {err.decode(errors='replace')[:200]}"
            )

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
        # -ss combined with -stream_loop mis-times every loop pass: with -readrate ffmpeg then
        # sleeps ~(start offset) seconds at each wrap, long enough to stall the stream. So a
        # seeked start plays through ONCE; at its end _stream restarts from 0 with a clean loop.
        self._single_pass = self._start_offset > 0
        if (self.loop or self._loop_range is not None) and not self._single_pass:
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
            if self._closed or not (self.loop or self._loop_range is not None):
                break

            if self._single_pass:
                # Planned end of a seeked pass: loop again from the start, no delay.
                self._start_offset = 0.0
                self._proc_started = None
                continue

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
