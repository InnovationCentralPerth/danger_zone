"""Snapshot retention.

Ingest persists one JPEG per camera every ``1/vlm_fps`` seconds, so an
always-on deployment accumulates files quickly. This prunes anything older
than ``SNAPSHOT_MAX_AGE_DAYS`` (default 30) and removes empty day folders.
"""

from __future__ import annotations

import logging
import time
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
    removed = 0
    for path in root.rglob("*.jpg"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except OSError as exc:  # pragma: no cover - races with concurrent writes
            logger.warning("Could not prune %s: %s", path, exc)

    _remove_empty_dirs(root)
    if removed:
        logger.info("Pruned %d snapshots older than %d days", removed, max_age_days)
    return removed


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
