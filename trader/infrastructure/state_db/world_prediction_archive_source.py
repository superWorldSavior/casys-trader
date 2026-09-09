"""Read-only SQLite source for closed World prediction archive partitions."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import quote

from trader.application.world_model.prediction_archive_ports import PredictionRow
from trader.domain.world_prediction_archive import (
    InvalidWorldPredictionArchiveError,
    WorldPredictionArchiveManifest,
    WorldPredictionArchivePlan,
)
from trader.infrastructure.state_db.world_prediction_storage_lock import (
    shared_prediction_storage_lease,
)
from trader.infrastructure.state_db.world_prediction_tiers import (
    PredictionReadUnavailable,
    cold_catalog_present,
    validate_registered_cold_storage,
)

_TABLE = "world_shadow_predictions"
_COLD_PARTITIONS = "world_prediction_cold_partitions"
_CATALOG_COLUMNS = (
    "partition_id",
    "manifest_json",
    "manifest_sha256",
    "recorded_date",
    "relative_path",
    "row_count",
)


def _digest(value: object) -> str:
    payload = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _sha256_stored_text(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


@contextmanager
def _readonly(path: Path):
    if not path.is_file():
        raise FileNotFoundError(str(path))
    uri = f"file:{quote(str(path.resolve()), safe='/')}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    try:
        yield connection
    finally:
        connection.close()


def _schema(connection: sqlite3.Connection) -> tuple[tuple[str, str, int, int], ...]:
    tables = {str(row[0]) for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if _TABLE not in tables:
        raise RuntimeError(f"missing table: {_TABLE}")
    rows = tuple(
        (str(row[1]), str(row[2] or "TEXT").upper(), int(row[3]), int(row[5]))
        for row in connection.execute(f"PRAGMA table_info({_TABLE})")
    )
    if not rows or "prediction_id" not in {row[0] for row in rows} or "recorded_at" not in {row[0] for row in rows}:
        raise RuntimeError("world prediction archive requires prediction_id and recorded_at")
    return rows


class SQLiteWorldPredictionArchiveSource:
    """Streams immutable rows without loading a partition into Python memory."""

    def __init__(self, db_path: str | Path, *, batch_size: int = 1_000) -> None:
        if isinstance(batch_size, bool) or not isinstance(batch_size, int) or batch_size < 1:
            raise ValueError("batch_size must be >= 1")
        self.db_path = Path(db_path)
        self.batch_size = batch_size

    def export_storage_lease(self, *, apply: bool = False):
        """Shared lease spanning one export service operation, never source methods."""

        return shared_prediction_storage_lease(self.db_path, create=apply)

    def canonical_catalog(self) -> tuple[WorldPredictionArchiveManifest, ...]:
        """Read and verify the active catalog without creating schema."""

        with _readonly(self.db_path) as connection:
            try:
                if not cold_catalog_present(connection):
                    return ()
            except PredictionReadUnavailable as exc:
                raise InvalidWorldPredictionArchiveError(f"canonical catalog unavailable: {exc}") from exc
            columns = {
                str(row[1]) for row in connection.execute(f"PRAGMA table_info({_COLD_PARTITIONS})")
            }
            missing = [name for name in _CATALOG_COLUMNS if name not in columns]
            if missing:
                raise InvalidWorldPredictionArchiveError(
                    "world_prediction_cold_partitions schema is incomplete: " + ", ".join(missing)
                )
            order_by = "recorded_date, partition_id"
            selected = ", ".join(_CATALOG_COLUMNS)
            manifests: list[WorldPredictionArchiveManifest] = []
            for row in connection.execute(f"SELECT {selected} FROM {_COLD_PARTITIONS} ORDER BY {order_by}"):
                partition_id, manifest_json, manifest_sha256, recorded_date, relative_path, row_count = row
                if not isinstance(manifest_json, str):
                    raise InvalidWorldPredictionArchiveError("canonical partition manifest_json is unreadable")
                stored_hash = _sha256_stored_text(manifest_json)
                if stored_hash != str(manifest_sha256 or ""):
                    raise InvalidWorldPredictionArchiveError(
                        f"canonical partition registry hash mismatch: {partition_id}"
                    )
                try:
                    payload = json.loads(manifest_json)
                except json.JSONDecodeError as exc:
                    raise InvalidWorldPredictionArchiveError(
                        "canonical partition manifest_json is unreadable"
                    ) from exc
                if not isinstance(payload, dict):
                    raise InvalidWorldPredictionArchiveError("canonical partition manifest_json must be an object")
                try:
                    manifest = WorldPredictionArchiveManifest.from_mapping(payload)
                except (TypeError, ValueError) as exc:
                    raise InvalidWorldPredictionArchiveError(
                        f"canonical partition manifest is invalid: {exc}"
                    ) from exc
                if (
                    manifest.relative_path != str(relative_path or "")
                    or manifest.relative_path != str(partition_id or "")
                    or manifest.recorded_date.isoformat() != str(recorded_date or "")
                    or manifest.row_count != int(row_count or 0)
                ):
                    raise InvalidWorldPredictionArchiveError(
                        f"canonical partition registry identity mismatch: {partition_id}"
                    )
                manifests.append(manifest)
            try:
                validate_registered_cold_storage(connection)
            except PredictionReadUnavailable as exc:
                raise InvalidWorldPredictionArchiveError(f"canonical catalog unavailable: {exc}") from exc
            return tuple(manifests)

    def plan_before(self, before: date) -> tuple[WorldPredictionArchivePlan, ...]:
        if not isinstance(before, date) or isinstance(before, datetime):
            raise TypeError("before must be a date")
        with _readonly(self.db_path) as connection:
            schema = _schema(connection)
            columns = tuple(row[0] for row in schema)
            column_types = tuple(row[1] for row in schema)
            schema_sha256 = _digest(schema)
            cursor = connection.execute(
                f"SELECT recorded_at, prediction_id FROM {_TABLE} "
                "WHERE recorded_at < ? ORDER BY recorded_at, prediction_id",
                (before.isoformat(),),
            )
            plans: list[WorldPredictionArchivePlan] = []
            current_day: date | None = None
            count = 0
            first_key: tuple[str, str] | None = None
            last_key: tuple[str, str] | None = None

            def flush() -> None:
                if current_day is None or first_key is None or last_key is None or count < 1:
                    return
                plans.append(
                    WorldPredictionArchivePlan(
                        recorded_date=current_day,
                        row_count=count,
                        first_key=first_key,
                        last_key=last_key,
                        column_names=columns,
                        column_types=column_types,
                        schema_sha256=schema_sha256,
                    )
                )

            while True:
                batch = cursor.fetchmany(self.batch_size)
                if not batch:
                    break
                for recorded_at_raw, prediction_id_raw in batch:
                    recorded_at = str(recorded_at_raw)
                    prediction_id = str(prediction_id_raw)
                    if len(recorded_at) < 10:
                        raise RuntimeError("world prediction recorded_at is not an ISO timestamp")
                    recorded_day = date.fromisoformat(recorded_at[:10])
                    key = (recorded_at, prediction_id)
                    if current_day is None:
                        current_day = recorded_day
                        first_key = key
                        last_key = key
                        count = 1
                        continue
                    if recorded_day != current_day:
                        flush()
                        current_day = recorded_day
                        first_key = key
                        last_key = key
                        count = 1
                        continue
                    last_key = key
                    count += 1
            flush()
            return tuple(plans)

    def iter_partition(self, plan: WorldPredictionArchivePlan) -> Iterator[PredictionRow]:
        with _readonly(self.db_path) as connection:
            schema = _schema(connection)
            if tuple(row[0] for row in schema) != plan.column_names or _digest(schema) != plan.schema_sha256:
                raise RuntimeError("world prediction schema changed after archive planning")
            selected = ", ".join(f'"{name}"' for name in plan.column_names)
            start = plan.recorded_date.isoformat()
            end = (plan.recorded_date + timedelta(days=1)).isoformat()
            cursor = connection.execute(
                f"SELECT {selected} FROM {_TABLE} WHERE recorded_at >= ? AND recorded_at < ? "
                "ORDER BY recorded_at, prediction_id",
                (start, end),
            )
            while True:
                rows = cursor.fetchmany(self.batch_size)
                if not rows:
                    break
                yield from (tuple(row) for row in rows)


__all__ = ["SQLiteWorldPredictionArchiveSource"]
