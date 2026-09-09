"""Offline SQLite + Parquet cutover for World shadow predictions.

Preview is read-only.  Apply builds a compact candidate beside the source.
Adopt atomically replaces the source only after durable verification.
"""

from __future__ import annotations

import gc
import hashlib
import json
import os
import sqlite3
import stat
from collections.abc import Callable, Iterator, Mapping, Sequence
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

from trader.application.world_model.prediction_cutover import (
    CUTOVER_JOURNAL_SCHEMA,
    CUTOVER_REPORT_SCHEMA,
    reject_open_or_future_cutoff,
    resolve_cutover_clock,
    utc_cutover_today,
)
from trader.domain.world_prediction_archive import (
    InvalidWorldPredictionArchiveError,
    WorldPredictionArchiveManifest,
    WorldPredictionArchivePlan,
)
from trader.infrastructure.state_db.world_model_store import WORLD_MODEL_REQUIRED_TABLES
from trader.infrastructure.state_db.world_prediction_archive_source import (
    SQLiteWorldPredictionArchiveSource,
)
from trader.infrastructure.state_db.world_prediction_parquet_store import (
    DuckDbWorldPredictionParquetStore,
)
from trader.infrastructure.state_db.world_prediction_storage_lock import (
    PredictionStorageBusyError,
    cutover_journal_path,
    exclusive_prediction_storage_lease,
    shared_prediction_storage_lease,
)
from trader.infrastructure.state_db.world_prediction_tiers import (
    PredictionReadUnavailable,
    ensure_cold_schema,
    prediction_count,
    read_prediction_identities,
    read_prediction_rows,
    register_cold_partition,
    validate_registered_cold_storage,
)

_HOT_TABLE = "world_shadow_predictions"
_DELETE_TRIGGER = "world_predictions_no_delete"
_GENERATION_TABLE = "world_prediction_cutover_generations"
_COLD_PARTITIONS = "world_prediction_cold_partitions"
_COLD_INDEX = "world_prediction_cold_index"
_OPERATOR_TABLES = frozenset({_COLD_PARTITIONS, _COLD_INDEX, _GENERATION_TABLE})
_BATCH = 400
_CONTENT_READ_BATCH = 8192
_STAGES = (
    "acquired",
    "snapshot",
    "archives_verified",
    "candidate",
    "compacted",
    "verified",
    "prepared",
    "pre_swap",
    "swapped",
    "success",
)


class PredictionCutoverFailpoint(RuntimeError):
    """Test-only crash injected after a durable journal stage."""

    def __init__(self, stage: str) -> None:
        self.stage = stage
        super().__init__(f"cutover failpoint: {stage}")


def _quoted(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _lstat(path: Path) -> os.stat_result | None:
    try:
        return os.lstat(path)
    except FileNotFoundError:
        return None


def _is_symlink(path: Path) -> bool:
    st = _lstat(path)
    return st is not None and stat.S_ISLNK(st.st_mode)


def _is_regular(path: Path) -> bool:
    st = _lstat(path)
    return st is not None and stat.S_ISREG(st.st_mode)


def _is_dir(path: Path) -> bool:
    st = _lstat(path)
    return st is not None and stat.S_ISDIR(st.st_mode)


def _lexically_confined(path: Path, root: Path) -> bool:
    candidate = Path(os.path.normpath(os.path.abspath(path)))
    authority = Path(os.path.normpath(os.path.abspath(root)))
    try:
        candidate.relative_to(authority)
    except ValueError:
        return False
    return True


def _assert_confined(path: Path, root: Path, *, what: str, leaf_must_exist: bool = True) -> None:
    if not _lexically_confined(path, root):
        raise RuntimeError(f"{what} escapes the state root: {path}")
    candidate = Path(os.path.normpath(os.path.abspath(path)))
    authority = Path(os.path.normpath(os.path.abspath(root)))
    relative = candidate.relative_to(authority)
    current = authority
    st = _lstat(current)
    if st is None:
        raise RuntimeError(f"missing state root: {current}")
    if stat.S_ISLNK(st.st_mode):
        raise RuntimeError(f"refusing symlink state ancestor: {current}")
    if not stat.S_ISDIR(st.st_mode):
        raise RuntimeError(f"state ancestor is not a directory: {current}")
    parts = relative.parts
    for index, part in enumerate(parts):
        if part in {"", ".", ".."}:
            raise RuntimeError(f"unsafe path component: {part}")
        current = current / part
        st = _lstat(current)
        if st is None:
            if leaf_must_exist or index < len(parts) - 1:
                raise RuntimeError(f"missing {what}: {current}")
            return
        if stat.S_ISLNK(st.st_mode):
            raise RuntimeError(f"refusing symlink {what}: {current}")


def _assert_regular_file(path: Path, *, what: str, root: Path | None = None) -> None:
    if root is not None:
        _assert_confined(path, root, what=what)
    st = _lstat(path)
    if st is None:
        raise FileNotFoundError(str(path))
    if stat.S_ISLNK(st.st_mode):
        raise RuntimeError(f"refusing symlink {what}: {path}")
    if not stat.S_ISREG(st.st_mode):
        raise RuntimeError(f"{what} is not a regular file: {path}")


def _assert_directory(path: Path, *, what: str, must_exist: bool = True) -> None:
    st = _lstat(path)
    if st is None:
        if must_exist:
            raise FileNotFoundError(str(path))
        return
    if stat.S_ISLNK(st.st_mode):
        raise RuntimeError(f"refusing symlink {what}: {path}")
    if not stat.S_ISDIR(st.st_mode):
        raise RuntimeError(f"{what} is not a directory: {path}")


def _fsync_regular_file(path: Path) -> None:
    fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise RuntimeError(f"not a regular file: {path}")
        os.fsync(fd)
    finally:
        os.close(fd)


def _fsync_directory(path: Path) -> None:
    fd = os.open(str(path), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _file_sha256(path: Path) -> str:
    _assert_regular_file(path, what="hashed file")
    digest = hashlib.sha256()
    fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW)
    try:
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    finally:
        os.close(fd)
    return f"sha256:{digest.hexdigest()}"


def _sidecar(path: Path, suffix: str) -> Path:
    return Path(str(path) + suffix)


def _durable_fingerprint(path: Path) -> dict[str, Any]:
    """Main file + WAL bytes. SHM is omitted: reads may create or mutate it."""

    _assert_regular_file(path, what="sqlite database")
    payload: dict[str, Any] = {
        "path": str(path),
        "size": os.lstat(path).st_size,
        "sha256": _file_sha256(path),
    }
    wal = _sidecar(path, "-wal")
    st = _lstat(wal)
    if st is None:
        payload["wal"] = None
        return payload
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
        raise RuntimeError(f"refusing non-regular sqlite sidecar: {wal}")
    payload["wal"] = {"size": st.st_size, "sha256": _file_sha256(wal)}
    return payload


def _source_fingerprint(path: Path) -> dict[str, Any]:
    return _durable_fingerprint(path)


def _fingerprint_digest(fingerprint: Mapping[str, Any]) -> str:
    encoded = json.dumps(fingerprint, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _archive_row_bytes(row: tuple[object, ...]) -> bytes:
    def encode(value: object) -> object:
        if isinstance(value, bytes):
            return {"$bytes_hex": value.hex()}
        return value

    return (
        json.dumps([encode(value) for value in row], ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
    )


def _feed(digest: Any, row: Sequence[object]) -> None:
    encoded = json.dumps(list(row), ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    digest.update(len(encoded).to_bytes(8, "big"))
    digest.update(encoded)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _require_offline(state_dir: Path) -> dict[str, Any]:
    pid_path = state_dir / "daemon.pid"
    st = _lstat(pid_path)
    if st is None:
        return {"daemon_pid": None, "daemon_alive": False, "stale_pid": False}
    if stat.S_ISLNK(st.st_mode):
        raise RuntimeError("daemon.pid is a symlink")
    if not stat.S_ISREG(st.st_mode):
        raise RuntimeError("daemon.pid is not a regular file")
    raw = pid_path.read_text(encoding="utf-8").strip()
    if not raw:
        raise RuntimeError("daemon.pid is malformed: empty")
    try:
        pid = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"daemon.pid is malformed: {raw!r}") from exc
    if pid <= 0:
        raise RuntimeError(f"daemon.pid is malformed: {pid}")
    if _pid_alive(pid):
        raise RuntimeError(f"live daemon.pid {pid} refuses offline cutover")
    return {"daemon_pid": pid, "daemon_alive": False, "stale_pid": True}


def _readonly(path: Path) -> sqlite3.Connection:
    _assert_regular_file(path, what="sqlite database")
    uri = f"file:{quote(str(path.resolve()), safe='/')}?mode=ro&cache=private"
    connection = sqlite3.connect(uri, uri=True)
    connection.execute("PRAGMA query_only=ON")
    return connection


def _write_connection(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(str(path))
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=5000")
    connection.isolation_level = None
    return connection


def _table_names(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }


def _assert_world_ledger(path: Path, *, root: Path) -> None:
    _assert_regular_file(path, what="world_model.db", root=root)
    if path.name != "world_model.db":
        raise ValueError("World prediction cutover requires a dedicated world_model.db")
    if path.name.casefold() == "casys.db":
        raise ValueError("World prediction cutover requires a dedicated world_model.db, never casys.db")
    connection = _readonly(path)
    try:
        tables = _table_names(connection)
        missing = WORLD_MODEL_REQUIRED_TABLES - tables
        if missing:
            raise RuntimeError(
                "world_model.db is not a valid dedicated world ledger; missing tables: "
                + ", ".join(sorted(missing))
            )
    finally:
        connection.close()


def _journal_file(db_path: Path) -> Path:
    return Path(cutover_journal_path(db_path))


def _write_journal(path: Path, payload: Mapping[str, Any], *, root: Path) -> None:
    _assert_confined(path, root, what="cutover journal", leaf_must_exist=False)
    st = _lstat(path)
    if st is not None and (stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode)):
        raise RuntimeError(f"refusing non-regular cutover journal: {path}")
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    if _is_symlink(temporary):
        raise RuntimeError(f"refusing symlink journal staging file: {temporary}")
    with open(temporary, "w", encoding="utf-8") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)
    _fsync_regular_file(path)
    _fsync_directory(path.parent)


def _read_journal(path: Path, *, root: Path) -> dict[str, Any]:
    _assert_regular_file(path, what="cutover journal", root=root)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"cutover journal unreadable: {path}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("cutover journal must be an object")
    if payload.get("schema_version") != CUTOVER_JOURNAL_SCHEMA:
        raise RuntimeError(f"unsupported cutover journal schema: {payload.get('schema_version')!r}")
    return payload


def _clear_journal(path: Path, *, root: Path) -> None:
    st = _lstat(path)
    if st is None:
        return
    _assert_regular_file(path, what="cutover journal", root=root)
    os.unlink(path)
    _fsync_directory(path.parent)


def _stage_index(stage: str) -> int:
    try:
        return _STAGES.index(stage)
    except ValueError as exc:
        raise RuntimeError(f"unknown cutover stage: {stage}") from exc


def _reached(journal: Mapping[str, Any], stage: str) -> bool:
    return _stage_index(str(journal.get("stage") or "")) >= _stage_index(stage)


def _sqlite_backup(source: Path, destination: Path, *, root: Path) -> None:
    _assert_regular_file(source, what="sqlite backup source", root=root)
    _assert_confined(destination, root, what="sqlite backup destination", leaf_must_exist=False)
    if _is_symlink(destination):
        raise RuntimeError(f"refusing symlink sqlite destination: {destination}")
    src = _readonly(source)
    try:
        dst = sqlite3.connect(str(destination))
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    os.chmod(destination, 0o600)
    _fsync_regular_file(destination)
    _fsync_directory(destination.parent)


def _remove_incomplete(path: Path, *, root: Path) -> None:
    st = _lstat(path)
    if st is None:
        return
    _assert_regular_file(path, what="incomplete cutover copy", root=root)
    os.unlink(path)
    for suffix in ("-wal", "-shm"):
        side = _sidecar(path, suffix)
        side_st = _lstat(side)
        if side_st is None:
            continue
        if stat.S_ISLNK(side_st.st_mode) or not stat.S_ISREG(side_st.st_mode):
            raise RuntimeError(f"refusing non-regular sqlite sidecar: {side}")
        os.unlink(side)
    _fsync_directory(path.parent)


def _checkpoint_delete_on(connection: sqlite3.Connection, path: Path) -> None:
    row = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    if row is None:
        raise RuntimeError(f"wal_checkpoint returned no result for {path}")
    busy, log, checkpointed = int(row[0]), int(row[1]), int(row[2])
    if busy != 0:
        raise RuntimeError(
            f"wal checkpoint busy for {path}: busy={busy} log={log} checkpointed={checkpointed}"
        )
    if log != checkpointed:
        raise RuntimeError(
            f"wal checkpoint incomplete for {path}: busy={busy} log={log} checkpointed={checkpointed}"
        )
    mode = connection.execute("PRAGMA journal_mode=DELETE").fetchone()
    if mode is None or str(mode[0]).lower() != "delete":
        raise RuntimeError(f"journal_mode must equal delete for {path}, got {mode!r}")
    row = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    if row is None:
        raise RuntimeError(f"wal_checkpoint after DELETE returned no result for {path}")
    busy, log, checkpointed = int(row[0]), int(row[1]), int(row[2])
    if busy != 0 or log != checkpointed:
        raise RuntimeError(
            f"wal checkpoint after DELETE incomplete for {path}: "
            f"busy={busy} log={log} checkpointed={checkpointed}"
        )


def _assert_no_wal_sidecars(path: Path) -> None:
    for suffix in ("-wal", "-shm"):
        side = _sidecar(path, suffix)
        st = _lstat(side)
        if st is None:
            continue
        raise RuntimeError(f"WAL/SHM still present after checkpoint/DELETE; aborting without unlink: {side}")


def _require_checkpoint_delete(path: Path, *, root: Path) -> None:
    _assert_regular_file(path, what="sqlite checkpoint target", root=root)
    gc.collect()
    connection = sqlite3.connect(str(path), timeout=30.0)
    try:
        connection.execute("PRAGMA busy_timeout=5000")
        connection.isolation_level = None
        _checkpoint_delete_on(connection, path)
    finally:
        connection.close()
    _assert_no_wal_sidecars(path)


def _checkpoint_source_proving_backup(source_path: Path, backup_path: Path, *, root: Path) -> None:
    """Prove source==backup, close every handle, then switch the source to DELETE."""

    _assert_regular_file(source_path, what="sqlite checkpoint target", root=root)
    connection = sqlite3.connect(str(source_path), timeout=30.0)
    try:
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("BEGIN IMMEDIATE")
        backup = _readonly(backup_path)
        try:
            _assert_logical_equal(connection, backup, what="source vs frozen backup")
        finally:
            backup.close()
        connection.execute("ROLLBACK")
    finally:
        connection.close()
    gc.collect()
    _require_checkpoint_delete(source_path, root=root)


def _plan_from_manifest(manifest: WorldPredictionArchiveManifest) -> WorldPredictionArchivePlan:
    return WorldPredictionArchivePlan(
        recorded_date=manifest.recorded_date,
        row_count=manifest.row_count,
        first_key=manifest.first_key,
        last_key=manifest.last_key,
        column_names=manifest.column_names,
        column_types=manifest.column_types,
        schema_sha256=manifest.schema_sha256,
    )


def _require_canonical_catalog(
    source: SQLiteWorldPredictionArchiveSource,
    sink: DuckDbWorldPredictionParquetStore,
) -> tuple[WorldPredictionArchiveManifest, ...]:
    try:
        catalog = source.canonical_catalog()
    except InvalidWorldPredictionArchiveError as exc:
        raise PredictionReadUnavailable(f"canonical catalog unavailable: {exc}") from exc
    for manifest in catalog:
        plan = _plan_from_manifest(manifest)
        try:
            existing = sink.published(plan)
        except InvalidWorldPredictionArchiveError as exc:
            raise PredictionReadUnavailable(
                f"canonical partition {manifest.recorded_date.isoformat()} unavailable: {exc.reason}"
            ) from exc
        if existing is None:
            raise PredictionReadUnavailable(
                f"canonical partition {manifest.recorded_date.isoformat()} is missing"
            )
        if existing.to_dict() != manifest.to_dict():
            raise PredictionReadUnavailable(
                f"canonical partition {manifest.recorded_date.isoformat()} does not match the registered manifest"
            )
    return catalog


def _iter_partition_tuples(
    connection: sqlite3.Connection,
    plan: WorldPredictionArchivePlan,
) -> Iterator[tuple[object, ...]]:
    selected = ", ".join(_quoted(name) for name in plan.column_names)
    start = plan.recorded_date.isoformat()
    end = (plan.recorded_date + timedelta(days=1)).isoformat()
    cursor = connection.execute(
        f"SELECT {selected} FROM {_HOT_TABLE} WHERE recorded_at >= ? AND recorded_at < ? "
        "ORDER BY recorded_at, prediction_id",
        (start, end),
    )
    while True:
        rows = cursor.fetchmany(_BATCH)
        if not rows:
            break
        yield from (tuple(row) for row in rows)


def _assert_plan_metadata(connection: sqlite3.Connection, plan: WorldPredictionArchivePlan) -> None:
    start = plan.recorded_date.isoformat()
    end = (plan.recorded_date + timedelta(days=1)).isoformat()
    count = int(
        connection.execute(
            f"SELECT COUNT(*) FROM {_HOT_TABLE} WHERE recorded_at >= ? AND recorded_at < ?",
            (start, end),
        ).fetchone()[0]
    )
    first = connection.execute(
        f"SELECT recorded_at, prediction_id FROM {_HOT_TABLE} WHERE recorded_at >= ? AND recorded_at < ? "
        "ORDER BY recorded_at, prediction_id LIMIT 1",
        (start, end),
    ).fetchone()
    last = connection.execute(
        f"SELECT recorded_at, prediction_id FROM {_HOT_TABLE} WHERE recorded_at >= ? AND recorded_at < ? "
        "ORDER BY recorded_at DESC, prediction_id DESC LIMIT 1",
        (start, end),
    ).fetchone()
    if count != plan.row_count:
        raise RuntimeError(
            f"preview row_count mismatch for {plan.recorded_date.isoformat()}: planned {plan.row_count}, sqlite {count}"
        )
    if first is None or last is None or tuple(first) != plan.first_key or tuple(last) != plan.last_key:
        raise RuntimeError(f"preview first/last key mismatch for {plan.recorded_date.isoformat()}")


def _partition_proof(
    connection: sqlite3.Connection, plan: WorldPredictionArchivePlan
) -> tuple[str, int, tuple[str, ...]]:
    digest = hashlib.sha256()
    ids: list[str] = []
    index = plan.column_names.index("prediction_id")
    for row in _iter_partition_tuples(connection, plan):
        digest.update(_archive_row_bytes(row))
        ids.append(str(row[index]))
    return f"sha256:{digest.hexdigest()}", len(ids), tuple(ids)


def _dict_rows(
    connection: sqlite3.Connection, plan: WorldPredictionArchivePlan
) -> Iterator[dict[str, object]]:
    for row in _iter_partition_tuples(connection, plan):
        yield {name: value for name, value in zip(plan.column_names, row, strict=True)}


def _eligible_plans(
    db_path: Path,
    before: date,
    *,
    catalog: Sequence[WorldPredictionArchiveManifest],
) -> tuple[tuple[WorldPredictionArchivePlan, ...], tuple[dict[str, Any], ...]]:
    source = SQLiteWorldPredictionArchiveSource(db_path)
    plans = tuple(source.plan_before(before))
    canonical_dates = {manifest.recorded_date for manifest in catalog}
    selected: list[WorldPredictionArchivePlan] = []
    late: list[dict[str, Any]] = []
    for plan in plans:
        if plan.recorded_date in canonical_dates:
            late.append(
                {
                    "recorded_date": plan.recorded_date.isoformat(),
                    "late_hot_count": plan.row_count,
                    "status": "canonical_active",
                }
            )
            continue
        selected.append(plan)
    return tuple(selected), tuple(late)


def _ensure_archives(
    backup_path: Path,
    archive_root: Path,
    plans: Sequence[WorldPredictionArchivePlan],
    *,
    clock: datetime,
    root: Path,
) -> tuple[tuple[WorldPredictionArchivePlan, WorldPredictionArchiveManifest, tuple[str, ...]], ...]:
    _assert_directory(archive_root, what="archive root", must_exist=False)
    if _is_symlink(archive_root):
        raise RuntimeError(f"refusing symlink archive root: {archive_root}")
    sink = DuckDbWorldPredictionParquetStore(archive_root, clock=lambda: clock)
    source = SQLiteWorldPredictionArchiveSource(backup_path)
    backup = _readonly(backup_path)
    proven: list[tuple[WorldPredictionArchivePlan, WorldPredictionArchiveManifest, tuple[str, ...]]] = []
    try:
        for plan in plans:
            sqlite_hash, sqlite_count, ids = _partition_proof(backup, plan)
            if sqlite_count != plan.row_count:
                raise RuntimeError(
                    f"frozen sqlite partition {plan.recorded_date.isoformat()} changed: "
                    f"planned {plan.row_count}, hashed {sqlite_count}"
                )
            try:
                existing = sink.published(plan)
            except InvalidWorldPredictionArchiveError as exc:
                raise RuntimeError(
                    f"archive proof mismatch for {plan.recorded_date.isoformat()}; "
                    f"refusing to destroy authoritative archives: {exc.reason}"
                ) from exc
            if existing is None:
                manifest = sink.publish(plan, source.iter_partition(plan))
            else:
                manifest = existing
            if manifest.content_sha256 != sqlite_hash or manifest.row_count != sqlite_count:
                raise RuntimeError(
                    f"divergent archive/sqlite content for {plan.recorded_date.isoformat()} "
                    f"(sqlite_count={sqlite_count} archive_count={manifest.row_count})"
                )
            proven.append((plan, manifest, ids))
    finally:
        backup.close()
    return tuple(proven)


def _trigger_sql(connection: sqlite3.Connection, name: str) -> str:
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?",
        (name,),
    ).fetchone()
    if row is None or not row[0]:
        raise RuntimeError(f"missing trigger {name}")
    return str(row[0])


def _delete_ids(connection: sqlite3.Connection, ids: Sequence[str]) -> None:
    for offset in range(0, len(ids), _BATCH):
        chunk = tuple(ids[offset : offset + _BATCH])
        placeholders = ",".join("?" for _ in chunk)
        connection.execute(
            f"DELETE FROM {_HOT_TABLE} WHERE prediction_id IN ({placeholders})",
            chunk,
        )


def _append_generation(connection: sqlite3.Connection, journal: Mapping[str, Any], *, parity_sha256: str) -> None:
    for statement in (
        f"""
        CREATE TABLE IF NOT EXISTS {_GENERATION_TABLE} (
            generation TEXT PRIMARY KEY,
            cutover_id TEXT NOT NULL UNIQUE,
            source_lineage TEXT NOT NULL,
            before_date TEXT NOT NULL,
            activated_at TEXT NOT NULL,
            source_fingerprint_sha256 TEXT NOT NULL,
            backup_path TEXT NOT NULL,
            candidate_path TEXT NOT NULL,
            parity_sha256 TEXT NOT NULL,
            row_count INTEGER NOT NULL
        )
        """,
        f"""
        CREATE TRIGGER IF NOT EXISTS world_prediction_cutover_generations_no_delete
        BEFORE DELETE ON {_GENERATION_TABLE}
        BEGIN
            SELECT RAISE(ABORT, 'world_prediction_cutover_generations are append-only');
        END
        """,
        f"""
        CREATE TRIGGER IF NOT EXISTS world_prediction_cutover_generations_no_update
        BEFORE UPDATE ON {_GENERATION_TABLE}
        BEGIN
            SELECT RAISE(ABORT, 'world_prediction_cutover_generations are append-only');
        END
        """,
    ):
        connection.execute(statement)
    existing = connection.execute(
        f"SELECT 1 FROM {_GENERATION_TABLE} WHERE generation=?",
        (journal["generation"],),
    ).fetchone()
    if existing is not None:
        return
    connection.execute(
        f"INSERT INTO {_GENERATION_TABLE}("
        "generation, cutover_id, source_lineage, before_date, activated_at, "
        "source_fingerprint_sha256, backup_path, candidate_path, parity_sha256, row_count"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            journal["generation"],
            journal["cutover_id"],
            journal["source_path"],
            journal["before"],
            journal["activated_at"],
            journal["source_fingerprint_sha256"],
            journal["backup_path"],
            journal["candidate_path"],
            parity_sha256,
            int(journal.get("row_count") or 0),
        ),
    )


def _registered_partition_ids(connection: sqlite3.Connection) -> set[str]:
    tables = _table_names(connection)
    if _COLD_PARTITIONS not in tables:
        return set()
    return {
        str(row[0])
        for row in connection.execute(f"SELECT partition_id FROM {_COLD_PARTITIONS}")
    }


def _apply_candidate_transaction(
    candidate_path: Path,
    proven: Sequence[tuple[WorldPredictionArchivePlan, WorldPredictionArchiveManifest, tuple[str, ...]]],
    journal: Mapping[str, Any],
) -> str:
    connection = _write_connection(candidate_path)
    moved_ids: list[str] = []
    try:
        connection.execute("BEGIN IMMEDIATE")
        ensure_cold_schema(connection)
        already = _registered_partition_ids(connection)
        for plan, manifest, ids in proven:
            if manifest.relative_path in already:
                continue
            count = register_cold_partition(
                connection,
                manifest=manifest,
                rows=_dict_rows(connection, plan),
                cutover_id=str(journal["cutover_id"]),
                activated_at=str(journal["activated_at"]),
            )
            if count != len(ids):
                raise RuntimeError(
                    f"register_cold_partition count mismatch for {plan.recorded_date.isoformat()}: "
                    f"{count} != {len(ids)}"
                )
            moved_ids.extend(ids)
        lineage = hashlib.sha256(
            json.dumps(
                {"generation": journal["generation"], "prediction_ids": moved_ids},
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        parity_sha256 = f"sha256:{lineage}"
        _append_generation(connection, journal, parity_sha256=parity_sha256)
        original_sql = _trigger_sql(connection, _DELETE_TRIGGER)
        connection.execute(f"DROP TRIGGER {_DELETE_TRIGGER}")
        _delete_ids(connection, moved_ids)
        connection.execute(original_sql)
        restored = _trigger_sql(connection, _DELETE_TRIGGER)
        if restored != original_sql:
            raise RuntimeError("world_predictions_no_delete trigger was not restored exactly")
        connection.execute("COMMIT")
        return parity_sha256
    except Exception:
        try:
            connection.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        connection.close()


def _vacuum_and_check(candidate_path: Path) -> None:
    connection = _write_connection(candidate_path)
    try:
        connection.execute("VACUUM")
        integrity = [str(row[0]) for row in connection.execute("PRAGMA integrity_check")]
        if integrity != ["ok"]:
            raise RuntimeError(f"candidate integrity_check failed: {integrity}")
        foreign = list(connection.execute("PRAGMA foreign_key_check"))
        if foreign:
            raise RuntimeError(f"candidate foreign_key_check failed: {foreign[:5]!r}")
        trigger = _trigger_sql(connection, _DELETE_TRIGGER)
        if "append-only" not in trigger:
            raise RuntimeError("world_predictions_no_delete trigger missing after vacuum")
    finally:
        connection.close()


def _table_digest(connection: sqlite3.Connection, table: str) -> dict[str, Any]:
    info = list(connection.execute(f"PRAGMA table_info({_quoted(table)})"))
    columns = [str(row[1]) for row in info]
    pk = [str(row[1]) for row in sorted(info, key=lambda item: int(item[5] or 0)) if int(row[5] or 0) > 0]
    order = pk or columns
    selected = ", ".join(_quoted(name) for name in columns)
    order_sql = ", ".join(_quoted(name) for name in order)
    cursor = connection.execute(f"SELECT {selected} FROM {_quoted(table)} ORDER BY {order_sql}")
    digest = hashlib.sha256()
    count = 0
    try:
        while True:
            rows = cursor.fetchmany(_BATCH)
            if not rows:
                break
            for row in rows:
                _feed(digest, tuple(row))
                count += 1
    finally:
        cursor.close()
    return {"rows": count, "sha256": digest.hexdigest(), "columns": columns}


def _master_objects(connection: sqlite3.Connection) -> dict[str, str]:
    return {
        f"{row[0]}:{row[1]}": str(row[2])
        for row in connection.execute(
            "SELECT type, name, sql FROM sqlite_master "
            "WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' "
            "ORDER BY type, name"
        )
    }


def _is_operator_schema_name(name: str) -> bool:
    return name in _OPERATOR_TABLES or name == "world_shadow_predictions_no_cold_identity" or name.startswith(
        (
            "world_prediction_cold_",
            "world_prediction_cutover_",
            "idx_world_prediction_cold_",
            "idx_world_prediction_cutover_",
        )
    )


def _assert_schema_preserved(backup: sqlite3.Connection, candidate: sqlite3.Connection) -> None:
    backup_objs = _master_objects(backup)
    candidate_objs = _master_objects(candidate)
    for key, sql in backup_objs.items():
        if candidate_objs.get(key) != sql:
            raise RuntimeError(f"schema object changed during cutover: {key}")
    extra = set(candidate_objs) - set(backup_objs)
    for key in extra:
        name = key.split(":", 1)[1]
        if not _is_operator_schema_name(name):
            raise RuntimeError(f"candidate grew unexpected schema object: {key}")


def _pk_and_columns(connection: sqlite3.Connection, table: str) -> tuple[list[str], list[str]]:
    info = list(connection.execute(f"PRAGMA table_info({_quoted(table)})"))
    columns = [str(row[1]) for row in info]
    pk = [str(row[1]) for row in sorted(info, key=lambda item: int(item[5] or 0)) if int(row[5] or 0) > 0]
    return (pk or list(columns), columns)


def _rows_by_pk(connection: sqlite3.Connection, table: str) -> dict[tuple[object, ...], tuple[object, ...]]:
    if table not in _table_names(connection):
        return {}
    pk, columns = _pk_and_columns(connection, table)
    selected = ", ".join(_quoted(name) for name in columns)
    order_sql = ", ".join(_quoted(name) for name in pk)
    cursor = connection.execute(f"SELECT {selected} FROM {_quoted(table)} ORDER BY {order_sql}")
    mapping: dict[tuple[object, ...], tuple[object, ...]] = {}
    indexes = [columns.index(name) for name in pk]
    while True:
        rows = cursor.fetchmany(_BATCH)
        if not rows:
            break
        for row in rows:
            payload = tuple(row)
            mapping[tuple(payload[index] for index in indexes)] = payload
    return mapping


def _expected_operator_appends(
    backup: sqlite3.Connection,
    proven: Sequence[tuple[WorldPredictionArchivePlan, WorldPredictionArchiveManifest, tuple[str, ...]]],
    journal: Mapping[str, Any],
) -> dict[str, set[tuple[object, ...]]]:
    backup_partitions = _rows_by_pk(backup, _COLD_PARTITIONS)
    backup_index = _rows_by_pk(backup, _COLD_INDEX)
    backup_generations = _rows_by_pk(backup, _GENERATION_TABLE)
    new_partitions: set[tuple[object, ...]] = set()
    new_index: set[tuple[object, ...]] = set()
    for _plan, manifest, ids in proven:
        partition_key = (manifest.relative_path,)
        if partition_key not in backup_partitions:
            new_partitions.add(partition_key)
            for prediction_id in ids:
                index_key = (prediction_id,)
                if index_key not in backup_index:
                    new_index.add(index_key)
    generation_key = (str(journal["generation"]),)
    new_generations = set() if generation_key in backup_generations else {generation_key}
    return {
        _COLD_PARTITIONS: new_partitions,
        _COLD_INDEX: new_index,
        _GENERATION_TABLE: new_generations,
    }


def _assert_operator_appends(
    backup: sqlite3.Connection,
    candidate: sqlite3.Connection,
    proven: Sequence[tuple[WorldPredictionArchivePlan, WorldPredictionArchiveManifest, tuple[str, ...]]],
    journal: Mapping[str, Any],
) -> None:
    expected = _expected_operator_appends(backup, proven, journal)
    for table in (_COLD_PARTITIONS, _COLD_INDEX, _GENERATION_TABLE):
        backup_rows = _rows_by_pk(backup, table)
        candidate_rows = _rows_by_pk(candidate, table)
        for key, row in backup_rows.items():
            if candidate_rows.get(key) != row:
                raise RuntimeError(f"existing {table} row changed during cutover: {key!r}")
        extra = set(candidate_rows) - set(backup_rows)
        if extra != expected[table]:
            raise RuntimeError(
                f"{table} appends are not the expected cutover rows: "
                f"extra={sorted(str(item) for item in extra)} "
                f"expected={sorted(str(item) for item in expected[table])}"
            )


def _other_table_parity(
    backup: sqlite3.Connection,
    candidate: sqlite3.Connection,
    *,
    proven: Sequence[tuple[WorldPredictionArchivePlan, WorldPredictionArchiveManifest, tuple[str, ...]]],
    journal: Mapping[str, Any],
) -> dict[str, Any]:
    _assert_schema_preserved(backup, candidate)
    backup_tables = [name for name in sorted(_table_names(backup) - {"sqlite_sequence"}) if name != _HOT_TABLE]
    proofs: dict[str, Any] = {}
    for table in backup_tables:
        if table in _OPERATOR_TABLES:
            continue
        expected = _table_digest(backup, table)
        actual = _table_digest(candidate, table)
        if expected != actual:
            raise RuntimeError(f"table {table} content changed during cutover")
        proofs[table] = {"rows": expected["rows"], "sha256": expected["sha256"]}
    _assert_operator_appends(backup, candidate, proven, journal)
    extra = (_table_names(candidate) - _table_names(backup)) - _OPERATOR_TABLES - {"sqlite_sequence"}
    if extra:
        raise RuntimeError(f"candidate grew unexpected tables: {sorted(extra)}")
    return proofs


def _logical_ledger_sha256(connection: sqlite3.Connection) -> str:
    digest = hashlib.sha256()
    tables = sorted(_table_names(connection) - {"sqlite_sequence"})
    for table in tables:
        proof = _table_digest(connection, table)
        _feed(digest, [table, proof["rows"], proof["sha256"], proof["columns"]])
    schema = json.dumps(_master_objects(connection), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest.update(len(schema.encode("utf-8")).to_bytes(8, "big"))
    digest.update(schema.encode("utf-8"))
    return f"sha256:{digest.hexdigest()}"


def _open_logical(path: Path) -> sqlite3.Connection:
    return _readonly(path)


def _assert_logical_equal(left: sqlite3.Connection, right: sqlite3.Connection, *, what: str) -> None:
    if _logical_ledger_sha256(left) != _logical_ledger_sha256(right):
        raise RuntimeError(f"{what}: logical ledger diverged from the frozen backup")


def _assert_source_matches_frozen_backup(source_path: Path, backup_path: Path, *, locked: bool = False) -> None:
    if locked:
        source = sqlite3.connect(str(source_path), timeout=30.0)
        try:
            source.execute("PRAGMA busy_timeout=5000")
            source.execute("BEGIN IMMEDIATE")
            backup = _readonly(backup_path)
            try:
                _assert_logical_equal(source, backup, what="source vs frozen backup")
            finally:
                backup.close()
            source.execute("ROLLBACK")
        finally:
            source.close()
        return
    source = _readonly(source_path)
    try:
        backup = _readonly(backup_path)
        try:
            _assert_logical_equal(source, backup, what="source vs frozen backup")
        finally:
            backup.close()
    finally:
        source.close()


def _assert_backup_intact(backup_path: Path, journal: Mapping[str, Any]) -> None:
    expected = journal.get("source_logical_sha256")
    if not isinstance(expected, str) or not expected:
        return
    actual = _logical_ledger_from_path(backup_path)
    if actual != expected:
        raise RuntimeError("retained backup logical content changed; refusing to continue")


def _assert_durable_fingerprint(path: Path, expected: Mapping[str, Any] | None, *, what: str) -> None:
    if not isinstance(expected, Mapping) or not expected:
        return
    actual = _durable_fingerprint(path)
    comparable_actual = {key: actual.get(key) for key in ("size", "sha256", "wal")}
    comparable_expected = {key: expected.get(key) for key in ("size", "sha256", "wal")}
    if comparable_actual != comparable_expected:
        raise RuntimeError(f"{what} durable fingerprint changed")


def _prediction_columns(connection: sqlite3.Connection) -> tuple[str, ...]:
    return tuple(str(row[1]) for row in connection.execute(f"PRAGMA table_info({_HOT_TABLE})"))


def _assert_no_divergent_hot_cold(connection: sqlite3.Connection) -> None:
    tables = _table_names(connection)
    if _COLD_INDEX not in tables or _HOT_TABLE not in tables:
        return
    hot_cols = {str(row[1]) for row in connection.execute(f"PRAGMA table_info({_quoted(_HOT_TABLE)})")}
    cold_cols = {str(row[1]) for row in connection.execute(f"PRAGMA table_info({_quoted(_COLD_INDEX)})")}
    compare = [
        name
        for name in (
            "recorded_at",
            "episode_id",
            "horizon_code",
            "model_kind",
            "model_version",
            "run_id",
            "predicted_at",
            "input_sha256",
            "prediction_sha256",
            "payload_sha256",
        )
        if name in hot_cols and name in cold_cols
    ]
    if not compare:
        return
    mismatch = " OR ".join(f"h.{_quoted(name)} IS NOT c.{_quoted(name)}" for name in compare)
    row = connection.execute(
        f"SELECT h.prediction_id FROM {_HOT_TABLE} AS h "
        f"JOIN {_COLD_INDEX} AS c ON c.prediction_id = h.prediction_id "
        f"WHERE {mismatch} LIMIT 1"
    ).fetchone()
    if row is not None:
        raise RuntimeError(f"hot/cold prediction content diverges for {row[0]}")


def _ordered_prediction_ids(connection: sqlite3.Connection) -> list[str]:
    tables = _table_names(connection)
    if _COLD_INDEX in tables:
        _assert_no_divergent_hot_cold(connection)
        sql = (
            "SELECT prediction_id FROM ("
            f"SELECT prediction_id, recorded_at FROM {_HOT_TABLE} "
            f"UNION ALL SELECT c.prediction_id, c.recorded_at FROM {_COLD_INDEX} AS c "
            f"WHERE NOT EXISTS (SELECT 1 FROM {_HOT_TABLE} AS h WHERE h.prediction_id = c.prediction_id)"
            ") ORDER BY recorded_at, prediction_id"
        )
    else:
        sql = f"SELECT prediction_id FROM {_HOT_TABLE} ORDER BY recorded_at, prediction_id"
    return [str(row[0]) for row in connection.execute(sql)]


def _identity_tuple(row: Mapping[str, Any] | Sequence[Any]) -> tuple[Any, ...]:
    if isinstance(row, Mapping):
        return (row.get("episode_id"), row.get("horizon_code"), row.get("model_kind"), row.get("model_version"))
    return (row[0], row[1], row[2], row[3])


def _logical_count(connection: sqlite3.Connection) -> int:
    tables = _table_names(connection)
    if _COLD_INDEX in tables or _COLD_PARTITIONS in tables:
        return int(prediction_count(connection))
    return int(connection.execute(f"SELECT COUNT(*) FROM {_HOT_TABLE}").fetchone()[0])


def _content_digest(
    connection: sqlite3.Connection,
    *,
    columns: Sequence[str],
    ids: Sequence[str],
) -> str:
    digest = hashlib.sha256()
    previous_factory = connection.row_factory
    connection.row_factory = sqlite3.Row
    try:
        for offset in range(0, len(ids), _CONTENT_READ_BATCH):
            batch = list(ids[offset : offset + _CONTENT_READ_BATCH])
            rows = read_prediction_rows(connection, columns=tuple(columns), prediction_ids=batch, order="recorded")
            by_id = {str(row["prediction_id"]): row for row in rows}
            if len(by_id) != len(batch):
                raise RuntimeError("prediction content batch cardinality mismatch")
            for prediction_id in batch:
                row = by_id[prediction_id]
                _feed(digest, [row.get(column) for column in columns])
    finally:
        connection.row_factory = previous_factory
    return f"sha256:{digest.hexdigest()}"


def _verify_parity(
    backup_path: Path,
    candidate_path: Path,
    proven: Sequence[tuple[WorldPredictionArchivePlan, WorldPredictionArchiveManifest, tuple[str, ...]]],
    journal: Mapping[str, Any],
) -> dict[str, Any]:
    backup = _readonly(backup_path)
    candidate = _readonly(candidate_path)
    try:
        other = _other_table_parity(backup, candidate, proven=proven, journal=journal)
        columns = _prediction_columns(backup)
        backup_ids = _ordered_prediction_ids(backup)
        candidate_ids = _ordered_prediction_ids(candidate)
        if backup_ids != candidate_ids:
            raise RuntimeError("logical prediction identity order mismatch after cutover")
        backup_count = _logical_count(backup)
        candidate_count = _logical_count(candidate)
        if backup_count != candidate_count or backup_count != len(backup_ids):
            raise RuntimeError(
                f"logical prediction count mismatch: backup={backup_count} "
                f"candidate={candidate_count} ids={len(backup_ids)}"
            )
        backup_identities = [_identity_tuple(row) for row in read_prediction_identities(backup)]
        candidate_identities = [_identity_tuple(row) for row in read_prediction_identities(candidate)]
        if backup_identities != candidate_identities:
            raise RuntimeError("logical prediction identities mismatch after cutover")
        content = _content_digest(candidate, columns=columns, ids=candidate_ids)
        backup_content = _content_digest(backup, columns=columns, ids=backup_ids)
        if content != backup_content:
            raise RuntimeError("logical prediction content digest mismatch after cutover")
        partitions = []
        for plan, manifest, ids in proven:
            partitions.append(
                {
                    "recorded_date": plan.recorded_date.isoformat(),
                    "row_count": len(ids),
                    "relative_path": manifest.relative_path,
                    "content_sha256": manifest.content_sha256,
                    "parquet_sha256": manifest.parquet_sha256,
                    "parquet_bytes": manifest.parquet_bytes,
                }
            )
        hot_count = int(candidate.execute(f"SELECT COUNT(*) FROM {_HOT_TABLE}").fetchone()[0])
        cold_count = 0
        if _COLD_INDEX in _table_names(candidate):
            cold_count = int(candidate.execute(f"SELECT COUNT(*) FROM {_COLD_INDEX}").fetchone()[0])
        return {
            "prediction_count": candidate_count,
            "hot_count": hot_count,
            "cold_count": cold_count,
            "content_sha256": content,
            "identity_count": len(candidate_identities),
            "other_tables": other,
            "partitions": partitions,
        }
    finally:
        backup.close()
        candidate.close()


def _source_has_generation(path: Path, generation: str) -> bool:
    connection = _readonly(path)
    try:
        if _GENERATION_TABLE not in _table_names(connection):
            return False
        row = connection.execute(
            f"SELECT 1 FROM {_GENERATION_TABLE} WHERE generation=?",
            (generation,),
        ).fetchone()
        return row is not None
    finally:
        connection.close()


def _file_bytes(path: Path) -> int:
    total = os.lstat(path).st_size
    for suffix in ("-wal", "-shm"):
        side = _sidecar(path, suffix)
        st = _lstat(side)
        if st is not None and stat.S_ISREG(st.st_mode) and not stat.S_ISLNK(st.st_mode):
            total += st.st_size
    return total


def _allocate_cutover_id(db_path: Path, now: datetime) -> str:
    base = now.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    suffix = 0
    while True:
        cutover_id = f"cutover-{base}" if suffix == 0 else f"cutover-{base}-{suffix}"
        backup = Path(str(db_path) + f".cutover-backup.{cutover_id}")
        candidate = Path(str(db_path) + f".cutover-candidate.{cutover_id}")
        if _lstat(backup) is None and _lstat(candidate) is None:
            return cutover_id
        suffix += 1


def _trip(failpoint: str | None, stage: str) -> None:
    if failpoint == f"after_{stage}":
        raise PredictionCutoverFailpoint(stage)


def _advance(journal_path: Path, journal: dict[str, Any], stage: str, *, root: Path, failpoint: str | None) -> None:
    journal["stage"] = stage
    _write_journal(journal_path, journal, root=root)
    _trip(failpoint, stage)


def _preview(
    state_dir: Path,
    db_path: Path,
    before: date,
    *,
    today: date,
    now: datetime,
) -> dict[str, Any]:
    _assert_directory(state_dir, what="state dir")
    with shared_prediction_storage_lease(db_path, create=False):
        _assert_world_ledger(db_path, root=state_dir)
        archive_root = state_dir / "world_model_archive"
        source = SQLiteWorldPredictionArchiveSource(db_path)
        sink = DuckDbWorldPredictionParquetStore(archive_root, clock=lambda: now)
        catalog = source.canonical_catalog()
        if catalog:
            _require_canonical_catalog(source, sink)
        selected, late = _eligible_plans(db_path, before, catalog=catalog)
        days: list[dict[str, Any]] = []
        connection = _readonly(db_path)
        try:
            for plan in selected:
                _assert_plan_metadata(connection, plan)
                existing = None
                if _is_dir(archive_root):
                    try:
                        published = sink.published(plan)
                    except InvalidWorldPredictionArchiveError:
                        published = None
                    existing = None if published is None else published.parquet_bytes
                days.append(
                    {
                        "recorded_date": plan.recorded_date.isoformat(),
                        "row_count": plan.row_count,
                        "status": "planned",
                        "existing_parquet_bytes": existing,
                    }
                )
        finally:
            connection.close()
        return {
            "schema_version": CUTOVER_REPORT_SCHEMA,
            "ok": True,
            "mode": "preview",
            "apply": False,
            "adopt": False,
            "before": before.isoformat(),
            "today": today.isoformat(),
            "source_path": str(db_path),
            "archive_root": str(archive_root),
            "source_bytes": _file_bytes(db_path),
            "planned_backup_path": str(db_path) + ".cutover-backup",
            "planned_candidate_path": str(db_path) + ".cutover-candidate",
            "sqlite_read_sidecars": (
                "SQLite URI mode=ro may create -wal/-shm sidecars if they were absent; "
                "preview does not create operator artifacts or mutate source payloads"
            ),
            "eligible_days": days,
            "late_hot_days": list(late),
            "row_count": sum(item["row_count"] for item in days),
            "source_retained": True,
            "journal": None,
        }


def _report(
    *,
    mode: str,
    journal: Mapping[str, Any],
    parity: Mapping[str, Any] | None,
    source_bytes: int,
    candidate_bytes: int | None,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": CUTOVER_REPORT_SCHEMA,
        "ok": True,
        "mode": mode,
        "apply": True,
        "adopt": bool(journal.get("adopt")),
        "before": journal["before"],
        "cutover_id": journal["cutover_id"],
        "generation": journal["generation"],
        "source_path": journal["source_path"],
        "backup_path": journal["backup_path"],
        "candidate_path": journal["candidate_path"],
        "archive_root": journal["archive_root"],
        "stage": journal["stage"],
        "source_bytes": source_bytes,
        "candidate_bytes": candidate_bytes,
        "source_retained": mode != "adopt",
        "parity": parity,
        "eligible_days": journal.get("eligible_days") or [],
        "late_hot_days": journal.get("late_hot_days") or [],
        "rollback_after_new_writes": (
            "not supported; restore the retained backup only before new writes, "
            "otherwise rehydrate cold partitions into a copy of the CURRENT ledger"
        ),
    }
    if extra:
        payload.update(extra)
    return payload


def _logical_ledger_from_path(path: Path) -> str:
    connection = _open_logical(path)
    try:
        return _logical_ledger_sha256(connection)
    finally:
        connection.close()


def _recompute_parity(
    backup_path: Path,
    candidate_path: Path,
    proven: Sequence[tuple[WorldPredictionArchivePlan, WorldPredictionArchiveManifest, tuple[str, ...]]],
    journal: Mapping[str, Any],
) -> dict[str, Any]:
    with shared_prediction_storage_lease(candidate_path, create=True):
        return _verify_parity(backup_path, candidate_path, proven, journal)


def _assert_parity_stable(stored: object, parity: Mapping[str, Any]) -> None:
    if not isinstance(stored, dict) or not stored:
        return
    if stored.get("content_sha256") != parity.get("content_sha256") or stored.get("prediction_count") != parity.get(
        "prediction_count"
    ):
        raise RuntimeError("candidate parity changed since verification")


def _verify_adopted_source(
    db_path: Path,
    journal: Mapping[str, Any],
    archive_root: Path,
    now: datetime,
) -> None:
    connection = sqlite3.connect(str(db_path))
    try:
        integrity = [str(row[0]) for row in connection.execute("PRAGMA integrity_check")]
        if integrity != ["ok"]:
            raise RuntimeError(f"adopted source integrity_check failed: {integrity}")
        foreign = list(connection.execute("PRAGMA foreign_key_check"))
        if foreign:
            raise RuntimeError(f"adopted source foreign_key_check failed: {foreign[:5]!r}")
        if _GENERATION_TABLE not in _table_names(connection):
            raise RuntimeError("adopted world_model.db is missing the intended cutover generation marker")
        row = connection.execute(
            f"SELECT generation FROM {_GENERATION_TABLE} WHERE generation=?",
            (journal["generation"],),
        ).fetchone()
        if row is None:
            raise RuntimeError("adopted world_model.db is missing the intended cutover generation marker")
        validate_registered_cold_storage(connection)
        hot_count = int(connection.execute(f"SELECT COUNT(*) FROM {_HOT_TABLE}").fetchone()[0])
        cold_count = 0
        if _COLD_INDEX in _table_names(connection):
            cold_count = int(connection.execute(f"SELECT COUNT(*) FROM {_COLD_INDEX}").fetchone()[0])
    finally:
        connection.close()
    parity = journal.get("parity") if isinstance(journal.get("parity"), dict) else {}
    if parity:
        if hot_count != int(parity.get("hot_count", -1)) or cold_count != int(parity.get("cold_count", -1)):
            raise RuntimeError("adopted source hot/cold counts do not match verified candidate parity")
        expected_candidate = journal.get("candidate_checkpoint_fingerprint")
        if isinstance(expected_candidate, dict) and expected_candidate:
            _assert_durable_fingerprint(db_path, expected_candidate, what="adopted source")
    source = SQLiteWorldPredictionArchiveSource(db_path)
    sink = DuckDbWorldPredictionParquetStore(archive_root, clock=lambda: now)
    _require_canonical_catalog(source, sink)


def _prove_and_checkpoint_before_swap(
    *,
    db_path: Path,
    backup_path: Path,
    candidate_path: Path,
    proven: Sequence[tuple[WorldPredictionArchivePlan, WorldPredictionArchiveManifest, tuple[str, ...]]],
    journal: dict[str, Any],
    root: Path,
    failpoint: str | None,
) -> dict[str, Any]:
    _require_checkpoint_delete(candidate_path, root=root)
    _checkpoint_source_proving_backup(db_path, backup_path, root=root)
    _trip(failpoint, "source_checkpoint")
    _assert_source_matches_frozen_backup(db_path, backup_path, locked=True)
    if not _source_has_generation(candidate_path, str(journal["generation"])):
        raise RuntimeError("candidate is missing the intended cutover generation marker")
    parity = _recompute_parity(backup_path, candidate_path, proven, journal)
    _assert_parity_stable(journal.get("parity"), parity)
    source_ckpt = _durable_fingerprint(db_path)
    candidate_ckpt = _durable_fingerprint(candidate_path)
    prior_source = journal.get("source_checkpoint_fingerprint")
    prior_candidate = journal.get("candidate_checkpoint_fingerprint")
    _assert_durable_fingerprint(
        db_path,
        prior_source if isinstance(prior_source, dict) else None,
        what="source after checkpoint",
    )
    _assert_durable_fingerprint(
        candidate_path,
        prior_candidate if isinstance(prior_candidate, dict) else None,
        what="candidate after checkpoint",
    )
    journal["parity"] = parity
    journal["source_checkpoint_fingerprint"] = source_ckpt
    journal["candidate_checkpoint_fingerprint"] = candidate_ckpt
    journal["source_logical_sha256"] = _logical_ledger_from_path(backup_path)
    return parity


def _run_apply(
    *,
    state_dir: Path,
    db_path: Path,
    before: date | None,
    apply_adopt: bool,
    resume: bool,
    now: datetime,
    failpoint: str | None,
    offline: Mapping[str, Any],
) -> dict[str, Any]:
    root = state_dir
    journal_path = _journal_file(db_path)
    archive_root = state_dir / "world_model_archive"
    if resume:
        if _lstat(journal_path) is None:
            raise RuntimeError("no cutover journal to resume")
        journal = _read_journal(journal_path, root=root)
        if before is not None and journal.get("before") != before.isoformat():
            raise RuntimeError(
                f"resume before {before.isoformat()} does not match journal {journal.get('before')}"
            )
        before = date.fromisoformat(str(journal["before"]))
        if apply_adopt:
            journal["adopt"] = True
        swapped = _source_has_generation(db_path, str(journal["generation"]))
        if swapped:
            if _stage_index(str(journal["stage"])) < _stage_index("pre_swap"):
                raise RuntimeError("source generation matches but journal is not at swap; refusing ambiguous state")
            _verify_adopted_source(db_path, journal, archive_root, now)
            _advance(journal_path, journal, "swapped", root=root, failpoint=failpoint)
            source_bytes = _file_bytes(db_path)
            _advance(journal_path, journal, "success", root=root, failpoint=None)
            _clear_journal(journal_path, root=root)
            return _report(
                mode="adopt",
                journal={**journal, "stage": "success"},
                parity=journal.get("parity") if isinstance(journal.get("parity"), dict) else None,
                source_bytes=source_bytes,
                candidate_bytes=None,
                extra={"resumed": True, "offline": dict(offline), "journal": None, "backup_retained": True},
            )
        backup_path = Path(str(journal["backup_path"]))
        candidate_path = Path(str(journal["candidate_path"]))
        _assert_confined(backup_path, root, what="backup", leaf_must_exist=_reached(journal, "snapshot"))
        _assert_confined(candidate_path, root, what="candidate", leaf_must_exist=_reached(journal, "snapshot"))
        if not _reached(journal, "snapshot"):
            _assert_durable_fingerprint(
                db_path,
                journal.get("source_fingerprint") if isinstance(journal.get("source_fingerprint"), dict) else None,
                what="source before snapshot",
            )
        else:
            _assert_backup_intact(backup_path, journal)
            _assert_source_matches_frozen_backup(db_path, backup_path)
    else:
        if _lstat(journal_path) is not None:
            raise RuntimeError(f"unresolved cutover journal requires resume: {journal_path}")
        if before is None:
            raise TypeError("before is required unless resume=True")
        reject_open_or_future_cutoff(before, today=now.date())
        _assert_world_ledger(db_path, root=root)
        fingerprint = _source_fingerprint(db_path)
        cutover_id = _allocate_cutover_id(db_path, now)
        backup_path = Path(str(db_path) + f".cutover-backup.{cutover_id}")
        candidate_path = Path(str(db_path) + f".cutover-candidate.{cutover_id}")
        journal = {
            "schema_version": CUTOVER_JOURNAL_SCHEMA,
            "cutover_id": cutover_id,
            "generation": cutover_id,
            "before": before.isoformat(),
            "source_path": str(db_path),
            "source_fingerprint": fingerprint,
            "source_fingerprint_sha256": _fingerprint_digest(fingerprint),
            "backup_path": str(backup_path),
            "candidate_path": str(candidate_path),
            "archive_root": str(archive_root),
            "stage": "acquired",
            "adopt": apply_adopt,
            "activated_at": now.isoformat(),
            "eligible_days": [],
            "late_hot_days": [],
            "row_count": 0,
            "parity": None,
        }
        _advance(journal_path, journal, "acquired", root=root, failpoint=failpoint)

    assert before is not None
    source = SQLiteWorldPredictionArchiveSource(db_path)
    sink = DuckDbWorldPredictionParquetStore(archive_root, clock=lambda: now)
    catalog = _require_canonical_catalog(source, sink)
    selected, late = _eligible_plans(db_path, before, catalog=catalog)
    journal["late_hot_days"] = list(late)
    journal["eligible_days"] = [
        {"recorded_date": plan.recorded_date.isoformat(), "row_count": plan.row_count, "status": "planned"}
        for plan in selected
    ]
    journal["row_count"] = sum(plan.row_count for plan in selected)
    journal["adopt"] = bool(journal.get("adopt")) or apply_adopt
    _write_journal(journal_path, journal, root=root)

    backup_path = Path(str(journal["backup_path"]))
    candidate_path = Path(str(journal["candidate_path"]))

    if not _reached(journal, "snapshot"):
        _remove_incomplete(backup_path, root=root)
        _remove_incomplete(candidate_path, root=root)
        _sqlite_backup(db_path, backup_path, root=root)
        _sqlite_backup(backup_path, candidate_path, root=root)
        journal["source_logical_sha256"] = _logical_ledger_from_path(backup_path)
        journal["backup_fingerprint"] = _durable_fingerprint(backup_path)
        _advance(journal_path, journal, "snapshot", root=root, failpoint=failpoint)
    else:
        _assert_backup_intact(backup_path, journal)
        _assert_source_matches_frozen_backup(db_path, backup_path)

    proven: tuple[tuple[WorldPredictionArchivePlan, WorldPredictionArchiveManifest, tuple[str, ...]], ...]
    if not _reached(journal, "archives_verified"):
        proven = _ensure_archives(backup_path, archive_root, selected, clock=now, root=root)
        journal["eligible_days"] = [
            {
                "recorded_date": plan.recorded_date.isoformat(),
                "row_count": len(ids),
                "relative_path": manifest.relative_path,
                "content_sha256": manifest.content_sha256,
                "status": "verified",
            }
            for plan, manifest, ids in proven
        ]
        _advance(journal_path, journal, "archives_verified", root=root, failpoint=failpoint)
    else:
        proven = _ensure_archives(backup_path, archive_root, selected, clock=now, root=root)

    if not _reached(journal, "candidate"):
        lineage = _apply_candidate_transaction(candidate_path, proven, journal)
        journal["lineage_sha256"] = lineage
        _advance(journal_path, journal, "candidate", root=root, failpoint=failpoint)

    if not _reached(journal, "compacted"):
        _vacuum_and_check(candidate_path)
        _advance(journal_path, journal, "compacted", root=root, failpoint=failpoint)

    parity = _recompute_parity(backup_path, candidate_path, proven, journal)
    if _reached(journal, "verified"):
        _assert_parity_stable(journal.get("parity"), parity)
        journal["parity"] = parity
    else:
        journal["parity"] = parity
        _advance(journal_path, journal, "verified", root=root, failpoint=failpoint)

    source_bytes = _file_bytes(db_path)
    candidate_bytes = _file_bytes(candidate_path)

    if not journal.get("adopt"):
        _advance(journal_path, journal, "prepared", root=root, failpoint=failpoint)
        _clear_journal(journal_path, root=root)
        return _report(
            mode="prepare",
            journal={**journal, "stage": "prepared"},
            parity=parity,
            source_bytes=source_bytes,
            candidate_bytes=candidate_bytes,
            extra={"resumed": resume, "offline": dict(offline), "journal": None},
        )

    parity = _prove_and_checkpoint_before_swap(
        db_path=db_path,
        backup_path=backup_path,
        candidate_path=candidate_path,
        proven=proven,
        journal=journal,
        root=root,
        failpoint=failpoint,
    )
    if not _reached(journal, "pre_swap"):
        _advance(journal_path, journal, "pre_swap", root=root, failpoint=failpoint)

    if not _reached(journal, "swapped"):
        _assert_source_matches_frozen_backup(db_path, backup_path, locked=True)
        _assert_durable_fingerprint(
            db_path,
            journal.get("source_checkpoint_fingerprint")
            if isinstance(journal.get("source_checkpoint_fingerprint"), dict)
            else None,
            what="source immediately before replace",
        )
        _assert_durable_fingerprint(
            candidate_path,
            journal.get("candidate_checkpoint_fingerprint")
            if isinstance(journal.get("candidate_checkpoint_fingerprint"), dict)
            else None,
            what="candidate immediately before replace",
        )
        os.replace(candidate_path, db_path)
        _fsync_directory(db_path.parent)
        _advance(journal_path, journal, "swapped", root=root, failpoint=failpoint)

    _verify_adopted_source(db_path, journal, archive_root, now)
    _advance(journal_path, journal, "success", root=root, failpoint=None)
    _clear_journal(journal_path, root=root)
    return _report(
        mode="adopt",
        journal={**journal, "stage": "success"},
        parity=parity,
        source_bytes=_file_bytes(db_path),
        candidate_bytes=candidate_bytes,
        extra={"resumed": resume, "offline": dict(offline), "journal": None, "backup_retained": True},
    )


def prepare_prediction_cutover(
    state_dir: str | Path,
    before: date | None,
    *,
    apply: bool = False,
    adopt: bool = False,
    resume: bool = False,
    clock: Callable[[], datetime] | datetime | None = None,
    failpoint: str | None = None,
) -> dict[str, Any]:
    """Plan, prepare or adopt an offline World prediction hot/cold cutover."""

    state = Path(state_dir)
    now = resolve_cutover_clock(clock)
    today = utc_cutover_today(now)
    if resume:
        apply = True
    if adopt and not apply:
        raise ValueError("adopt=True requires apply=True")
    if not resume:
        if before is None:
            raise TypeError("before is required unless resume=True")
        reject_open_or_future_cutoff(before, today=today)
    db_path = state / "world_model.db"
    if not apply:
        return _preview(state, db_path, before, today=today, now=now)

    _assert_directory(state, what="state dir")
    offline = _require_offline(state)
    allow_in_progress = bool(resume)
    journal_path = _journal_file(db_path)
    if not resume and _lstat(journal_path) is not None:
        raise RuntimeError(f"unresolved cutover journal requires resume: {journal_path}")
    with exclusive_prediction_storage_lease(db_path, allow_in_progress=allow_in_progress):
        return _run_apply(
            state_dir=state,
            db_path=db_path,
            before=before,
            apply_adopt=bool(adopt),
            resume=resume,
            now=now,
            failpoint=failpoint,
            offline=offline,
        )


def resume_prediction_cutover(
    state_dir: str | Path,
    *,
    adopt: bool = False,
    clock: Callable[[], datetime] | datetime | None = None,
    failpoint: str | None = None,
) -> dict[str, Any]:
    return prepare_prediction_cutover(
        state_dir,
        None,
        apply=True,
        adopt=adopt,
        resume=True,
        clock=clock,
        failpoint=failpoint,
    )


__all__ = [
    "PredictionCutoverFailpoint",
    "PredictionStorageBusyError",
    "prepare_prediction_cutover",
    "resume_prediction_cutover",
]
