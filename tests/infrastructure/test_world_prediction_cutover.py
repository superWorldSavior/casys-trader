from __future__ import annotations

import json
import os
import sqlite3
import subprocess
from contextlib import closing
from datetime import date, datetime, timezone
from pathlib import Path

import duckdb
import pytest

from scripts.cutover_world_predictions import main as cutover_main
from tests.infrastructure.test_world_model_store import (
    DEFAULT_EPISODE_ID,
    _episode,
    _outcome,
    _prediction,
)
from trader.application.world_model.prediction_archive import WorldPredictionArchiveService
import trader.infrastructure.state_db.world_model_store as world_model_store
from trader.infrastructure.state_db.world_model_store import WorldModelStore
from trader.infrastructure.state_db.world_prediction_archive_source import (
    SQLiteWorldPredictionArchiveSource,
)
from trader.infrastructure.state_db.world_prediction_cutover import (
    PredictionCutoverFailpoint,
    prepare_prediction_cutover,
    resume_prediction_cutover,
)
from trader.infrastructure.state_db.world_prediction_parquet_store import (
    DuckDbWorldPredictionParquetStore,
)
from trader.infrastructure.state_db.world_prediction_storage_lock import (
    PredictionStorageBusyError,
    cutover_journal_path,
    exclusive_prediction_storage_lease,
    storage_lock_path,
)
from trader.infrastructure.state_db.world_prediction_tiers import (
    PredictionReadUnavailable,
    prediction_count,
    read_prediction_identities,
)

UTC = timezone.utc
BEFORE = date(2026, 8, 26)
CLOCK = datetime(2026, 8, 28, 12, tzinfo=UTC)
BLOB = "Q" * 12_000


def _clock() -> datetime:
    return CLOCK


def _set_now(monkeypatch: pytest.MonkeyPatch, recorded_at: str) -> None:
    monkeypatch.setattr(world_model_store, "_utc_now", lambda: recorded_at)


def _fat_prediction(prediction_id: str, **overrides: object) -> dict[str, object]:
    payload = _prediction(prediction_id)
    prediction = dict(payload["prediction"])  # type: ignore[arg-type]
    prediction["blob"] = BLOB
    payload["prediction"] = prediction
    payload.update(overrides)
    return payload


def _world_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db_path = tmp_path / "world_model.db"
    store = WorldModelStore(db_path)
    try:
        assert store.append_episode(_episode())
        assert store.append_legacy_outcome_event(_outcome())
        for index in range(4):
            _set_now(monkeypatch, "2026-08-24T0{0}:00:00+00:00".format(index + 1))
            assert store.append_legacy_prediction(_fat_prediction(f"p-24-{index}"))
        for index in range(4):
            _set_now(monkeypatch, "2026-08-25T0{0}:00:00+00:00".format(index + 1))
            assert store.append_legacy_prediction(_fat_prediction(f"p-25-{index}"))
        _set_now(monkeypatch, "2026-08-28T01:00:00+00:00")
        assert store.append_legacy_prediction(_prediction("p-live"))
    finally:
        store.close()
    return tmp_path


def _connect(db_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(str(db_path), timeout=30.0)
    connection.execute("PRAGMA busy_timeout=5000")
    return connection


def _hot_count(db_path: Path) -> int:
    connection = _connect(db_path)
    try:
        return int(connection.execute("SELECT COUNT(*) FROM world_shadow_predictions").fetchone()[0])
    finally:
        connection.close()


def _trigger_sql(db_path: Path, name: str = "world_predictions_no_delete") -> str:
    connection = _connect(db_path)
    try:
        row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?",
            (name,),
        ).fetchone()
    finally:
        connection.close()
    assert row is not None
    return str(row[0])


def _outcome_digest(db_path: Path) -> tuple[int, str]:
    import hashlib

    digest = hashlib.sha256()
    connection = _connect(db_path)
    try:
        rows = list(connection.execute("SELECT * FROM world_outcome_events ORDER BY outcome_event_id"))
        for row in rows:
            encoded = json.dumps(list(row), ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()
            digest.update(len(encoded).to_bytes(8, "big"))
            digest.update(encoded)
    finally:
        connection.close()
    return len(rows), digest.hexdigest()


def _operator_paths(state: Path, db_path: Path) -> set[str]:
    names: set[str] = set()
    journal = Path(cutover_journal_path(db_path))
    if journal.exists():
        names.add(journal.name)
    if (state / "world_model_archive").exists():
        names.add("world_model_archive")
    for path in state.glob("world_model.db.cutover-*"):
        names.add(path.name)
    return names


def test_preview_is_read_only_and_summarizes_closed_days(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _world_state(tmp_path, monkeypatch)
    db_path = state / "world_model.db"
    lock_before = Path(storage_lock_path(db_path)).exists()
    hot_before = _hot_count(db_path)
    outcomes_before = _outcome_digest(db_path)

    report = prepare_prediction_cutover(state, BEFORE, clock=_clock)

    assert report["ok"] is True
    assert report["mode"] == "preview"
    assert report["apply"] is False
    assert report["row_count"] == 8
    assert [item["recorded_date"] for item in report["eligible_days"]] == ["2026-08-24", "2026-08-25"]
    assert report["planned_backup_path"].endswith(".cutover-backup")
    assert report["planned_candidate_path"].endswith(".cutover-candidate")
    assert "sqlite_read_sidecars" in report
    assert not (state / "world_model_archive").exists()
    assert not Path(cutover_journal_path(db_path)).exists()
    if not lock_before:
        assert not Path(storage_lock_path(db_path)).exists()
    assert _operator_paths(state, db_path) == set()
    assert _hot_count(db_path) == hot_before == 9
    assert _outcome_digest(db_path) == outcomes_before


def test_preview_and_apply_reject_open_or_future_utc_day(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _world_state(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="open UTC day"):
        prepare_prediction_cutover(state, date(2026, 8, 29), clock=_clock)
    with pytest.raises(ValueError, match="open UTC day"):
        prepare_prediction_cutover(state, date(2099, 1, 1), apply=True, clock=_clock)
    assert not (state / "world_model_archive").exists()


def test_apply_refuses_live_pid(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state = _world_state(tmp_path, monkeypatch)
    (state / "daemon.pid").write_text(str(os.getpid()), encoding="utf-8")
    with pytest.raises(RuntimeError, match="live daemon"):
        prepare_prediction_cutover(state, BEFORE, apply=True, clock=_clock)


def test_apply_treats_permission_denied_pid_as_alive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _world_state(tmp_path, monkeypatch)
    (state / "daemon.pid").write_text("1", encoding="utf-8")
    monkeypatch.setattr(os, "kill", lambda pid, sig: (_ for _ in ()).throw(PermissionError("denied")))
    with pytest.raises(RuntimeError, match="live daemon"):
        prepare_prediction_cutover(state, BEFORE, apply=True, clock=_clock)


def test_apply_refuses_malformed_pid(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state = _world_state(tmp_path, monkeypatch)
    (state / "daemon.pid").write_text("not-a-pid", encoding="utf-8")
    with pytest.raises(RuntimeError, match="malformed"):
        prepare_prediction_cutover(state, BEFORE, apply=True, clock=_clock)


def test_apply_accepts_stale_pid(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state = _world_state(tmp_path, monkeypatch)
    proc = subprocess.Popen(["sleep", "30"])
    pid = proc.pid
    proc.kill()
    proc.wait()
    (state / "daemon.pid").write_text(str(pid), encoding="utf-8")
    report = prepare_prediction_cutover(state, BEFORE, apply=True, clock=_clock)
    assert report["ok"] is True
    assert report["mode"] == "prepare"
    assert report["offline"]["stale_pid"] is True


def test_apply_refuses_live_exclusive_lease(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state = _world_state(tmp_path, monkeypatch)
    db_path = state / "world_model.db"
    with exclusive_prediction_storage_lease(db_path):
        with pytest.raises(PredictionStorageBusyError):
            prepare_prediction_cutover(state, BEFORE, apply=True, clock=_clock)


def test_first_cutover_allows_initialized_empty_cold_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _world_state(tmp_path, monkeypatch)
    db_path = state / "world_model.db"
    connection = _connect(db_path)
    try:
        tables = {str(row[0]) for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "world_prediction_cold_partitions" in tables
        assert "world_prediction_cold_index" in tables
        assert connection.execute("SELECT COUNT(*) FROM world_prediction_cold_partitions").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM world_prediction_cold_index").fetchone()[0] == 0
    finally:
        connection.close()
    adopted = prepare_prediction_cutover(state, BEFORE, apply=True, adopt=True, clock=_clock)
    assert adopted["ok"] is True
    assert _hot_count(db_path) == 1
    with sqlite3.connect(db_path) as connection:
        assert prediction_count(connection) == 9
        assert connection.execute("SELECT COUNT(*) FROM world_prediction_cold_partitions").fetchone()[0] == 2
        assert connection.execute("SELECT COUNT(*) FROM world_prediction_cold_index").fetchone()[0] == 8


def test_prepare_leaves_source_canonical_and_adopt_compacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _world_state(tmp_path, monkeypatch)
    db_path = state / "world_model.db"
    source_before = db_path.read_bytes()
    outcomes_before = _outcome_digest(db_path)
    trigger_before = _trigger_sql(db_path)
    source_size = db_path.stat().st_size

    prepared = prepare_prediction_cutover(state, BEFORE, apply=True, clock=_clock)
    assert prepared["mode"] == "prepare"
    assert prepared["source_retained"] is True
    assert Path(prepared["backup_path"]).is_file()
    assert Path(prepared["candidate_path"]).is_file()
    assert not Path(cutover_journal_path(db_path)).exists()
    assert db_path.read_bytes() == source_before
    assert _hot_count(db_path) == 9
    candidate = Path(prepared["candidate_path"])
    assert _hot_count(candidate) == 1
    with sqlite3.connect(candidate) as connection:
        assert connection.execute("SELECT COUNT(*) FROM world_prediction_cold_index").fetchone()[0] == 8
        assert prediction_count(connection) == 9
        identities = read_prediction_identities(connection)
    assert len(identities) >= 1
    assert _outcome_digest(candidate) == outcomes_before
    assert "append-only" in _trigger_sql(candidate)
    assert _trigger_sql(candidate) == trigger_before
    with sqlite3.connect(candidate) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("DELETE FROM world_shadow_predictions")
            connection.commit()

    adopted = prepare_prediction_cutover(state, BEFORE, apply=True, adopt=True, clock=_clock)
    assert adopted["mode"] == "adopt"
    assert adopted["ok"] is True
    assert not Path(cutover_journal_path(db_path)).exists()
    assert Path(adopted["backup_path"]).is_file()
    assert _hot_count(db_path) == 1
    with sqlite3.connect(db_path) as connection:
        assert prediction_count(connection) == 9
        assert connection.execute("SELECT COUNT(*) FROM world_episodes").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM world_outcome_events").fetchone()[0] == 1
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("DELETE FROM world_shadow_predictions")
            connection.commit()
    assert _outcome_digest(db_path) == outcomes_before
    assert _trigger_sql(db_path) == trigger_before
    assert db_path.stat().st_size < source_size


def test_damaged_and_divergent_archives_block_without_quarantine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _world_state(tmp_path, monkeypatch)
    db_path = state / "world_model.db"
    archive_root = state / "world_model_archive"
    source = SQLiteWorldPredictionArchiveSource(db_path)
    sink = DuckDbWorldPredictionParquetStore(archive_root, clock=_clock)
    plan = source.plan_before(BEFORE)[0]
    manifest = sink.publish(plan, source.iter_partition(plan))
    parquet = archive_root / manifest.relative_path
    original = parquet.read_bytes()
    parquet.write_bytes(original[:32])

    with pytest.raises(RuntimeError, match="archive proof mismatch|authoritative"):
        prepare_prediction_cutover(state, BEFORE, apply=True, clock=_clock)
    assert parquet.read_bytes() == original[:32]
    assert list(archive_root.rglob("quarantine.json")) == []
    assert _hot_count(db_path) == 9

    parquet.write_bytes(original)
    other = tmp_path / "other.parquet"
    duckdb.connect(":memory:").execute(
        "COPY (SELECT * REPLACE (replace(payload_json, '}', ',\"tamper\":true}') AS payload_json) "
        f"FROM read_parquet('{parquet}', hive_partitioning=false)) "
        f"TO '{other}' (FORMAT PARQUET, COMPRESSION ZSTD)"
    )
    tampered = other.read_bytes()
    parquet.write_bytes(tampered)
    digest = __import__("hashlib").sha256(tampered).hexdigest()
    payload = json.loads((parquet.parent / "manifest.json").read_text(encoding="utf-8"))
    payload["parquet_sha256"] = f"sha256:{digest}"
    payload["parquet_bytes"] = len(tampered)
    rows = duckdb.connect(":memory:").execute(
        f"SELECT * FROM read_parquet('{parquet}', hive_partitioning=false) ORDER BY recorded_at, prediction_id"
    ).fetchall()
    content = __import__("hashlib").sha256()
    for row in rows:
        encoded = json.dumps(
            [value.hex() if isinstance(value, bytes) else value for value in row],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8") + b"\n"
        content.update(encoded)
    payload["content_sha256"] = f"sha256:{content.hexdigest()}"
    (parquet.parent / "manifest.json").write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="divergent archive/sqlite|archive proof mismatch"):
        prepare_prediction_cutover(state, BEFORE, apply=True, resume=True, clock=_clock)
    assert parquet.read_bytes() == tampered
    assert list(archive_root.rglob("quarantine.json")) == []


def test_missing_canonical_partition_after_adopt_blocks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _world_state(tmp_path, monkeypatch)
    db_path = state / "world_model.db"
    prepare_prediction_cutover(state, BEFORE, apply=True, adopt=True, clock=_clock)
    archive = state / "world_model_archive"
    _set_now(monkeypatch, "2026-08-26T01:00:00+00:00")
    store = WorldModelStore(db_path)
    try:
        store.append_legacy_prediction(_fat_prediction("p-26-0"))
    finally:
        store.close()
    parquet = next(archive.rglob("part-00000.parquet"))
    parquet.unlink()

    with pytest.raises(PredictionReadUnavailable):
        prepare_prediction_cutover(state, date(2026, 8, 27), apply=True, clock=_clock)


def test_failpoints_resume_including_after_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _world_state(tmp_path, monkeypatch)
    db_path = state / "world_model.db"
    journal = Path(cutover_journal_path(db_path))

    with pytest.raises(PredictionCutoverFailpoint, match="snapshot"):
        prepare_prediction_cutover(state, BEFORE, apply=True, adopt=True, clock=_clock, failpoint="after_snapshot")
    assert journal.is_file()
    resumed = prepare_prediction_cutover(state, BEFORE, apply=True, adopt=True, resume=True, clock=_clock)
    assert resumed["ok"] is True
    assert resumed["mode"] == "adopt"
    assert not journal.exists()
    assert _hot_count(db_path) == 1

    fresh = tmp_path / "fresh"
    fresh.mkdir()
    _world_state(fresh, monkeypatch)
    fresh_db = fresh / "world_model.db"
    with pytest.raises(PredictionCutoverFailpoint, match="swap"):
        prepare_prediction_cutover(
            fresh, BEFORE, apply=True, adopt=True, clock=_clock, failpoint="after_swapped"
        )
    assert Path(cutover_journal_path(fresh_db)).is_file()
    with sqlite3.connect(fresh_db) as connection:
        generations = list(
            connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='world_prediction_cutover_generations'"
            )
        )
    assert generations
    finalized = resume_prediction_cutover(fresh, adopt=True, clock=_clock)
    assert finalized["ok"] is True
    assert finalized["mode"] == "adopt"
    assert not Path(cutover_journal_path(fresh_db)).exists()
    assert _hot_count(fresh_db) == 1


def test_repeated_cutover_on_existing_cold_is_safe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _world_state(tmp_path, monkeypatch)
    db_path = state / "world_model.db"
    first = prepare_prediction_cutover(state, BEFORE, apply=True, adopt=True, clock=_clock)
    cold_before = 0
    with sqlite3.connect(db_path) as connection:
        cold_before = int(connection.execute("SELECT COUNT(*) FROM world_prediction_cold_partitions").fetchone()[0])
    repeated = prepare_prediction_cutover(state, BEFORE, apply=True, adopt=True, clock=_clock)
    assert repeated["ok"] is True
    with sqlite3.connect(db_path) as connection:
        cold_after = int(connection.execute("SELECT COUNT(*) FROM world_prediction_cold_partitions").fetchone()[0])
        assert prediction_count(connection) == 9
    assert cold_after == cold_before
    assert _hot_count(db_path) == 1
    assert Path(first["backup_path"]).is_file()
    assert Path(repeated["backup_path"]).is_file()
    assert Path(first["backup_path"]) != Path(repeated["backup_path"])


def test_exporter_cannot_quarantine_canonical_date_after_cutover(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _world_state(tmp_path, monkeypatch)
    db_path = state / "world_model.db"
    prepare_prediction_cutover(state, BEFORE, apply=True, adopt=True, clock=_clock)
    archive_root = state / "world_model_archive"
    parquet = next(p for p in archive_root.rglob("part-00000.parquet") if "2026-08-24" in str(p))
    original = parquet.read_bytes()
    _set_now(monkeypatch, "2026-08-24T12:00:00+00:00")
    store = WorldModelStore(db_path)
    try:
        store.append_legacy_prediction(_prediction("p-late-24", episode_id=DEFAULT_EPISODE_ID))
    finally:
        store.close()
    service = WorldPredictionArchiveService(
        source=SQLiteWorldPredictionArchiveSource(db_path),
        sink=DuckDbWorldPredictionParquetStore(archive_root, clock=_clock),
        clock=_clock,
    )
    report = service.execute(before=BEFORE, apply=True)
    statuses = [item["status"] for item in report["partitions"]]
    assert "canonical_active" in statuses
    assert "rebuilt" not in statuses
    canonical = next(item for item in report["partitions"] if item["status"] == "canonical_active")
    assert canonical["late_hot_count"] == 1
    assert parquet.read_bytes() == original
    assert _hot_count(db_path) == 2


def test_cli_dry_run_default_and_resume_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    state = _world_state(tmp_path, monkeypatch)
    code = cutover_main(["--state-dir", str(state), "--before", "2026-08-26"], clock=_clock)
    preview = json.loads(capsys.readouterr().out)
    assert code == 0
    assert preview["ok"] is True
    assert preview["mode"] == "preview"
    assert not (state / "world_model_archive").exists()

    code = cutover_main(
        ["--state-dir", str(state), "--before", "2026-08-29"],
        clock=_clock,
    )
    failed = json.loads(capsys.readouterr().out)
    assert code == 1
    assert failed["ok"] is False
    assert "open UTC day" in failed["error"]


def _wal_only_insert_marker(db_path: Path, version: int = 999999) -> None:
    connection = sqlite3.connect(str(db_path))
    try:
        mode = connection.execute("PRAGMA journal_mode=WAL").fetchone()
        assert str(mode[0]).lower() == "wal"
        connection.execute(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
            (version, "2026-08-28T12:00:00+00:00"),
        )
        connection.commit()
    finally:
        connection.close()


def _marker_present(db_path: Path, version: int = 999999) -> bool:
    connection = _connect(db_path)
    try:
        row = connection.execute("SELECT 1 FROM schema_migrations WHERE version=?", (version,)).fetchone()
    finally:
        connection.close()
    return row is not None


def test_wal_only_source_mutation_refuses_resume_without_adopting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _world_state(tmp_path, monkeypatch)
    db_path = state / "world_model.db"
    with pytest.raises(PredictionCutoverFailpoint, match="snapshot"):
        prepare_prediction_cutover(state, BEFORE, apply=True, adopt=True, clock=_clock, failpoint="after_snapshot")
    journal = Path(cutover_journal_path(db_path))
    with closing(sqlite3.connect(db_path)) as writer:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        assert writer.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] == 0
        main_before = db_path.read_bytes()
        writer.execute(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
            (999999, "2026-08-28T12:00:00+00:00"),
        )
        writer.commit()
        assert db_path.read_bytes() == main_before
        assert Path(str(db_path) + "-wal").stat().st_size > 0
        assert journal.is_file()
        with pytest.raises(RuntimeError, match="frozen backup|diverged|fingerprint"):
            prepare_prediction_cutover(state, BEFORE, apply=True, adopt=True, resume=True, clock=_clock)
        assert db_path.read_bytes() == main_before
    assert journal.is_file()
    assert _hot_count(db_path) == 9
    assert _marker_present(db_path)
    assert Path(json.loads(journal.read_text(encoding="utf-8"))["backup_path"]).is_file()


def test_after_pre_swap_source_or_candidate_change_refuses_and_keeps_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_state = tmp_path / "source"
    source_state.mkdir()
    _world_state(source_state, monkeypatch)
    source_db = source_state / "world_model.db"
    with pytest.raises(PredictionCutoverFailpoint, match="pre_swap"):
        prepare_prediction_cutover(
            source_state, BEFORE, apply=True, adopt=True, clock=_clock, failpoint="after_pre_swap"
        )
    _wal_only_insert_marker(source_db, version=888888)
    journal = Path(cutover_journal_path(source_db))
    assert journal.is_file()
    with pytest.raises(RuntimeError, match="frozen backup|diverged|fingerprint"):
        resume_prediction_cutover(source_state, adopt=True, clock=_clock)
    assert journal.is_file()
    assert _hot_count(source_db) == 9
    assert _marker_present(source_db, 888888)

    candidate_state = tmp_path / "candidate"
    candidate_state.mkdir()
    _world_state(candidate_state, monkeypatch)
    candidate_db = candidate_state / "world_model.db"
    with pytest.raises(PredictionCutoverFailpoint, match="pre_swap"):
        prepare_prediction_cutover(
            candidate_state, BEFORE, apply=True, adopt=True, clock=_clock, failpoint="after_pre_swap"
        )
    payload = json.loads(Path(cutover_journal_path(candidate_db)).read_text(encoding="utf-8"))
    candidate_path = Path(payload["candidate_path"])
    connection = sqlite3.connect(str(candidate_path))
    try:
        connection.execute(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (777777, '2026-08-28T12:00:00+00:00')"
        )
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(RuntimeError, match="parity|fingerprint|generation|changed"):
        resume_prediction_cutover(candidate_state, adopt=True, clock=_clock)
    assert Path(cutover_journal_path(candidate_db)).is_file()
    assert _hot_count(candidate_db) == 9
    assert not _marker_present(candidate_db, 777777)


def test_checkpoint_crash_gap_resume_uses_logical_backup_not_initial_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _world_state(tmp_path, monkeypatch)
    db_path = state / "world_model.db"
    with pytest.raises(PredictionCutoverFailpoint, match="source_checkpoint"):
        prepare_prediction_cutover(
            state, BEFORE, apply=True, adopt=True, clock=_clock, failpoint="after_source_checkpoint"
        )
    journal = Path(cutover_journal_path(db_path))
    payload = json.loads(journal.read_text(encoding="utf-8"))
    assert payload["stage"] == "verified"
    resumed = resume_prediction_cutover(state, adopt=True, clock=_clock)
    assert resumed["ok"] is True
    assert resumed["mode"] == "adopt"
    assert not journal.exists()
    assert _hot_count(db_path) == 1
    with sqlite3.connect(db_path) as connection:
        assert prediction_count(connection) == 9


def test_after_swapped_corrupt_archive_preserves_unresolved_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _world_state(tmp_path, monkeypatch)
    db_path = state / "world_model.db"
    with pytest.raises(PredictionCutoverFailpoint, match="swap"):
        prepare_prediction_cutover(state, BEFORE, apply=True, adopt=True, clock=_clock, failpoint="after_swapped")
    journal = Path(cutover_journal_path(db_path))
    assert journal.is_file()
    parquet = next((state / "world_model_archive").rglob("part-00000.parquet"))
    parquet.write_bytes(parquet.read_bytes()[:32])
    with pytest.raises((PredictionReadUnavailable, RuntimeError)):
        resume_prediction_cutover(state, adopt=True, clock=_clock)
    assert journal.is_file()
    assert _hot_count(db_path) == 1


def test_repeated_cutover_appends_new_closed_day_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _world_state(tmp_path, monkeypatch)
    db_path = state / "world_model.db"
    first = prepare_prediction_cutover(state, BEFORE, apply=True, adopt=True, clock=_clock)
    assert first["ok"] is True
    with sqlite3.connect(db_path) as connection:
        first_partitions = int(connection.execute("SELECT COUNT(*) FROM world_prediction_cold_partitions").fetchone()[0])
        first_index = int(connection.execute("SELECT COUNT(*) FROM world_prediction_cold_index").fetchone()[0])
        first_generations = int(
            connection.execute("SELECT COUNT(*) FROM world_prediction_cutover_generations").fetchone()[0]
        )
    _set_now(monkeypatch, "2026-08-26T01:00:00+00:00")
    store = WorldModelStore(db_path)
    try:
        assert store.append_legacy_prediction(_fat_prediction("p-26-0"))
    finally:
        store.close()
    second = prepare_prediction_cutover(state, date(2026, 8, 27), apply=True, adopt=True, clock=_clock)
    assert second["ok"] is True
    assert _hot_count(db_path) == 1
    with sqlite3.connect(db_path) as connection:
        assert prediction_count(connection) == 10
        assert connection.execute("SELECT COUNT(*) FROM world_prediction_cold_partitions").fetchone()[0] == first_partitions + 1
        assert connection.execute("SELECT COUNT(*) FROM world_prediction_cold_index").fetchone()[0] == first_index + 1
        assert (
            connection.execute("SELECT COUNT(*) FROM world_prediction_cutover_generations").fetchone()[0]
            == first_generations + 1
        )
        ids = {
            str(row[0])
            for row in connection.execute("SELECT prediction_id FROM world_prediction_cold_index")
        }
        assert "p-26-0" in ids
        assert "p-24-0" in ids


def test_resume_after_source_mutation_does_not_restore_stale_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _world_state(tmp_path, monkeypatch)
    db_path = state / "world_model.db"
    with pytest.raises(PredictionCutoverFailpoint, match="snapshot"):
        prepare_prediction_cutover(state, BEFORE, apply=True, adopt=True, clock=_clock, failpoint="after_snapshot")
    _wal_only_insert_marker(db_path, version=424242)
    with pytest.raises(RuntimeError, match="frozen backup|diverged|fingerprint"):
        resume_prediction_cutover(state, adopt=True, clock=_clock)
    assert _hot_count(db_path) == 9
    assert _marker_present(db_path, 424242)
    journal = json.loads(Path(cutover_journal_path(db_path)).read_text(encoding="utf-8"))
    backup = Path(journal["backup_path"])
    assert backup.is_file()
    with sqlite3.connect(backup) as connection:
        assert connection.execute("SELECT 1 FROM schema_migrations WHERE version=424242").fetchone() is None


def test_logical_ids_dedup_exact_hot_cold_overlap(tmp_path: Path) -> None:
    from trader.infrastructure.state_db.world_prediction_cutover import _ordered_prediction_ids

    db_path = tmp_path / "overlap.db"
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "CREATE TABLE world_shadow_predictions (prediction_id TEXT PRIMARY KEY, recorded_at TEXT NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE world_prediction_cold_index (prediction_id TEXT PRIMARY KEY, recorded_at TEXT NOT NULL)"
        )
        connection.execute("INSERT INTO world_shadow_predictions VALUES ('p-shared', '2026-08-24T01:00:00+00:00')")
        connection.execute("INSERT INTO world_shadow_predictions VALUES ('p-hot', '2026-08-24T02:00:00+00:00')")
        connection.execute("INSERT INTO world_prediction_cold_index VALUES ('p-shared', '2026-08-24T01:00:00+00:00')")
        connection.execute("INSERT INTO world_prediction_cold_index VALUES ('p-cold', '2026-08-24T00:00:00+00:00')")
        ids = _ordered_prediction_ids(connection)
    assert ids == ["p-cold", "p-shared", "p-hot"]


@pytest.mark.parametrize("resume_after_swap", [False, True])
@pytest.mark.parametrize(
    ("before", "hot_count", "cold_count"),
    [(date(2026, 8, 24), 9, 0), (date(2026, 8, 29), 0, 9)],
)
def test_adopt_and_resume_accept_an_empty_storage_tier(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    before: date,
    hot_count: int,
    cold_count: int,
    resume_after_swap: bool,
) -> None:
    state = _world_state(tmp_path, monkeypatch)
    clock = datetime(2026, 8, 30, tzinfo=UTC)
    if resume_after_swap:
        with pytest.raises(PredictionCutoverFailpoint, match="swapped"):
            prepare_prediction_cutover(
                state, before, apply=True, adopt=True, clock=clock, failpoint="after_swapped"
            )
        report = resume_prediction_cutover(state, adopt=True, clock=clock)
    else:
        report = prepare_prediction_cutover(state, before, apply=True, adopt=True, clock=clock)
    assert report["ok"] is True
    assert report["parity"]["hot_count"] == hot_count
    assert report["parity"]["cold_count"] == cold_count
    assert not Path(cutover_journal_path(state / "world_model.db")).exists()
    with closing(sqlite3.connect(state / "world_model.db")) as connection:
        assert prediction_count(connection) == 9


def test_adopt_initializes_cold_schema_on_a_legacy_world_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _world_state(tmp_path, monkeypatch)
    db_path = state / "world_model.db"
    with closing(sqlite3.connect(db_path)) as connection:
        connection.execute("DROP TRIGGER world_shadow_predictions_no_cold_identity")
        connection.execute("DROP TABLE world_prediction_cold_index")
        connection.execute("DROP TABLE world_prediction_cold_partitions")
        connection.commit()
    report = prepare_prediction_cutover(state, BEFORE, apply=True, adopt=True, clock=_clock)
    assert report["ok"] is True
    assert report["parity"]["hot_count"] == 1
    assert report["parity"]["cold_count"] == 8
    with closing(sqlite3.connect(db_path)) as connection:
        connection.execute("ATTACH DATABASE ? AS original", (report["backup_path"],))
        with pytest.raises(sqlite3.IntegrityError, match="already exists in cold storage"):
            connection.execute(
                "INSERT INTO world_shadow_predictions SELECT * FROM original.world_shadow_predictions "
                "WHERE prediction_id = 'p-24-0'"
            )
