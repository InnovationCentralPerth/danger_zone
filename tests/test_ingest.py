"""Tests for the source-agnostic ingest layer."""

from __future__ import annotations

import asyncio
import io
import shutil
import time
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from PIL import Image

from app.ingest import (
    FileSource,
    Frame,
    FrameRing,
    JpegStreamParser,
    RateGate,
    SyntheticSource,
    build_source,
    ingest_manager,
)
from app.ingest.base import jpeg_dimensions
from app.ingest.synthetic_source import Phase, phases_from_config, render_frame
from app.runtime.bus import bus


def _jpeg(width: int = 64, height: int = 48) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), (120, 30, 30)).save(buf, format="JPEG")
    return buf.getvalue()


# ---- primitives ------------------------------------------------------------


def test_jpeg_dimensions_reads_header():
    assert jpeg_dimensions(_jpeg(320, 240)) == (320, 240)


def test_jpeg_stream_parser_splits_concatenated_frames():
    a, b, c = _jpeg(32, 32), _jpeg(48, 16), _jpeg(16, 48)
    parser = JpegStreamParser()
    # Feed in awkward chunks so markers straddle boundaries.
    payload = a + b + c
    out: list[bytes] = []
    for i in range(0, len(payload), 7):
        out.extend(parser.feed(payload[i : i + 7]))
    assert out == [a, b, c]


def test_jpeg_stream_parser_skips_leading_junk():
    a = _jpeg()
    parser = JpegStreamParser()
    frames = parser.feed(b"garbage" + a)
    assert frames == [a]


def test_frame_ring_drops_oldest():
    ring = FrameRing(maxlen=2)
    for i in range(3):
        ring.push(Frame("cam", i, 0.0, 0.0, 1, 1, b"x"))
    assert [f.seq for f in ring.snapshot()] == [1, 2]
    assert ring.latest() is not None
    assert ring.latest().seq == 2  # type: ignore[union-attr]
    assert len(ring) == 2


def test_rate_gate_limits_emissions():
    gate = RateGate(2.0)  # one every 0.5s
    assert gate.ready(now=100.0) is True
    assert gate.ready(now=100.1) is False
    assert gate.ready(now=100.6) is True
    always = RateGate(0)
    assert always.ready() is True
    assert always.ready() is True


# ---- synthetic source ------------------------------------------------------


def test_phases_from_config_falls_back_to_default():
    assert phases_from_config(None)
    assert phases_from_config([])
    phases = phases_from_config(
        [{"duration": 2, "person": True, "hard_hat": True}, {"duration": -1}]
    )
    assert phases == (Phase(2.0, person=True, hard_hat=True),)


def test_synthetic_phase_at_wraps():
    src = SyntheticSource("cam", script=(Phase(1, person=False), Phase(1, person=True)))
    assert src.phase_at(0.5).person is False
    assert src.phase_at(1.5).person is True
    assert src.phase_at(2.5).person is False


def test_render_frame_is_valid_jpeg():
    data = render_frame(Phase(1, person=True, hard_hat=True), 0.0, 320, 180)
    assert data.startswith(b"\xff\xd8")
    assert jpeg_dimensions(data) == (320, 180)


async def test_synthetic_source_streams_frames():
    src = SyntheticSource("cam-synth", fps=50, width=160, height=90)
    frames: list[Frame] = []
    async for frame in src.stream():
        frames.append(frame)
        if len(frames) == 3:
            break
    await src.aclose()
    assert [f.seq for f in frames] == [0, 1, 2]
    assert all(f.camera_id == "cam-synth" for f in frames)
    assert all(jpeg_dimensions(f.jpeg) == (160, 90) for f in frames)


# ---- file source (requires ffmpeg) ----------------------------------------


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
async def test_file_source_decodes_and_loops(tmp_path):
    clip = tmp_path / "clip.mp4"
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-y",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        "testsrc=duration=1:size=160x120:rate=10",
        "-pix_fmt",
        "yuv420p",
        str(clip),
    )
    assert await proc.wait() == 0

    src = FileSource("cam-file", str(clip), fps=5, width=160, loop=True)
    frames: list[Frame] = []

    async def collect() -> None:
        async for frame in src.stream():
            frames.append(frame)
            if len(frames) == 3:
                return

    await asyncio.wait_for(collect(), timeout=20)
    await src.aclose()
    assert len(frames) == 3
    assert all(f.width == 160 for f in frames)
    assert frames[-1].seq == 2


# ---- factory ---------------------------------------------------------------


def test_build_source_rejects_unknown_type():
    with pytest.raises(ValueError):
        build_source({"serial": "x", "source": {"type": "carrier-pigeon"}})


def test_build_source_requires_uri_for_file():
    with pytest.raises(ValueError):
        build_source({"serial": "x", "source": {"type": "file"}})


def test_build_source_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        build_source({"serial": "x", "source": {"type": "file", "uri": str(tmp_path / "nope.mp4")}})


def test_build_source_synthetic():
    src = build_source({"serial": "x", "source": {"type": "synthetic", "fps": 3, "width": 320}})
    assert isinstance(src, SyntheticSource)
    assert src.fps == 3


# ---- manager ---------------------------------------------------------------


class _FakeSource:
    def __init__(self, camera_id: str, count: int) -> None:
        self.camera_id = camera_id
        self.count = count
        self.closed = False

    def stream(self) -> AsyncIterator[Frame]:
        return self._gen()

    async def _gen(self) -> AsyncIterator[Frame]:
        jpeg = _jpeg(32, 32)
        for i in range(self.count):
            now = time.monotonic()
            yield Frame(self.camera_id, i, float(i), now, 32, 32, jpeg)
            await asyncio.sleep(0.01)

    async def aclose(self) -> None:
        self.closed = True


@pytest.fixture
def ingest_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("INGEST_ENABLED", "true")
    from app import config

    config.get_settings.cache_clear()
    yield tmp_path
    config.get_settings.cache_clear()


async def test_manager_publishes_snapshot_and_persists(
    ingest_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    camera = {"serial": "CAM-1", "source": {"type": "synthetic"}, "vlm_fps": 1000}
    fake = _FakeSource("CAM-1", count=5)
    monkeypatch.setattr("app.ingest.manager.load_cameras", lambda: [camera])
    monkeypatch.setattr("app.ingest.manager.build_source", lambda _cam: fake)

    queue = bus.subscribe("camera.snapshot")
    await ingest_manager.stop()
    ingest_manager.start()
    try:
        event = await asyncio.wait_for(queue.get(), timeout=5)
    finally:
        await ingest_manager.stop()
        bus.unsubscribe("camera.snapshot", queue)

    payload = event.payload
    assert payload["serial"] == "CAM-1"
    assert payload["trigger"] == "ingest"
    path = Path(payload["path"])
    assert path.is_file()
    assert path.read_bytes().startswith(b"\xff\xd8")
    assert fake.closed is True


async def test_manager_ignores_cameras_without_source(
    ingest_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "app.ingest.manager.load_cameras",
        lambda: [{"serial": "MERAKI-ONLY"}],
    )
    await ingest_manager.stop()
    ingest_manager.start()
    try:
        assert ingest_manager.status() == []
    finally:
        await ingest_manager.stop()
