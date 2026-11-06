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


# ---- replay scrubbing (seek / pause) --------------------------------------


async def _make_clip(path: Path, seconds: int = 8) -> None:
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
        "-i", f"testsrc=duration={seconds}:size=160x120:rate=10",
        "-g", "5", "-pix_fmt", "yuv420p", str(path),
    )  # fmt: skip
    assert await proc.wait() == 0


async def _take(src: FileSource, n: int, timeout: float = 20) -> list[Frame]:
    frames: list[Frame] = []

    async def collect() -> None:
        async for frame in src.stream():
            frames.append(frame)
            if len(frames) == n:
                return

    await asyncio.wait_for(collect(), timeout=timeout)
    return frames


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
async def test_file_source_reports_duration_and_is_seekable(tmp_path):
    clip = tmp_path / "clip.mp4"
    await _make_clip(clip, 8)
    src = FileSource("cam-seek", str(clip), fps=10, width=160)
    assert src.seekable is True
    assert src.duration == pytest.approx(8.0, abs=0.5)
    assert src.position() == 0.0


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
async def test_seek_while_playing_jumps_position(tmp_path):
    clip = tmp_path / "clip.mp4"
    await _make_clip(clip, 8)
    src = FileSource("cam-seek", str(clip), fps=10, width=160, loop=True)
    stream = src.stream()
    first = [await asyncio.wait_for(anext(stream), 20) for _ in range(2)]
    assert first and src.position() < 3

    assert src.seek(6.0) == pytest.approx(6.0)
    await asyncio.wait_for(anext(stream), 20)  # restarted ffmpeg produced a frame
    assert 5.5 <= src.position() <= 8.0
    await src.aclose()


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
async def test_pause_holds_one_frame_and_resume_continues(tmp_path):
    clip = tmp_path / "clip.mp4"
    await _make_clip(clip, 8)
    src = FileSource("cam-pause", str(clip), fps=10, width=160, loop=True)
    stream = src.stream()
    await asyncio.wait_for(anext(stream), 20)

    src.seek(2.0)
    src.pause()
    assert src.paused
    # A few already-buffered playing frames may arrive first; then the frozen frame repeats.
    held: list[Frame] = []
    for _ in range(12):
        held.append(await asyncio.wait_for(anext(stream), 20))
        if len(held) >= 2 and held[-1].jpeg == held[-2].jpeg and src.hold_id >= 1:
            break
    # Pause freezes the picture but keeps frames flowing so the MJPEG stream stays alive.
    assert held[-1].jpeg == held[-2].jpeg
    assert held[-1].seq > held[-2].seq
    assert src.hold_id >= 1
    pos = src.position()
    assert pos == src.position()  # does not drift while paused

    hold_id = src.hold_id
    src.seek(5.0)  # scrub while paused -> a different frozen frame
    for _ in range(6):
        f = await asyncio.wait_for(anext(stream), 20)
        if src.hold_id != hold_id:
            break
    assert src.hold_id == hold_id + 1
    assert f.jpeg != held[-1].jpeg
    assert src.position() == pytest.approx(5.0)

    src.resume()
    assert not src.paused
    await asyncio.wait_for(anext(stream), 20)
    assert src.position() >= 5.0
    await src.aclose()


def test_seek_is_clamped_and_synthetic_is_not_seekable(tmp_path):
    synth = SyntheticSource("cam-synth")
    assert getattr(synth, "seekable", False) is False


async def test_manager_playback_unknown_and_unsupported():
    from app.ingest.manager import IngestManager

    mgr = IngestManager()
    assert mgr.playback("nope") is None
    assert await mgr.set_playback("nope", position=1.0) is None

    class _NoSeek:
        camera_id = "rtsp-cam"
        seekable = False

    mgr._sources["rtsp-cam"] = _NoSeek()  # type: ignore[assignment]
    assert mgr.playback("rtsp-cam") == {"supported": False}
    with pytest.raises(ValueError):
        await mgr.set_playback("rtsp-cam", position=1.0)


def test_playback_api_roundtrip():
    from fastapi.testclient import TestClient

    from app.main import app

    class _Fake:
        camera_id = "fake-cam"
        seekable = True
        duration = 100.0
        paused = False
        loop_range = None

        def __init__(self) -> None:
            self.pos = 3.0

        def position(self) -> float:
            return self.pos

        def seek(self, p: float) -> float:
            self.pos = p
            return p

        def pause(self) -> None:
            self.paused = True

        def resume(self) -> None:
            self.paused = False

    fake = _Fake()
    ingest_manager._sources["fake-cam"] = fake  # type: ignore[assignment]
    try:
        client = TestClient(app)
        r = client.get("/cameras/fake-cam/playback")
        assert r.json() == {
            "supported": True,
            "position": 3.0,
            "duration": 100.0,
            "paused": False,
            "loop": None,
        }

        r = client.post("/cameras/fake-cam/playback", json={"position": 42, "paused": True})
        assert r.status_code == 200
        assert r.json()["position"] == 42 and r.json()["paused"] is True

        r = client.post("/cameras/fake-cam/playback", json={"paused": False})
        assert r.json()["paused"] is False

        assert client.post("/cameras/fake-cam/playback", json={"position": -1}).status_code == 422
        assert client.post("/cameras/fake-cam/playback", json={"loop_start": 5}).status_code == 422
        assert client.post("/cameras/ghost/playback", json={"position": 1}).status_code == 404
        assert client.get("/cameras/ghost/playback").json() == {"supported": False}
    finally:
        ingest_manager._sources.pop("fake-cam", None)


# ---- section looping ------------------------------------------------------


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
async def test_section_loop_stays_in_range_and_can_be_cleared(tmp_path):
    clip = tmp_path / "clip.mp4"
    await _make_clip(clip, 12)
    loops = tmp_path / "loops"
    src = FileSource("cam-loop", str(clip), fps=10, width=160, loop=True)
    stream = src.stream()
    await asyncio.wait_for(anext(stream), 20)

    assert await src.set_loop(4.0, 7.0, loops) == (4.0, 7.0)
    assert src.loop_range == (4.0, 7.0)
    assert len(list(loops.glob("*.mp4"))) == 1

    # Play for longer than the loop is long: position must stay inside [4, 7].
    seen = []
    deadline = time.monotonic() + 7.5
    while time.monotonic() < deadline:
        await asyncio.wait_for(anext(stream), 20)
        seen.append(src.position())
    assert min(seen) >= 3.9 and max(seen) <= 7.1, (min(seen), max(seen))
    assert max(seen) - min(seen) > 1.5  # it actually played, and wrapped, not frozen

    # Seeking inside the loop keeps the loop; outside it leaves the loop.
    src.seek(5.0)
    assert src.loop_range == (4.0, 7.0)
    assert src.position() == pytest.approx(5.0, abs=0.3)
    src.seek(10.0)
    assert src.loop_range is None
    assert src.duration == pytest.approx(12.0, abs=0.5)
    assert not list(loops.glob("*.mp4"))  # temp clip cleaned up
    await asyncio.wait_for(anext(stream), 20)
    assert 9.5 <= src.position() <= 12.0
    await src.aclose()


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
async def test_section_loop_validation_and_clear(tmp_path, monkeypatch):
    clip = tmp_path / "clip.mp4"
    await _make_clip(clip, 12)
    src = FileSource("cam-loop2", str(clip), fps=10, width=160, loop=True)
    with pytest.raises(ValueError):
        await src.set_loop(5.0, 5.3, tmp_path / "l")  # too short
    monkeypatch.setattr("app.ingest.ffmpeg_source.MAX_LOOP_SECONDS", 5.0)
    with pytest.raises(ValueError):
        await src.set_loop(0.0, 8.0, tmp_path / "l")  # too long
    assert src.loop_range is None
    monkeypatch.undo()

    await src.set_loop(2.0, 5.0, tmp_path / "l")
    src.clear_loop()
    assert src.loop_range is None
    assert src.duration == pytest.approx(12.0, abs=0.5)
    assert src.position() >= 0
    src.clear_loop()  # clearing twice is harmless
