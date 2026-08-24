"""Read-only filesystem probe for World Model resource-budget decisions.

Never creates ``world_model.db`` and never writes. A missing store is reported
as zero size. SQLite is opened read-only only when the file exists.
"""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path
from urllib.parse import quote

from trader.domain.world_resource import WorldResourceUsage

_SIDECARS = ("-wal", "-shm", "-journal")


def _on_disk_bytes(path: Path) -> int:
    total = path.stat().st_size
    for suffix in _SIDECARS:
        sidecar = path.parent / f"{path.name}{suffix}"
        if sidecar.is_file():
            total += sidecar.stat().st_size
    return int(total)


def _logical_bytes(path: Path) -> int:
    uri = f"file:{quote(str(path.resolve()), safe='/')}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    try:
        page_count = int(connection.execute("PRAGMA page_count").fetchone()[0])
        page_size = int(connection.execute("PRAGMA page_size").fetchone()[0])
    finally:
        connection.close()
    return page_count * page_size


def _free_bytes(path: Path) -> int:
    target = path if path.exists() else path.parent
    if not target.exists():
        raise FileNotFoundError(f"world model store parent missing: {target}")
    return int(shutil.disk_usage(target).free)


class FilesystemWorldResourceProbe:
    """One-shot logical/on-disk/free measurement for ``world_model.db``."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)

    def measure(self) -> WorldResourceUsage:
        path = self.db_path
        store_exists = path.is_file()
        if not store_exists:
            return WorldResourceUsage(
                logical_bytes=0,
                on_disk_bytes=0,
                free_bytes=_free_bytes(path),
                store_exists=False,
            )
        return WorldResourceUsage(
            logical_bytes=_logical_bytes(path),
            on_disk_bytes=_on_disk_bytes(path),
            free_bytes=_free_bytes(path),
            store_exists=True,
        )


__all__ = ["FilesystemWorldResourceProbe"]
