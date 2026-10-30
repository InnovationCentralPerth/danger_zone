"""Source-agnostic video ingest primitives.

Every ingest backend (file, RTSP, synthetic, ...) implements the same tiny
:class:`FrameSource` protocol and yields :class:`Frame` objects. Nothing
downstream — agents, detectors, dashboard — needs to know where a frame came
from. Swapping a recorded clip for a live Meraki RTSP stream is a config
change, not a code change.

The module also holds the small helpers shared by the backends:

* :class:`JpegStreamParser` — split a concatenated MJPEG byte stream into
  individual JPEG frames (what ``ffmpeg -f image2pipe`` emits).
* :func:`jpeg_dimensions` — read width/height straight from the JPEG header.
* :class:`FrameRing` — bounded drop-oldest buffer so a slow consumer (a 235B
  VLM, say) can never stall ingest.
* :class:`RateGate` — emit at most *n* frames per second.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

# Cap the parser's internal buffer so a corrupt stream can't grow it forever.
_MAX_PARSE_BUFFER = 32 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class Frame:
    """A single decoded video frame plus provenance metadata.

    ``jpeg`` is always a complete, self-contained JPEG image so it can be
    written to disk or sent to a VLM without further processing.
    """

    camera_id: str
    seq: int
    source_ts: float
    ingest_ts: float
    width: int
    height: int
    jpeg: bytes


@runtime_checkable
class FrameSource(Protocol):
    """Anything that can produce a stream of frames for one camera."""

    camera_id: str

    def stream(self) -> AsyncIterator[Frame]:
        """Yield frames until the source is closed or exhausted."""
        ...

    async def aclose(self) -> None:
        """Stop the source and release any underlying resources."""
        ...


def jpeg_dimensions(data: bytes) -> tuple[int, int]:
    """Return ``(width, height)`` from a JPEG's SOF marker.

    Cheaper than decoding the image just to learn its size. Raises
    :class:`ValueError` if no start-of-frame marker is present.
    """
    n = len(data)
    i = 2  # skip the leading SOI marker
    while i + 9 < n:
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        # Markers without a payload segment.
        if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7 or marker == 0x01:
            i += 2
            continue
        seg_len = int.from_bytes(data[i + 2 : i + 4], "big")
        # SOF0..SOF15, excluding DHT (C4), JPG (C8) and DAC (CC).
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            height = int.from_bytes(data[i + 5 : i + 7], "big")
            width = int.from_bytes(data[i + 7 : i + 9], "big")
            return width, height
        i += 2 + seg_len
    raise ValueError("JPEG has no SOF marker")


class JpegStreamParser:
    """Incrementally split a concatenated MJPEG stream into JPEG frames."""

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, chunk: bytes) -> list[bytes]:
        self._buf.extend(chunk)
        frames: list[bytes] = []
        while True:
            start = self._buf.find(b"\xff\xd8")
            if start < 0:
                # Keep the final byte: it may be the 0xFF half of a marker
                # split across two chunks.
                if len(self._buf) > 1:
                    del self._buf[:-1]
                break
            end = self._buf.find(b"\xff\xd9", start + 2)
            if end < 0:
                # Drop any junk before the frame we're still waiting on.
                if start > 0:
                    del self._buf[:start]
                break
            frames.append(bytes(self._buf[start : end + 2]))
            del self._buf[: end + 2]

        if len(self._buf) > _MAX_PARSE_BUFFER:
            self._buf.clear()
        return frames


class FrameRing:
    """Bounded drop-oldest ring buffer of recent frames for one camera."""

    def __init__(self, maxlen: int = 16) -> None:
        if maxlen < 1:
            raise ValueError("maxlen must be >= 1")
        self._frames: deque[Frame] = deque(maxlen=maxlen)

    def push(self, frame: Frame) -> None:
        self._frames.append(frame)

    def latest(self) -> Frame | None:
        return self._frames[-1] if self._frames else None

    def snapshot(self) -> list[Frame]:
        return list(self._frames)

    def __len__(self) -> int:
        return len(self._frames)


class RateGate:
    """Allow at most ``fps`` emissions per second.

    ``fps <= 0`` disables gating (every call returns ``True``).
    """

    def __init__(self, fps: float) -> None:
        self.interval = 1.0 / fps if fps > 0 else 0.0
        self._last = float("-inf")

    def ready(self, now: float | None = None) -> bool:
        if self.interval <= 0:
            return True
        now = time.monotonic() if now is None else now
        if now - self._last >= self.interval:
            self._last = now
            return True
        return False
