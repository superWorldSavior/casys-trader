from __future__ import annotations

import json

from trader.interfaces.cli.world_model import read_world_model_status
from trader.runtime import cli


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
            "label": {"simple_return": 0.01, "move_class": "UP"},
            "evidence": {"source": "fixture"},
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
            "label": {"simple_return": 0.01, "move_class": "DOWN", "direction": "DOWN"},
            "evidence": {"source": "fixture"},
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
