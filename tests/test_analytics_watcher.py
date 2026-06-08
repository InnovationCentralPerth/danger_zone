"""Tests for the analytics_watcher service."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from app.integrations.meraki import MerakiError
from app.main import _init_db
from app.services import analytics_watcher
from app.services.agent_mode import AgentMode, set_mode


def _ff(end_ts: str, *, average_count: float = 0.0, entrances: int = 0) -> list[dict]:
    """Helper: build a /analytics/recent response with one Full Frame row."""
    return [
        {
            "zoneId": "0",
            "startTs": "2026-05-28T07:00:00Z",
            "endTs": end_ts,
            "entrances": entrances,
            "averageCount": average_count,
        }
    ]


@pytest.fixture(autouse=True)
def setup_env(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DEBUG", "false")
    from app import config

    config.get_settings.cache_clear()
    _init_db()
    analytics_watcher._reset_for_tests()
    set_mode("zone_intrusion", AgentMode.ACTIVE)
    set_mode("ppe_compliance", AgentMode.ACTIVE)
    yield
    analytics_watcher._reset_for_tests()


def _patch_client(analytics_side_effect=None):
    """Patch the meraki client singleton used inside the watcher."""
    mock_client = AsyncMock()
    if analytics_side_effect is not None:
        mock_client.get_camera_analytics_recent = AsyncMock(side_effect=analytics_side_effect)
    return patch("app.services.analytics_watcher.get_client", return_value=mock_client), mock_client


# ---- basic state -----------------------------------------------------------


def test_default_state_is_disabled():
    state = analytics_watcher.get_state("SERIAL-A")
    assert state.enabled is False
    assert state.running is False
    assert state.interval_seconds == analytics_watcher.DEFAULT_INTERVAL_SECONDS


async def test_interval_clamped_to_minimum():
    p, _ = _patch_client(analytics_side_effect=lambda *_a: _ff("T1"))
    with (
        p,
        patch(
            "app.services.analytics_watcher.fetch_and_persist_snapshot",
            new=AsyncMock(return_value="/tmp/x.jpg"),
        ),
    ):
        state = await analytics_watcher.enable("SERIAL-A", interval_seconds=0)
        assert state.interval_seconds == analytics_watcher.MIN_INTERVAL_SECONDS
        await analytics_watcher.disable("SERIAL-A")


async def test_interval_clamped_to_maximum():
    p, _ = _patch_client(analytics_side_effect=lambda *_a: _ff("T1"))
    with p:
        state = await analytics_watcher.enable("SERIAL-A", interval_seconds=9999)
        assert state.interval_seconds == analytics_watcher.MAX_INTERVAL_SECONDS
        await analytics_watcher.disable("SERIAL-A")


# ---- trigger semantics -----------------------------------------------------


async def test_quiet_tick_when_no_person_in_frame():
    """averageCount=0, no entrance delta → quiet tick, no snapshot."""
    from app.runtime.bus import bus

    p, _ = _patch_client(analytics_side_effect=lambda *_a: _ff("T1", average_count=0.0))
    tick_q = bus.subscribe("analytics_watcher.tick")
    snap_q = bus.subscribe("camera.snapshot")
    with (
        p,
        patch(
            "app.services.analytics_watcher.fetch_and_persist_snapshot",
            new=AsyncMock(return_value="/tmp/x.jpg"),
        ) as mock_snap,
    ):
        await analytics_watcher.enable("SERIAL-A", interval_seconds=2)
        tick = await asyncio.wait_for(tick_q.get(), timeout=1.0)
        await analytics_watcher.disable("SERIAL-A")

    assert tick.payload["status"] == "quiet"
    mock_snap.assert_not_called()
    assert snap_q.empty()
    bus.unsubscribe("analytics_watcher.tick", tick_q)
    bus.unsubscribe("camera.snapshot", snap_q)


async def test_person_tick_when_average_count_positive():
    """averageCount>0 → snapshot fetched + published with trigger='watcher'."""
    from app.runtime.bus import bus

    p, _ = _patch_client(analytics_side_effect=lambda *_a: _ff("T1", average_count=0.4))
    snap_q = bus.subscribe("camera.snapshot")
    with (
        p,
        patch(
            "app.services.analytics_watcher.fetch_and_persist_snapshot",
            new=AsyncMock(return_value="/tmp/x.jpg"),
        ) as mock_snap,
    ):
        await analytics_watcher.enable("SERIAL-A", interval_seconds=2)
        event = await asyncio.wait_for(snap_q.get(), timeout=1.0)
        await analytics_watcher.disable("SERIAL-A")

    assert event.payload["serial"] == "SERIAL-A"
    assert event.payload["trigger"] == "watcher"
    mock_snap.assert_called()
    bus.unsubscribe("camera.snapshot", snap_q)


async def test_rising_entrance_edge_triggers_snapshot(monkeypatch):
    """Sequence: (entrances=0)→(entrances=1) should trigger even if avg=0."""
    from app.runtime.bus import bus

    # Use a sub-second interval so the test doesn't have to wait for the real
    # 2 s minimum between polls.
    monkeypatch.setattr(analytics_watcher, "MIN_INTERVAL_SECONDS", 0)

    responses = iter(
        [
            _ff("T1", average_count=0.0, entrances=0),
            _ff("T2", average_count=0.0, entrances=1),
            _ff("T3", average_count=0.0, entrances=1),  # no further rise
        ]
    )

    async def fake_recent(*_a, **_k):
        return next(responses, _ff("Tend", entrances=1))

    mock_client = AsyncMock()
    mock_client.get_camera_analytics_recent = fake_recent
    snap_q = bus.subscribe("camera.snapshot")
    with (
        patch("app.services.analytics_watcher.get_client", return_value=mock_client),
        patch(
            "app.services.analytics_watcher.fetch_and_persist_snapshot",
            new=AsyncMock(return_value="/tmp/x.jpg"),
        ),
    ):
        await analytics_watcher.enable("SERIAL-A", interval_seconds=0)
        event = await asyncio.wait_for(snap_q.get(), timeout=2.0)
        await analytics_watcher.disable("SERIAL-A")

    assert event.payload["trigger"] == "watcher"
    bus.unsubscribe("camera.snapshot", snap_q)


async def test_same_end_ts_does_not_retrigger():
    """If endTs hasn't advanced, no trigger even with positive averageCount."""
    from app.runtime.bus import bus

    stuck = _ff("STUCK", average_count=0.5, entrances=1)

    async def fake_recent(*_a, **_k):
        return stuck

    mock_client = AsyncMock()
    mock_client.get_camera_analytics_recent = fake_recent
    snap_q = bus.subscribe("camera.snapshot")
    tick_q = bus.subscribe("analytics_watcher.tick")
    with (
        patch("app.services.analytics_watcher.get_client", return_value=mock_client),
        patch(
            "app.services.analytics_watcher.fetch_and_persist_snapshot",
            new=AsyncMock(return_value="/tmp/x.jpg"),
        ) as mock_snap,
    ):
        await analytics_watcher.enable("SERIAL-A", interval_seconds=2)
        # First tick — will trigger because rt.last_end_ts starts as None
        first = await asyncio.wait_for(tick_q.get(), timeout=1.0)
        assert first.payload["status"] == "person"
        # Drain its camera.snapshot
        await asyncio.wait_for(snap_q.get(), timeout=1.0)
        # Next tick — same endTs, no further trigger expected
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(snap_q.get(), timeout=0.3)
        await analytics_watcher.disable("SERIAL-A")
        # Snapshot fn should have been called exactly once
        assert mock_snap.call_count == 1
    bus.unsubscribe("camera.snapshot", snap_q)
    bus.unsubscribe("analytics_watcher.tick", tick_q)


# ---- safety rails ----------------------------------------------------------


async def test_idle_skip_when_all_agents_passive():
    """Watcher should not even call Meraki when both gated agents are passive."""
    from app.runtime.bus import bus

    set_mode("zone_intrusion", AgentMode.PASSIVE)
    set_mode("ppe_compliance", AgentMode.PASSIVE)

    mock_client = AsyncMock()
    mock_client.get_camera_analytics_recent = AsyncMock(return_value=_ff("T1"))
    tick_q = bus.subscribe("analytics_watcher.tick")
    with patch("app.services.analytics_watcher.get_client", return_value=mock_client):
        await analytics_watcher.enable("SERIAL-A", interval_seconds=2)
        tick = await asyncio.wait_for(tick_q.get(), timeout=1.0)
        await analytics_watcher.disable("SERIAL-A")

    assert tick.payload["status"] == "idle"
    mock_client.get_camera_analytics_recent.assert_not_called()
    bus.unsubscribe("analytics_watcher.tick", tick_q)


async def test_meraki_error_triggers_backoff_tick():
    from app.runtime.bus import bus

    mock_client = AsyncMock()
    mock_client.get_camera_analytics_recent = AsyncMock(side_effect=MerakiError("boom"))
    tick_q = bus.subscribe("analytics_watcher.tick")
    with patch("app.services.analytics_watcher.get_client", return_value=mock_client):
        await analytics_watcher.enable("SERIAL-A", interval_seconds=2)
        tick = await asyncio.wait_for(tick_q.get(), timeout=1.0)
        await analytics_watcher.disable("SERIAL-A")

    assert tick.payload["status"] == "error"
    assert "boom" in tick.payload["error"]
    assert tick.payload["next_in"] >= 2
    bus.unsubscribe("analytics_watcher.tick", tick_q)


async def test_auto_shutoff_when_max_runtime_exceeded(monkeypatch):
    from app.runtime.bus import bus

    monkeypatch.setattr(analytics_watcher, "MAX_RUNTIME_SECONDS", 0)
    mock_client = AsyncMock()
    mock_client.get_camera_analytics_recent = AsyncMock(return_value=_ff("T1"))
    tick_q = bus.subscribe("analytics_watcher.tick")
    with patch("app.services.analytics_watcher.get_client", return_value=mock_client):
        await analytics_watcher.enable("SERIAL-A", interval_seconds=2)
        tick = await asyncio.wait_for(tick_q.get(), timeout=1.0)

    assert tick.payload["status"] == "auto-shutoff"
    assert analytics_watcher.get_state("SERIAL-A").enabled is False
    bus.unsubscribe("analytics_watcher.tick", tick_q)


# ---- lifecycle -------------------------------------------------------------


async def test_disable_no_op_when_not_running():
    state = await analytics_watcher.disable("SERIAL-Z")
    assert state.enabled is False
    assert state.running is False


async def test_restore_on_startup_respawns_enabled_loops():
    mock_client = AsyncMock()
    mock_client.get_camera_analytics_recent = AsyncMock(return_value=_ff("T1"))
    with patch("app.services.analytics_watcher.get_client", return_value=mock_client):
        await analytics_watcher.enable("SERIAL-A", interval_seconds=2)
        # Simulate restart: drop the in-memory task without touching settings.
        for _s, t in list(analytics_watcher._tasks.items()):
            t.cancel()
        analytics_watcher._tasks.clear()
        analytics_watcher._runtime.clear()

        with patch(
            "app.services.config_loader.load_cameras",
            return_value=[{"serial": "SERIAL-A"}],
        ):
            analytics_watcher.restore_on_startup()

        await asyncio.sleep(0.05)
        assert analytics_watcher.get_state("SERIAL-A").running is True
        await analytics_watcher.disable("SERIAL-A")


# ---- pure helpers ----------------------------------------------------------


def test_extract_full_frame_picks_zone_zero():
    rows = [
        {"zoneId": "703124491823219265", "error": "No updates in past 30 minutes"},
        {"zoneId": "0", "averageCount": 0.1, "entrances": 0, "endTs": "T"},
    ]
    ff = analytics_watcher._extract_full_frame(rows)
    assert ff is not None and ff["zoneId"] == "0"


def test_extract_full_frame_returns_none_when_missing():
    rows = [{"zoneId": "999"}]
    assert analytics_watcher._extract_full_frame(rows) is None
