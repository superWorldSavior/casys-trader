from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import replace
from datetime import date, datetime, timezone
from pathlib import Path

import duckdb
import pytest

from scripts.archive_world_predictions import main as archive_world_predictions_main
from trader.application.world_model.prediction_archive import WorldPredictionArchiveService
from trader.domain.world_prediction_archive import (
    PREDICTION_ARCHIVE_SCHEMA,
    InvalidWorldPredictionArchiveError,
    PredictionArchiveState,
    WorldPredictionArchiveManifest,
    WorldPredictionArchivePlan,
)
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
)

UTC = timezone.utc


def _db(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TABLE world_shadow_predictions (
                prediction_id TEXT PRIMARY KEY,
                episode_id TEXT NOT NULL,
                recorded_at TEXT NOT NULL,
                payload_json TEXT NOT NULL
            )
            """
        )
        connection.execute(
            "CREATE INDEX idx_world_predictions_recorded_at ON world_shadow_predictions(recorded_at, prediction_id)"
        )
        connection.executemany(
            "INSERT INTO world_shadow_predictions VALUES (?, ?, ?, ?)",
            [
                ("p-1", "e-1", "2026-08-24T01:00:00+00:00", json.dumps({"value": 1})),
                ("p-2", "e-1", "2026-08-24T02:00:00+00:00", json.dumps({"value": 2})),
                ("p-live", "e-2", "2026-08-28T01:00:00+00:00", json.dumps({"value": 3})),
            ],
        )


def test_export_is_verified_idempotent_and_keeps_sqlite_authority(tmp_path: Path) -> None:
    db_path = tmp_path / "world_model.db"
    _db(db_path)
    source = SQLiteWorldPredictionArchiveSource(db_path, batch_size=1)
    sink = DuckDbWorldPredictionParquetStore(
        tmp_path / "world_model_archive",
        clock=lambda: datetime(2026, 8, 28, 3, tzinfo=UTC),
        insert_batch_size=1,
    )
    service = WorldPredictionArchiveService(source=source, sink=sink)

    dry_run = service.execute(before=date(2026, 8, 25), apply=False)
    assert dry_run["partition_count"] == 1
    assert dry_run["partitions"][0]["status"] == "planned"
    assert not (tmp_path / "world_model_archive").exists()

    applied = service.execute(before=date(2026, 8, 25), apply=True)
    manifest = applied["partitions"][0]["manifest"]
    assert applied["partitions"][0]["status"] == "verified"
    assert manifest["schema_version"] == PREDICTION_ARCHIVE_SCHEMA
    assert manifest["source_retained"] is True
    parquet = tmp_path / "world_model_archive" / manifest["relative_path"]
    assert parquet.is_file()
    assert parquet.stat().st_mode & 0o777 == 0o600
    assert duckdb.connect(":memory:").execute("SELECT COUNT(*) FROM read_parquet(?)", [str(parquet)]).fetchone()[0] == 2
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM world_shadow_predictions").fetchone()[0] == 3

    repeated = service.execute(before=date(2026, 8, 25), apply=True)
    assert repeated["partitions"][0]["status"] == "already_verified"


def test_tampered_parquet_is_rejected(tmp_path: Path) -> None:
    db_path = tmp_path / "world_model.db"
    _db(db_path)
    source = SQLiteWorldPredictionArchiveSource(db_path)
    plan = source.plan_before(date(2026, 8, 25))[0]
    sink = DuckDbWorldPredictionParquetStore(tmp_path / "archive")
    manifest = sink.publish(plan, source.iter_partition(plan))
    parquet = tmp_path / "archive" / manifest.relative_path
    parquet.write_bytes(parquet.read_bytes() + b"tampered")

    with pytest.raises(InvalidWorldPredictionArchiveError, match="hash mismatch"):
        sink.published(plan)


def test_manifest_refuses_claim_that_sqlite_source_was_removed(tmp_path: Path) -> None:
    db_path = tmp_path / "world_model.db"
    _db(db_path)
    source = SQLiteWorldPredictionArchiveSource(db_path)
    plan = source.plan_before(date(2026, 8, 25))[0]

    with pytest.raises(ValueError, match="retain"):
        WorldPredictionArchiveManifest(
            schema_version=PREDICTION_ARCHIVE_SCHEMA,
            state=PredictionArchiveState.VERIFIED,
            recorded_date=plan.recorded_date,
            relative_path="partition/part.parquet",
            row_count=plan.row_count,
            first_key=plan.first_key,
            last_key=plan.last_key,
            column_names=plan.column_names,
            column_types=plan.column_types,
            schema_sha256=plan.schema_sha256,
            content_sha256="sha256:" + "1" * 64,
            parquet_sha256="sha256:" + "2" * 64,
            parquet_bytes=1,
            compression="zstd",
            producer="test",
            verified_at=datetime(2026, 8, 28, tzinfo=UTC),
            source_retained=False,
        )


def _closed_day_clock() -> datetime:
    return datetime(2026, 8, 28, 12, tzinfo=UTC)


class _RefusingSink:
    def published(self, plan: WorldPredictionArchivePlan) -> None:
        return None

    def publish(self, plan: WorldPredictionArchivePlan, rows) -> WorldPredictionArchiveManifest:
        raise AssertionError(f"must not publish open or future day {plan.recorded_date}")


def test_service_rejects_cutoff_that_includes_open_or_future_utc_days(tmp_path: Path) -> None:
    db_path = tmp_path / "world_model.db"
    _db(db_path)
    service = WorldPredictionArchiveService(
        source=SQLiteWorldPredictionArchiveSource(db_path),
        sink=_RefusingSink(),
        clock=_closed_day_clock,
    )

    with pytest.raises(ValueError, match="open UTC day"):
        service.execute(before=date(2026, 8, 29), apply=False)
    with pytest.raises(ValueError, match="open UTC day"):
        service.execute(before=date(2099, 1, 1), apply=True)

    planned = service.execute(before=date(2026, 8, 28), apply=False)
    assert planned["partition_count"] == 1
    assert planned["partitions"][0]["plan"]["recorded_date"] == "2026-08-24"


def test_service_refuses_source_plan_for_the_open_utc_day() -> None:
    open_day = date(2026, 8, 28)
    plan = WorldPredictionArchivePlan(
        recorded_date=open_day,
        row_count=1,
        first_key=("2026-08-28T01:00:00+00:00", "p-live"),
        last_key=("2026-08-28T01:00:00+00:00", "p-live"),
        column_names=("prediction_id",),
        column_types=("TEXT",),
        schema_sha256="sha256:" + "a" * 64,
    )

    class OpenDaySource:
        def plan_before(self, before: date):
            assert before == open_day
            return (plan,)

        def iter_partition(self, requested):
            raise AssertionError("must not stream the open UTC day")

    service = WorldPredictionArchiveService(
        source=OpenDaySource(),
        sink=_RefusingSink(),
        clock=_closed_day_clock,
    )
    with pytest.raises(ValueError, match="open UTC day"):
        service.execute(before=open_day, apply=True)


def test_cli_before_future_fails_cleanly(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _db(tmp_path / "world_model.db")
    code = archive_world_predictions_main(
        ["--state-dir", str(tmp_path), "--before", "2026-08-29"],
        clock=_closed_day_clock,
    )
    report = json.loads(capsys.readouterr().out)

    assert code == 1
    assert report["ok"] is False
    assert report["source_retained"] is True
    assert report["authority"] == "shadow_only"
    assert report["decision_effect"] == "none"
    assert "open UTC day" in report["error"]
    assert not (tmp_path / "world_model_archive").exists()


def test_archive_queries_use_bounded_recorded_at_index(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "world_model.db"
    _db(db_path)
    executed: list[str] = []
    real_connect = sqlite3.connect

    def tracing_connect(*args, **kwargs):
        connection = real_connect(*args, **kwargs)
        connection.set_trace_callback(lambda sql: executed.append(" ".join(str(sql).split())))
        return connection

    monkeypatch.setattr(
        "trader.infrastructure.state_db.world_prediction_archive_source.sqlite3.connect",
        tracing_connect,
    )
    source = SQLiteWorldPredictionArchiveSource(db_path, batch_size=1)
    plans = source.plan_before(date(2026, 8, 25))
    rows = list(source.iter_partition(plans[0]))

    assert len(plans) == 1
    assert len(rows) == 2
    table_sql = [sql for sql in executed if "world_shadow_predictions" in sql.lower()]
    assert table_sql
    assert all("substr(" not in sql.lower() for sql in table_sql)
    assert any("recorded_at <" in sql and "prediction_id" in sql.lower() for sql in table_sql)
    assert any("recorded_at >=" in sql and "recorded_at <" in sql for sql in table_sql)

    with sqlite3.connect(db_path) as connection:
        stream_plan = " ".join(
            str(row[-1])
            for row in connection.execute(
                "EXPLAIN QUERY PLAN "
                "SELECT recorded_at, prediction_id FROM world_shadow_predictions "
                "WHERE recorded_at < ? ORDER BY recorded_at, prediction_id",
                ("2026-08-25",),
            )
        )
        range_plan = " ".join(
            str(row[-1])
            for row in connection.execute(
                "EXPLAIN QUERY PLAN "
                "SELECT prediction_id, episode_id, recorded_at, payload_json "
                "FROM world_shadow_predictions "
                "WHERE recorded_at >= ? AND recorded_at < ? "
                "ORDER BY recorded_at, prediction_id",
                ("2026-08-24", "2026-08-25"),
            )
        )
    assert "SEARCH" in stream_plan
    assert "COVERING INDEX idx_world_predictions_recorded_at" in stream_plan
    assert "recorded_at<?" in stream_plan
    assert "SEARCH" in range_plan
    assert "idx_world_predictions_recorded_at" in range_plan
    assert "recorded_at>?" in range_plan and "recorded_at<?" in range_plan


def test_parquet_verification_rejects_physical_column_type_mismatch(tmp_path: Path) -> None:
    db_path = tmp_path / "world_model.db"
    _db(db_path)
    source = SQLiteWorldPredictionArchiveSource(db_path)
    plan = source.plan_before(date(2026, 8, 25))[0]
    sink = DuckDbWorldPredictionParquetStore(
        tmp_path / "archive",
        clock=lambda: datetime(2026, 8, 28, 3, tzinfo=UTC),
    )
    manifest = sink.publish(plan, source.iter_partition(plan))
    parquet = tmp_path / "archive" / manifest.relative_path
    duckdb.connect(":memory:").execute(
        "COPY (SELECT prediction_id, episode_id, recorded_at, "
        "length(payload_json) AS payload_json "
        f"FROM read_parquet('{parquet}', hive_partitioning=false)) "
        f"TO '{parquet}' (FORMAT PARQUET, COMPRESSION ZSTD)"
    )
    digest = hashlib.sha256(parquet.read_bytes()).hexdigest()
    tampered = replace(manifest, parquet_sha256=f"sha256:{digest}")

    with pytest.raises(RuntimeError, match="column type"):
        sink._verify_parquet(parquet, tampered)


def _two_closed_days(path: Path) -> None:
    _db(path)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO world_shadow_predictions VALUES (?, ?, ?, ?)",
            ("p-3", "e-3", "2026-08-25T04:00:00+00:00", json.dumps({"value": 4})),
        )


def _service(tmp_path: Path) -> WorldPredictionArchiveService:
    db_path = tmp_path / "world_model.db"
    _two_closed_days(db_path)
    return WorldPredictionArchiveService(
        source=SQLiteWorldPredictionArchiveSource(db_path),
        sink=DuckDbWorldPredictionParquetStore(
            tmp_path / "world_model_archive",
            clock=lambda: datetime(2026, 8, 28, 3, tzinfo=UTC),
        ),
        clock=_closed_day_clock,
    )


def test_damaged_parquet_is_quarantined_and_later_dates_still_export(tmp_path: Path) -> None:
    service = _service(tmp_path)
    first = service.execute(before=date(2026, 8, 26), apply=True)
    assert [item["status"] for item in first["partitions"]] == ["verified", "verified"]
    day_24 = first["partitions"][0]["manifest"]
    parquet = tmp_path / "world_model_archive" / day_24["relative_path"]
    original = parquet.read_bytes()
    parquet.write_bytes(original[:20])

    dry_run = service.execute(before=date(2026, 8, 26), apply=False)
    assert dry_run["partitions"][0]["status"] == "invalid"
    assert "hash mismatch" in dry_run["partitions"][0]["reason"]
    assert dry_run["partitions"][1]["status"] == "already_verified"
    assert dry_run["source_retained"] is True
    assert dry_run["decision_effect"] == "none"
    assert parquet.is_file()
    assert parquet.read_bytes() == original[:20]

    rebuilt = service.execute(before=date(2026, 8, 26), apply=True)
    assert rebuilt["partitions"][0]["status"] == "rebuilt"
    assert rebuilt["partitions"][1]["status"] == "already_verified"
    assert rebuilt["authority"] == "shadow_only"
    assert rebuilt["decision_effect"] == "none"
    assert rebuilt["source_retained"] is True
    quarantine = rebuilt["partitions"][0]["quarantine"]
    quarantined = tmp_path / "world_model_archive" / quarantine["relative_path"]
    assert quarantine["reason"]
    assert quarantine["source_retained"] is True
    assert quarantine["authority"] == "shadow_only"
    assert quarantine["decision_effect"] == "none"
    assert (quarantined / "part-00000.parquet").read_bytes() == original[:20]
    meta = json.loads((quarantined / "quarantine.json").read_text(encoding="utf-8"))
    assert meta["source_retained"] is True
    assert meta["decision_effect"] == "none"
    assert "hash mismatch" in meta["reason"]
    restored = tmp_path / "world_model_archive" / rebuilt["partitions"][0]["manifest"]["relative_path"]
    assert restored.is_file()
    assert (
        duckdb.connect(":memory:").execute("SELECT COUNT(*) FROM read_parquet(?)", [str(restored)]).fetchone()[0] == 2
    )
    assert parquet.resolve() == restored.resolve()
    assert parquet.read_bytes() != original[:20]


def test_missing_manifest_is_quarantined_without_blocking_later_days(tmp_path: Path) -> None:
    service = _service(tmp_path)
    first = service.execute(before=date(2026, 8, 26), apply=True)
    partition = (tmp_path / "world_model_archive" / first["partitions"][0]["manifest"]["relative_path"]).parent
    manifest_path = partition / "manifest.json"
    preserved = (partition / "part-00000.parquet").read_bytes()
    manifest_path.unlink()

    rebuilt = service.execute(before=date(2026, 8, 26), apply=True)
    assert rebuilt["partitions"][0]["status"] == "rebuilt"
    assert rebuilt["partitions"][1]["status"] == "already_verified"
    quarantined = tmp_path / "world_model_archive" / rebuilt["partitions"][0]["quarantine"]["relative_path"]
    assert not (quarantined / "manifest.json").exists()
    assert (quarantined / "part-00000.parquet").read_bytes() == preserved
    assert "manifest" in rebuilt["partitions"][0]["quarantine"]["reason"]


def test_symlink_parquet_is_not_treated_as_verified(tmp_path: Path) -> None:
    db_path = tmp_path / "world_model.db"
    _db(db_path)
    source = SQLiteWorldPredictionArchiveSource(db_path)
    plan = source.plan_before(date(2026, 8, 25))[0]
    sink = DuckDbWorldPredictionParquetStore(
        tmp_path / "archive",
        clock=lambda: datetime(2026, 8, 28, 3, tzinfo=UTC),
    )
    manifest = sink.publish(plan, source.iter_partition(plan))
    parquet = tmp_path / "archive" / manifest.relative_path
    backup = parquet.with_name("copy.parquet")
    backup.write_bytes(parquet.read_bytes())
    parquet.unlink()
    parquet.symlink_to(backup)

    with pytest.raises(InvalidWorldPredictionArchiveError, match="symlink|regular"):
        sink.published(plan)
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM world_shadow_predictions").fetchone()[0] == 3


def test_archive_root_symlink_is_rejected_on_publish(tmp_path: Path) -> None:
    db_path = tmp_path / "world_model.db"
    _db(db_path)
    source = SQLiteWorldPredictionArchiveSource(db_path)
    plan = source.plan_before(date(2026, 8, 25))[0]
    real_root = tmp_path / "real-archive"
    real_root.mkdir()
    linked = tmp_path / "linked-archive"
    linked.symlink_to(real_root)
    sink = DuckDbWorldPredictionParquetStore(
        linked,
        clock=lambda: datetime(2026, 8, 28, 3, tzinfo=UTC),
    )

    with pytest.raises((RuntimeError, InvalidWorldPredictionArchiveError), match="symlink"):
        sink.publish(plan, source.iter_partition(plan))
    assert list(real_root.rglob("*.parquet")) == []
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM world_shadow_predictions").fetchone()[0] == 3


def test_publish_refuses_symlinked_partition_directory(tmp_path: Path) -> None:
    db_path = tmp_path / "world_model.db"
    _db(db_path)
    source = SQLiteWorldPredictionArchiveSource(db_path)
    plan = source.plan_before(date(2026, 8, 25))[0]
    sink = DuckDbWorldPredictionParquetStore(
        tmp_path / "archive",
        clock=lambda: datetime(2026, 8, 28, 3, tzinfo=UTC),
    )
    partition = sink._partition_dir(plan)
    partition.parent.mkdir(parents=True, mode=0o700)
    outside = tmp_path / "outside-partition"
    outside.mkdir()
    partition.symlink_to(outside)

    with pytest.raises((RuntimeError, InvalidWorldPredictionArchiveError), match="symlink"):
        sink.publish(plan, source.iter_partition(plan))
    assert list(outside.rglob("*")) == []


def test_publish_fsyncs_parquet_and_parent_directory_before_verified(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from trader.infrastructure.state_db import world_prediction_parquet_store as parquet_store

    db_path = tmp_path / "world_model.db"
    _db(db_path)
    source = SQLiteWorldPredictionArchiveSource(db_path)
    plan = source.plan_before(date(2026, 8, 25))[0]
    sink = DuckDbWorldPredictionParquetStore(
        tmp_path / "archive",
        clock=lambda: datetime(2026, 8, 28, 3, tzinfo=UTC),
    )
    fsynced_files: list[Path] = []
    fsynced_dirs: list[Path] = []
    real_file = parquet_store._fsync_regular_file
    real_dir = parquet_store._fsync_directory

    def capture_file(path: Path) -> None:
        fsynced_files.append(Path(path))
        real_file(path)

    def capture_dir(path: Path) -> None:
        fsynced_dirs.append(Path(path))
        real_dir(path)

    monkeypatch.setattr(parquet_store, "_fsync_regular_file", capture_file)
    monkeypatch.setattr(parquet_store, "_fsync_directory", capture_dir)

    manifest = sink.publish(plan, source.iter_partition(plan))
    parquet = tmp_path / "archive" / manifest.relative_path
    partition = parquet.parent

    assert parquet in fsynced_files
    assert partition in fsynced_dirs
    assert parquet.is_file() and not parquet.is_symlink()
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM world_shadow_predictions").fetchone()[0] == 3


_COLD_PARTITIONS_DDL = """
CREATE TABLE IF NOT EXISTS world_prediction_cold_partitions (
    partition_id TEXT PRIMARY KEY,
    manifest_json TEXT NOT NULL,
    manifest_sha256 TEXT NOT NULL,
    cutover_id TEXT,
    activated_at TEXT,
    recorded_date TEXT NOT NULL,
    relative_path TEXT NOT NULL,
    row_count INTEGER NOT NULL
)
"""


def _activate_canonical_catalog(
    db_path: Path,
    manifest: dict[str, object],
    *,
    cutover_id: str = "cutover-test",
    manifest_json: str | None = None,
    manifest_sha256: str | None = None,
    relative_path: str | None = None,
) -> None:
    payload = manifest_json if manifest_json is not None else json.dumps(manifest, ensure_ascii=False, sort_keys=True)
    digest = manifest_sha256 or ("sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest())
    with sqlite3.connect(db_path) as connection:
        connection.execute(_COLD_PARTITIONS_DDL)
        connection.execute(
            "CREATE TABLE IF NOT EXISTS world_prediction_cold_index ("
            "prediction_id TEXT PRIMARY KEY, partition_id TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO world_prediction_cold_partitions("
            "partition_id, manifest_json, manifest_sha256, cutover_id, activated_at, "
            "recorded_date, relative_path, row_count) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                str(manifest["relative_path"]),
                payload,
                digest,
                cutover_id,
                "2026-08-28T03:00:00+00:00",
                str(manifest["recorded_date"]),
                str(relative_path if relative_path is not None else manifest["relative_path"]),
                int(manifest["row_count"]),  # type: ignore[arg-type]
            ),
        )
        connection.execute(
            "INSERT OR REPLACE INTO world_prediction_cold_index(prediction_id, partition_id) "
            "SELECT prediction_id, ? FROM world_shadow_predictions WHERE substr(recorded_at, 1, 10) = ?",
            (str(manifest["relative_path"]), str(manifest["recorded_date"])),
        )


def test_plan_before_and_catalog_do_not_create_cold_schema(tmp_path: Path) -> None:
    db_path = tmp_path / "world_model.db"
    _db(db_path)
    source = SQLiteWorldPredictionArchiveSource(db_path)

    plans = source.plan_before(date(2026, 8, 25))
    catalog = source.canonical_catalog()

    assert len(plans) == 1
    assert catalog == ()
    with sqlite3.connect(db_path) as connection:
        tables = {str(row[0]) for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "world_prediction_cold_partitions" not in tables
    assert "world_prediction_cold_index" not in tables


def test_exporter_skips_canonical_active_date_and_leaves_late_hot_row(tmp_path: Path) -> None:
    db_path = tmp_path / "world_model.db"
    _two_closed_days(db_path)
    archive_root = tmp_path / "world_model_archive"
    source = SQLiteWorldPredictionArchiveSource(db_path)
    sink = DuckDbWorldPredictionParquetStore(
        archive_root,
        clock=lambda: datetime(2026, 8, 28, 3, tzinfo=UTC),
    )
    service = WorldPredictionArchiveService(source=source, sink=sink, clock=_closed_day_clock)
    first = service.execute(before=date(2026, 8, 26), apply=True)
    day_24 = first["partitions"][0]["manifest"]
    parquet = archive_root / day_24["relative_path"]
    original = parquet.read_bytes()
    _activate_canonical_catalog(db_path, day_24)

    with sqlite3.connect(db_path) as connection:
        connection.execute("DELETE FROM world_shadow_predictions WHERE prediction_id IN ('p-1', 'p-2')")
        connection.execute(
            "INSERT INTO world_shadow_predictions VALUES (?, ?, ?, ?)",
            ("p-late", "e-late", "2026-08-24T09:00:00+00:00", json.dumps({"value": 99})),
        )

    applied = service.execute(before=date(2026, 8, 26), apply=True)
    statuses = [item["status"] for item in applied["partitions"]]
    canonical = next(item for item in applied["partitions"] if item["status"] == "canonical_active")

    assert "rebuilt" not in statuses
    assert canonical["late_hot_count"] == 1
    assert canonical["source_retained"] is True
    assert parquet.read_bytes() == original
    assert not list(archive_root.rglob("quarantine/**"))
    with sqlite3.connect(db_path) as connection:
        ids = {
            str(row[0])
            for row in connection.execute("SELECT prediction_id FROM world_shadow_predictions ORDER BY prediction_id")
        }
    assert "p-late" in ids
    assert "p-3" in ids


def test_exporter_refuses_to_quarantine_or_rebuild_damaged_canonical_partition(tmp_path: Path) -> None:
    db_path = tmp_path / "world_model.db"
    _db(db_path)
    archive_root = tmp_path / "world_model_archive"
    source = SQLiteWorldPredictionArchiveSource(db_path)
    sink = DuckDbWorldPredictionParquetStore(
        archive_root,
        clock=lambda: datetime(2026, 8, 28, 3, tzinfo=UTC),
    )
    service = WorldPredictionArchiveService(source=source, sink=sink, clock=_closed_day_clock)
    first = service.execute(before=date(2026, 8, 25), apply=True)
    manifest = first["partitions"][0]["manifest"]
    parquet = archive_root / manifest["relative_path"]
    original = parquet.read_bytes()
    parquet.write_bytes(original[:20])
    _activate_canonical_catalog(db_path, manifest)

    with pytest.raises(InvalidWorldPredictionArchiveError, match="canonical|unavailable"):
        service.execute(before=date(2026, 8, 25), apply=True)
    with pytest.raises(InvalidWorldPredictionArchiveError, match="canonical|unavailable"):
        service.execute(before=date(2026, 8, 25), apply=False)

    assert parquet.read_bytes() == original[:20]
    assert list(archive_root.rglob("quarantine.json")) == []
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM world_shadow_predictions").fetchone()[0] == 3


def test_exporter_still_publishes_unaffected_days_when_another_date_is_canonical(tmp_path: Path) -> None:
    db_path = tmp_path / "world_model.db"
    _two_closed_days(db_path)
    archive_root = tmp_path / "world_model_archive"
    source = SQLiteWorldPredictionArchiveSource(db_path)
    sink = DuckDbWorldPredictionParquetStore(
        archive_root,
        clock=lambda: datetime(2026, 8, 28, 3, tzinfo=UTC),
    )
    service = WorldPredictionArchiveService(source=source, sink=sink, clock=_closed_day_clock)
    first = service.execute(before=date(2026, 8, 25), apply=True)
    day_24 = first["partitions"][0]["manifest"]
    _activate_canonical_catalog(db_path, day_24)

    applied = service.execute(before=date(2026, 8, 26), apply=True)
    statuses = {item["status"] for item in applied["partitions"]}

    assert "canonical_active" in statuses
    assert "verified" in statuses
    assert "rebuilt" not in statuses
    day_25 = next(item for item in applied["partitions"] if item["status"] == "verified")
    assert day_25["manifest"]["recorded_date"] == "2026-08-25"
    assert (archive_root / day_24["relative_path"]).is_file()


def test_canonical_catalog_accepts_compact_and_spaced_exact_stored_hashes(tmp_path: Path) -> None:
    db_path = tmp_path / "world_model.db"
    _db(db_path)
    source = SQLiteWorldPredictionArchiveSource(db_path)
    sink = DuckDbWorldPredictionParquetStore(
        tmp_path / "world_model_archive",
        clock=lambda: datetime(2026, 8, 28, 3, tzinfo=UTC),
    )
    plan = source.plan_before(date(2026, 8, 25))[0]
    manifest = sink.publish(plan, source.iter_partition(plan)).to_dict()
    compact = json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    _activate_canonical_catalog(db_path, manifest, manifest_json=compact)
    assert source.canonical_catalog()[0].to_dict() == manifest

    spaced = json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    with sqlite3.connect(db_path) as connection:
        connection.execute("DELETE FROM world_prediction_cold_partitions")
    _activate_canonical_catalog(db_path, manifest, manifest_json=spaced)
    assert source.canonical_catalog()[0].row_count == 2
    assert source.canonical_catalog()[0].source_retained is True


def test_canonical_catalog_fails_closed_on_hash_or_path_mismatch(tmp_path: Path) -> None:
    db_path = tmp_path / "world_model.db"
    _db(db_path)
    source = SQLiteWorldPredictionArchiveSource(db_path)
    sink = DuckDbWorldPredictionParquetStore(
        tmp_path / "world_model_archive",
        clock=lambda: datetime(2026, 8, 28, 3, tzinfo=UTC),
    )
    plan = source.plan_before(date(2026, 8, 25))[0]
    manifest = sink.publish(plan, source.iter_partition(plan)).to_dict()
    _activate_canonical_catalog(
        db_path,
        manifest,
        manifest_sha256="sha256:" + ("0" * 64),
    )
    with pytest.raises(InvalidWorldPredictionArchiveError, match="hash mismatch"):
        source.canonical_catalog()

    with sqlite3.connect(db_path) as connection:
        connection.execute("DELETE FROM world_prediction_cold_partitions")
    _activate_canonical_catalog(db_path, manifest, relative_path="not-the-registered-path.parquet")
    with pytest.raises(InvalidWorldPredictionArchiveError, match="identity mismatch"):
        source.canonical_catalog()


def test_exporter_rejects_substituted_canonical_partition_matching_plan_fields(tmp_path: Path) -> None:
    db_path = tmp_path / "world_model.db"
    _db(db_path)
    archive_root = tmp_path / "world_model_archive"
    source = SQLiteWorldPredictionArchiveSource(db_path)
    sink = DuckDbWorldPredictionParquetStore(
        archive_root,
        clock=lambda: datetime(2026, 8, 28, 3, tzinfo=UTC),
    )
    service = WorldPredictionArchiveService(source=source, sink=sink, clock=_closed_day_clock)
    first = service.execute(before=date(2026, 8, 25), apply=True)
    manifest = first["partitions"][0]["manifest"]
    parquet = archive_root / manifest["relative_path"]
    original = parquet.read_bytes()
    _activate_canonical_catalog(db_path, manifest)

    alt_db = tmp_path / "alt.db"
    _db(alt_db)
    with sqlite3.connect(alt_db) as connection:
        connection.execute(
            "UPDATE world_shadow_predictions SET payload_json=? WHERE prediction_id='p-1'",
            (json.dumps({"value": 1, "tamper": True}),),
        )
    alt_root = tmp_path / "alt-archive"
    alt_source = SQLiteWorldPredictionArchiveSource(alt_db)
    alt_sink = DuckDbWorldPredictionParquetStore(
        alt_root,
        clock=lambda: datetime(2026, 8, 28, 3, tzinfo=UTC),
    )
    alt_plan = alt_source.plan_before(date(2026, 8, 25))[0]
    assert alt_plan.row_count == 2
    assert tuple(alt_plan.first_key) == tuple(manifest["first_key"])
    assert tuple(alt_plan.last_key) == tuple(manifest["last_key"])
    assert alt_plan.schema_sha256 == manifest["schema_sha256"]
    alt_manifest = alt_sink.publish(alt_plan, alt_source.iter_partition(alt_plan))
    alt_part = (alt_root / alt_manifest.relative_path).parent
    parquet.write_bytes((alt_part / "part-00000.parquet").read_bytes())
    (parquet.parent / "manifest.json").write_text(
        (alt_part / "manifest.json").read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    with pytest.raises(InvalidWorldPredictionArchiveError, match="manifest.*does not match"):
        service.execute(before=date(2026, 8, 25), apply=False)
    with pytest.raises(InvalidWorldPredictionArchiveError, match="manifest.*does not match"):
        service.execute(before=date(2026, 8, 25), apply=True)
    assert list(archive_root.rglob("quarantine.json")) == []
    assert parquet.read_bytes() != original
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM world_shadow_predictions").fetchone()[0] == 3


def test_exporter_refuses_busy_lease_and_any_cutover_journal(tmp_path: Path) -> None:
    db_path = tmp_path / "world_model.db"
    _db(db_path)
    service = WorldPredictionArchiveService(
        source=SQLiteWorldPredictionArchiveSource(db_path),
        sink=DuckDbWorldPredictionParquetStore(
            tmp_path / "world_model_archive",
            clock=lambda: datetime(2026, 8, 28, 3, tzinfo=UTC),
        ),
        clock=_closed_day_clock,
    )
    with exclusive_prediction_storage_lease(db_path):
        with pytest.raises(PredictionStorageBusyError):
            service.execute(before=date(2026, 8, 25), apply=False)

    journal = Path(cutover_journal_path(db_path))
    journal.write_text(
        json.dumps({"schema_version": "world_prediction_cutover_journal.v1", "stage": "success"}),
        encoding="utf-8",
    )
    with pytest.raises(PredictionStorageBusyError, match="cutover journal"):
        service.execute(before=date(2026, 8, 25), apply=True)
    assert not (tmp_path / "world_model_archive").exists()


def test_apply_export_holds_a_real_lease_before_reading_legacy_catalog(tmp_path: Path) -> None:
    db_path = tmp_path / "world_model.db"
    _db(db_path)
    lock_path = Path(str(db_path) + ".storage.lock")

    class CheckedSource(SQLiteWorldPredictionArchiveSource):
        def canonical_catalog(self):
            assert lock_path.is_file()
            with pytest.raises(PredictionStorageBusyError):
                with exclusive_prediction_storage_lease(self.db_path):
                    pass
            return super().canonical_catalog()

    source = SQLiteWorldPredictionArchiveSource(db_path)
    sink = DuckDbWorldPredictionParquetStore(tmp_path / "world_model_archive", clock=_closed_day_clock)
    WorldPredictionArchiveService(source=source, sink=sink, clock=_closed_day_clock).execute(
        before=date(2026, 8, 25), apply=False
    )
    assert not lock_path.exists()
    report = WorldPredictionArchiveService(
        source=CheckedSource(db_path), sink=sink, clock=_closed_day_clock
    ).execute(before=date(2026, 8, 25), apply=True)
    assert report["partitions"][0]["status"] == "verified"


@pytest.mark.parametrize("half", ["partitions", "index"])
def test_exporter_refuses_partial_cold_catalog(tmp_path: Path, half: str) -> None:
    db_path = tmp_path / "world_model.db"
    _db(db_path)
    connection = sqlite3.connect(db_path)
    try:
        if half == "partitions":
            connection.execute(_COLD_PARTITIONS_DDL)
        else:
            connection.execute(
                "CREATE TABLE world_prediction_cold_index (prediction_id TEXT PRIMARY KEY, partition_id TEXT)"
            )
        connection.commit()
    finally:
        connection.close()
    archive_root = tmp_path / "world_model_archive"
    service = WorldPredictionArchiveService(
        source=SQLiteWorldPredictionArchiveSource(db_path),
        sink=DuckDbWorldPredictionParquetStore(archive_root, clock=_closed_day_clock),
        clock=_closed_day_clock,
    )
    for apply in (False, True):
        with pytest.raises(InvalidWorldPredictionArchiveError, match="incomplete"):
            service.execute(before=date(2026, 8, 25), apply=apply)
    assert not archive_root.exists()
