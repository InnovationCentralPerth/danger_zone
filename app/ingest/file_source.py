"""Recorded-clip source — the dependable option for a live demo."""

from __future__ import annotations

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
