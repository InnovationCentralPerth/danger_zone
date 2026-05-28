"""Tests for the auto_capture service."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from app.integrations.meraki import MerakiError
from app.main import _init_db
from app.services import auto_capture
from app.services.agent_mode import AgentMode, set_mode


@pytest.fixture(autouse=True)
def setup_env(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DEBUG", "false")
    from app import config

    config.get_settings.cache_clear()
    _init_db()
    auto_capture._reset_for_tests()
    # Default both gated agents to active so the loop actually fetches.
    set_mode("zone_intrusion", AgentMode.ACTIVE)
    set_mode("ppe_compliance", AgentMode.ACTIVE)
    yield
    auto_capture._reset_for_tests()


def test_default_state_is_disabled():
    state = auto_capture.get_state("SERIAL-A")
    assert state.enabled is False
    assert state.running is False
    assert state.interval_seconds == auto_capture.DEFAULT_INTERVAL_SECONDS


async def test_enable_persists_and_spawns_task():
    with patch(
        "app.services.auto_capture.fetch_and_persist_snapshot",
        new=AsyncMock(return_value="/tmp/x.jpg"),
    ):
        state = await auto_capture.enable("SERIAL-A", interval_seconds=2)
        assert state.enabled is True
        assert state.running is True
        assert state.interval_seconds == 2
        # Allow at least one loop iteration to run.
        await asyncio.sleep(0.05)
        await auto_capture.disable("SERIAL-A")

    assert auto_capture.get_state("SERIAL-A").enabled is False
    assert auto_capture.get_state("SERIAL-A").running is False


async def test_interval_clamped_to_minimum():
    with patch(
        "app.services.auto_capture.fetch_and_persist_snapshot",
        new=AsyncMock(return_value="/tmp/x.jpg"),
    ):
        state = await auto_capture.enable("SERIAL-A", interval_seconds=0)
        assert state.interval_seconds == auto_capture.MIN_INTERVAL_SECONDS
        await auto_capture.disable("SERIAL-A")


async def test_loop_publishes_camera_snapshot_when_agents_active():
    from app.runtime.bus import bus

    q = bus.subscribe("camera.snapshot")
    with patch(
        "app.services.auto_capture.fetch_and_persist_snapshot",
        new=AsyncMock(return_value="/tmp/x.jpg"),
    ) as mock_fetch:
        await auto_capture.enable("SERIAL-A", interval_seconds=2)
        # Wait for at least one fetch
        event = await asyncio.wait_for(q.get(), timeout=1.0)
        await auto_capture.disable("SERIAL-A")

    assert event.payload["serial"] == "SERIAL-A"
    assert event.payload["trigger"] == "auto"
    mock_fetch.assert_called()
    bus.unsubscribe("camera.snapshot", q)


async def test_loop_skips_fetch_when_all_agents_passive():
    from app.runtime.bus import bus

    set_mode("zone_intrusion", AgentMode.PASSIVE)
    set_mode("ppe_compliance", AgentMode.PASSIVE)

    tick_q = bus.subscribe("auto_capture.tick")
    with patch(
        "app.services.auto_capture.fetch_and_persist_snapshot",
        new=AsyncMock(return_value="/tmp/x.jpg"),
    ) as mock_fetch:
        await auto_capture.enable("SERIAL-A", interval_seconds=2)
        tick = await asyncio.wait_for(tick_q.get(), timeout=1.0)
        await auto_capture.disable("SERIAL-A")

    assert tick.payload["status"] == "idle"
    mock_fetch.assert_not_called()
    bus.unsubscribe("auto_capture.tick", tick_q)


async def test_meraki_error_triggers_backoff_tick():
    from app.runtime.bus import bus

    tick_q = bus.subscribe("auto_capture.tick")
    with patch(
        "app.services.auto_capture.fetch_and_persist_snapshot",
        new=AsyncMock(side_effect=MerakiError("boom")),
    ):
        await auto_capture.enable("SERIAL-A", interval_seconds=2)
        # First tick will be the error
        tick = await asyncio.wait_for(tick_q.get(), timeout=1.0)
        await auto_capture.disable("SERIAL-A")

    assert tick.payload["status"] == "error"
    assert "boom" in tick.payload["error"]
    assert tick.payload["next_in"] >= 2
    bus.unsubscribe("auto_capture.tick", tick_q)


async def test_auto_shutoff_when_max_runtime_exceeded(monkeypatch):
    """If the loop has been running > MAX_RUNTIME_SECONDS it disables itself."""
    from app.runtime.bus import bus

    monkeypatch.setattr(auto_capture, "MAX_RUNTIME_SECONDS", 0)

    tick_q = bus.subscribe("auto_capture.tick")
    with patch(
        "app.services.auto_capture.fetch_and_persist_snapshot",
        new=AsyncMock(return_value="/tmp/x.jpg"),
    ):
        await auto_capture.enable("SERIAL-A", interval_seconds=2)
        tick = await asyncio.wait_for(tick_q.get(), timeout=1.0)

    assert tick.payload["status"] == "auto-shutoff"
    # Persisted state should now be disabled.
    assert auto_capture.get_state("SERIAL-A").enabled is False
    bus.unsubscribe("auto_capture.tick", tick_q)


async def test_disable_no_op_when_not_running():
    state = await auto_capture.disable("SERIAL-Z")
    assert state.enabled is False
    assert state.running is False


async def test_restore_on_startup_respawns_enabled_loops(tmp_path):
    """A loop persisted as enabled should resume after a fake restart."""
    with patch(
        "app.services.auto_capture.fetch_and_persist_snapshot",
        new=AsyncMock(return_value="/tmp/x.jpg"),
    ):
        await auto_capture.enable("SERIAL-A", interval_seconds=2)
        # Simulate restart: drop the in-memory task without touching settings.
        for _s, t in list(auto_capture._tasks.items()):
            t.cancel()
        auto_capture._tasks.clear()
        auto_capture._runtime.clear()

        with patch(
            "app.services.config_loader.load_cameras",
            return_value=[{"serial": "SERIAL-A"}],
        ):
            auto_capture.restore_on_startup()

        await asyncio.sleep(0.05)
        assert auto_capture.get_state("SERIAL-A").running is True
        await auto_capture.disable("SERIAL-A")
