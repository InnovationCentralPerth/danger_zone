"""Snapshot retention.

Ingest persists one JPEG per camera every ``1/vlm_fps`` seconds, so an
always-on deployment accumulates files quickly. This prunes anything older
than ``SNAPSHOT_MAX_AGE_DAYS`` (default 30) and removes empty day folders.
"""

from __future__ import annotations

import contextlib
import logging
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from app.config import get_settings

logger = logging.getLogger(__name__)


def prune_snapshots(now: float | None = None) -> int:
    """Delete snapshot files older than the configured retention window.

    Returns the number of files removed. ``now`` is injectable for tests.
    """
    settings = get_settings()
    max_age_days = settings.snapshot_max_age_days
    if max_age_days <= 0:
        return 0

    root = settings.snapshots_dir
    if not root.exists():
        return 0

    cutoff = (time.time() if now is None else now) - max_age_days * 86400
    # Snapshots live in YYYY/MM/DD folders. Only day folders on or before the cutoff day (plus one
    # day of slack for time zones) can hold expired files, so skip the rest without touching
    # their (very many) files: scanning all of them froze the app.
    last_day = (datetime.fromtimestamp(cutoff, tz=UTC) + timedelta(days=1)).date()
    removed = 0
    touched: list[Path] = []
    for path in root.glob("*.jpg"):  # loose files directly in the root (not recursive)
        with contextlib.suppress(OSError):
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
    for folder in _day_folders(root, last_day):
        touched.append(folder)
        for path in folder.rglob("*.jpg"):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
                    removed += 1
            except OSError as exc:  # pragma: no cover - races with concurrent writes
                logger.warning("Could not prune %s: %s", path, exc)

    for folder in touched:
        _remove_empty_dirs(folder)
        with contextlib.suppress(OSError):
            if folder.exists() and not any(folder.iterdir()):
                folder.rmdir()
    if removed:
        logger.info("Pruned %d snapshots older than %d days", removed, max_age_days)
    return removed


def _day_folders(root: Path, last_day: date) -> list[Path]:
    """``root/YYYY/MM/DD`` folders dated on or before ``last_day``, plus any other sub-folder
    (not a dated one) so unusual layouts are still pruned."""
    out: list[Path] = []
    for entry in (p for p in root.iterdir() if p.is_dir()):
        if not entry.name.isdigit():
            out.append(entry)
            continue
        for month in (p for p in entry.iterdir() if p.is_dir() and p.name.isdigit()):
            for day in (p for p in month.iterdir() if p.is_dir() and p.name.isdigit()):
                try:
                    folder_date = date(int(entry.name), int(month.name), int(day.name))
                except ValueError:
                    out.append(day)
                    continue
                if folder_date <= last_day:
                    out.append(day)
    return out


def _remove_empty_dirs(root: Path) -> None:
    """Remove empty subdirectories below ``root`` (but not ``root`` itself)."""
    for folder in sorted(root.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        if not folder.is_dir():
            continue
        try:
            if any(folder.iterdir()):
                continue
            folder.rmdir()
        except OSError:
            pass
