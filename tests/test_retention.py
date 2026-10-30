"""Tests for snapshot retention."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from app.services.retention import prune_snapshots


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SNAPSHOT_MAX_AGE_DAYS", "7")
    from app import config

    config.get_settings.cache_clear()
    yield tmp_path
    config.get_settings.cache_clear()


def test_prune_removes_only_expired_files(data_dir: Path) -> None:
    folder = data_dir / "snapshots" / "2026" / "01" / "01"
    folder.mkdir(parents=True)
    old = folder / "old.jpg"
    new = folder / "new.jpg"
    old.write_bytes(b"old")
    new.write_bytes(b"new")
    now = time.time()
    os.utime(old, (now - 8 * 86400, now - 8 * 86400))
    os.utime(new, (now - 86400, now - 86400))

    assert prune_snapshots(now=now) == 1
    assert not old.exists()
    assert new.exists()


def test_prune_removes_empty_day_folders(data_dir: Path) -> None:
    folder = data_dir / "snapshots" / "2026" / "01" / "01"
    folder.mkdir(parents=True)
    old = folder / "old.jpg"
    old.write_bytes(b"old")
    now = time.time()
    os.utime(old, (now - 8 * 86400, now - 8 * 86400))

    assert prune_snapshots(now=now) == 1
    assert not folder.exists()


def test_prune_noop_when_disabled(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SNAPSHOT_MAX_AGE_DAYS", "0")
    from app import config

    config.get_settings.cache_clear()
    folder = data_dir / "snapshots"
    folder.mkdir(parents=True)
    old = folder / "old.jpg"
    old.write_bytes(b"old")
    assert prune_snapshots() == 0
    assert old.exists()
