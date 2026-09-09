"""Cold catalog schema, registration, and canonical Parquet integrity.

Reader helpers are implemented in ``world_prediction_reader`` and re-exported
here. Those helpers do not acquire the cooperative storage lease: the QUERY
caller must hold a shared lease spanning SQLite open, transaction, and close.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import threading
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from trader.domain.world_prediction_archive import WorldPredictionArchiveManifest
from trader.infrastructure.state_db.world_prediction_parquet_store import DuckDbWorldPredictionParquetStore

WORLD_PREDICTION_COLD_PARTITIONS_TABLE = "world_prediction_cold_partitions"
WORLD_PREDICTION_COLD_INDEX_TABLE = "world_prediction_cold_index"
WORLD_MODEL_ARCHIVE_DIRNAME = "world_model_archive"
PREDICTION_JSON_COLUMNS = ("payload_json", "prediction_json")
COLD_INDEX_SCALAR_COLUMNS = (
    "prediction_id",
    "run_id",
    "episode_id",
    "horizon_code",
    "model_kind",
    "model_version",
    "predicted_at",
    "input_sha256",
    "prediction_sha256",
    "payload_sha256",
    "recorded_at",
    "study_cohort_id",
    "lane_id",
    "manifest_sha256",
    "feature_contract_fingerprint",
    "feature_mask_fingerprint",
)
_REQUIRED_INDEX_COLUMNS = (
    "prediction_id",
    "run_id",
    "episode_id",
    "horizon_code",
    "input_sha256",
    "prediction_sha256",
    "payload_sha256",
    "recorded_at",
)
_HASH_CACHE_LOCK = threading.Lock()
_HASH_CACHE: dict[str, "_PartitionCache"] = {}

_COLD_SCHEMA_DDL = (
    """
CREATE TABLE IF NOT EXISTS world_prediction_cold_partitions (
                partition_id     TEXT PRIMARY KEY,
                manifest_json    TEXT NOT NULL,
                manifest_sha256  TEXT NOT NULL,
                cutover_id       TEXT NOT NULL,
                activated_at     TEXT NOT NULL,
                recorded_date    TEXT NOT NULL,
                relative_path    TEXT NOT NULL,
                row_count        INTEGER NOT NULL
            )
            """,
    """
CREATE TABLE IF NOT EXISTS world_prediction_cold_index (
                prediction_id                  TEXT PRIMARY KEY,
                partition_id                   TEXT NOT NULL
                    REFERENCES world_prediction_cold_partitions(partition_id),
                run_id                         TEXT NOT NULL,
                episode_id                     TEXT NOT NULL,
                horizon_code                   TEXT NOT NULL,
                model_kind                     TEXT,
                model_version                  TEXT,
                predicted_at                   TEXT,
                input_sha256                   TEXT NOT NULL,
                prediction_sha256              TEXT NOT NULL,
                payload_sha256                 TEXT NOT NULL,
                recorded_at                    TEXT NOT NULL,
                study_cohort_id                TEXT,
                lane_id                        TEXT,
                manifest_sha256                TEXT,
                feature_contract_fingerprint   TEXT,
                feature_mask_fingerprint       TEXT
            )
            """,
    """
CREATE INDEX IF NOT EXISTS idx_world_prediction_cold_index_partition
            ON world_prediction_cold_index(partition_id, prediction_id)
            """,
    """
CREATE INDEX IF NOT EXISTS idx_world_prediction_cold_index_recorded
            ON world_prediction_cold_index(recorded_at, prediction_id)
            """,
    """
CREATE INDEX IF NOT EXISTS idx_world_prediction_cold_index_identity
            ON world_prediction_cold_index(episode_id, horizon_code, model_kind, model_version)
            """,
    """
CREATE INDEX IF NOT EXISTS idx_world_prediction_cold_index_study_cohort
            ON world_prediction_cold_index(study_cohort_id, prediction_id)
            """,
    """
CREATE INDEX IF NOT EXISTS idx_world_prediction_cold_index_run_episode
            ON world_prediction_cold_index(run_id, horizon_code, episode_id, prediction_id)
            """,
    """
CREATE TRIGGER IF NOT EXISTS world_prediction_cold_partitions_no_delete
            BEFORE DELETE ON world_prediction_cold_partitions
            BEGIN
                SELECT RAISE(ABORT, 'world_prediction_cold_partitions are append-only');
            END
            """,
    """
CREATE TRIGGER IF NOT EXISTS world_prediction_cold_partitions_no_update
            BEFORE UPDATE ON world_prediction_cold_partitions
            BEGIN
                SELECT RAISE(ABORT, 'world_prediction_cold_partitions are append-only');
            END
            """,
    """
CREATE TRIGGER IF NOT EXISTS world_prediction_cold_index_no_delete
            BEFORE DELETE ON world_prediction_cold_index
            BEGIN
                SELECT RAISE(ABORT, 'world_prediction_cold_index are append-only');
            END
            """,
    """
CREATE TRIGGER IF NOT EXISTS world_prediction_cold_index_no_update
            BEFORE UPDATE ON world_prediction_cold_index
            BEGIN
                SELECT RAISE(ABORT, 'world_prediction_cold_index are append-only');
            END
            """,
)

_HOT_COLD_IDENTITY_TRIGGER = """
CREATE TRIGGER IF NOT EXISTS world_shadow_predictions_no_cold_identity
            BEFORE INSERT ON world_shadow_predictions
            BEGIN
                SELECT RAISE(ABORT, 'prediction_id already exists in cold storage')
                WHERE EXISTS (
                    SELECT 1 FROM world_prediction_cold_index AS cold
                    WHERE cold.prediction_id = NEW.prediction_id
                );
            END
            """


class PredictionReadUnavailable(RuntimeError):
    """Authoritative cold prediction storage is missing, corrupt, or inconsistent."""

    def __init__(self, reason: str) -> None:
        self.reason = str(reason)
        super().__init__(self.reason)


@dataclass(frozen=True)
class _PartitionCache:
    parquet_identity: tuple[int, int, int, int, int]
    manifest_identity: tuple[int, int, int, int, int]
    parquet_sha256: str
    parquet_bytes: int


def ensure_cold_schema(connection: sqlite3.Connection | sqlite3.Cursor) -> None:
    """Add cold catalog DDL without committing the caller transaction."""

    executor = _executor(connection)
    cold_catalog_present(executor)
    for statement in _COLD_SCHEMA_DDL:
        executor.execute(statement)
    if _table_exists(executor, "world_shadow_predictions"):
        executor.execute(_HOT_COLD_IDENTITY_TRIGGER)


def register_cold_partition(
    connection: sqlite3.Connection | sqlite3.Cursor,
    *,
    manifest: WorldPredictionArchiveManifest | Mapping[str, Any],
    rows: Iterable[Any],
    cutover_id: str,
    activated_at: str,
) -> int:
    """Install one immutable catalog partition and its exact cold index rows.

    ``rows`` are the original SQLite tuples (manifest column order), mappings,
    or ``sqlite.Row`` values. Does not commit the caller transaction.
    """

    executor = _executor(connection)
    ensure_cold_schema(executor)
    parsed = _as_manifest(manifest)
    cutover = _required_text(cutover_id, "cutover_id")
    activated = _required_text(activated_at, "activated_at")
    _assert_manifest_matches_source_schema(executor, parsed)
    materialized, tuples = _materialize_register_rows(parsed, rows)
    if len(materialized) != parsed.row_count:
        raise ValueError(
            f"cold partition row count {len(materialized)} does not match manifest {parsed.row_count}"
        )
    first_key = (str(materialized[0]["recorded_at"]), str(materialized[0]["prediction_id"]))
    last_key = (str(materialized[-1]["recorded_at"]), str(materialized[-1]["prediction_id"]))
    if first_key != tuple(parsed.first_key) or last_key != tuple(parsed.last_key):
        raise ValueError("cold partition first/last keys do not match the manifest")
    content_sha256 = _content_sha256(tuples)
    if content_sha256 != parsed.content_sha256:
        raise ValueError("cold partition rows do not match manifest content hash")
    archive_root = world_model_archive_root(executor)
    parquet_path = validate_manifest_partition_files(archive_root=archive_root, manifest=parsed)
    try:
        DuckDbWorldPredictionParquetStore(archive_root)._verify_parquet(parquet_path, parsed)
    except PredictionReadUnavailable:
        raise
    except (OSError, TypeError, ValueError, RuntimeError) as exc:
        raise ValueError(f"canonical parquet does not match source/manifest: {exc}") from exc
    manifest_json = _canonical_manifest_json(parsed)
    manifest_sha256 = _sha256_bytes(manifest_json.encode("utf-8"))
    partition_id = parsed.relative_path
    executor.execute(
        """
        INSERT INTO world_prediction_cold_partitions(
            partition_id, manifest_json, manifest_sha256, cutover_id, activated_at,
            recorded_date, relative_path, row_count
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            partition_id,
            manifest_json,
            manifest_sha256,
            cutover,
            activated,
            parsed.recorded_date.isoformat(),
            parsed.relative_path,
            parsed.row_count,
        ),
    )
    insert_sql = (
        "INSERT INTO world_prediction_cold_index("
        + ", ".join(("partition_id", *COLD_INDEX_SCALAR_COLUMNS))
        + ") VALUES ("
        + ", ".join("?" for _ in range(1 + len(COLD_INDEX_SCALAR_COLUMNS)))
        + ")"
    )
    for row in materialized:
        executor.execute(
            insert_sql,
            (partition_id, *(row.get(name) for name in COLD_INDEX_SCALAR_COLUMNS)),
        )
    return len(materialized)


def cold_catalog_present(connection: sqlite3.Connection | sqlite3.Cursor) -> bool:
    executor = _executor(connection)
    names = _table_names(executor)
    partitions = WORLD_PREDICTION_COLD_PARTITIONS_TABLE in names
    index = WORLD_PREDICTION_COLD_INDEX_TABLE in names
    if partitions != index:
        raise PredictionReadUnavailable(
            "world prediction cold catalog is incomplete: partitions and index must exist together"
        )
    return partitions


def world_model_database_path(connection: sqlite3.Connection | sqlite3.Cursor) -> Path | None:
    for row in _executor(connection).execute("PRAGMA database_list"):
        name, file = _database_list_row(row)
        if name == "main":
            return Path(file) if file else None
    return None


def world_model_archive_root(connection: sqlite3.Connection | sqlite3.Cursor) -> Path:
    db_path = world_model_database_path(connection)
    if db_path is None:
        raise PredictionReadUnavailable("world-model database path is unavailable")
    return db_path.parent / WORLD_MODEL_ARCHIVE_DIRNAME


def validate_registered_cold_storage(connection: sqlite3.Connection | sqlite3.Cursor) -> None:
    """Fail closed if any registered canonical partition is missing or modified."""

    if not cold_catalog_present(connection):
        return
    executor = _executor(connection)
    try:
        rows = list(
            executor.execute(
                "SELECT partition_id, manifest_json, manifest_sha256, relative_path, row_count "
                "FROM world_prediction_cold_partitions"
            )
        )
        indexed_counts: dict[str, int] = {}
        for row in executor.execute(
            "SELECT partition_id, COUNT(*) AS row_count FROM world_prediction_cold_index GROUP BY partition_id"
        ):
            mapping = _row_mapping(row, ("partition_id", "row_count"))
            partition_id = _required_catalog_text(mapping.get("partition_id"), "partition_id")
            indexed_counts[partition_id] = _required_catalog_int(mapping.get("row_count"), "index row_count")
        orphan = executor.execute(
            "SELECT partition_id FROM world_prediction_cold_index "
            "WHERE partition_id NOT IN (SELECT partition_id FROM world_prediction_cold_partitions) "
            "LIMIT 1"
        ).fetchone()
        if orphan is not None:
            orphan_id = _row_mapping(orphan, ("partition_id",)).get("partition_id")
            raise PredictionReadUnavailable(f"cold index references missing partition: {orphan_id}")
        if not rows:
            return
        archive_root = world_model_archive_root(executor)
        for row in rows:
            mapping = _row_mapping(
                row, ("partition_id", "manifest_json", "manifest_sha256", "relative_path", "row_count")
            )
            partition_id = _required_catalog_text(mapping.get("partition_id"), "partition_id")
            manifest_json = mapping.get("manifest_json")
            if not isinstance(manifest_json, str) or not manifest_json:
                raise PredictionReadUnavailable(f"cold partition registry manifest_json is invalid: {partition_id}")
            registry_hash = mapping.get("manifest_sha256")
            if not isinstance(registry_hash, str) or not registry_hash:
                raise PredictionReadUnavailable(f"cold partition registry hash is invalid: {partition_id}")
            if _sha256_bytes(manifest_json.encode("utf-8")) != registry_hash:
                raise PredictionReadUnavailable(f"cold partition registry hash mismatch: {partition_id}")
            try:
                parsed_json = json.loads(manifest_json)
                if not isinstance(parsed_json, dict):
                    raise TypeError("manifest_json must be an object")
                manifest = WorldPredictionArchiveManifest.from_mapping(parsed_json)
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise PredictionReadUnavailable(
                    f"cold partition registry manifest is invalid: {partition_id}"
                ) from exc
            relative_path = _required_catalog_text(mapping.get("relative_path"), "relative_path")
            row_count = _required_catalog_int(mapping.get("row_count"), "row_count")
            if (
                manifest.relative_path != relative_path
                or manifest.relative_path != partition_id
                or manifest.row_count != row_count
            ):
                raise PredictionReadUnavailable(f"cold partition registry identity mismatch: {partition_id}")
            if indexed_counts.get(partition_id, 0) != row_count:
                raise PredictionReadUnavailable(
                    f"cold index row count mismatch for {partition_id}: "
                    f"{indexed_counts.get(partition_id, 0)} != {row_count}"
                )
            validate_manifest_partition_files(archive_root=archive_root, manifest=manifest)
    except PredictionReadUnavailable:
        raise
    except (OSError, TypeError, ValueError, sqlite3.Error, json.JSONDecodeError) as exc:
        raise PredictionReadUnavailable(str(exc) or type(exc).__name__) from exc


def validate_manifest_partition_files(
    *,
    archive_root: Path,
    manifest: WorldPredictionArchiveManifest,
) -> Path:
    """Validate the registered parquet + sidecar manifest and return the parquet path."""

    parquet_path = _confined_regular_file(archive_root, manifest.relative_path, what="canonical parquet")
    manifest_path = parquet_path.parent / "manifest.json"
    try:
        before_parquet = _file_identity(parquet_path, what="canonical parquet")
        before_manifest = _file_identity(manifest_path, what="canonical partition manifest")
        _assert_external_manifest(manifest_path, manifest)
        digest, size = _cached_or_hash_parquet(parquet_path, before_parquet, before_manifest, manifest)
        if digest != manifest.parquet_sha256 or size != manifest.parquet_bytes:
            raise PredictionReadUnavailable(f"canonical parquet digest or size mismatch: {parquet_path}")
        after_parquet = _file_identity(parquet_path, what="canonical parquet")
        after_manifest = _file_identity(manifest_path, what="canonical partition manifest")
        if after_parquet != before_parquet or after_manifest != before_manifest:
            raise PredictionReadUnavailable(f"canonical parquet changed during validation: {parquet_path}")
        return parquet_path
    except PredictionReadUnavailable:
        raise
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise PredictionReadUnavailable(str(exc) or type(exc).__name__) from exc


def partition_file_identity(path: Path, *, what: str) -> tuple[int, int, int, int, int]:
    return _file_identity(path, what=what)


def _executor(connection: sqlite3.Connection | sqlite3.Cursor) -> sqlite3.Connection | sqlite3.Cursor:
    if not isinstance(connection, (sqlite3.Connection, sqlite3.Cursor)):
        raise TypeError("connection must be a sqlite3 Connection or Cursor")
    return connection


def _table_names(executor: sqlite3.Connection | sqlite3.Cursor) -> set[str]:
    return {str(row[0]) for row in executor.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _table_exists(executor: sqlite3.Connection | sqlite3.Cursor, name: str) -> bool:
    return name in _table_names(executor)


def _as_manifest(value: WorldPredictionArchiveManifest | Mapping[str, Any]) -> WorldPredictionArchiveManifest:
    if isinstance(value, WorldPredictionArchiveManifest):
        return value
    if isinstance(value, Mapping):
        return WorldPredictionArchiveManifest.from_mapping(value)
    raise TypeError("manifest must be WorldPredictionArchiveManifest or a mapping")


def _required_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value.strip()


def _required_catalog_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PredictionReadUnavailable(f"cold catalog {field} is invalid")
    return value.strip()


def _required_catalog_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise PredictionReadUnavailable(f"cold catalog {field} is invalid")
    if value < 0:
        raise PredictionReadUnavailable(f"cold catalog {field} is invalid")
    return value


def _assert_manifest_matches_source_schema(
    executor: sqlite3.Connection | sqlite3.Cursor,
    manifest: WorldPredictionArchiveManifest,
) -> None:
    if not _table_exists(executor, "world_shadow_predictions"):
        raise ValueError("world_shadow_predictions is required to register a cold partition")
    source_names: list[str] = []
    source_types: list[str] = []
    for row in executor.execute("PRAGMA table_info(world_shadow_predictions)"):
        mapping = _row_mapping(row, ("cid", "name", "type", "notnull", "dflt_value", "pk"))
        name = str(mapping["name"])
        source_names.append(name)
        source_types.append(str(mapping.get("type") or "TEXT").upper())
    for required in (*PREDICTION_JSON_COLUMNS, *COLD_INDEX_SCALAR_COLUMNS):
        if required not in manifest.column_names:
            raise ValueError(f"cold partition manifest is missing required column {required}")
        if required not in source_names:
            raise ValueError(f"world_shadow_predictions is missing required column {required}")
    if tuple(manifest.column_names) != tuple(source_names):
        raise ValueError("cold partition manifest columns do not match source world_shadow_predictions schema")
    manifest_types = tuple(str(item).upper() for item in manifest.column_types)
    if manifest_types != tuple(source_types):
        raise ValueError("cold partition manifest column types do not match source world_shadow_predictions schema")


def _canonical_manifest_json(manifest: WorldPredictionArchiveManifest) -> str:
    return json.dumps(manifest.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256_bytes(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _row_bytes(row: tuple[object, ...]) -> bytes:
    def encode(value: object) -> object:
        if isinstance(value, bytes):
            return {"$bytes_hex": value.hex()}
        return value

    return json.dumps([encode(value) for value in row], ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"


def _content_sha256(rows: list[tuple[object, ...]]) -> str:
    digest = hashlib.sha256()
    for row in rows:
        digest.update(_row_bytes(row))
    return "sha256:" + digest.hexdigest()


def _materialize_register_rows(
    manifest: WorldPredictionArchiveManifest,
    rows: Iterable[Any],
) -> tuple[list[dict[str, Any]], list[tuple[object, ...]]]:
    materialized: list[dict[str, Any]] = []
    tuples: list[tuple[object, ...]] = []
    seen: set[str] = set()
    for raw in rows:
        mapping, as_tuple = _coerce_register_row(raw, manifest.column_names)
        prediction_id = mapping.get("prediction_id")
        if not isinstance(prediction_id, str) or not prediction_id.strip():
            raise ValueError("cold partition row requires prediction_id")
        if prediction_id in seen:
            raise ValueError(f"duplicate prediction_id in cold partition rows: {prediction_id}")
        seen.add(prediction_id)
        for required in _REQUIRED_INDEX_COLUMNS:
            if required not in manifest.column_names:
                raise ValueError(f"cold partition manifest is missing required column {required}")
            if mapping.get(required) in (None, ""):
                raise ValueError(f"cold partition row requires {required}")
        materialized.append(mapping)
        tuples.append(as_tuple)
    if not materialized:
        raise ValueError("cold partition rows must not be empty")
    return materialized, tuples


def _coerce_register_row(
    row: Any,
    column_names: tuple[str, ...],
) -> tuple[dict[str, Any], tuple[object, ...]]:
    if isinstance(row, sqlite3.Row):
        mapping = {str(key): row[key] for key in row.keys()}
    elif isinstance(row, Mapping):
        mapping = {str(key): value for key, value in row.items()}
    elif isinstance(row, (tuple, list)):
        if len(row) != len(column_names):
            raise ValueError("cold partition row does not match manifest column_names")
        mapping = {name: value for name, value in zip(column_names, row, strict=True)}
    else:
        raise TypeError("cold partition rows must be mappings, sqlite.Row, or tuples")
    try:
        as_tuple = tuple(mapping[name] for name in column_names)
    except KeyError as exc:
        raise ValueError(f"cold partition row is missing column {exc.args[0]}") from exc
    return mapping, as_tuple


def _database_list_row(row: Any) -> tuple[str, str]:
    if isinstance(row, sqlite3.Row):
        return str(row["name"]), str(row["file"] or "")
    if isinstance(row, Mapping):
        return str(row.get("name") or ""), str(row.get("file") or "")
    return str(row[1]), str(row[2] or "")


def _row_mapping(row: Any, columns: tuple[str, ...]) -> dict[str, Any]:
    if isinstance(row, sqlite3.Row):
        return {str(key): row[key] for key in row.keys()}
    if isinstance(row, Mapping):
        return {str(key): value for key, value in row.items()}
    return {name: value for name, value in zip(columns, row, strict=True)}


def _confined_regular_file(root: Path, relative: str, *, what: str) -> Path:
    rel = PurePosixPath(relative)
    if rel.is_absolute() or ".." in rel.parts or not rel.parts:
        raise PredictionReadUnavailable(f"{what} path is not a confined relative path: {relative}")
    authority = Path(os.path.normpath(os.path.abspath(root)))
    try:
        st = os.lstat(authority)
    except FileNotFoundError as exc:
        raise PredictionReadUnavailable(f"missing archive root: {authority}") from exc
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        raise PredictionReadUnavailable(f"archive root is not a real directory: {authority}")
    current = authority
    for part in rel.parts:
        if part in {"", ".", ".."}:
            raise PredictionReadUnavailable(f"unsafe archive path component: {part}")
        current = current / part
        try:
            st = os.lstat(current)
        except FileNotFoundError as exc:
            raise PredictionReadUnavailable(f"missing {what}: {current}") from exc
        if stat.S_ISLNK(st.st_mode):
            raise PredictionReadUnavailable(f"refusing symlink {what}: {current}")
    if not stat.S_ISREG(st.st_mode):
        raise PredictionReadUnavailable(f"{what} is not a regular file: {current}")
    return current


def _file_identity(path: Path, *, what: str) -> tuple[int, int, int, int, int]:
    try:
        st = os.lstat(path)
    except FileNotFoundError as exc:
        raise PredictionReadUnavailable(f"missing {what}: {path}") from exc
    if stat.S_ISLNK(st.st_mode):
        raise PredictionReadUnavailable(f"refusing symlink {what}: {path}")
    if not stat.S_ISREG(st.st_mode):
        raise PredictionReadUnavailable(f"{what} is not a regular file: {path}")
    return (st.st_dev, st.st_ino, st.st_mtime_ns, st.st_ctime_ns, st.st_size)


def _assert_external_manifest(path: Path, expected: WorldPredictionArchiveManifest) -> None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PredictionReadUnavailable(f"canonical partition manifest unreadable: {path}") from exc
    if not isinstance(payload, Mapping):
        raise PredictionReadUnavailable(f"canonical partition manifest must be an object: {path}")
    try:
        actual = WorldPredictionArchiveManifest.from_mapping(payload)
    except (TypeError, ValueError) as exc:
        raise PredictionReadUnavailable(f"canonical partition manifest is invalid: {path}") from exc
    if actual.to_dict() != expected.to_dict():
        raise PredictionReadUnavailable(f"canonical partition manifest does not match registry: {path}")


def _cached_or_hash_parquet(
    parquet_path: Path,
    parquet_identity: tuple[int, int, int, int, int],
    manifest_identity: tuple[int, int, int, int, int],
    manifest: WorldPredictionArchiveManifest,
) -> tuple[str, int]:
    cache_key = str(parquet_path)
    with _HASH_CACHE_LOCK:
        cached = _HASH_CACHE.get(cache_key)
    if (
        cached is not None
        and cached.parquet_identity == parquet_identity
        and cached.manifest_identity == manifest_identity
        and cached.parquet_sha256 == manifest.parquet_sha256
        and cached.parquet_bytes == manifest.parquet_bytes
        and parquet_identity[4] == manifest.parquet_bytes
    ):
        return cached.parquet_sha256, cached.parquet_bytes
    digest = _file_sha256(parquet_path)
    size = parquet_identity[4]
    if size != manifest.parquet_bytes:
        raise PredictionReadUnavailable(f"canonical parquet size mismatch: {parquet_path}")
    with _HASH_CACHE_LOCK:
        _HASH_CACHE[cache_key] = _PartitionCache(
            parquet_identity=parquet_identity,
            manifest_identity=manifest_identity,
            parquet_sha256=digest,
            parquet_bytes=size,
        )
    return digest, size


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise PredictionReadUnavailable(f"canonical parquet is not a regular file: {path}")
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    finally:
        os.close(fd)
    return "sha256:" + digest.hexdigest()


from trader.infrastructure.state_db.world_prediction_reader import (  # noqa: E402
    cold_prediction_for_replay,
    prediction_count,
    read_prediction_identities,
    read_prediction_rows,
)

__all__ = [
    "COLD_INDEX_SCALAR_COLUMNS",
    "PREDICTION_JSON_COLUMNS",
    "PredictionReadUnavailable",
    "WORLD_MODEL_ARCHIVE_DIRNAME",
    "WORLD_PREDICTION_COLD_INDEX_TABLE",
    "WORLD_PREDICTION_COLD_PARTITIONS_TABLE",
    "cold_catalog_present",
    "cold_prediction_for_replay",
    "ensure_cold_schema",
    "partition_file_identity",
    "prediction_count",
    "read_prediction_identities",
    "read_prediction_rows",
    "register_cold_partition",
    "validate_manifest_partition_files",
    "validate_registered_cold_storage",
    "world_model_archive_root",
    "world_model_database_path",
]
