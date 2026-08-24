from __future__ import annotations

import sqlite3

from trader.domain.world_resource import WorldResourceUsage
from trader.infrastructure.state_db.world_resource_probe import FilesystemWorldResourceProbe


def test_missing_store_is_measured_without_creating_files(tmp_path) -> None:
    db_path = tmp_path / "world_model.db"
    usage = FilesystemWorldResourceProbe(db_path).measure()
    assert isinstance(usage, WorldResourceUsage)
    assert usage.store_exists is False
    assert usage.logical_bytes == 0
    assert usage.on_disk_bytes == 0
    assert usage.free_bytes > 0
    assert db_path.exists() is False
    assert list(tmp_path.iterdir()) == []


def test_existing_sqlite_reports_logical_and_on_disk_size(tmp_path) -> None:
    db_path = tmp_path / "world_model.db"
    connection = sqlite3.connect(db_path)
    try:
        connection.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, payload BLOB)")
        connection.execute("INSERT INTO t(payload) VALUES (?)", (b"x" * 4096,))
        connection.commit()
        page_count = int(connection.execute("PRAGMA page_count").fetchone()[0])
        page_size = int(connection.execute("PRAGMA page_size").fetchone()[0])
    finally:
        connection.close()

    usage = FilesystemWorldResourceProbe(db_path).measure()
    on_disk = db_path.stat().st_size
    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = db_path.parent / f"{db_path.name}{suffix}"
        if sidecar.is_file():
            on_disk += sidecar.stat().st_size
    assert usage.store_exists is True
    assert usage.logical_bytes == page_count * page_size
    assert usage.on_disk_bytes == on_disk
    assert usage.free_bytes > 0
