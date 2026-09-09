"""QUERY-lot integration for mixed SQLite+Parquet prediction reads."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from collections.abc import Mapping

from tests.domain.test_world_cohort import COHORT_ID, _manifest
from trader.infrastructure.state_db import world_model_query as query
from trader.infrastructure.state_db.world_model_query import (
    _PREDICTION_COHORT_FIELDS,
    _readonly_connection,
    read_world_cohort_catalog,
    read_world_cohort_ledger,
    read_world_model_ledger,
    read_world_pattern_ledger,
)
from trader.infrastructure.state_db.world_model_store import WorldModelStore
from trader.infrastructure.state_db.world_prediction_storage_lock import PredictionStorageBusyError
from trader.infrastructure.state_db.world_prediction_tiers import PredictionReadUnavailable
from trader.interfaces.cli.world_model import read_world_cohort_status
from trader.reporting.read_models.world_cohort import read_world_cohort_report
from trader.reporting.read_models.world_graph import read_world_graph_status
from trader.reporting.read_models.world_patterns import read_world_pattern_report, read_world_pattern_status
from trader.runtime import cli


_CANONICAL_PREDICTION_COLUMNS = (
    "prediction_id",
    "episode_id",
    "horizon_code",
    "model_kind",
    "model_version",
    "predicted_at",
    "recorded_at",
    "payload_json",
    *_PREDICTION_COHORT_FIELDS,
)
_RECORDED_AT = "2026-08-24T01:05:00+00:00"


def _prediction_row(**overrides: object) -> dict[str, Any]:
    payload = {
        "prediction_id": "pred-1",
        "episode_id": "ep-1",
        "horizon_code": "elapsed_1d.v1",
        "model_kind": "hierarchical_dirichlet_world_baseline",
        "model_version": "v1",
        "predicted_at": "2026-08-24T01:00:00+00:00",
        "recorded_at": _RECORDED_AT,
        "payload_json": json.dumps(
            {
                "prediction": {
                    "study_cohort_id": "json-should-lose",
                    "probabilities": {"DOWN": 0.2, "FLAT": 0.2, "UP": 0.6},
                }
            }
        ),
        "study_cohort_id": COHORT_ID,
        "lane_id": "markov.market",
        "manifest_sha256": "a" * 64,
        "feature_contract_fingerprint": "b" * 64,
        "feature_mask_fingerprint": "c" * 64,
    }
    payload.update(overrides)
    return payload


def _empty_store(tmp_path: Path) -> Path:
    db_path = tmp_path / "world_model.db"
    store = WorldModelStore(db_path)
    store.close()
    return db_path


def _registered_cohort(tmp_path: Path) -> tuple[Path, str]:
    from trader.application.world_model.cohort_service import WorldCohortService
    from trader.domain.world_cohort import RegisterWorldCohort

    db_path = tmp_path / "world_model.db"
    store = WorldModelStore(db_path)
    try:
        manifest = _manifest()
        WorldCohortService(repository=store, query=store).register(RegisterWorldCohort(manifest=manifest))
        return db_path, manifest.cohort_id
    finally:
        store.close()


def _install_reader(
    monkeypatch: pytest.MonkeyPatch,
    *,
    rows: list[dict[str, Any]] | None = None,
    lease=None,
    raise_read: BaseException | None = None,
    raise_lease: BaseException | None = None,
) -> dict[str, Any]:
    captured: dict[str, Any] = {"calls": [], "lease_held": False, "lease_calls": []}
    returned = list(rows) if rows is not None else [_prediction_row()]

    def fake_read(
        connection: sqlite3.Connection,
        *,
        columns,
        study_cohort_id=None,
        prediction_ids=None,
        episode_ids=None,
        run_id=None,
        horizon_code=None,
        order="recorded",
        limit=None,
    ) -> list[dict[str, Any]]:
        assert captured["lease_held"] is True
        connection.execute("SELECT 1")
        captured["calls"].append(
            {
                "columns": list(columns),
                "study_cohort_id": study_cohort_id,
                "prediction_ids": prediction_ids,
                "episode_ids": episode_ids,
                "run_id": run_id,
                "horizon_code": horizon_code,
                "order": order,
                "limit": limit,
            }
        )
        if raise_read is not None:
            raise raise_read
        return list(returned)

    @contextmanager
    def fake_lease(path: Path, *, create: bool = False):
        captured["lease_calls"].append({"path": Path(path), "create": create})
        assert create is False
        if raise_lease is not None:
            raise raise_lease
        captured["lease_held"] = True
        try:
            yield
        finally:
            captured["lease_held"] = False

    monkeypatch.setattr(query, "read_prediction_rows", fake_read)
    monkeypatch.setattr(query, "shared_prediction_storage_lease", lease or fake_lease)
    return captured


def _assert_unavailable(payload: Mapping[str, Any], *, error_type: str) -> None:
    assert payload["status"] == "unavailable"
    assert payload["exists"] is True
    assert str(payload["error"]).startswith(f"{error_type}:")
    assert payload["status"] != "loaded"


def test_three_ledgers_route_through_shared_reader_with_cohort_filter_and_columns(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _install_reader(monkeypatch)
    db_path, cohort_id = _registered_cohort(tmp_path)

    global_ledger = read_world_model_ledger(db_path)
    cohort_ledger = read_world_cohort_ledger(db_path, cohort_id)
    pattern_ledger = read_world_pattern_ledger(db_path, cohort_id)

    assert [item["order"] for item in captured["calls"]] == ["recorded", "recorded", "recorded"]
    assert captured["calls"][0]["study_cohort_id"] is None
    assert captured["calls"][1]["study_cohort_id"] == cohort_id
    assert captured["calls"][2]["study_cohort_id"] == cohort_id
    for item in captured["calls"]:
        assert item["columns"] == list(_CANONICAL_PREDICTION_COLUMNS)
        assert item["prediction_ids"] is None
        assert item["episode_ids"] is None
        assert item["run_id"] is None
        assert item["horizon_code"] is None
        assert item["limit"] is None

    for ledger in (global_ledger, cohort_ledger, pattern_ledger):
        assert ledger["status"] == "loaded"
        row = ledger["predictions"][0]
        assert row["prediction_id"] == "pred-1"
        assert row["ready_at"] == _RECORDED_AT
        assert row["recorded_at"] == _RECORDED_AT
        assert row["study_cohort_id"] == COHORT_ID
        assert row["prediction"]["study_cohort_id"] == COHORT_ID


def test_legacy_hot_table_filters_selected_columns(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _install_reader(monkeypatch, rows=[])
    db_path = tmp_path / "world_model.db"
    connection = sqlite3.connect(db_path)
    try:
        connection.execute("CREATE TABLE world_episodes (episode_id TEXT PRIMARY KEY, training_eligible INTEGER)")
        connection.execute(
            """
            CREATE TABLE world_outcome_events (
                outcome_event_id TEXT PRIMARY KEY,
                episode_id TEXT,
                horizon_code TEXT,
                status TEXT,
                move_class TEXT,
                training_eligible INTEGER,
                payload_json TEXT
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE world_shadow_predictions (
                prediction_id TEXT PRIMARY KEY,
                episode_id TEXT,
                horizon_code TEXT,
                model_kind TEXT,
                model_version TEXT,
                predicted_at TEXT,
                recorded_at TEXT,
                payload_json TEXT
            )
            """
        )
        connection.commit()
    finally:
        connection.close()

    ledger = read_world_model_ledger(db_path)
    assert ledger["status"] == "loaded"
    assert captured["calls"][0]["columns"] == [
        "prediction_id",
        "episode_id",
        "horizon_code",
        "model_kind",
        "model_version",
        "predicted_at",
        "recorded_at",
        "payload_json",
    ]


def test_missing_cold_is_unavailable_on_every_full_ledger_not_partial(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_reader(monkeypatch, raise_read=PredictionReadUnavailable("cold partition missing"))
    db_path, cohort_id = _registered_cohort(tmp_path)

    global_ledger = read_world_model_ledger(db_path)
    cohort_ledger = read_world_cohort_ledger(db_path, cohort_id)
    pattern_ledger = read_world_pattern_ledger(db_path, cohort_id)

    for ledger in (global_ledger, cohort_ledger, pattern_ledger):
        _assert_unavailable(ledger, error_type="PredictionReadUnavailable")
        assert ledger.get("status") != "loaded"
        assert "counts" not in ledger or ledger["status"] != "loaded"


def test_shared_lock_refusal_is_unavailable_including_catalog(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_reader(monkeypatch, raise_lease=PredictionStorageBusyError("exclusive cutover"))
    db_path, cohort_id = _registered_cohort(tmp_path)

    payloads = [
        read_world_model_ledger(db_path),
        read_world_cohort_ledger(db_path, cohort_id),
        read_world_pattern_ledger(db_path, cohort_id),
        read_world_cohort_catalog(db_path),
    ]
    for payload in payloads:
        _assert_unavailable(payload, error_type="PredictionStorageBusyError")
    assert payloads[3]["cohorts"] == []


def test_missing_database_does_not_acquire_lease_or_create_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[object] = []

    def boom(*_args: object, **_kwargs: object):
        calls.append(1)
        raise AssertionError("lease must not run for a missing database")

    monkeypatch.setattr(query, "shared_prediction_storage_lease", boom)
    db_path = tmp_path / "world_model.db"

    assert read_world_model_ledger(db_path)["status"] == "not_started"
    assert read_world_cohort_ledger(db_path, COHORT_ID)["status"] == "not_started"
    assert read_world_cohort_catalog(db_path)["status"] == "not_started"
    assert read_world_pattern_ledger(db_path, COHORT_ID)["status"] == "not_started"
    assert not db_path.exists()
    assert calls == []


def _connection_is_closed(connection: sqlite3.Connection) -> bool:
    try:
        connection.execute("SELECT 1")
    except sqlite3.ProgrammingError:
        return True
    return False


def test_readonly_connection_holds_lease_during_sqlite_read_and_closes_before_release(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = _empty_store(tmp_path)
    events: list[str] = []
    sqls: list[str] = []
    live: list[sqlite3.Connection] = []
    real_connect = sqlite3.connect

    @contextmanager
    def fake_lease(path: Path, *, create: bool = False):
        assert create is False
        assert Path(path) == db_path
        events.append("lease_enter")
        try:
            yield
        finally:
            assert live
            assert all(_connection_is_closed(connection) for connection in live)
            events.append("lease_exit")

    def tracking_connect(*args: object, **kwargs: object) -> sqlite3.Connection:
        connection = real_connect(*args, **kwargs)
        events.append("db_open")
        connection.set_trace_callback(lambda sql: sqls.append(sql))
        live.append(connection)
        return connection

    monkeypatch.setattr(query, "shared_prediction_storage_lease", fake_lease)
    monkeypatch.setattr(query.sqlite3, "connect", tracking_connect)

    with _readonly_connection(db_path) as connection:
        events.append("in_body")
        assert connection.in_transaction is True
        assert connection.execute("SELECT 1").fetchone()[0] == 1
        events.append("did_read")

    assert events == ["lease_enter", "db_open", "in_body", "did_read", "lease_exit"]
    assert all(_connection_is_closed(connection) for connection in live)
    assert any(item.strip().upper().startswith("BEGIN") for item in sqls)
    assert any("ROLLBACK" in item.upper() for item in sqls)


def test_readonly_connection_releases_lease_if_connect_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    @contextmanager
    def fake_lease(path: Path, *, create: bool = False):
        events.append("enter")
        try:
            yield
        finally:
            events.append("exit")

    monkeypatch.setattr(query, "shared_prediction_storage_lease", fake_lease)
    monkeypatch.setattr(
        query.sqlite3,
        "connect",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(sqlite3.OperationalError("cannot open")),
    )
    with pytest.raises(sqlite3.OperationalError, match="cannot open"):
        with _readonly_connection(tmp_path / "world_model.db"):
            raise AssertionError("body must not run")
    assert events == ["enter", "exit"]


def test_readonly_connection_releases_lease_if_query_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = _empty_store(tmp_path)
    events: list[str] = []

    @contextmanager
    def fake_lease(path: Path, *, create: bool = False):
        events.append("enter")
        try:
            yield
        finally:
            events.append("exit")

    monkeypatch.setattr(query, "shared_prediction_storage_lease", fake_lease)
    with pytest.raises(sqlite3.Error):
        with _readonly_connection(db_path) as connection:
            connection.execute("SELECT * FROM no_such_table")
    assert events == ["enter", "exit"]


def test_status_paths_skip_prediction_reader_reports_keep_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _install_reader(monkeypatch)
    db_path, cohort_id = _registered_cohort(tmp_path)
    state_dir = tmp_path

    cohort_status = read_world_cohort_status(state_dir, cohort_id)
    assert cohort_status["status"] == "loaded"
    assert cohort_status["phase"] == "registered"
    assert read_world_pattern_status(state_dir, cohort_id)["status"] == "loaded"
    assert captured["calls"] == []

    assert read_world_cohort_ledger(db_path, cohort_id)["predictions"]
    assert read_world_pattern_ledger(db_path, cohort_id)["predictions"]
    assert read_world_model_ledger(db_path)["predictions"]
    assert len(captured["calls"]) == 3

    captured["calls"].clear()
    assert read_world_cohort_report(state_dir, cohort_id)["status"] == "loaded"
    assert read_world_pattern_report(state_dir, cohort_id)["status"] == "loaded"
    assert len(captured["calls"]) == 2

    with pytest.raises(TypeError):
        read_world_cohort_ledger(db_path, cohort_id, False)  # type: ignore[misc]
    with pytest.raises(TypeError):
        read_world_pattern_ledger(db_path, cohort_id, False)  # type: ignore[misc]


def test_include_predictions_false_keeps_manifest_events_without_reader(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _install_reader(monkeypatch)
    db_path, cohort_id = _registered_cohort(tmp_path)

    ledger = read_world_cohort_ledger(db_path, cohort_id, include_predictions=False)
    assert captured["calls"] == []
    assert ledger["status"] == "loaded"
    assert ledger["manifest"]["cohort_id"] == cohort_id
    assert ledger["events"]
    assert ledger["predictions"] == []

    pattern = read_world_pattern_ledger(db_path, cohort_id, include_predictions=False)
    assert captured["calls"] == []
    assert pattern["status"] == "loaded"
    assert pattern["predictions"] == []


def test_graph_status_reuses_precomputed_world_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from trader.reporting.read_models import world_graph as graph_mod

    calls: list[object] = []

    def fake_world(state_dir: str | Path, **_kwargs: object) -> dict[str, Any]:
        calls.append(state_dir)
        return {"exists": True, "status": "loaded", "predictions_by_model": {"x.graph.v1": 9}}

    monkeypatch.setattr(graph_mod, "read_world_model_status", fake_world)
    precomputed = {
        "exists": True,
        "status": "warming_up",
        "predictions_by_model": {"pilot.graph.v1": 3, "baseline@v1": 8},
    }

    reused = read_world_graph_status(tmp_path, world_status=precomputed)
    assert calls == []
    assert reused["status"] == "warming_up"
    assert reused["exists"] is True
    assert reused["gaps"]["graph_predictions"] == 3

    fallback = read_world_graph_status(tmp_path)
    assert len(calls) == 1
    assert fallback["gaps"]["graph_predictions"] == 9


def test_world_status_cli_computes_world_once_and_preserves_graph_counts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from trader.interfaces.cli import world_model as world_cli
    from trader.reporting.read_models import world_graph as graph_mod

    world_payload = {
        "status": "loaded",
        "exists": True,
        "authority": "shadow_only",
        "counts": {"episodes": 2, "outcome_events": 1, "predictions": 5},
        "evaluation": {"matched": 1},
        "predictions_by_model": {"pilot.graph.v1": 4, "baseline@v1": 1},
    }
    monkeypatch.setattr(cli.daemon, "STATE_DIR", tmp_path)
    monkeypatch.setattr(world_cli, "read_world_model_status", lambda state_dir, **_kwargs: world_payload)

    def fail_if_called(state_dir: str | Path, **_kwargs: object) -> dict[str, Any]:
        raise AssertionError("graph must reuse the precomputed world status")

    monkeypatch.setattr(graph_mod, "read_world_model_status", fail_if_called)

    assert cli.main(["world", "status", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["counts"]["predictions"] == 5
    assert payload["graph"]["gaps"]["graph_predictions"] == 4
    assert payload["graph"]["status"] == "loaded"
