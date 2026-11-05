"""Tests for camera metadata and the MJPEG ring stream."""

from __future__ import annotations

import io
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.api.cameras import _mjpeg_frames
from app.ingest.base import Frame, FrameRing
from app.main import app


def _jpeg() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (32, 24), (10, 120, 30)).save(buf, format="JPEG")
    return buf.getvalue()


def _cfg():
    return [
        {
            "serial": "LIVE-1",
            "name": "Live Cam",
            "location": "Lab",
            "source": {"type": "rtsp", "uri": "rtsp://example/live"},
            "vlm_fps": 1.0,
        },
        {
            "serial": "REPLAY-1",
            "name": "Replay Cam",
            "location": "Lab",
            "demo_label": "REPLAY",
            "source": {"type": "file", "uri": "data/demo/x.mp4"},
            "vlm_fps": 2.0,
        },
        {
            "serial": "CLOUD-1",
            "name": "Cloud Cam",
            "location": "Lab",
        },
    ]


def test_camera_list_exposes_source_and_demo_label():
    with patch("app.services.config_loader.load_cameras", return_value=_cfg()):
        client = TestClient(app)
        r = client.get("/cameras/")
    assert r.status_code == 200
    rows = {c["serial"]: c for c in r.json()}
    assert rows["LIVE-1"]["source_type"] == "rtsp"
    assert rows["LIVE-1"]["demo_label"] is None
    assert rows["REPLAY-1"]["source_type"] == "file"
    assert rows["REPLAY-1"]["demo_label"] == "REPLAY"
    assert rows["CLOUD-1"]["source_type"] is None
    assert rows["LIVE-1"]["vlm_fps"] == 1.0


async def test_mjpeg_frames_emits_newest_frame():
    ring = FrameRing(maxlen=2)
    jpeg = _jpeg()
    ring.push(Frame("cam", 0, 0.0, 0.0, 32, 24, jpeg))

    gen = _mjpeg_frames(ring, stale_seconds=1.0, poll_seconds=0.01)
    chunk = await anext(gen)
    await gen.aclose()

    assert chunk.startswith(b"--frame\r\n")
    assert b"Content-Type: image/jpeg" in chunk
    assert jpeg in chunk


async def test_mjpeg_frames_stops_when_source_is_stale():
    ring = FrameRing(maxlen=1)
    gen = _mjpeg_frames(ring, stale_seconds=0.0, poll_seconds=0.01)
    with pytest.raises(StopAsyncIteration):
        await anext(gen)
    await gen.aclose()
