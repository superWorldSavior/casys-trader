from __future__ import annotations

import json
from pathlib import Path

from tests.domain.test_world_cohort import COHORT_ID, _manifest
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


def test_world_status_does_not_create_a_missing_database(tmp_path) -> None:
    result = read_world_model_status(tmp_path)

    assert result["status"] == "not_started"
    assert result["authority"] == "shadow_only"
    assert result["counts"]["episodes"] == 0
    assert result["predictions_by_model"] == {}
    assert result["evaluation"]["status"] == "warming_up"
    assert result["impact"]["actual_contribution"]["status"] == "not_attributable"
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
    episode = {
        "episode_id": "episode-1",
        "venue": "US",
        "symbol": "SPY",
        "observed_at": "2026-08-22T00:00:00+00:00",
        "available_at": "2026-08-22T00:00:00+00:00",
        "as_of_bar_ts": "2026-08-22T00:00:00+00:00",
        "bar_interval": "1h",
        "feature_contract_version": "world_features.v1",
        "sampling_policy_version": "cycle_snapshot.v1",
        "training_eligible": True,
        "observation": {"symbol": "SPY", "features": {"return": 0.01}},
        "source_evidence": {"source": "fixture"},
    }
    assert store.append_episode(episode)
    assert store.append_outcome_event(
        {
            "outcome_event_id": "outcome-1",
            "episode_id": "episode-1",
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
                "episode_id": "episode-1",
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
    assert store.append_episode(
        {
            "episode_id": "episode-index-authority",
            "venue": "US",
            "symbol": "SPY",
            "observed_at": "2026-08-22T00:00:00+00:00",
            "available_at": "2026-08-22T00:00:00+00:00",
            "as_of_bar_ts": "2026-08-22T00:00:00+00:00",
            "bar_interval": "1h",
            "feature_contract_version": "world_features.v1",
            "sampling_policy_version": "cycle_snapshot.v1",
            "training_eligible": True,
            "observation": {"symbol": "SPY", "features": {"return": 0.01}},
            "source_evidence": {"source": "fixture"},
        }
    )
    assert store.append_outcome_event(
        {
            "outcome_event_id": "outcome-index-authority",
            "episode_id": "episode-index-authority",
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
            "episode_id": "episode-index-authority",
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


def test_world_cohort_validate_rejects_invalid_manifest_without_creating_db(
    tmp_path, monkeypatch, capsys
) -> None:
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


def test_world_cohort_status_and_report_missing_db_do_not_create_or_migrate(
    tmp_path, monkeypatch, capsys
) -> None:
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
    invalidate = parser.parse_args(
        ["world", "cohort", "invalidate", COHORT_ID, "--reason", "future_leak"]
    )
    assert invalidate.cohort_command == "invalidate"
    world_status = parser.parse_args(["world", "status", "--json"])
    assert world_status.world_command == "status"
