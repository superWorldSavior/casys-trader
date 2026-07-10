"""Filesystem retention policy for daily radar cache snapshots."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

RADAR_CACHE_RETENTION_DAYS = 30


def purge_old_radar_cache(cache_dir: Path, now_iso: str) -> None:
    """Delete dated cache files older than the configured retention window."""

    if not cache_dir.is_dir():
        return
    try:
        now_date = datetime.fromisoformat(now_iso[:10])
    except ValueError:
        return
    for path in cache_dir.glob("*.json"):
        stem = path.stem
        if len(stem) != 10 or stem[4] != "-" or stem[7] != "-":
            continue
        try:
            file_date = datetime.strptime(stem, "%Y-%m-%d")
        except ValueError:
            continue
        if (now_date - file_date).days <= RADAR_CACHE_RETENTION_DAYS:
            continue
        try:
            path.unlink()
        except OSError:
            pass
