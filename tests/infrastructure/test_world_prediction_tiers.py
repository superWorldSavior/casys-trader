"""Foundation contract for the World Model SQLite + Parquet hot/cold ledger."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from dataclasses import replace
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import patch

import duckdb
import pytest

from tests.infrastructure.test_world_model_store import DEFAULT_EPISODE_ID, _episode, _observation, _prediction
from trader.domain.world_episode import AnchorBar
from trader.infrastructure.state_db.connection import StateDb
from trader.infrastructure.state_db.sqlite_in import sqlite_in_chunks, sqlite_placeholders
from trader.infrastructure.state_db.world_graph_store import WorldGraphStore
from trader.infrastructure.state_db.world_model_store import (
    WORLD_MODEL_MIGRATIONS,
    WorldModelConflictError,
    WorldModelStore,
    _open_dedicated_state_db,
)
from trader.infrastructure.state_db.world_prediction_archive_source import SQLiteWorldPredictionArchiveSource
from trader.infrastructure.state_db.world_prediction_parquet_store import DuckDbWorldPredictionParquetStore
from trader.infrastructure.state_db.world_prediction_storage_lock import (
    PredictionStorageBusyError,
    cutover_journal_path,
    exclusive_prediction_storage_lease,
    prediction_storage_lease,
    shared_prediction_storage_lease,
    storage_lock_path,
)
from trader.infrastructure.state_db.world_prediction_tiers import (
    PredictionReadUnavailable,
    cold_catalog_present,
    cold_prediction_for_replay,
    ensure_cold_schema,
    prediction_count,
    read_prediction_identities,
    read_prediction_rows,
    register_cold_partition,
    validate_registered_cold_storage,
)

UTC = timezone.utc
_CUTOVER_ID = "cutover-test-1"
_ACTIVATED_AT = "2026-09-07T00:00:00+00:00"
_DELETE_TRIGGER_SQL = """
CREATE TRIGGER IF NOT EXISTS world_predictions_no_delete
            BEFORE DELETE ON world_shadow_predictions
            BEGIN
                SELECT RAISE(ABORT, 'world_shadow_predictions are append-only');
            END
"""
_SCALAR_FIELDS = (
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
_READ_COLUMNS = (
    *_SCALAR_FIELDS,
    "payload_json",
    "prediction_json",
    "episode_observed_at",
)


def _connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(str(path), isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def _table_names(connection: sqlite3.Connection) -> set[str]:
    return {str(row[0]) for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _insert_prediction(
    connection: sqlite3.Connection,
    *,
    prediction_id: str,
    episode_id: str = DEFAULT_EPISODE_ID,
    run_id: str = "run-a",
    horizon_code: str = "elapsed_4h.v1",
    model_kind: str | None = "markov",
    model_version: str | None = "markov-v1",
    recorded_at: str = "2026-08-24T01:00:00+00:00",
    study_cohort_id: str | None = "cohort-a",
    payload: dict[str, object] | None = None,
) -> dict[str, object]:
    nested = {"p_up": 0.6, "study_cohort_id": "json-should-lose"}
    record = payload if payload is not None else {"prediction_id": prediction_id, "study_cohort_id": "json-should-lose"}
    payload_json = json.dumps(record, separators=(",", ":"), sort_keys=True)
    prediction_json = json.dumps(nested, separators=(",", ":"), sort_keys=True)
    values = {
        "prediction_id": prediction_id,
        "run_id": run_id,
        "episode_id": episode_id,
        "horizon_code": horizon_code,
        "model_kind": model_kind,
        "model_version": model_version,
        "predicted_at": "2026-08-24T00:00:00+00:00",
        "input_sha256": "sha256:" + "11" * 32,
        "prediction_json": prediction_json,
        "prediction_sha256": "sha256:" + "22" * 32,
        "payload_json": payload_json,
        "payload_sha256": "sha256:" + "33" * 32,
        "recorded_at": recorded_at,
        "study_cohort_id": study_cohort_id,
        "lane_id": "lane-a",
        "manifest_sha256": "sha256:" + "44" * 32,
        "feature_contract_fingerprint": "sha256:" + "55" * 32,
        "feature_mask_fingerprint": "sha256:" + "66" * 32,
    }
    connection.execute(
        """
        INSERT INTO world_shadow_predictions(
            prediction_id, run_id, episode_id, horizon_code, model_kind, model_version,
            predicted_at, input_sha256, prediction_json, prediction_sha256, payload_json,
            payload_sha256, recorded_at, study_cohort_id, lane_id, manifest_sha256,
            feature_contract_fingerprint, feature_mask_fingerprint
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        tuple(values[name] for name in (
            "prediction_id",
            "run_id",
            "episode_id",
            "horizon_code",
            "model_kind",
            "model_version",
            "predicted_at",
            "input_sha256",
            "prediction_json",
            "prediction_sha256",
            "payload_json",
            "payload_sha256",
            "recorded_at",
            "study_cohort_id",
            "lane_id",
            "manifest_sha256",
            "feature_contract_fingerprint",
            "feature_mask_fingerprint",
        )),
    )
    return values


def _seed_store(path: Path) -> WorldModelStore:
    store = WorldModelStore(path)
    assert store.append_episode(_episode()) is True
    return store


def _export_partitions(db_path: Path) -> list[tuple[object, list[tuple[object, ...]]]]:
    source = SQLiteWorldPredictionArchiveSource(db_path)
    sink = DuckDbWorldPredictionParquetStore(
        db_path.parent / "world_model_archive",
        clock=lambda: datetime(2026, 9, 7, tzinfo=UTC),
    )
    exported: list[tuple[object, list[tuple[object, ...]]]] = []
    for plan in source.plan_before(date(2099, 1, 1)):
        rows = list(source.iter_partition(plan))
        manifest = sink.publish(plan, iter(rows))
        exported.append((manifest, rows))
    return exported


def _register_exported(connection: sqlite3.Connection, exported) -> int:
    ensure_cold_schema(connection)
    total = 0
    for manifest, rows in exported:
        total += register_cold_partition(
            connection,
            manifest=manifest,
            rows=rows,
            cutover_id=_CUTOVER_ID,
            activated_at=_ACTIVATED_AT,
        )
    return total


def _delete_hot(connection: sqlite3.Connection, prediction_ids: list[str]) -> None:
    connection.execute("DROP TRIGGER IF EXISTS world_predictions_no_delete")
    for chunk in sqlite_in_chunks(prediction_ids):
        connection.execute(
            f"DELETE FROM world_shadow_predictions WHERE prediction_id IN ({sqlite_placeholders(len(chunk))})",
            chunk,
        )
    connection.execute(_DELETE_TRIGGER_SQL)


def test_ensure_cold_schema_accepts_cursor_and_does_not_commit_caller_transaction(tmp_path: Path) -> None:
    db_path = tmp_path / "world_model.db"
    schema = StateDb(db_path)
    schema.apply_migrations(WORLD_MODEL_MIGRATIONS)
    schema.close()

    connection = _connect(db_path)
    try:
        cursor = connection.cursor()
        cursor.execute("BEGIN")
        ensure_cold_schema(cursor)
        names = _table_names(connection)
        assert "world_prediction_cold_partitions" in names
        assert "world_prediction_cold_index" in names
        cursor.execute("ROLLBACK")
        names = _table_names(connection)
        assert "world_prediction_cold_partitions" not in names
        assert "world_prediction_cold_index" not in names
    finally:
        connection.close()


def test_empty_memory_cold_catalog_keeps_hot_only_reads_without_disk_path() -> None:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    try:
        connection.executescript(
            """
            CREATE TABLE world_shadow_predictions (
                prediction_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                episode_id TEXT NOT NULL,
                horizon_code TEXT NOT NULL,
                model_kind TEXT,
                model_version TEXT,
                predicted_at TEXT,
                input_sha256 TEXT,
                prediction_json TEXT NOT NULL,
                prediction_sha256 TEXT,
                payload_json TEXT NOT NULL,
                payload_sha256 TEXT,
                recorded_at TEXT NOT NULL
            );
            INSERT INTO world_shadow_predictions(
                prediction_id, run_id, episode_id, horizon_code, model_kind, model_version,
                predicted_at, input_sha256, prediction_json, prediction_sha256, payload_json,
                payload_sha256, recorded_at
            ) VALUES (
                'p-hot', 'run-hot', 'e-hot', '4h', 'markov', 'v1',
                '2026-08-24T00:00:00+00:00', 'sha256:' || replace(hex(zeroblob(32)), '00', 'ab'),
                '{"ok":true}', 'sha256:' || replace(hex(zeroblob(32)), '00', 'cd'),
                '{"prediction_id":"p-hot"}', 'sha256:' || replace(hex(zeroblob(32)), '00', 'ef'),
                '2026-08-24T01:00:00+00:00'
            );
            """
        )
        ensure_cold_schema(connection)
        validate_registered_cold_storage(connection)
        assert prediction_count(connection) == 1
        assert read_prediction_identities(connection) == [
            {
                "episode_id": "e-hot",
                "horizon_code": "4h",
                "model_kind": "markov",
                "model_version": "v1",
            }
        ]
    finally:
        connection.close()


def test_cold_schema_is_outside_versioned_migration_and_legacy_hot_only_reads(tmp_path: Path) -> None:
    migration_sql = "\n".join(WORLD_MODEL_MIGRATIONS[0][1])
    assert "world_prediction_cold_partitions" not in migration_sql
    assert "world_prediction_cold_index" not in migration_sql

    db_path = tmp_path / "world_model.db"
    store = _seed_store(db_path)
    try:
        versions = [int(row["version"]) for row in store._db.query_all("SELECT version FROM schema_migrations")]
        assert versions == [1]
        names = {row["name"] for row in store._db.query_all("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "world_prediction_cold_partitions" in names
        assert store.append_legacy_prediction(_prediction()) is True
        rows = store.list_predictions()
        assert [row["prediction_id"] for row in rows] == ["prediction-1"]
        assert store.counts()["predictions"] == 1
        assert store.list_prediction_identities() == [
            {
                "episode_id": DEFAULT_EPISODE_ID,
                "horizon_code": "elapsed_4h.v1",
                "model_kind": "markov",
                "model_version": "markov-v1",
                "study_cohort_id": None,
                "lane_id": None,
            }
        ]
    finally:
        store.close()

    legacy = sqlite3.connect(":memory:")
    legacy.row_factory = sqlite3.Row
    try:
        legacy.executescript(
            """
            CREATE TABLE world_episodes (episode_id TEXT PRIMARY KEY, observed_at TEXT NOT NULL);
            CREATE TABLE world_shadow_predictions (
                prediction_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                episode_id TEXT NOT NULL,
                horizon_code TEXT NOT NULL,
                model_kind TEXT,
                model_version TEXT,
                predicted_at TEXT,
                input_sha256 TEXT,
                prediction_json TEXT NOT NULL,
                prediction_sha256 TEXT,
                payload_json TEXT NOT NULL,
                payload_sha256 TEXT,
                recorded_at TEXT NOT NULL
            );
            INSERT INTO world_episodes VALUES ('e-legacy', '2026-08-24T00:00:00+00:00');
            INSERT INTO world_shadow_predictions(
                prediction_id, run_id, episode_id, horizon_code, model_kind, model_version,
                predicted_at, input_sha256, prediction_json, prediction_sha256, payload_json,
                payload_sha256, recorded_at
            ) VALUES (
                'p-legacy', 'run-legacy', 'e-legacy', '4h', 'markov', 'v1',
                '2026-08-24T00:00:00+00:00', 'sha256:' || replace(hex(zeroblob(32)), '00', 'ab'),
                '{"ok":true}', 'sha256:' || replace(hex(zeroblob(32)), '00', 'cd'),
                '{"prediction_id":"p-legacy"}', 'sha256:' || replace(hex(zeroblob(32)), '00', 'ef'),
                '2026-08-24T01:00:00+00:00'
            );
            """
        )
        rows = read_prediction_rows(
            legacy,
            columns=("prediction_id", "payload_json", "study_cohort_id", "episode_observed_at"),
            order="recorded",
        )
        assert [row["prediction_id"] for row in rows] == ["p-legacy"]
        assert "study_cohort_id" not in rows[0]
        assert rows[0]["episode_observed_at"] == "2026-08-24T00:00:00+00:00"
        assert prediction_count(legacy) == 1
        assert read_prediction_identities(legacy) == [
            {
                "episode_id": "e-legacy",
                "horizon_code": "4h",
                "model_kind": "markov",
                "model_version": "v1",
            }
        ]
    finally:
        legacy.close()


def test_cold_catalog_is_append_only_and_partition_id_is_relative_path(tmp_path: Path) -> None:
    db_path = tmp_path / "world_model.db"
    store = _seed_store(db_path)
    store.close()
    connection = _connect(db_path)
    try:
        _insert_prediction(connection, prediction_id="p-1")
        exported = _export_partitions(db_path)
        assert exported
        connection.execute("BEGIN")
        count = _register_exported(connection, exported)
        connection.execute("COMMIT")
        assert count == 1
        manifest, rows = exported[0]
        partition_id = connection.execute(
            "SELECT partition_id, recorded_date, relative_path, row_count FROM world_prediction_cold_partitions"
        ).fetchone()
        assert partition_id["partition_id"] == manifest.relative_path
        assert partition_id["partition_id"] != manifest.recorded_date.isoformat()
        assert partition_id["relative_path"] == manifest.relative_path
        assert partition_id["recorded_date"] == manifest.recorded_date.isoformat()
        assert partition_id["row_count"] == 1
        assert "part-00000.parquet" in manifest.relative_path

        indexed = connection.execute("SELECT * FROM world_prediction_cold_index WHERE prediction_id='p-1'").fetchone()
        original = dict(zip(manifest.column_names, rows[0], strict=True))
        for field in _SCALAR_FIELDS:
            assert indexed[field] == original[field]
        assert "payload_json" not in indexed.keys()
        assert "prediction_json" not in indexed.keys()

        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("UPDATE world_prediction_cold_partitions SET row_count=0")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("DELETE FROM world_prediction_cold_index WHERE prediction_id='p-1'")
        with pytest.raises((sqlite3.IntegrityError, ValueError, PredictionReadUnavailable)):
            register_cold_partition(
                connection,
                manifest=manifest,
                rows=rows,
                cutover_id="cutover-2",
                activated_at=_ACTIVATED_AT,
            )
    finally:
        connection.close()


def test_register_roundtrips_sqlite_row_and_tuple_and_rejects_schema_drift(tmp_path: Path) -> None:
    db_path = tmp_path / "world_model.db"
    store = _seed_store(db_path)
    connection = _connect(db_path)
    try:
        _insert_prediction(connection, prediction_id="p-row", recorded_at="2026-08-24T01:00:00+00:00")
        _insert_prediction(connection, prediction_id="p-tuple", recorded_at="2026-08-25T01:00:00+00:00")
        store.close()
        exported = _export_partitions(db_path)
        assert len(exported) == 2
        ensure_cold_schema(connection)
        first_manifest, _first_rows = exported[0]
        selected = ", ".join(f'"{name}"' for name in first_manifest.column_names)
        mapping_rows = connection.execute(
            f"SELECT {selected} FROM world_shadow_predictions WHERE prediction_id='p-row'"
        ).fetchall()
        assert register_cold_partition(
            connection,
            manifest=first_manifest.to_dict(),
            rows=mapping_rows,
            cutover_id=_CUTOVER_ID,
            activated_at=_ACTIVATED_AT,
        ) == 1
        second_manifest, second_rows = exported[1]
        drifted = list(second_rows[0])
        drifted[list(second_manifest.column_names).index("payload_sha256")] = "sha256:" + "99" * 32
        with pytest.raises((PredictionReadUnavailable, ValueError)):
            register_cold_partition(
                connection,
                manifest=second_manifest,
                rows=[tuple(drifted)],
                cutover_id=_CUTOVER_ID,
                activated_at=_ACTIVATED_AT,
            )
        assert register_cold_partition(
            connection,
            manifest=second_manifest,
            rows=second_rows,
            cutover_id=_CUTOVER_ID,
            activated_at=_ACTIVATED_AT,
        ) == 1
    finally:
        connection.close()


def test_hot_insert_rejects_cold_prediction_id(tmp_path: Path) -> None:
    db_path = tmp_path / "world_model.db"
    store = _seed_store(db_path)
    connection = _connect(db_path)
    try:
        original = _insert_prediction(connection, prediction_id="p-cold")
        store.close()
        exported = _export_partitions(db_path)
        _register_exported(connection, exported)
        _delete_hot(connection, ["p-cold"])
        with pytest.raises(sqlite3.IntegrityError, match="cold storage"):
            connection.execute(
                """
                INSERT INTO world_shadow_predictions(
                    prediction_id, run_id, episode_id, horizon_code, model_kind, model_version,
                    predicted_at, input_sha256, prediction_json, prediction_sha256, payload_json,
                    payload_sha256, recorded_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    original["prediction_id"],
                    original["run_id"],
                    original["episode_id"],
                    original["horizon_code"],
                    original["model_kind"],
                    original["model_version"],
                    original["predicted_at"],
                    original["input_sha256"],
                    original["prediction_json"],
                    original["prediction_sha256"],
                    original["payload_json"],
                    original["payload_sha256"],
                    original["recorded_at"],
                ),
            )
    finally:
        connection.close()


def test_global_filtered_hydration_order_limit_and_lineage_authority(tmp_path: Path) -> None:
    db_path = tmp_path / "world_model.db"
    store = WorldModelStore(db_path)
    late_payload = _episode(
        observation=_observation(
            symbol="MSFT",
            as_of_bar_ts="2026-08-23T00:00:00+00:00",
            available_at="2026-08-23T00:00:00+00:00",
            captured_at="2026-08-23T00:01:00+00:00",
            anchor=AnchorBar(
                ts="2026-08-23T00:00:00+00:00",
                open=100.0,
                high=102.0,
                low=99.0,
                close=101.0,
                volume=1_000.0,
                source="fixture",
            ),
        )
    )
    assert store.append_episode(_episode()) is True
    assert store.append_episode(late_payload) is True
    late_episode_id = str(late_payload["episode_id"])
    store.close()
    connection = _connect(db_path)
    try:
        _insert_prediction(
            connection,
            prediction_id="p-old",
            recorded_at="2026-08-24T03:00:00+00:00",
            run_id="run-b",
        )
        _insert_prediction(
            connection,
            prediction_id="p-older",
            recorded_at="2026-08-24T01:00:00+00:00",
            run_id="run-a",
            model_kind=None,
            model_version=None,
        )
        _insert_prediction(
            connection,
            prediction_id="p-late-ep",
            episode_id=late_episode_id,
            recorded_at="2026-08-24T02:00:00+00:00",
            run_id="run-a",
            study_cohort_id="cohort-b",
        )
        _insert_prediction(
            connection,
            prediction_id="p-hot",
            recorded_at="2026-08-26T01:00:00+00:00",
            run_id="run-a",
            study_cohort_id="cohort-a",
        )
        exported = _export_partitions(db_path)
        _register_exported(connection, exported)
        _delete_hot(connection, ["p-old", "p-older", "p-late-ep"])

        rows = read_prediction_rows(connection, columns=_READ_COLUMNS, order="recorded")
        assert [row["prediction_id"] for row in rows] == ["p-older", "p-late-ep", "p-old", "p-hot"]
        older = rows[0]
        assert older["payload_json"]
        assert older["prediction_json"]
        assert older["study_cohort_id"] == "cohort-a"
        assert json.loads(older["payload_json"])["study_cohort_id"] == "json-should-lose"

        filtered = read_prediction_rows(
            connection,
            columns=_READ_COLUMNS,
            study_cohort_id="cohort-a",
            run_id="run-a",
            horizon_code="elapsed_4h.v1",
            order="recorded",
        )
        assert [row["prediction_id"] for row in filtered] == ["p-older", "p-hot"]

        limited = read_prediction_rows(connection, columns=("prediction_id", "recorded_at"), order="recorded", limit=2)
        assert [row["prediction_id"] for row in limited] == ["p-older", "p-late-ep"]

        by_ids = read_prediction_rows(
            connection,
            columns=("prediction_id",),
            prediction_ids=["p-hot", "p-old"],
            order="recorded",
        )
        assert [row["prediction_id"] for row in by_ids] == ["p-old", "p-hot"]
        assert read_prediction_rows(connection, columns=("prediction_id",), prediction_ids=[], order="recorded") == []
        assert read_prediction_rows(connection, columns=("prediction_id",), episode_ids=[], order="recorded") == []

        episode_order = read_prediction_rows(
            connection,
            columns=("prediction_id", "run_id", "episode_observed_at"),
            order="episode",
        )
        assert [row["prediction_id"] for row in episode_order] == ["p-hot", "p-older", "p-late-ep", "p-old"]
    finally:
        connection.close()


def test_duplicate_hot_cold_same_id_dedups_and_divergent_fails_closed(tmp_path: Path) -> None:
    db_path = tmp_path / "world_model.db"
    store = _seed_store(db_path)
    connection = _connect(db_path)
    try:
        _insert_prediction(connection, prediction_id="p-dup")
        _insert_prediction(
            connection,
            prediction_id="p-other",
            model_kind="markov",
            model_version="markov-v1",
            recorded_at="2026-08-24T02:00:00+00:00",
        )
        store.close()
        exported = _export_partitions(db_path)
        _register_exported(connection, exported)
        rows = read_prediction_rows(connection, columns=_READ_COLUMNS, order="recorded")
        assert [row["prediction_id"] for row in rows] == ["p-dup", "p-other"]
        assert prediction_count(connection) == 2

        connection.execute("DROP TRIGGER IF EXISTS world_shadow_predictions_no_update")
        connection.execute("DROP TRIGGER IF EXISTS world_predictions_no_update")
        connection.execute("UPDATE world_shadow_predictions SET payload_sha256=? WHERE prediction_id='p-dup'", ("sha256:" + "aa" * 32,))
        with pytest.raises(PredictionReadUnavailable):
            read_prediction_rows(connection, columns=_READ_COLUMNS, order="recorded")
    finally:
        connection.close()


def test_distinct_ids_sharing_business_tuple_remain_distinct(tmp_path: Path) -> None:
    db_path = tmp_path / "world_model.db"
    store = _seed_store(db_path)
    connection = _connect(db_path)
    try:
        _insert_prediction(connection, prediction_id="p-a", recorded_at="2026-08-24T01:00:00+00:00")
        _insert_prediction(connection, prediction_id="p-b", recorded_at="2026-08-24T02:00:00+00:00")
        store.close()
        exported = _export_partitions(db_path)
        _register_exported(connection, exported)
        _delete_hot(connection, ["p-a"])
        rows = read_prediction_rows(connection, columns=("prediction_id",), order="recorded")
        assert [row["prediction_id"] for row in rows] == ["p-a", "p-b"]
        identities = read_prediction_identities(connection)
        assert identities == [
            {
                "episode_id": DEFAULT_EPISODE_ID,
                "horizon_code": "elapsed_4h.v1",
                "model_kind": "markov",
                "model_version": "markov-v1",
                "study_cohort_id": "cohort-a",
                "lane_id": "lane-a",
            }
        ]
        assert "payload_json" not in identities[0]
    finally:
        connection.close()


def test_missing_and_corrupt_canonical_files_fail_closed(tmp_path: Path) -> None:
    db_path = tmp_path / "world_model.db"
    store = _seed_store(db_path)
    connection = _connect(db_path)
    try:
        _insert_prediction(connection, prediction_id="p-file")
        store.close()
        exported = _export_partitions(db_path)
        _register_exported(connection, exported)
        _delete_hot(connection, ["p-file"])
        parquet = db_path.parent / "world_model_archive" / exported[0][0].relative_path
        manifest_path = parquet.parent / "manifest.json"
        assert parquet.is_file()

        parquet.unlink()
        with pytest.raises(PredictionReadUnavailable):
            read_prediction_rows(connection, columns=_READ_COLUMNS)
        with pytest.raises(PredictionReadUnavailable):
            prediction_count(connection)
        with pytest.raises(PredictionReadUnavailable):
            read_prediction_identities(connection)
        parquet.write_bytes(b"not-parquet")
        with pytest.raises(PredictionReadUnavailable):
            read_prediction_rows(connection, columns=_READ_COLUMNS)

        exported_again_path = parquet
        # restore a regular file then tamper the sidecar manifest
        exported_again_path.write_bytes(b"not-parquet")
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        payload["row_count"] = 99
        manifest_path.write_text(json.dumps(payload), encoding="utf-8")
        with pytest.raises(PredictionReadUnavailable):
            read_prediction_identities(connection)
        with pytest.raises(PredictionReadUnavailable):
            cold_prediction_for_replay(connection, "missing-id")
        with pytest.raises(PredictionReadUnavailable):
            cold_prediction_for_replay(connection, "p-file")
    finally:
        connection.close()


def test_cold_replay_exact_conflict_and_live_validation_exemption(tmp_path: Path) -> None:
    db_path = tmp_path / "world_model.db"
    store = WorldModelStore(db_path)
    try:
        payload = _prediction("cold-replay")
        payload["recommendation"] = "GO"
        assert store.append_episode(_episode()) is True
        assert store.append_legacy_prediction(payload) is True
        prediction_id = payload["prediction_id"]
        store.close()
        connection = _connect(db_path)
        try:
            exported = _export_partitions(db_path)
            _register_exported(connection, exported)
            _delete_hot(connection, [prediction_id])
            metadata = cold_prediction_for_replay(connection, prediction_id)
            assert metadata is not None
            assert metadata["prediction_id"] == prediction_id
            assert metadata["payload_sha256"]
            assert metadata["prediction_sha256"]
            assert "payload_json" not in metadata
        finally:
            connection.close()

        restarted = WorldModelStore(db_path)
        try:
            assert restarted.append_prediction(payload) is False
            conflict = dict(payload)
            conflict["prediction"] = dict(payload["prediction"])
            conflict["prediction"]["p_up"] = 0.1
            with pytest.raises(WorldModelConflictError, match="different canonical content"):
                restarted.append_prediction(conflict)
            assert restarted.counts()["predictions"] == 1
            rows = restarted.list_predictions()
            assert len(rows) == 1
            assert rows[0]["prediction_id"] == prediction_id
            assert rows[0]["prediction"]["p_up"] == 0.6
        finally:
            restarted.close()
    finally:
        store.close()


def test_1100_prediction_ids_are_served_below_sqlite_variable_limit(tmp_path: Path) -> None:
    db_path = tmp_path / "world_model.db"
    store = _seed_store(db_path)
    connection = _connect(db_path)
    try:
        ids = [f"p-{index:04d}" for index in range(1100)]
        for index, prediction_id in enumerate(ids):
            _insert_prediction(
                connection,
                prediction_id=prediction_id,
                recorded_at=f"2026-08-24T00:{index // 60:02d}:{index % 60:02d}.{index:06d}+00:00",
            )
        store.close()
        rows = read_prediction_rows(
            connection,
            columns=("prediction_id",),
            prediction_ids=ids,
            order="recorded",
        )
        assert [row["prediction_id"] for row in rows] == ids
        assert prediction_count(connection) == 1100
    finally:
        connection.close()


def test_storage_leases_journal_guard_and_readonly_does_not_create_files(tmp_path: Path) -> None:
    db_path = tmp_path / "world_model.db"
    lock_path = storage_lock_path(db_path)
    journal_path = cutover_journal_path(db_path)
    assert lock_path == db_path.with_name("world_model.db.storage.lock")
    assert journal_path == db_path.with_name("world_model.db.cutover.json")

    missing_dir = tmp_path / "absent-parent" / "world_model.db"
    noop = shared_prediction_storage_lease(missing_dir, create=False)
    try:
        assert not missing_dir.parent.exists()
        assert not storage_lock_path(missing_dir).exists()
        noop.close()
        noop.close()
    finally:
        noop.close()

    journal_path.write_text("{}", encoding="utf-8")
    with pytest.raises(PredictionStorageBusyError):
        shared_prediction_storage_lease(db_path, create=False)
    assert not lock_path.exists()
    with exclusive_prediction_storage_lease(db_path, allow_in_progress=True) as exclusive:
        assert lock_path.is_file()
        with pytest.raises(PredictionStorageBusyError):
            shared_prediction_storage_lease(db_path, create=True)
        exclusive.close()
        exclusive.close()
    assert journal_path.exists()
    journal_path.unlink()

    with shared_prediction_storage_lease(db_path, create=True) as first:
        with shared_prediction_storage_lease(db_path, create=True) as second:
            with pytest.raises(PredictionStorageBusyError):
                exclusive_prediction_storage_lease(db_path)
            second.close()
        first.close()

    with prediction_storage_lease(db_path, exclusive=True, create=True) as exclusive:
        with pytest.raises(PredictionStorageBusyError):
            prediction_storage_lease(db_path, exclusive=False, create=True)
        exclusive.close()

    store = WorldModelStore(db_path)
    try:
        assert lock_path.is_file()
        with pytest.raises(PredictionStorageBusyError):
            exclusive_prediction_storage_lease(db_path)
        supplied = StateDb(tmp_path / "supplied-world.db")
        supplied_store = WorldModelStore(supplied)
        try:
            assert storage_lock_path(supplied.path).is_file()
            supplied_store.close()
            assert supplied.query_one("SELECT 1")[0] == 1
            with exclusive_prediction_storage_lease(supplied.path):
                assert supplied.query_one("SELECT 1")[0] == 1
        finally:
            supplied.close()
    finally:
        store.close()
    with exclusive_prediction_storage_lease(db_path):
        pass

    journal_path.write_text("{}", encoding="utf-8")
    with pytest.raises(PredictionStorageBusyError):
        WorldModelStore(db_path)
    journal_path.unlink()

    with patch(
        "trader.infrastructure.state_db.world_model_store.apply_current_world_model_schema",
        side_effect=RuntimeError("schema boom"),
    ):
        failed_path = tmp_path / "boom.db"
        with pytest.raises(RuntimeError, match="schema boom"):
            WorldModelStore(failed_path)
        with exclusive_prediction_storage_lease(failed_path):
            pass


def _insert_many(connection: sqlite3.Connection, ids: list[str], *, day: str) -> None:
    for index, prediction_id in enumerate(ids):
        _insert_prediction(
            connection,
            prediction_id=prediction_id,
            recorded_at=f"{day}T00:{index // 60:02d}:{index % 60:02d}.{index:06d}+00:00",
        )


def test_dedicated_helper_lease_lifetime_and_journal_blocks_before_sqlite_open(tmp_path: Path) -> None:
    db_path = tmp_path / "world_model.db"
    journal_path = cutover_journal_path(db_path)
    journal_path.write_text(json.dumps({"status": "completed"}), encoding="utf-8")
    with pytest.raises(PredictionStorageBusyError):
        _open_dedicated_state_db(db_path)
    assert not db_path.exists()
    journal_path.unlink()

    opened = _open_dedicated_state_db(db_path)
    try:
        assert storage_lock_path(db_path).is_file()
        with pytest.raises(PredictionStorageBusyError):
            exclusive_prediction_storage_lease(db_path)
    finally:
        opened.close()
    with exclusive_prediction_storage_lease(db_path):
        pass

    store = WorldModelStore(db_path)
    try:
        assert store._storage_lease is None
        graph = WorldGraphStore(tmp_path / "graph-world.db")
        try:
            with pytest.raises(PredictionStorageBusyError):
                exclusive_prediction_storage_lease(graph.path)
        finally:
            graph.close()
        with exclusive_prediction_storage_lease(graph.path):
            pass
    finally:
        store.close()


def test_failed_wal_init_closes_partial_connection_before_releasing_lease(tmp_path: Path) -> None:
    db_path = tmp_path / "world_model.db"
    original_connect = sqlite3.connect
    failed_connections: list[sqlite3.Connection] = []

    class _WalLockedOnceConnection(sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):
            if not getattr(self, "_journal_mode_wal_failed", False) and str(sql).strip() == "PRAGMA journal_mode=WAL":
                self._journal_mode_wal_failed = True
                raise sqlite3.OperationalError("database is locked")
            return super().execute(sql, *args, **kwargs)

    def one_shot_wal_locked_factory(*args, **kwargs):
        if not failed_connections:
            connection = original_connect(*args, factory=_WalLockedOnceConnection, **kwargs)
            failed_connections.append(connection)
            return connection
        return original_connect(*args, **kwargs)

    with (
        patch("trader.infrastructure.state_db.connection.sqlite3.connect", one_shot_wal_locked_factory),
        patch("trader.infrastructure.state_db.world_model_store.time.sleep", return_value=None),
    ):
        opened = _open_dedicated_state_db(db_path)
    try:
        assert len(failed_connections) == 1
        with pytest.raises(sqlite3.ProgrammingError):
            failed_connections[0].execute("SELECT 1")
        assert opened.query_one("SELECT 1")[0] == 1
        with pytest.raises(PredictionStorageBusyError):
            exclusive_prediction_storage_lease(db_path)
    finally:
        opened.close()
    with exclusive_prediction_storage_lease(db_path):
        pass


def test_exclusive_lease_without_create_rejects_missing_lock(tmp_path: Path) -> None:
    db_path = tmp_path / "missing" / "world_model.db"
    with pytest.raises(PredictionStorageBusyError):
        prediction_storage_lease(db_path, exclusive=True, create=False)
    noop = prediction_storage_lease(db_path, exclusive=False, create=False)
    try:
        assert not db_path.parent.exists()
        assert not storage_lock_path(db_path).exists()
    finally:
        noop.close()

    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x", encoding="utf-8")
    with pytest.raises(PredictionStorageBusyError):
        prediction_storage_lease(blocker / "world_model.db", exclusive=False, create=True)

    lock_path = storage_lock_path(tmp_path / "world_model.db")
    real_lstat = os.lstat

    def _boom(path: str | os.PathLike[str], *args: object, **kwargs: object) -> os.stat_result:
        if Path(path) == lock_path:
            raise OSError("lstat denied")
        return real_lstat(path, *args, **kwargs)

    with patch("trader.infrastructure.state_db.world_prediction_storage_lock.os.lstat", _boom):
        with pytest.raises(PredictionStorageBusyError):
            prediction_storage_lease(tmp_path / "world_model.db", exclusive=False, create=False)


def test_missing_cold_file_refuses_fresh_append(tmp_path: Path) -> None:
    db_path = tmp_path / "world_model.db"
    store = _seed_store(db_path)
    connection = _connect(db_path)
    try:
        _insert_prediction(connection, prediction_id="p-file")
        store.close()
        exported = _export_partitions(db_path)
        _register_exported(connection, exported)
        _delete_hot(connection, ["p-file"])
        parquet = db_path.parent / "world_model_archive" / exported[0][0].relative_path
        parquet.unlink()
    finally:
        connection.close()

    restarted = WorldModelStore(db_path)
    try:
        with pytest.raises(PredictionReadUnavailable):
            restarted.append_legacy_prediction(_prediction("fresh-legacy"))
        with pytest.raises(PredictionReadUnavailable):
            restarted.append_prediction(_prediction("fresh-live"))
    finally:
        restarted.close()


def test_partial_cold_schema_and_index_orphans_fail_closed(tmp_path: Path) -> None:
    db_path = tmp_path / "world_model.db"
    store = _seed_store(db_path)
    connection = _connect(db_path)
    try:
        _insert_prediction(connection, prediction_id="p-cat")
        store.close()
        ensure_cold_schema(connection)
        connection.execute("BEGIN")
        connection.execute("DROP TABLE world_prediction_cold_index")
        with pytest.raises(PredictionReadUnavailable):
            cold_catalog_present(connection)
        with pytest.raises(PredictionReadUnavailable):
            read_prediction_identities(connection)
        with pytest.raises(PredictionReadUnavailable):
            ensure_cold_schema(connection)
        names = _table_names(connection)
        assert "world_prediction_cold_partitions" in names
        assert "world_prediction_cold_index" not in names
        connection.execute("ROLLBACK")
        assert "world_prediction_cold_index" in _table_names(connection)

        exported = _export_partitions(db_path)
        _register_exported(connection, exported)
        connection.execute("DROP TRIGGER IF EXISTS world_prediction_cold_index_no_delete")
        connection.execute("DELETE FROM world_prediction_cold_index WHERE prediction_id='p-cat'")
        with pytest.raises(PredictionReadUnavailable):
            validate_registered_cold_storage(connection)

        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute(
            """
            INSERT INTO world_prediction_cold_index(
                prediction_id, partition_id, run_id, episode_id, horizon_code,
                input_sha256, prediction_sha256, payload_sha256, recorded_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "orphan-p",
                "missing-partition",
                "run-a",
                DEFAULT_EPISODE_ID,
                "elapsed_4h.v1",
                "sha256:" + "11" * 32,
                "sha256:" + "22" * 32,
                "sha256:" + "33" * 32,
                "2026-08-24T01:00:00+00:00",
            ),
        )
        with pytest.raises(PredictionReadUnavailable):
            validate_registered_cold_storage(connection)
    finally:
        connection.close()

    partial = _connect(db_path)
    try:
        partial.execute("DROP TABLE world_prediction_cold_index")
        with pytest.raises(PredictionReadUnavailable):
            ensure_cold_schema(partial)
        assert "world_prediction_cold_index" not in _table_names(partial)
        with pytest.raises(PredictionReadUnavailable):
            WorldModelStore(db_path)
        assert "world_prediction_cold_index" not in _table_names(partial)
        assert "world_prediction_cold_partitions" in _table_names(partial)
    finally:
        partial.close()

    malformed_path = tmp_path / "malformed.db"
    malformed_store = _seed_store(malformed_path)
    malformed_store.close()
    malformed = _connect(malformed_path)
    try:
        ensure_cold_schema(malformed)
        bad_json = "{not-json"
        malformed.execute(
            """
            INSERT INTO world_prediction_cold_partitions(
                partition_id, manifest_json, manifest_sha256, cutover_id, activated_at,
                recorded_date, relative_path, row_count
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "bad/part.parquet",
                bad_json,
                "sha256:" + hashlib.sha256(bad_json.encode("utf-8")).hexdigest(),
                _CUTOVER_ID,
                _ACTIVATED_AT,
                "2026-08-26",
                "bad/part.parquet",
                1,
            ),
        )
        with pytest.raises(PredictionReadUnavailable):
            validate_registered_cold_storage(malformed)
    finally:
        malformed.close()


def test_register_rejects_forged_parquet_digest_and_missing_payload_column(tmp_path: Path) -> None:
    db_path = tmp_path / "world_model.db"
    store = _seed_store(db_path)
    connection = _connect(db_path)
    try:
        _insert_prediction(connection, prediction_id="p-reg")
        store.close()
        exported = _export_partitions(db_path)
        manifest, rows = exported[0]
        ensure_cold_schema(connection)
        names = list(manifest.column_names)
        types = list(manifest.column_types)
        idx = names.index("payload_json")
        names.pop(idx)
        types.pop(idx)
        missing_payload = replace(manifest, column_names=tuple(names), column_types=tuple(types))
        with pytest.raises((PredictionReadUnavailable, ValueError)):
            register_cold_partition(
                connection,
                manifest=missing_payload,
                rows=rows,
                cutover_id=_CUTOVER_ID,
                activated_at=_ACTIVATED_AT,
            )

        parquet = db_path.parent / "world_model_archive" / manifest.relative_path
        sidecar = parquet.parent / "manifest.json"
        forged_path = parquet.with_name("forged.parquet")
        duck = duckdb.connect(":memory:")
        try:
            duck.execute("SET autoinstall_known_extensions=false")
            duck.execute("SET autoload_known_extensions=false")
            src = str(parquet).replace("'", "''")
            dst = str(forged_path).replace("'", "''")
            duck.execute(
                "COPY (SELECT * REPLACE ('{\"forged\":true}' AS payload_json) "
                f"FROM read_parquet('{src}', hive_partitioning=false)) "
                f"TO '{dst}' (FORMAT PARQUET)"
            )
        finally:
            duck.close()
        parquet.write_bytes(forged_path.read_bytes())
        digest = "sha256:" + hashlib.sha256(parquet.read_bytes()).hexdigest()
        forged = replace(manifest, parquet_sha256=digest, parquet_bytes=parquet.stat().st_size)
        sidecar.write_text(json.dumps(forged.to_dict()), encoding="utf-8")
        with pytest.raises((PredictionReadUnavailable, ValueError)):
            register_cold_partition(
                connection,
                manifest=forged,
                rows=rows,
                cutover_id=_CUTOVER_ID,
                activated_at=_ACTIVATED_AT,
            )
    finally:
        connection.close()


def test_reader_bind_budget_mixed_tiers_and_sqlite_variable_limit(tmp_path: Path) -> None:
    db_path = tmp_path / "world_model.db"
    store = _seed_store(db_path)
    connection = _connect(db_path)
    try:
        ids = [f"p-{index:04d}" for index in range(1200)]
        _insert_many(connection, ids, day="2026-08-24")
        store.close()
        exported = _export_partitions(db_path)
        _register_exported(connection, exported)
        _delete_hot(connection, ids[:600])
        connection.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 999)
        rows = read_prediction_rows(
            connection,
            columns=("prediction_id", "recorded_at"),
            prediction_ids=ids,
            episode_ids=[DEFAULT_EPISODE_ID, "missing-episode"],
            study_cohort_id="cohort-a",
            run_id="run-a",
            horizon_code="elapsed_4h.v1",
            order="recorded",
            limit=25,
        )
        assert [row["prediction_id"] for row in rows] == ids[:25]
        assert len({row["prediction_id"] for row in rows}) == 25
    finally:
        connection.close()


def test_overlapping_json_mismatch_is_rejected_without_hydrating_count(tmp_path: Path) -> None:
    db_path = tmp_path / "world_model.db"
    store = _seed_store(db_path)
    connection = _connect(db_path)
    try:
        original = _insert_prediction(connection, prediction_id="p-json")
        store.close()
        exported = _export_partitions(db_path)
        _register_exported(connection, exported)
        connection.execute("DROP TRIGGER IF EXISTS world_shadow_predictions_no_update")
        connection.execute("DROP TRIGGER IF EXISTS world_predictions_no_update")
        connection.execute(
            "UPDATE world_shadow_predictions SET payload_json=? WHERE prediction_id='p-json'",
            ('{"prediction_id":"p-json","diverged":true}',),
        )
        assert prediction_count(connection) == 1
        assert read_prediction_identities(connection) == [
            {
                "episode_id": DEFAULT_EPISODE_ID,
                "horizon_code": "elapsed_4h.v1",
                "model_kind": "markov",
                "model_version": "markov-v1",
                "study_cohort_id": "cohort-a",
                "lane_id": "lane-a",
            }
        ]
        with pytest.raises(PredictionReadUnavailable):
            read_prediction_rows(connection, columns=_READ_COLUMNS, order="recorded")
        stored = connection.execute(
            "SELECT payload_sha256 FROM world_shadow_predictions WHERE prediction_id='p-json'"
        ).fetchone()
        assert stored["payload_sha256"] == original["payload_sha256"]
    finally:
        connection.close()


def test_cold_payload_read_issues_one_duckdb_query_per_partition(tmp_path: Path) -> None:
    db_path = tmp_path / "world_model.db"
    store = _seed_store(db_path)
    connection = _connect(db_path)
    try:
        first = [f"p-a-{index:04d}" for index in range(600)]
        second = [f"p-b-{index:04d}" for index in range(550)]
        _insert_many(connection, first, day="2026-08-24")
        _insert_many(connection, second, day="2026-08-25")
        store.close()
        exported = _export_partitions(db_path)
        assert len(exported) == 2
        _register_exported(connection, exported)
        ids = first + second
        _delete_hot(connection, ids)
        read_parquet_sql: list[str] = []
        real_connect = duckdb.connect

        class _DuckDbProxy:
            def __init__(self, conn: object) -> None:
                self._conn = conn

            def execute(self, sql: object, params: object = None):
                if "read_parquet" in str(sql).lower():
                    read_parquet_sql.append(str(sql))
                if params is None:
                    return self._conn.execute(sql)
                return self._conn.execute(sql, params)

            def close(self) -> None:
                self._conn.close()

            def __getattr__(self, name: str):
                return getattr(self._conn, name)

        def _connect_proxy(*args: object, **kwargs: object):
            return _DuckDbProxy(real_connect(*args, **kwargs))

        with patch("trader.infrastructure.state_db.world_prediction_reader.duckdb.connect", _connect_proxy):
            rows = read_prediction_rows(
                connection,
                columns=("prediction_id", "payload_json"),
                prediction_ids=ids,
                order="recorded",
            )
        assert [row["prediction_id"] for row in rows] == ids
        assert all(row["payload_json"] for row in rows)
        assert len(read_parquet_sql) == 2
    finally:
        connection.close()


def test_reader_tuple_row_factory_orphan_rows_and_missing_episodes(tmp_path: Path) -> None:
    db_path = tmp_path / "world_model.db"
    store = _seed_store(db_path)
    connection = _connect(db_path)
    try:
        _insert_prediction(connection, prediction_id="p-tuple")
        store.close()
        exported = _export_partitions(db_path)
        _register_exported(connection, exported)
        _delete_hot(connection, ["p-tuple"])
    finally:
        connection.close()

    tuples = sqlite3.connect(str(db_path))
    try:
        rows = read_prediction_rows(tuples, columns=("prediction_id", "payload_json"), order="recorded")
        assert [row["prediction_id"] for row in rows] == ["p-tuple"]
        assert "payload_json" in rows[0]
        replayed = cold_prediction_for_replay(tuples, "p-tuple")
        assert replayed is not None
        assert replayed["prediction_id"] == "p-tuple"
        assert replayed["payload_sha256"]
    finally:
        tuples.close()

    legacy = sqlite3.connect(":memory:")
    try:
        legacy.executescript(
            """
            CREATE TABLE world_shadow_predictions (
                prediction_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                episode_id TEXT NOT NULL,
                horizon_code TEXT NOT NULL,
                model_kind TEXT,
                model_version TEXT,
                predicted_at TEXT,
                input_sha256 TEXT,
                prediction_json TEXT NOT NULL,
                prediction_sha256 TEXT,
                payload_json TEXT NOT NULL,
                payload_sha256 TEXT,
                recorded_at TEXT NOT NULL
            );
            INSERT INTO world_shadow_predictions(
                prediction_id, run_id, episode_id, horizon_code, model_kind, model_version,
                predicted_at, input_sha256, prediction_json, prediction_sha256, payload_json,
                payload_sha256, recorded_at
            ) VALUES (
                'p-orphan', 'run-legacy', 'missing-episode', '4h', 'markov', 'v1',
                '2026-08-24T00:00:00+00:00', 'sha256:' || replace(hex(zeroblob(32)), '00', 'ab'),
                '{"ok":true}', 'sha256:' || replace(hex(zeroblob(32)), '00', 'cd'),
                '{"prediction_id":"p-orphan"}', 'sha256:' || replace(hex(zeroblob(32)), '00', 'ef'),
                '2026-08-24T01:00:00+00:00'
            );
            """
        )
        rows = read_prediction_rows(
            legacy,
            columns=("prediction_id", "payload_json", "study_cohort_id", "episode_observed_at"),
            order="recorded",
        )
        assert [row["prediction_id"] for row in rows] == ["p-orphan"]
        assert "study_cohort_id" not in rows[0]
        assert "episode_observed_at" not in rows[0]
        episode_order = read_prediction_rows(legacy, columns=("prediction_id",), order="episode")
        assert [row["prediction_id"] for row in episode_order] == ["p-orphan"]
    finally:
        legacy.close()

    with_episodes = sqlite3.connect(":memory:")
    try:
        with_episodes.executescript(
            """
            CREATE TABLE world_episodes (episode_id TEXT PRIMARY KEY, observed_at TEXT NOT NULL);
            CREATE TABLE world_shadow_predictions (
                prediction_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                episode_id TEXT NOT NULL,
                horizon_code TEXT NOT NULL,
                prediction_json TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                recorded_at TEXT NOT NULL
            );
            INSERT INTO world_episodes VALUES ('e-kept', '2026-08-24T00:00:00+00:00');
            INSERT INTO world_shadow_predictions VALUES (
                'p-kept', 'run-b', 'e-kept', '4h', '{}', '{}', '2026-08-24T02:00:00+00:00'
            );
            INSERT INTO world_shadow_predictions VALUES (
                'p-missing-ep', 'run-a', 'missing-episode', '4h', '{}', '{}', '2026-08-24T01:00:00+00:00'
            );
            """
        )
        recorded = read_prediction_rows(
            with_episodes, columns=("prediction_id", "episode_observed_at"), order="recorded"
        )
        assert [row["prediction_id"] for row in recorded] == ["p-missing-ep", "p-kept"]
        assert recorded[0]["episode_observed_at"] is None
        episode_order = read_prediction_rows(
            with_episodes, columns=("prediction_id", "episode_observed_at"), order="episode"
        )
        assert [row["prediction_id"] for row in episode_order] == ["p-missing-ep", "p-kept"]
    finally:
        with_episodes.close()


def test_direct_import_world_prediction_reader_does_not_circular_import() -> None:
    root = str(Path(__file__).resolve().parents[2])
    env = os.environ.copy()
    env["PYTHONPATH"] = root + os.pathsep + env.get("PYTHONPATH", "")
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from trader.infrastructure.state_db.world_prediction_reader import read_prediction_rows, cold_prediction_for_replay; "
            "from trader.infrastructure.state_db.world_prediction_tiers import read_prediction_rows as exported; "
            "assert read_prediction_rows is exported",
        ],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
