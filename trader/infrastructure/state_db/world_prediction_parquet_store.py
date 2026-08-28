"""Atomic DuckDB-backed Parquet projection for World shadow predictions."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import tempfile
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
import duckdb

from trader.application.world_model.prediction_archive_ports import PredictionRow
from trader.domain.world_prediction_archive import (
    InvalidWorldPredictionArchiveError,
    PREDICTION_ARCHIVE_QUARANTINE_SCHEMA,
    PREDICTION_ARCHIVE_SCHEMA,
    PredictionArchiveState,
    WorldPredictionArchiveManifest,
    WorldPredictionArchivePlan,
    WorldPredictionArchiveQuarantine,
)

_DATASET = "world_shadow_predictions"
_PARQUET_NAME = "part-00000.parquet"
_MANIFEST_NAME = "manifest.json"
_SQLITE_TO_DUCKDB = {
    "TEXT": "VARCHAR",
    "VARCHAR": "VARCHAR",
    "INTEGER": "BIGINT",
    "INT": "BIGINT",
    "REAL": "DOUBLE",
    "FLOAT": "DOUBLE",
    "DOUBLE": "DOUBLE",
    "BLOB": "BLOB",
}


def _quoted_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _sql_literal(value: str | Path) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _row_bytes(row: tuple[object, ...]) -> bytes:
    def encode(value: object) -> object:
        if isinstance(value, bytes):
            return {"$bytes_hex": value.hex()}
        return value

    return (
        json.dumps([encode(value) for value in row], ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
    )


def _lstat(path: Path) -> os.stat_result | None:
    try:
        return os.lstat(path)
    except FileNotFoundError:
        return None


def _is_symlink(path: Path) -> bool:
    st = _lstat(path)
    return st is not None and stat.S_ISLNK(st.st_mode)


def _is_dir_nofollow(path: Path) -> bool:
    st = _lstat(path)
    return st is not None and stat.S_ISDIR(st.st_mode)


def _is_regular_nofollow(path: Path) -> bool:
    st = _lstat(path)
    return st is not None and stat.S_ISREG(st.st_mode)


def _lexically_confined(path: Path, root: Path) -> bool:
    candidate = Path(os.path.normpath(os.path.abspath(path)))
    authority = Path(os.path.normpath(os.path.abspath(root)))
    try:
        candidate.relative_to(authority)
    except ValueError:
        return False
    return True


def _assert_confined_nofollow(path: Path, root: Path, *, what: str, leaf_must_exist: bool = True) -> None:
    if not _lexically_confined(path, root):
        raise RuntimeError(f"{what} escapes the archive root: {path}")
    candidate = Path(os.path.normpath(os.path.abspath(path)))
    authority = Path(os.path.normpath(os.path.abspath(root)))
    try:
        relative = candidate.relative_to(authority)
    except ValueError as exc:
        raise RuntimeError(f"{what} escapes the archive root: {path}") from exc
    current = authority
    try:
        st = os.lstat(current)
    except FileNotFoundError as exc:
        raise RuntimeError(f"missing archive root: {current}") from exc
    if stat.S_ISLNK(st.st_mode):
        raise RuntimeError(f"refusing symlink archive ancestor: {current}")
    if not stat.S_ISDIR(st.st_mode):
        raise RuntimeError(f"archive ancestor is not a directory: {current}")
    parts = relative.parts
    for index, part in enumerate(parts):
        if part in {"", ".", ".."}:
            raise RuntimeError(f"unsafe archive path component: {part}")
        current = current / part
        try:
            st = os.lstat(current)
        except FileNotFoundError as exc:
            if leaf_must_exist or index < len(parts) - 1:
                raise RuntimeError(f"missing archive path: {current}") from exc
            return
        if stat.S_ISLNK(st.st_mode):
            raise RuntimeError(f"refusing symlink archive target: {current}")


def _ensure_real_directory(path: Path, *, root: Path) -> None:
    if not _lexically_confined(path, root):
        raise RuntimeError(f"archive directory escapes the archive root: {path}")
    candidate = Path(os.path.normpath(os.path.abspath(path)))
    authority = Path(os.path.normpath(os.path.abspath(root)))
    try:
        st = os.lstat(authority)
    except FileNotFoundError:
        os.mkdir(authority, 0o700)
        st = os.lstat(authority)
    if stat.S_ISLNK(st.st_mode):
        raise RuntimeError(f"refusing symlink archive root: {authority}")
    if not stat.S_ISDIR(st.st_mode):
        raise RuntimeError(f"archive root is not a directory: {authority}")
    os.chmod(authority, 0o700)
    try:
        relative = candidate.relative_to(authority)
    except ValueError as exc:
        raise RuntimeError(f"archive directory escapes the archive root: {path}") from exc
    current = authority
    for part in relative.parts:
        current = current / part
        try:
            st = os.lstat(current)
        except FileNotFoundError:
            os.mkdir(current, 0o700)
            st = os.lstat(current)
        if stat.S_ISLNK(st.st_mode):
            raise RuntimeError(f"refusing symlink archive directory: {current}")
        if not stat.S_ISDIR(st.st_mode):
            raise RuntimeError(f"archive path is not a directory: {current}")
        os.chmod(current, 0o700)


def _fsync_regular_file(path: Path) -> None:
    fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise RuntimeError(f"archive target is not a regular file: {path}")
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
    if _is_symlink(path) or not _is_regular_nofollow(path):
        raise RuntimeError(f"archive parquet is missing, not regular, or is a symlink: {path}")
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


def _content_sha256(rows: Iterator[tuple[object, ...]]) -> tuple[str, int]:
    digest = hashlib.sha256()
    count = 0
    for row in rows:
        digest.update(_row_bytes(row))
        count += 1
    return f"sha256:{digest.hexdigest()}", count


class DuckDbWorldPredictionParquetStore:
    """Publishes one immutable, verified directory per closed UTC day."""

    def __init__(self, archive_root: str | Path, *, clock=None, insert_batch_size: int = 1_000) -> None:
        if isinstance(insert_batch_size, bool) or not isinstance(insert_batch_size, int) or insert_batch_size < 1:
            raise ValueError("insert_batch_size must be >= 1")
        self.archive_root = Path(archive_root)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.insert_batch_size = insert_batch_size

    def _schema_root(self) -> Path:
        return self.archive_root / _DATASET / f"schema={PREDICTION_ARCHIVE_SCHEMA}"

    def _partition_dir(self, plan: WorldPredictionArchivePlan) -> Path:
        return self._schema_root() / f"recorded_date={plan.recorded_date.isoformat()}"

    def _partition_relative(self, plan: WorldPredictionArchivePlan) -> Path:
        return (
            Path(_DATASET) / f"schema={PREDICTION_ARCHIVE_SCHEMA}" / f"recorded_date={plan.recorded_date.isoformat()}"
        )

    def published(self, plan: WorldPredictionArchivePlan) -> WorldPredictionArchiveManifest | None:
        return self._read_verified(plan)

    def _assert_usable_root(self) -> None:
        st = _lstat(self.archive_root)
        if st is None:
            return
        if stat.S_ISLNK(st.st_mode):
            raise RuntimeError(f"refusing symlink archive root: {self.archive_root}")
        if not stat.S_ISDIR(st.st_mode):
            raise RuntimeError(f"archive root is not a directory: {self.archive_root}")

    def _read_verified(self, plan: WorldPredictionArchivePlan) -> WorldPredictionArchiveManifest | None:
        self._assert_usable_root()
        partition = self._partition_dir(plan)
        st = _lstat(partition)
        if st is None:
            return None
        try:
            if stat.S_ISLNK(st.st_mode):
                raise InvalidWorldPredictionArchiveError(f"archive partition is a symlink: {partition}")
            if not stat.S_ISDIR(st.st_mode):
                raise InvalidWorldPredictionArchiveError(f"archive partition is not a directory: {partition}")
            _assert_confined_nofollow(partition, self.archive_root, what="archive partition")
            manifest_path = partition / _MANIFEST_NAME
            parquet_path = partition / _PARQUET_NAME
            if _is_symlink(manifest_path):
                raise InvalidWorldPredictionArchiveError(f"archive manifest is a symlink: {manifest_path}")
            if not _is_regular_nofollow(manifest_path):
                raise InvalidWorldPredictionArchiveError(f"archive partition exists without manifest: {partition}")
            if _is_symlink(parquet_path):
                raise InvalidWorldPredictionArchiveError(f"archive parquet is a symlink: {parquet_path}")
            if not _is_regular_nofollow(parquet_path):
                raise InvalidWorldPredictionArchiveError(f"archive parquet is not a regular file: {parquet_path}")
            try:
                payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise InvalidWorldPredictionArchiveError(f"archive manifest unreadable: {manifest_path}") from exc
            if not isinstance(payload, dict):
                raise InvalidWorldPredictionArchiveError(f"archive manifest must be an object: {manifest_path}")
            manifest = WorldPredictionArchiveManifest.from_mapping(payload)
            self._assert_manifest_matches_plan(manifest, plan)
            self._verify_parquet(parquet_path, manifest)
            return manifest
        except InvalidWorldPredictionArchiveError:
            raise
        except (OSError, TypeError, ValueError, RuntimeError) as exc:
            raise InvalidWorldPredictionArchiveError(str(exc) or type(exc).__name__) from exc

    def quarantine(self, plan: WorldPredictionArchivePlan, *, reason: str) -> dict[str, object]:
        """Atomically move a damaged partition aside; never overwrite or delete it."""

        self._assert_usable_root()
        partition = self._partition_dir(plan)
        if _lstat(partition) is None:
            raise RuntimeError(f"no archive partition to quarantine: {partition}")
        if _is_symlink(partition):
            raise RuntimeError(f"refusing to quarantine a symlink archive partition: {partition}")
        detected_at = self._aware_clock()
        parent = self._schema_root() / "quarantine" / f"recorded_date={plan.recorded_date.isoformat()}"
        _ensure_real_directory(parent, root=self.archive_root)
        stamp = detected_at.strftime("%Y%m%dT%H%M%S%fZ")
        dest = parent / stamp
        suffix = 0
        while _lstat(dest) is not None:
            suffix += 1
            dest = parent / f"{stamp}-{suffix}"
        os.replace(partition, dest)
        dest.chmod(0o700)
        _fsync_directory(dest)
        _fsync_directory(parent)
        record = WorldPredictionArchiveQuarantine(
            schema_version=PREDICTION_ARCHIVE_QUARANTINE_SCHEMA,
            state=PredictionArchiveState.QUARANTINED,
            recorded_date=plan.recorded_date,
            relative_path=str(
                Path(_DATASET)
                / f"schema={PREDICTION_ARCHIVE_SCHEMA}"
                / "quarantine"
                / f"recorded_date={plan.recorded_date.isoformat()}"
                / dest.name
            ),
            original_relative_path=str(self._partition_relative(plan)),
            reason=reason,
            detected_at=detected_at,
        )
        meta_path = dest / "quarantine.json"
        with meta_path.open("x", encoding="utf-8") as handle:
            json.dump(record.to_dict(), handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        meta_path.chmod(0o600)
        _fsync_regular_file(meta_path)
        _fsync_directory(dest)
        return record.to_dict()

    def _aware_clock(self) -> datetime:
        now = self.clock()
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("archive clock must return a timezone-aware UTC datetime")
        return now.astimezone(timezone.utc)

    def publish(
        self,
        plan: WorldPredictionArchivePlan,
        rows: Iterator[PredictionRow],
    ) -> WorldPredictionArchiveManifest:
        self._assert_usable_root()
        final = self._partition_dir(plan)
        if _is_symlink(final) or _is_symlink(final / _PARQUET_NAME) or _is_symlink(final / _MANIFEST_NAME):
            raise RuntimeError(f"refusing symlink archive target: {final}")
        try:
            existing = self._read_verified(plan)
        except InvalidWorldPredictionArchiveError as exc:
            self.quarantine(plan, reason=exc.reason)
            existing = None
        if existing is not None:
            return existing
        schema_root = self._schema_root()
        _ensure_real_directory(schema_root, root=self.archive_root)
        temporary_root = Path(tempfile.mkdtemp(prefix=f".{plan.partition_id}.", dir=schema_root))
        if _is_symlink(temporary_root):
            raise RuntimeError(f"refusing symlink archive staging directory: {temporary_root}")
        publish_dir = temporary_root / "partition"
        publish_dir.mkdir(mode=0o700)
        parquet_path = publish_dir / _PARQUET_NAME
        staging_db = temporary_root / "staging.duckdb"
        try:
            content_sha256, row_count = self._write_staging(plan, rows, staging_db, parquet_path)
            if row_count != plan.row_count:
                raise RuntimeError(
                    f"source changed during archive export: planned {plan.row_count} rows, streamed {row_count}"
                )
            if _is_symlink(parquet_path) or not _is_regular_nofollow(parquet_path):
                raise RuntimeError(f"archive parquet is missing, not regular, or is a symlink: {parquet_path}")
            parquet_path.chmod(0o600)
            _fsync_regular_file(parquet_path)
            _fsync_directory(publish_dir)
            parquet_sha256 = _file_sha256(parquet_path)
            manifest = WorldPredictionArchiveManifest(
                schema_version=PREDICTION_ARCHIVE_SCHEMA,
                state=PredictionArchiveState.VERIFIED,
                recorded_date=plan.recorded_date,
                relative_path=str(
                    Path(_DATASET)
                    / f"schema={PREDICTION_ARCHIVE_SCHEMA}"
                    / f"recorded_date={plan.recorded_date.isoformat()}"
                    / _PARQUET_NAME
                ),
                row_count=row_count,
                first_key=plan.first_key,
                last_key=plan.last_key,
                column_names=plan.column_names,
                column_types=plan.column_types,
                schema_sha256=plan.schema_sha256,
                content_sha256=content_sha256,
                parquet_sha256=parquet_sha256,
                parquet_bytes=os.lstat(parquet_path).st_size,
                compression="zstd",
                producer=f"casys-trader/duckdb-{duckdb.__version__}",
                verified_at=self.clock(),
            )
            self._verify_parquet(parquet_path, manifest)
            manifest_path = publish_dir / _MANIFEST_NAME
            with manifest_path.open("x", encoding="utf-8") as handle:
                json.dump(manifest.to_dict(), handle, ensure_ascii=False, sort_keys=True, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            manifest_path.chmod(0o600)
            _fsync_regular_file(manifest_path)
            _fsync_directory(publish_dir)
            if _is_symlink(final) or _is_symlink(final / _PARQUET_NAME) or _is_symlink(final / _MANIFEST_NAME):
                raise RuntimeError(f"refusing symlink archive target: {final}")
            if _lstat(final) is not None:
                try:
                    concurrent = self._read_verified(plan)
                except InvalidWorldPredictionArchiveError as exc:
                    self.quarantine(plan, reason=exc.reason)
                    concurrent = None
                if concurrent is not None:
                    return concurrent
            os.replace(publish_dir, final)
            published_parquet = final / _PARQUET_NAME
            published_manifest = final / _MANIFEST_NAME
            if _is_symlink(published_parquet) or not _is_regular_nofollow(published_parquet):
                raise RuntimeError(f"published parquet is missing, not regular, or is a symlink: {published_parquet}")
            if _is_symlink(published_manifest) or not _is_regular_nofollow(published_manifest):
                raise RuntimeError(f"published manifest is missing, not regular, or is a symlink: {published_manifest}")
            _fsync_regular_file(published_parquet)
            _fsync_regular_file(published_manifest)
            _fsync_directory(final)
            _fsync_directory(final.parent)
            return manifest
        finally:
            shutil.rmtree(temporary_root, ignore_errors=True)

    def _write_staging(
        self,
        plan: WorldPredictionArchivePlan,
        rows: Iterator[PredictionRow],
        staging_db: Path,
        parquet_path: Path,
    ) -> tuple[str, int]:
        types: list[str] = []
        for source_type in plan.column_types:
            rendered = source_type.upper()
            if rendered not in _SQLITE_TO_DUCKDB:
                raise RuntimeError(f"unsupported prediction archive column type: {source_type}")
            types.append(_SQLITE_TO_DUCKDB[rendered])
        definitions = ", ".join(
            f"{_quoted_identifier(name)} {column_type}"
            for name, column_type in zip(plan.column_names, types, strict=True)
        )
        placeholders = ", ".join("?" for _ in plan.column_names)
        insert_sql = f"INSERT INTO prediction_stage VALUES ({placeholders})"
        digest = hashlib.sha256()
        count = 0
        connection = duckdb.connect(str(staging_db))
        try:
            connection.execute(f"CREATE TABLE prediction_stage ({definitions})")
            batch: list[tuple[object, ...]] = []
            for raw in rows:
                row = tuple(raw)
                if len(row) != len(plan.column_names):
                    raise RuntimeError("prediction archive row does not match planned schema")
                digest.update(_row_bytes(row))
                count += 1
                batch.append(row)
                if len(batch) >= self.insert_batch_size:
                    connection.executemany(insert_sql, batch)
                    batch.clear()
            if batch:
                connection.executemany(insert_sql, batch)
            connection.execute(
                f"COPY (SELECT * FROM prediction_stage ORDER BY recorded_at, prediction_id) "
                f"TO {_sql_literal(parquet_path)} (FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 10000)"
            )
        finally:
            connection.close()
        return f"sha256:{digest.hexdigest()}", count

    def _verify_parquet(self, parquet_path: Path, manifest: WorldPredictionArchiveManifest) -> None:
        if _is_symlink(parquet_path):
            raise RuntimeError(f"archive parquet is a symlink: {parquet_path}")
        if not _is_regular_nofollow(parquet_path):
            raise RuntimeError(f"archive parquet missing: {parquet_path}")
        if _file_sha256(parquet_path) != manifest.parquet_sha256:
            raise RuntimeError("archive parquet file hash mismatch")
        connection = duckdb.connect(":memory:")
        try:
            relation = f"read_parquet({_sql_literal(parquet_path)}, hive_partitioning=false)"
            described = connection.execute(f"DESCRIBE SELECT * FROM {relation}").fetchall()
            columns = tuple(str(row[0]) for row in described)
            physical_types = tuple(str(row[1]).upper() for row in described)
            if columns != manifest.column_names:
                raise RuntimeError("archive parquet column order mismatch")
            expected_types: list[str] = []
            for source_type in manifest.column_types:
                rendered = source_type.upper()
                if rendered not in _SQLITE_TO_DUCKDB:
                    raise RuntimeError(f"unsupported prediction archive column type: {source_type}")
                expected_types.append(_SQLITE_TO_DUCKDB[rendered])
            if physical_types != tuple(expected_types):
                raise RuntimeError("archive parquet column type mismatch")
            stats = connection.execute(
                f"SELECT COUNT(*), COUNT(DISTINCT prediction_id), "
                f"min(recorded_at), min_by(prediction_id, (recorded_at, prediction_id)), "
                f"max(recorded_at), max_by(prediction_id, (recorded_at, prediction_id)) FROM {relation}"
            ).fetchone()
            if stats is None:
                raise RuntimeError("archive parquet stats unavailable")
            if int(stats[0]) != manifest.row_count or int(stats[1]) != manifest.row_count:
                raise RuntimeError("archive parquet row count or prediction identity mismatch")
            if (str(stats[2]), str(stats[3])) != manifest.first_key:
                raise RuntimeError("archive parquet first key mismatch")
            if (str(stats[4]), str(stats[5])) != manifest.last_key:
                raise RuntimeError("archive parquet last key mismatch")
            selected = ", ".join(_quoted_identifier(name) for name in manifest.column_names)
            cursor = connection.execute(f"SELECT {selected} FROM {relation} ORDER BY recorded_at, prediction_id")

            def rows() -> Iterator[tuple[object, ...]]:
                while True:
                    batch = cursor.fetchmany(1_000)
                    if not batch:
                        break
                    yield from (tuple(row) for row in batch)

            content_sha256, count = _content_sha256(rows())
            if count != manifest.row_count or content_sha256 != manifest.content_sha256:
                raise RuntimeError("archive parquet content hash mismatch")
        finally:
            connection.close()

    @staticmethod
    def _assert_manifest_matches_plan(
        manifest: WorldPredictionArchiveManifest,
        plan: WorldPredictionArchivePlan,
    ) -> None:
        if (
            manifest.recorded_date != plan.recorded_date
            or manifest.row_count != plan.row_count
            or manifest.first_key != plan.first_key
            or manifest.last_key != plan.last_key
            or manifest.column_names != plan.column_names
            or manifest.column_types != plan.column_types
            or manifest.schema_sha256 != plan.schema_sha256
        ):
            raise RuntimeError(f"published archive does not match current source plan: {plan.partition_id}")


__all__ = ["DuckDbWorldPredictionParquetStore"]
