"""Live RTSP source — the Meraki (or any IP camera) backend.

Meraki external RTSP is TCP-only on port 9000, unencrypted, and limited to
one stream per camera. We keep the transport explicit and ask ffmpeg to
reconnect aggressively, since the point of a demo is that the tile recovers
on its own after a network blip.
"""

from __future__ import annotations

from app.ingest.ffmpeg_source import FfmpegFrameSource

_RTSP_INPUT_ARGS = (
    "-rtsp_transport",
    "tcp",
    "-fflags",
    "nobuffer",
    "-flags",
    "low_delay",
    "-probesize",
    "500000",
    "-analyzeduration",
    "1000000",
)


class RtspSource(FfmpegFrameSource):
    """Decode a live RTSP stream (Meraki MV, generic IP camera, NVR)."""

    def __init__(
        self,
        camera_id: str,
        uri: str,
        *,
        fps: float = 5.0,
        width: int = 1280,
        restart_delay: float = 2.0,
    ) -> None:
        super().__init__(
            camera_id,
            uri,
            fps=fps,
            width=width,
            loop=False,
            speed=0.0,
            extra_input_args=_RTSP_INPUT_ARGS,
            restart_delay=restart_delay,
        )
