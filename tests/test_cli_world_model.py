from __future__ import annotations

import json
from pathlib import Path

from tests.domain.test_world_cohort import COHORT_ID, _manifest
from trader.domain.world_episode import (
    MARKET_FEATURE_CONTRACT_ID,
    AnchorBar,
    WorldEpisode,
    WorldObservation,
    canonical_sha256,
)
from trader.interfaces.cli.world_model import read_world_model_status
from trader.runtime import cli

_CLAIM = {
    "authority": "shadow_only",
    "decision_effect": "none",
    "recommendation": "NO_GO",
    "causal_claim": False,
    "pnl_claim": False,
    "actual_trader_contribution": "not_attributable",
}


def _write_manifest(directory: Path, **overrides: object) -> Path:
    path = directory / "cohort-manifest.json"
    path.write_text(json.dumps(_manifest(**overrides).to_dict()), encoding="utf-8")
    return path


def _run_json(monkeypatch, capsys, tmp_path: Path, argv: list[str]) -> tuple[int, dict]:
    monkeypatch.setattr(cli.daemon, "STATE_DIR", tmp_path)
    code = cli.main(argv)
    captured = capsys.readouterr()
    assert captured.out, captured.err
    return code, json.loads(captured.out)


def _assert_claims(payload: dict) -> None:
    for key, expected in _CLAIM.items():
        assert payload[key] == expected


def _db_path(tmp_path: Path) -> Path:
    return tmp_path / "world_model.db"


def _market_episode(*, venue: str = "US", symbol: str = "SPY", as_of: str = "2026-08-22T00:00:00+00:00") -> dict:
    observation = WorldObservation(
        venue=venue,
        symbol=symbol,
        bar_interval="1h",
        as_of_bar_ts=as_of,
        feature_contract_version=MARKET_FEATURE_CONTRACT_ID,
        sampling_policy_version="cycle_snapshot.v1",
        anchor=AnchorBar(
            ts=as_of,
            open=100.0,
            high=102.0,
            low=99.0,
            close=101.0,
            volume=1_000.0,
            source="fixture",
        ),
        available_at=as_of,
        captured_at=as_of,
        freshness="fresh",
        numeric_features={"return": 0.01},
    )
    return WorldEpisode(observation=observation).to_dict()


def test_world_status_does_not_create_a_missing_database(tmp_path) -> None:
    result = read_world_model_status(tmp_path)

    assert result["status"] == "not_started"
    assert result["authority"] == "shadow_only"
    assert result["counts"]["episodes"] == 0
    assert result["predictions_by_model"] == {}
    assert result["evaluation"]["status"] == "warming_up"
    assert result["impact"]["actual_contribution"]["status"] == "not_attributable"
    assert not (tmp_path / "world_model.db").exists()
    budget = result["resource_budget"]
    assert budget["authority"] == "shadow_only"
    assert budget["decision_effect"] == "none"
    assert budget["status"] in {"allowed", "skipped", "unavailable"}
    assert not (tmp_path / "world_model.db").exists()


def test_world_status_reads_an_empty_dedicated_store(tmp_path) -> None:
    from trader.infrastructure.state_db.world_model_store import WorldModelStore

    store = WorldModelStore(tmp_path / "world_model.db")
    store.close()

    result = read_world_model_status(tmp_path)

    assert result["status"] == "warming_up"
    assert result["counts"] == {
        "episodes": 0,
        "eligible_episodes": 0,
        "outcome_events": 0,
        "active_outcomes": 0,
        "predictions": 0,
    }
    assert result["predictions_by_model"] == {}
    assert result["evaluation"]["status"] == "warming_up"
    assert result["impact"]["actual_contribution"]["status"] == "not_attributable"


def test_world_status_compares_persisted_baseline_and_gru_predictions(tmp_path, monkeypatch) -> None:
    from trader.infrastructure.state_db import world_model_store
    from trader.infrastructure.state_db.world_model_store import WorldModelStore

    store = WorldModelStore(tmp_path / "world_model.db")
    episode = _market_episode()
    episode_id = episode["episode_id"]
    assert store.append_episode(episode)
    assert store.append_outcome_event(
        {
            "outcome_event_id": "outcome-1",
            "episode_id": episode_id,
            "horizon_code": "elapsed_4h.v1",
            "status": "observed",
            "move_class": "UP",
            "training_eligible": True,
            "label_available_at": "2026-08-22T04:00:00+00:00",
            "sealed_at": "2026-08-22T04:00:00+00:00",
            "source_raw_sha256": "d" * 64,
            "label": {
                "simple_return": 0.01,
                "move_class": "UP",
                "source_raw_sha256": "d" * 64,
            },
            "evidence": {"source": "fixture", "source_raw_sha256": "d" * 64},
        }
    )
    # ``ready_at`` is derived from the immutable storage ``recorded_at`` by
    # the CLI, not supplied by a prediction payload.  Freeze that append clock
    # before the 04:00Z label so this fixture proves a causal forecast.
    monkeypatch.setattr(
        world_model_store,
        "_utc_now",
        lambda: "2026-08-22T00:01:00+00:00",
    )
    for model_id, probabilities in (
        ("hierarchical_dirichlet_world_baseline", {"DOWN": 0.2, "FLAT": 0.3, "UP": 0.5}),
        ("online_gru_world_challenger", {"DOWN": 0.1, "FLAT": 0.2, "UP": 0.7}),
    ):
        assert store.append_prediction(
            {
                "prediction_id": f"prediction-{model_id}",
                "run_id": "world_shadow.v1",
                "episode_id": episode_id,
                "horizon_code": "elapsed_4h.v1",
                "model_kind": model_id,
                "model_version": "v1",
                "predicted_at": "2026-08-22T00:00:00+00:00",
                "input": {"return": 0.01},
                "prediction": {
                    "model_id": model_id,
                    "model_version": "v1",
                    "probabilities": probabilities,
                    "predicted_class": "UP",
                },
            }
        )
    store.close()

    result = read_world_model_status(tmp_path)

    assert result["predictions_by_model"] == {
        "hierarchical_dirichlet_world_baseline@v1": 1,
        "online_gru_world_challenger@v1": 1,
    }
    assert len(result["evaluation"]["groups"]) == 2
    assert result["evaluation"]["comparisons"][0]["matched_pairs"] == 1
    assert result["evaluation"]["comparisons"][0]["status"] == "insufficient_support"
    assert result["impact"]["market"]["matched"] == 2


def test_world_status_uses_indexed_model_and_direction_over_payload_claims(tmp_path, monkeypatch) -> None:
    from trader.infrastructure.state_db import world_model_store
    from trader.infrastructure.state_db.world_model_store import WorldModelStore

    store = WorldModelStore(tmp_path / "world_model.db")
    episode = _market_episode()
    episode_id = episode["episode_id"]
    assert store.append_episode(episode)
    assert store.append_outcome_event(
        {
            "outcome_event_id": "outcome-index-authority",
            "episode_id": episode_id,
            "horizon_code": "elapsed_4h.v1",
            # This is the indexed authority, intentionally contradicting the
            # payload's top-level and nested direction claims below.
            "move_class": "UP",
            "direction": "DOWN",
            "status": "observed",
            "training_eligible": True,
            "label_available_at": "2026-08-22T04:00:00+00:00",
            "sealed_at": "2026-08-22T04:00:00+00:00",
            "source_raw_sha256": "e" * 64,
            "label": {
                "simple_return": 0.01,
                "move_class": "DOWN",
                "direction": "DOWN",
                "source_raw_sha256": "e" * 64,
            },
            "evidence": {"source": "fixture", "source_raw_sha256": "e" * 64},
        }
    )
    monkeypatch.setattr(
        world_model_store,
        "_utc_now",
        lambda: "2026-08-22T00:01:00+00:00",
    )
    assert store.append_prediction(
        {
            "prediction_id": "prediction-index-authority",
            "run_id": "world_shadow.v1",
            "episode_id": episode_id,
            "horizon_code": "elapsed_4h.v1",
            # This indexed model identity must win over both payload claims.
            "model_kind": "hierarchical_dirichlet_world_baseline",
            "model_id": "payload-claimed-model",
            "model_version": "v1",
            "predicted_at": "2026-08-22T00:00:00+00:00",
            "input": {"return": 0.01},
            "prediction": {
                "model_id": "payload-claimed-model",
                "model_version": "v1",
                "probabilities": {"DOWN": 0.8, "FLAT": 0.1, "UP": 0.1},
                "predicted_class": "DOWN",
            },
        }
    )
    store.close()

    result = read_world_model_status(tmp_path)

    group = result["evaluation"]["groups"]
    assert len(group) == 1
    assert group[0]["model_id"] == "hierarchical_dirichlet_world_baseline"
    # DOWN was predicted, but the indexed move_class is UP: the authoritative
    # envelope must therefore score this row as incorrect.
    assert group[0]["accuracy"] == 0.0
    impact_group = result["impact"]["market"]["groups"]
    assert len(impact_group) == 1
    assert impact_group[0]["model_id"] == "hierarchical_dirichlet_world_baseline"
    assert impact_group[0]["calibration"]["accuracy"] == 0.0


def test_cli_world_status_json_is_machine_readable(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli.daemon, "STATE_DIR", tmp_path)

    assert cli.main(["world", "status", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "not_started"
    assert payload["decision_effect"] == "none"


def test_world_cohort_validate_is_read_only_and_machine_readable(tmp_path, monkeypatch, capsys) -> None:
    manifest_path = _write_manifest(tmp_path)
    expected = _manifest()

    code, payload = _run_json(
        monkeypatch,
        capsys,
        tmp_path,
        ["world", "cohort", "validate", "--manifest", str(manifest_path), "--json"],
    )

    assert code == 0
    assert payload["ok"] is True
    assert payload["command"] == "validate"
    assert payload["cohort_id"] == expected.cohort_id
    assert payload["manifest_sha256"] == expected.manifest_sha256
    assert payload["study_kind"] == "pipeline_pilot"
    _assert_claims(payload)
    assert not _db_path(tmp_path).exists()
    assert list(tmp_path.glob("world_model.db*")) == []


def test_world_cohort_validate_rejects_invalid_manifest_without_creating_db(tmp_path, monkeypatch, capsys) -> None:
    manifest_path = tmp_path / "bad-manifest.json"
    manifest_path.write_text("{}", encoding="utf-8")

    code, payload = _run_json(
        monkeypatch,
        capsys,
        tmp_path,
        ["world", "cohort", "validate", "--manifest", str(manifest_path), "--json"],
    )

    assert code == 1
    assert payload["ok"] is False
    assert payload["error"]["code"] == "invalid_manifest"
    _assert_claims(payload)
    assert not _db_path(tmp_path).exists()


def test_world_cohort_status_and_report_missing_db_do_not_create_or_migrate(tmp_path, monkeypatch, capsys) -> None:
    status_code, status = _run_json(
        monkeypatch,
        capsys,
        tmp_path,
        ["world", "cohort", "status", COHORT_ID, "--json"],
    )
    assert status_code == 0
    assert status["command"] == "status"
    assert status["status"] == "not_started"
    assert status["exists"] is False
    assert status["cohort_id"] == COHORT_ID
    _assert_claims(status)
    assert not _db_path(tmp_path).exists()

    report_code, report = _run_json(
        monkeypatch,
        capsys,
        tmp_path,
        ["world", "cohort", "report", COHORT_ID, "--json"],
    )
    assert report_code == 0
    assert report["schema_version"] == "world_cohort_report.v1"
    assert report["status"] == "not_started"
    assert report["exists"] is False
    _assert_claims(report)
    assert not _db_path(tmp_path).exists()
    assert list(tmp_path.glob("world_model.db*")) == []


def test_world_cohort_register_does_not_arm_or_start(tmp_path, monkeypatch, capsys) -> None:
    manifest_path = _write_manifest(tmp_path)
    expected = _manifest()

    code, payload = _run_json(
        monkeypatch,
        capsys,
        tmp_path,
        ["world", "cohort", "register", "--manifest", str(manifest_path)],
    )

    assert code == 0
    assert payload["ok"] is True
    assert payload["command"] == "register"
    assert payload["phase"] == "registered"
    assert payload["event_type"] == "world_cohort_registered"
    assert payload["cohort_id"] == expected.cohort_id
    assert payload["manifest_sha256"] == expected.manifest_sha256
    _assert_claims(payload)
    assert _db_path(tmp_path).exists()

    status_code, status = _run_json(
        monkeypatch,
        capsys,
        tmp_path,
        ["world", "cohort", "status", expected.cohort_id, "--json"],
    )
    assert status_code == 0
    assert status["phase"] == "registered"
    assert status["event_types"] == ["world_cohort_registered"]
    assert status["started_event_id"] is None
    _assert_claims(status)


def test_world_cohort_arm_and_start_require_explicit_commands(tmp_path, monkeypatch, capsys) -> None:
    manifest_path = _write_manifest(tmp_path)
    expected = _manifest()
    register_code, _register = _run_json(
        monkeypatch,
        capsys,
        tmp_path,
        ["world", "cohort", "register", "--manifest", str(manifest_path)],
    )
    assert register_code == 0

    start_before_arm, start_payload = _run_json(
        monkeypatch,
        capsys,
        tmp_path,
        ["world", "cohort", "start", expected.cohort_id],
    )
    assert start_before_arm == 1
    assert start_payload["ok"] is False
    assert start_payload["error"]["code"] == "domain_error"
    _assert_claims(start_payload)

    arm_code, armed = _run_json(
        monkeypatch,
        capsys,
        tmp_path,
        ["world", "cohort", "arm", expected.cohort_id],
    )
    assert arm_code == 0
    assert armed["ok"] is True
    assert armed["command"] == "arm"
    assert armed["phase"] == "armed"
    assert armed["event_type"] == "world_cohort_armed"
    assert armed["satisfied_sensor_ids"] == ["company"]
    _assert_claims(armed)

    start_code, started = _run_json(
        monkeypatch,
        capsys,
        tmp_path,
        ["world", "cohort", "start", expected.cohort_id],
    )
    assert start_code == 0
    assert started["ok"] is True
    assert started["command"] == "start"
    assert started["phase"] == "collecting"
    assert started["event_type"] == "world_cohort_started"
    _assert_claims(started)

    status_code, status = _run_json(
        monkeypatch,
        capsys,
        tmp_path,
        ["world", "cohort", "status", expected.cohort_id, "--json"],
    )
    assert status_code == 0
    assert status["phase"] == "collecting"
    assert status["event_types"] == [
        "world_cohort_registered",
        "world_cohort_armed",
        "world_cohort_started",
    ]
    assert status["started_event_id"]


def test_world_cohort_mutations_on_missing_db_do_not_create_store(tmp_path, monkeypatch, capsys) -> None:
    for argv in (
        ["world", "cohort", "arm", COHORT_ID],
        ["world", "cohort", "start", COHORT_ID],
        ["world", "cohort", "close", COHORT_ID, "--reason", "stop"],
        ["world", "cohort", "invalidate", COHORT_ID, "--reason", "accepted_drift"],
    ):
        code, payload = _run_json(monkeypatch, capsys, tmp_path, argv)
        assert code == 1
        assert payload["ok"] is False
        assert payload["error"]["code"] == "store_missing"
        _assert_claims(payload)
        assert not _db_path(tmp_path).exists()
    assert list(tmp_path.glob("world_model.db*")) == []


def test_world_cohort_close_and_invalidate_are_explicit(tmp_path, monkeypatch, capsys) -> None:
    manifest_path = _write_manifest(tmp_path)
    expected = _manifest()
    for argv in (
        ["world", "cohort", "register", "--manifest", str(manifest_path)],
        ["world", "cohort", "arm", expected.cohort_id],
        ["world", "cohort", "start", expected.cohort_id],
    ):
        code, payload = _run_json(monkeypatch, capsys, tmp_path, argv)
        assert code == 0, payload

    close_code, closed = _run_json(
        monkeypatch,
        capsys,
        tmp_path,
        ["world", "cohort", "close", expected.cohort_id, "--reason", "planned_stop"],
    )
    assert close_code == 0
    assert closed["ok"] is True
    assert closed["command"] == "close"
    assert closed["phase"] == "collection_closed"
    assert closed["event_type"] == "world_cohort_collection_closed"
    _assert_claims(closed)

    report_code, report = _run_json(
        monkeypatch,
        capsys,
        tmp_path,
        ["world", "cohort", "report", expected.cohort_id, "--json"],
    )
    assert report_code == 0
    assert report["schema_version"] == "world_cohort_report.v1"
    assert report["phase"] == "collection_closed"
    _assert_claims(report)

    invalidate_code, invalidated = _run_json(
        monkeypatch,
        capsys,
        tmp_path,
        [
            "world",
            "cohort",
            "invalidate",
            expected.cohort_id,
            "--reason",
            "accepted_drift",
        ],
    )
    assert invalidate_code == 0
    assert invalidated["ok"] is True
    assert invalidated["command"] == "invalidate"
    assert invalidated["phase"] == "invalidated"
    assert invalidated["event_type"] == "world_cohort_invalidated"
    _assert_claims(invalidated)


def test_world_cohort_validate_does_not_register(tmp_path, monkeypatch, capsys) -> None:
    manifest_path = _write_manifest(tmp_path)
    expected = _manifest()
    validate_code, _payload = _run_json(
        monkeypatch,
        capsys,
        tmp_path,
        ["world", "cohort", "validate", "--manifest", str(manifest_path), "--json"],
    )
    assert validate_code == 0
    assert not _db_path(tmp_path).exists()

    status_code, status = _run_json(
        monkeypatch,
        capsys,
        tmp_path,
        ["world", "cohort", "status", expected.cohort_id, "--json"],
    )
    assert status_code == 0
    assert status["status"] == "not_started"
    assert not _db_path(tmp_path).exists()


def test_world_cohort_parser_exposes_rfc_commands() -> None:
    parser = cli.build_parser()
    validate = parser.parse_args(["world", "cohort", "validate", "--manifest", "m.json", "--json"])
    assert validate.cohort_command == "validate"
    register = parser.parse_args(["world", "cohort", "register", "--manifest", "m.json"])
    assert register.cohort_command == "register"
    arm = parser.parse_args(["world", "cohort", "arm", COHORT_ID])
    assert arm.cohort_command == "arm"
    start = parser.parse_args(["world", "cohort", "start", COHORT_ID])
    assert start.cohort_command == "start"
    status = parser.parse_args(["world", "cohort", "status", COHORT_ID, "--json"])
    assert status.cohort_command == "status"
    report = parser.parse_args(["world", "cohort", "report", COHORT_ID, "--json"])
    assert report.cohort_command == "report"
    close = parser.parse_args(["world", "cohort", "close", COHORT_ID, "--reason", "done"])
    assert close.cohort_command == "close"
    invalidate = parser.parse_args(["world", "cohort", "invalidate", COHORT_ID, "--reason", "future_leak"])
    assert invalidate.cohort_command == "invalidate"
    world_status = parser.parse_args(["world", "status", "--json"])
    assert world_status.world_command == "status"
    macro_status = parser.parse_args(["world", "macro", "status", "--json"])
    assert macro_status.world_command == "macro"
    assert macro_status.macro_command == "status"


def test_world_macro_status_is_read_only_machine_readable_and_shadow_bounded(tmp_path, monkeypatch, capsys) -> None:
    (tmp_path / "gdelt").mkdir()
    (tmp_path / "gdelt" / "events.jsonl").write_text("{}", encoding="utf-8")

    code, payload = _run_json(monkeypatch, capsys, tmp_path, ["world", "macro", "status", "--json"])

    assert code == 0
    assert payload["schema_version"] == "world_macro_status.v1"
    assert payload["status"] == "not_started"
    assert payload["command"] == "status"
    assert "collection" in payload
    assert "coverage" in payload
    assert "freshness" in payload
    assert "gaps" in payload
    assert "attach" in payload
    assert "evaluation" not in payload
    assert "impact" not in payload
    assert payload["gaps"]["gdelt"] == "excluded"
    assert "should-not" not in json.dumps(payload)
    _assert_claims(payload)
    assert not _db_path(tmp_path).exists()
    assert not (tmp_path / "world_macro").exists()
    assert list(tmp_path.glob("world_model.db*")) == []


def test_world_status_json_keeps_macro_separate_from_ml_study(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli.daemon, "STATE_DIR", tmp_path)

    assert cli.main(["world", "status", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "not_started"
    assert payload["decision_effect"] == "none"
    assert payload["recommendation"] == "NO_GO"
    assert payload["macro"]["status"] == "not_started"
    assert "evaluation" in payload
    assert "evaluation" not in payload["macro"]
    assert payload["macro"]["gaps"]["gdelt"] == "excluded"
    assert not _db_path(tmp_path).exists()
    assert not (tmp_path / "world_macro").exists()


def test_world_graph_parser_exposes_status_and_report() -> None:
    parser = cli.build_parser()
    status = parser.parse_args(["world", "graph", "status", "--json"])
    assert status.world_command == "graph"
    assert status.graph_command == "status"
    report = parser.parse_args(["world", "graph", "report", "--json"])
    assert report.graph_command == "report"
    report_cohort = parser.parse_args(["world", "graph", "report", COHORT_ID, "--json"])
    assert report_cohort.cohort_id == COHORT_ID


def test_world_graph_status_is_read_only_and_exposes_budgets_gaps(tmp_path, monkeypatch, capsys) -> None:
    code, payload = _run_json(monkeypatch, capsys, tmp_path, ["world", "graph", "status", "--json"])

    assert code == 0
    assert payload["schema_version"] == "world_graph_status.v1"
    assert payload["command"] == "status"
    assert payload["status"] == "not_started"
    assert payload["flag"] == "CASYS_WORLD_MODEL_GRAPH_ENABLED"
    assert payload["flag_default"] == 0
    assert payload["budgets"]["max_depth"] == 4
    assert payload["budgets"]["max_paths_per_root"] == 32
    assert payload["budgets"]["policy_version"] == "graph_traversal.v1"
    assert "gaps" in payload
    assert payload["gaps"]["cohort_activation"] == "not_started"
    assert "evaluation" not in payload
    assert "impact" not in payload
    serialized = json.dumps(payload).lower()
    assert "should-not" not in serialized
    _assert_claims(payload)
    assert not _db_path(tmp_path).exists()
    assert list(tmp_path.glob("world_model.db*")) == []


def test_world_graph_report_is_read_only_shadow_bounded(tmp_path, monkeypatch, capsys) -> None:
    code, payload = _run_json(monkeypatch, capsys, tmp_path, ["world", "graph", "report", "--json"])

    assert code == 0
    assert payload["schema_version"] == "world_graph_report.v1"
    assert payload["command"] == "report"
    assert payload["status"] == "not_started"
    assert payload["budgets"]["max_depth"] == 4
    assert payload["gaps"]["cohort_activation"] == "not_started"
    assert "evaluation" not in payload
    assert "impact" not in payload
    _assert_claims(payload)
    assert not _db_path(tmp_path).exists()
    assert list(tmp_path.glob("world_model.db*")) == []


def test_world_status_json_nests_graph_budgets_without_causal_or_pnl(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli.daemon, "STATE_DIR", tmp_path)

    assert cli.main(["world", "status", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "not_started"
    assert payload["decision_effect"] == "none"
    graph = payload["graph"]
    assert graph["schema_version"] == "world_graph_status.v1"
    assert graph["flag_default"] == 0
    assert graph["budgets"]["max_paths_per_root"] == 32
    assert graph["gaps"]["cohort_activation"] == "not_started"
    assert "evaluation" not in graph
    assert graph["causal_claim"] is False
    assert graph["pnl_claim"] is False
    assert graph["authority"] == "shadow_only"
    assert not _db_path(tmp_path).exists()


def test_world_graph_status_reads_collecting_graph_cohort_not_not_started(tmp_path, monkeypatch, capsys) -> None:
    from tests.read_models.test_world_graph_report import GRAPH_COHORT_ID, _graph_manifest, _persist

    _persist(tmp_path, _manifest(), phase="collecting")
    _persist(tmp_path, _graph_manifest(), phase="collecting")

    code, payload = _run_json(monkeypatch, capsys, tmp_path, ["world", "graph", "status", "--json"])

    assert code == 0
    assert payload["schema_version"] == "world_graph_status.v1"
    assert payload["exists"] is True
    assert payload["gaps"]["cohort_activation"] == "collecting"
    assert payload["gaps"]["graph_cohort_id"] == GRAPH_COHORT_ID
    _assert_claims(payload)


def test_world_graph_report_and_nested_status_keep_c1_collecting_off_graph_activation(
    tmp_path, monkeypatch, capsys
) -> None:
    from tests.read_models.test_world_graph_report import _persist

    _persist(tmp_path, _manifest(), phase="collecting")

    status_code, status = _run_json(monkeypatch, capsys, tmp_path, ["world", "graph", "status", "--json"])
    assert status_code == 0
    assert status["gaps"]["cohort_activation"] == "no_graph_cohort"
    _assert_claims(status)

    report_code, report = _run_json(monkeypatch, capsys, tmp_path, ["world", "graph", "report", "--json"])
    assert report_code == 0
    assert report["gaps"]["cohort_activation"] == "no_graph_cohort"
    _assert_claims(report)

    monkeypatch.setattr(cli.daemon, "STATE_DIR", tmp_path)
    assert cli.main(["world", "status", "--json"]) == 0
    nested = json.loads(capsys.readouterr().out)
    assert nested["graph"]["gaps"]["cohort_activation"] == "no_graph_cohort"
    assert nested["decision_effect"] == "none"
    _assert_claims(nested["graph"])


_PATTERN_CUTOFF = "2026-09-01T00:00:00+00:00"
_PATTERN_EVAL_START = "2026-09-02T00:00:00+00:00"
_PATTERN_EVAL_FP = "b" * 64


def _pattern_discover_argv(*extra: str) -> list[str]:
    return [
        "world",
        "pattern",
        "discover",
        "--formation-cutoff",
        _PATTERN_CUTOFF,
        "--evaluation-start-not-before",
        _PATTERN_EVAL_START,
        *extra,
    ]


def _forbid_pattern_store(monkeypatch) -> None:
    def boom(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("WorldPatternStore must not be constructed")

    monkeypatch.setattr(
        "trader.infrastructure.state_db.world_pattern_store.WorldPatternStore",
        boom,
    )


def _table_names(path: Path) -> set[str]:
    import sqlite3

    connection = sqlite3.connect(path)
    try:
        return {str(row[0]) for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        connection.close()


def _count_table(path: Path, table: str) -> int:
    import sqlite3

    if not path.exists():
        return 0
    connection = sqlite3.connect(path)
    try:
        if table not in _table_names(path):
            return 0
        return int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    finally:
        connection.close()


def test_world_pattern_parser_exposes_discover_evaluate_link_status_and_report() -> None:
    parser = cli.build_parser()
    discover = parser.parse_args(
        [
            "world",
            "pattern",
            "discover",
            "--formation-cutoff",
            _PATTERN_CUTOFF,
            "--evaluation-start-not-before",
            _PATTERN_EVAL_START,
        ]
    )
    assert discover.world_command == "pattern"
    assert discover.pattern_command == "discover"
    assert discover.apply is False
    assert discover.include_source_evidence is False
    assert discover.hypothesis_id is None
    assert discover.min_support == 20
    assert discover.min_association == 0.10
    assert discover.max_candidates == 20
    evaluate = parser.parse_args(
        [
            "world",
            "pattern",
            "evaluate",
            "--as-of",
            "2026-09-10T00:00:00+00:00",
            "--evaluation-cohort-id",
            COHORT_ID,
            "--evaluation-dataset-fingerprint",
            _PATTERN_EVAL_FP,
        ]
    )
    assert evaluate.pattern_command == "evaluate"
    assert evaluate.apply is False
    link = parser.parse_args(
        ["world", "pattern", "link-outcomes", "--as-of", "2026-09-12T00:00:00+00:00"]
    )
    assert link.pattern_command == "link-outcomes"
    assert link.apply is False
    status = parser.parse_args(["world", "pattern", "status", "--json"])
    assert status.pattern_command == "status"
    report = parser.parse_args(["world", "pattern", "report", COHORT_ID, "--json"])
    assert report.pattern_command == "report"
    assert report.cohort_id == COHORT_ID
    apply_args = parser.parse_args(
        _pattern_discover_argv(
            "--apply",
            "--evaluation-cohort-id",
            COHORT_ID,
            "--evaluation-dataset-fingerprint",
            _PATTERN_EVAL_FP,
            "--hypothesis-id",
            "pattern-hypothesis:v1:" + "a" * 64,
            "--hypothesis-id",
            "pattern-hypothesis:v1:" + "c" * 64,
            "--include-source-evidence",
        )
    )
    assert apply_args.apply is True
    assert apply_args.include_source_evidence is True
    assert apply_args.evaluation_cohort_id == COHORT_ID
    assert apply_args.hypothesis_id == [
        "pattern-hypothesis:v1:" + "a" * 64,
        "pattern-hypothesis:v1:" + "c" * 64,
    ]


def test_world_pattern_discover_dry_run_missing_db_does_not_create(tmp_path, monkeypatch, capsys) -> None:
    _forbid_pattern_store(monkeypatch)
    code, payload = _run_json(monkeypatch, capsys, tmp_path, _pattern_discover_argv("--json"))

    assert code == 0
    assert payload["ok"] is True
    assert payload["command"] == "discover"
    assert payload["apply"] is False
    assert payload["exists"] is False
    assert payload["candidates"] == []
    assert payload["rejection_counts"].get("missing_db") == 1
    assert payload["eligible_records"] == 0
    assert payload["source_evidence_count"] == 0
    assert payload["source_evidence_fingerprint"] == canonical_sha256([])
    assert "source_evidence_ids" not in payload
    _assert_claims(payload)
    assert not _db_path(tmp_path).exists()
    assert list(tmp_path.glob("world_model.db*")) == []


def test_world_pattern_discover_include_source_evidence_restores_ids(
    tmp_path, monkeypatch, capsys
) -> None:
    _forbid_pattern_store(monkeypatch)
    code, payload = _run_json(
        monkeypatch,
        capsys,
        tmp_path,
        _pattern_discover_argv("--include-source-evidence", "--json"),
    )

    assert code == 0
    assert payload["source_evidence_count"] == 0
    assert payload["source_evidence_fingerprint"] == canonical_sha256([])
    assert payload["source_evidence_ids"] == []
    _assert_claims(payload)
    assert not _db_path(tmp_path).exists()


def test_world_pattern_discover_dry_run_partial_db_does_not_migrate(tmp_path, monkeypatch, capsys) -> None:
    import sqlite3

    _forbid_pattern_store(monkeypatch)
    db_path = _db_path(tmp_path)
    connection = sqlite3.connect(db_path)
    connection.execute("CREATE TABLE dummy (id INTEGER)")
    connection.commit()
    connection.close()

    code, payload = _run_json(monkeypatch, capsys, tmp_path, _pattern_discover_argv("--json"))

    assert code == 0
    assert payload["apply"] is False
    assert payload["candidates"] == []
    assert payload["rejection_counts"].get("schema_unavailable") == 1
    _assert_claims(payload)
    assert _table_names(db_path) == {"dummy"}


def test_world_pattern_apply_without_cohort_or_fingerprint_does_not_write(
    tmp_path, monkeypatch, capsys
) -> None:
    _forbid_pattern_store(monkeypatch)
    code, payload = _run_json(monkeypatch, capsys, tmp_path, _pattern_discover_argv("--apply", "--json"))

    assert code == 1
    assert payload["ok"] is False
    assert payload["error"]["code"] == "invalid_apply"
    _assert_claims(payload)
    assert not _db_path(tmp_path).exists()
    assert list(tmp_path.glob("world_model.db*")) == []


def test_world_pattern_apply_rejects_malformed_cohort_id_before_store(
    tmp_path, monkeypatch, capsys
) -> None:
    _forbid_pattern_store(monkeypatch)
    code, payload = _run_json(
        monkeypatch,
        capsys,
        tmp_path,
        _pattern_discover_argv(
            "--apply",
            "--evaluation-cohort-id",
            "world_cohort:graph_pilot",
            "--evaluation-dataset-fingerprint",
            _PATTERN_EVAL_FP,
            "--json",
        ),
    )

    assert code == 1
    assert payload["ok"] is False
    assert payload["error"]["code"] == "invalid_apply"
    _assert_claims(payload)
    assert not _db_path(tmp_path).exists()
    assert list(tmp_path.glob("world_model.db*")) == []


def test_world_pattern_apply_rejects_in_sample_batch_fingerprint(tmp_path, monkeypatch, capsys) -> None:
    _forbid_pattern_store(monkeypatch)
    dry_code, dry = _run_json(monkeypatch, capsys, tmp_path, _pattern_discover_argv("--json"))
    assert dry_code == 0
    batch_fp = dry["formation_dataset_fingerprint"]
    assert len(batch_fp) == 64

    code, payload = _run_json(
        monkeypatch,
        capsys,
        tmp_path,
        _pattern_discover_argv(
            "--apply",
            "--evaluation-cohort-id",
            COHORT_ID,
            "--evaluation-dataset-fingerprint",
            batch_fp,
            "--json",
        ),
    )

    assert code == 1
    assert payload["error"]["code"] == "in_sample_dataset"
    _assert_claims(payload)
    assert not _db_path(tmp_path).exists()


def test_world_pattern_apply_unknown_hypothesis_id_does_not_open_store(
    tmp_path, monkeypatch, capsys
) -> None:
    _forbid_pattern_store(monkeypatch)
    code, payload = _run_json(
        monkeypatch,
        capsys,
        tmp_path,
        _pattern_discover_argv(
            "--apply",
            "--evaluation-cohort-id",
            COHORT_ID,
            "--evaluation-dataset-fingerprint",
            _PATTERN_EVAL_FP,
            "--hypothesis-id",
            "pattern-hypothesis:v1:" + "d" * 64,
            "--json",
        ),
    )

    assert code == 1
    assert payload["error"]["code"] == "unknown_hypothesis"
    _assert_claims(payload)
    assert not _db_path(tmp_path).exists()


def test_world_pattern_apply_missing_db_is_store_missing(tmp_path, monkeypatch, capsys) -> None:
    _forbid_pattern_store(monkeypatch)
    code, payload = _run_json(
        monkeypatch,
        capsys,
        tmp_path,
        _pattern_discover_argv(
            "--apply",
            "--evaluation-cohort-id",
            COHORT_ID,
            "--evaluation-dataset-fingerprint",
            _PATTERN_EVAL_FP,
            "--json",
        ),
    )

    assert code == 1
    assert payload["error"]["code"] == "store_missing"
    _assert_claims(payload)
    assert not _db_path(tmp_path).exists()
    assert list(tmp_path.glob("world_model.db*")) == []


def test_world_pattern_apply_registers_and_is_idempotent(tmp_path, monkeypatch, capsys) -> None:
    from tests.state_db.test_world_pattern_formation_query import _seed_many
    from trader.domain.world_pattern import PatternEvaluationStarted, PatternHypothesisId
    from trader.infrastructure.state_db.world_pattern_store import WorldPatternStore

    _seed_many(tmp_path, monkeypatch, ("2330", "2454", "2303"))
    argv = _pattern_discover_argv(
        "--horizon",
        "elapsed_1d.v1",
        "--min-support",
        "1",
        "--min-association",
        "0",
        "--apply",
        "--evaluation-cohort-id",
        COHORT_ID,
        "--evaluation-dataset-fingerprint",
        _PATTERN_EVAL_FP,
        "--json",
    )
    code, payload = _run_json(monkeypatch, capsys, tmp_path, argv)

    assert code == 0
    assert payload["apply"] is True
    assert payload["candidates"]
    assert payload["applied"]
    assert payload["evaluation_cohort_id"] == COHORT_ID
    assert payload["evaluation_dataset_fingerprint"] == _PATTERN_EVAL_FP
    assert payload["evaluation_dataset_fingerprint"] != payload["formation_dataset_fingerprint"]
    assert "source_evidence_ids" not in payload
    assert "source_evidence_count" in payload
    assert len(payload["source_evidence_fingerprint"]) == 64
    for candidate in payload["candidates"]:
        assert payload["evaluation_dataset_fingerprint"] != candidate["spec"]["formation_dataset_fingerprint"]
    _assert_claims(payload)
    first_applied = payload["applied"]
    store = WorldPatternStore(_db_path(tmp_path))
    try:
        for row in first_applied:
            hypothesis = store.load(PatternHypothesisId(row["hypothesis_id"]))
            assert hypothesis.status == "evaluating"
            assert hypothesis.registered.registered_at.isoformat() == _PATTERN_CUTOFF
            started = next(
                event for event in hypothesis.events if isinstance(event, PatternEvaluationStarted)
            )
            assert started.started_at.isoformat() == _PATTERN_EVAL_START
            assert hypothesis.evaluation_cohort_id == COHORT_ID
    finally:
        store.close()
    assert _count_table(_db_path(tmp_path), "world_pattern_occurrence_events") == 0
    assert _count_table(_db_path(tmp_path), "world_shadow_predictions") == 0

    retry_code, retry = _run_json(monkeypatch, capsys, tmp_path, argv)
    assert retry_code == 0
    assert retry["applied"] == first_applied
    assert _count_table(_db_path(tmp_path), "world_pattern_hypothesis_events") == 2 * len(first_applied)
    assert _count_table(_db_path(tmp_path), "world_pattern_occurrence_events") == 0


def test_world_pattern_apply_rejects_candidate_formation_fingerprint(
    tmp_path, monkeypatch, capsys
) -> None:
    from tests.state_db.test_world_pattern_formation_query import _seed_many

    _seed_many(tmp_path, monkeypatch, ("2330", "2454", "2303"))
    dry_code, dry = _run_json(
        monkeypatch,
        capsys,
        tmp_path,
        _pattern_discover_argv(
            "--horizon",
            "elapsed_1d.v1",
            "--min-support",
            "1",
            "--min-association",
            "0",
            "--json",
        ),
    )
    assert dry_code == 0
    assert dry["apply"] is False
    assert dry["candidates"]
    assert "source_evidence_ids" not in dry
    assert dry["source_evidence_count"] >= 0
    assert len(dry["source_evidence_fingerprint"]) == 64
    assert _count_table(_db_path(tmp_path), "world_pattern_hypothesis_events") == 0
    candidate_fp = dry["candidates"][0]["spec"]["formation_dataset_fingerprint"]

    code, payload = _run_json(
        monkeypatch,
        capsys,
        tmp_path,
        _pattern_discover_argv(
            "--horizon",
            "elapsed_1d.v1",
            "--min-support",
            "1",
            "--min-association",
            "0",
            "--apply",
            "--evaluation-cohort-id",
            COHORT_ID,
            "--evaluation-dataset-fingerprint",
            candidate_fp,
            "--json",
        ),
    )

    assert code == 1
    assert payload["error"]["code"] == "in_sample_dataset"
    _assert_claims(payload)
    assert _count_table(_db_path(tmp_path), "world_pattern_hypothesis_events") == 0


def test_world_pattern_discover_omits_source_evidence_ids_unless_requested(
    tmp_path, monkeypatch, capsys
) -> None:
    from tests.state_db.test_world_pattern_formation_query import _seed_many

    _seed_many(tmp_path, monkeypatch, ("2330", "2454", "2303"))
    concise_argv = _pattern_discover_argv(
        "--horizon",
        "elapsed_1d.v1",
        "--min-support",
        "1",
        "--min-association",
        "0",
        "--json",
    )
    code, concise = _run_json(monkeypatch, capsys, tmp_path, concise_argv)
    assert code == 0
    assert concise["apply"] is False
    assert "source_evidence_ids" not in concise
    assert concise["source_evidence_count"] > 0
    assert len(concise["source_evidence_fingerprint"]) == 64
    _assert_claims(concise)

    full_code, full = _run_json(
        monkeypatch,
        capsys,
        tmp_path,
        _pattern_discover_argv(
            "--horizon",
            "elapsed_1d.v1",
            "--min-support",
            "1",
            "--min-association",
            "0",
            "--include-source-evidence",
            "--json",
        ),
    )
    assert full_code == 0
    assert full["source_evidence_ids"]
    assert full["source_evidence_count"] == len(full["source_evidence_ids"])
    assert full["source_evidence_fingerprint"] == canonical_sha256(sorted(full["source_evidence_ids"]))
    assert full["source_evidence_fingerprint"] == concise["source_evidence_fingerprint"]
    assert full["source_evidence_count"] == concise["source_evidence_count"]
    _assert_claims(full)
    assert _count_table(_db_path(tmp_path), "world_pattern_hypothesis_events") == 0


def test_world_pattern_report_missing_db_does_not_create(tmp_path, monkeypatch, capsys) -> None:
    code, payload = _run_json(
        monkeypatch,
        capsys,
        tmp_path,
        ["world", "pattern", "report", COHORT_ID, "--json"],
    )

    assert code == 0
    assert payload["command"] == "report"
    assert payload["schema_version"] == "world_pattern_report.v1"
    assert payload["status"] == "not_started"
    assert payload["exists"] is False
    assert payload["cohort_id"] == COHORT_ID
    _assert_claims(payload)
    assert not _db_path(tmp_path).exists()
    assert list(tmp_path.glob("world_model.db*")) == []


def test_world_pattern_evaluate_and_link_dry_run_do_not_construct_stores(
    tmp_path, monkeypatch, capsys
) -> None:
    _forbid_pattern_store(monkeypatch)

    def boom(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("WorldModelStore must not be constructed")

    monkeypatch.setattr("trader.infrastructure.state_db.world_model_store.WorldModelStore", boom)
    evaluate_code, evaluate = _run_json(
        monkeypatch,
        capsys,
        tmp_path,
        [
            "world",
            "pattern",
            "evaluate",
            "--as-of",
            "2026-09-10T00:00:00+00:00",
            "--evaluation-cohort-id",
            COHORT_ID,
            "--evaluation-dataset-fingerprint",
            _PATTERN_EVAL_FP,
            "--json",
        ],
    )
    assert evaluate_code == 0
    assert evaluate["command"] == "evaluate"
    assert evaluate["apply"] is False
    assert evaluate["matches"] == []
    _assert_claims(evaluate)

    link_code, link = _run_json(
        monkeypatch,
        capsys,
        tmp_path,
        ["world", "pattern", "link-outcomes", "--as-of", "2026-09-12T00:00:00+00:00", "--json"],
    )
    assert link_code == 0
    assert link["command"] == "link-outcomes"
    assert link["apply"] is False
    assert link["statuses"] == []
    _assert_claims(link)
    assert not _db_path(tmp_path).exists()


def test_world_pattern_status_missing_db_does_not_create(tmp_path, monkeypatch, capsys) -> None:
    code, payload = _run_json(monkeypatch, capsys, tmp_path, ["world", "pattern", "status", "--json"])
    assert code == 0
    assert payload["command"] == "status"
    assert payload["schema_version"] == "world_pattern_status.v1"
    assert payload["status"] == "not_started"
    assert payload["exists"] is False
    _assert_claims(payload)
    assert not _db_path(tmp_path).exists()
