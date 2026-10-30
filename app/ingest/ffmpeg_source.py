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
        return await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

    async def _stream(self) -> AsyncIterator[Frame]:
        seq = 0
        started = time.monotonic()
        delay = self.restart_delay
        while not self._closed:
            proc = await self._spawn()
            stderr_task = asyncio.create_task(self._drain_stderr(proc))
            try:
                stdout = proc.stdout
                if stdout is None:  # pragma: no cover - defensive
                    return
                parser = JpegStreamParser()
                async for chunk in _read_chunks(stdout):
                    for jpeg in parser.feed(chunk):
                        try:
                            width, height = jpeg_dimensions(jpeg)
                        except ValueError:
                            logger.warning("Dropping malformed frame from %s", self.camera_id)
                            continue
                        now = time.monotonic()
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

            if self._closed or not self.loop:
                break

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
