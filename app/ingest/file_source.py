"""Recorded-clip source — the dependable option for a live demo."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from app.ingest.ffmpeg_source import FfmpegFrameSource


class FileSource(FfmpegFrameSource):
    """Play a local video file (mp4, mkv, mov, ...), optionally looping.

    Frames are paced in real time (``-readrate``) so the demo behaves like a
    live camera rather than a 30-second clip finishing instantly.
    """

    def __init__(
        self,
        camera_id: str,
        uri: str,
        *,
        fps: float = 5.0,
        width: int = 1280,
        loop: bool = True,
        speed: float = 1.0,
    ) -> None:
        path = Path(uri).expanduser()
        if not path.is_file():
            raise FileNotFoundError(f"video file not found: {path}")
        super().__init__(
            camera_id,
            str(path.resolve()),
            fps=fps,
            width=width,
            loop=loop,
            speed=speed,
        )
        self.duration = _probe_duration(self.uri)
        self._full_duration = self.duration

    seekable = True


def _probe_duration(path: str) -> float | None:
    """Clip length in seconds via ffprobe, or None if it can't be read."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", path],
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
        ).stdout
        value = float(json.loads(out)["format"]["duration"])
    except (OSError, subprocess.SubprocessError, KeyError, ValueError):
        return None
    return value if value > 0 else None
