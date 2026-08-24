from __future__ import annotations

import pytest

from trader.reporting.read_models.world_evaluation import (
    BASELINE_MODEL_ID,
    GRU_MODEL_ID,
    evaluate_shadow,
)


def _prediction(**overrides):
    payload = {
        "episode_id": "episode-1",
        "horizon_id": "elapsed_4h.v1",
        "model_id": "markov",
        "model_version": "v1",
        "probabilities": {"DOWN": 0.1, "FLAT": 0.2, "UP": 0.7},
        "created_at": "2026-08-20T10:00:00+00:00",
    }
    payload.update(overrides)
    payload.setdefault("ready_at", payload["created_at"])
    return payload


def _outcome(**overrides):
    payload = {
        "event_id": "outcome-1",
        "episode_id": "episode-1",
        "horizon_id": "elapsed_4h.v1",
        "status": "observed",
        "direction": "UP",
        "training_eligible": True,
        "available_at": "2026-08-20T14:15:00+00:00",
    }
    payload.update(overrides)
    return payload


def test_evaluation_scores_only_prequential_predictions() -> None:
    result = evaluate_shadow([_prediction()], [_outcome()])

    assert result["status"] == "ready"
    assert result["matched"] == 1
    assert result["groups"][0]["brier"] == 0.14
    assert result["groups"][0]["brier_skill_vs_uniform"] > 0


def test_evaluation_rejects_prediction_created_after_label() -> None:
    result = evaluate_shadow(
        [
            _prediction(
                created_at="2026-08-20T15:00:00+00:00",
                ready_at="2026-08-20T10:00:00+00:00",
            )
        ],
        [_outcome()],
    )

    assert result["status"] == "warming_up"
    assert result["excluded"] == {"causal_order_unproven": 1}


def test_evaluation_rejects_prediction_ready_after_label_despite_earlier_logical_cutoff() -> None:
    result = evaluate_shadow(
        [
            _prediction(
                predicted_at="2026-08-20T10:00:00+00:00",
                ready_at="2026-08-20T15:00:00+00:00",
            )
        ],
        [_outcome()],
    )

    assert result["status"] == "warming_up"
    assert result["excluded"] == {"causal_order_unproven": 1}


def test_evaluation_requires_actual_ready_or_recorded_timestamp() -> None:
    result = evaluate_shadow([_prediction(ready_at=None)], [_outcome()])

    assert result["status"] == "warming_up"
    assert result["excluded"] == {"causal_order_unproven": 1}


def test_evaluation_uses_only_unambiguous_unsuperseded_revision() -> None:
    corrected = _outcome(
        event_id="outcome-2",
        supersedes_event_id="outcome-1",
        direction="FLAT",
    )
    result = evaluate_shadow([_prediction()], [_outcome(), corrected])

    assert result["matched"] == 1
    assert result["groups"][0]["brier"] == 1.14


def test_evaluation_does_not_score_pending_or_ineligible_outcomes() -> None:
    pending = _outcome(event_id="pending", status="pending")
    legacy = _outcome(event_id="legacy", training_eligible=False)

    result = evaluate_shadow([_prediction()], [pending, legacy])

    assert result["status"] == "warming_up"
    assert result["excluded"] == {"outcome_not_observed": 1}


def test_evaluation_accepts_runtime_store_envelopes() -> None:
    prediction = {
        "episode_id": "episode-1",
        "horizon_code": "elapsed_4h.v1",
        "model_kind": "markov",
        "model_version": "v1",
        "predicted_at": "2026-08-20T10:00:00+00:00",
        "recorded_at": "2026-08-20T10:01:00+00:00",
        "prediction": {"probabilities": {"DOWN": 0.1, "FLAT": 0.2, "UP": 0.7}},
    }
    outcome = {
        "outcome_event_id": "outcome-1",
        "episode_id": "episode-1",
        "horizon_code": "elapsed_4h.v1",
        "status": "observed",
        "move_class": "up",
        "training_eligible": True,
        "label_available_at": "2026-08-20T14:15:00+00:00",
    }

    result = evaluate_shadow([prediction], [outcome])

    assert result["matched"] == 1
    assert result["groups"][0]["model_id"] == "markov"


def test_evaluation_uses_the_models_flat_tie_break_at_cold_start() -> None:
    result = evaluate_shadow(
        [
            _prediction(
                probabilities={"DOWN": 1 / 3, "FLAT": 1 / 3, "UP": 1 / 3},
            )
        ],
        [_outcome(direction="FLAT")],
    )

    assert result["groups"][0]["accuracy"] == 1.0


def test_evaluation_deduplicates_exact_prediction_replays_and_rejects_divergence() -> None:
    replay = _prediction(prediction_id="immutable-prediction")
    result = evaluate_shadow([replay, _prediction(prediction_id="immutable-prediction")], [_outcome()])

    assert result["matched"] == 1
    assert result["excluded"] == {"duplicate_prediction_replay": 1}

    divergent = _prediction(
        prediction_id="different-model-state",
        probabilities={"DOWN": 0.6, "FLAT": 0.2, "UP": 0.2},
    )
    ambiguous = evaluate_shadow([replay, divergent], [_outcome()])

    assert ambiguous["status"] == "warming_up"
    assert ambiguous["matched"] == 0
    assert ambiguous["excluded"] == {"ambiguous_prediction_identity": 2}

    differing_ready_at = evaluate_shadow(
        [replay, _prediction(prediction_id="immutable-prediction", ready_at="2026-08-20T10:01:00+00:00")],
        [_outcome()],
    )

    assert differing_ready_at["matched"] == 0
    assert differing_ready_at["excluded"] == {"ambiguous_prediction_identity": 2}


def test_evaluation_groups_models_and_compares_paired_gru_to_baseline() -> None:
    baseline_id = "hierarchical_dirichlet_world_baseline"
    gru_id = "online_gru_world_challenger"
    predictions = [
        _prediction(
            prediction_id="baseline-1",
            episode_id="episode-1",
            model_id=baseline_id,
            probabilities={"DOWN": 0.3, "FLAT": 0.3, "UP": 0.4},
        ),
        _prediction(
            prediction_id="gru-1",
            episode_id="episode-1",
            model_id=gru_id,
            probabilities={"DOWN": 0.1, "FLAT": 0.1, "UP": 0.8},
        ),
        _prediction(
            prediction_id="baseline-2",
            episode_id="episode-2",
            model_id=baseline_id,
            created_at="2026-08-21T10:00:00+00:00",
            probabilities={"DOWN": 0.4, "FLAT": 0.3, "UP": 0.3},
        ),
        _prediction(
            prediction_id="gru-2",
            episode_id="episode-2",
            model_id=gru_id,
            created_at="2026-08-21T10:00:00+00:00",
            probabilities={"DOWN": 0.8, "FLAT": 0.1, "UP": 0.1},
        ),
    ]
    outcomes = [
        _outcome(event_id="outcome-1", episode_id="episode-1", direction="UP"),
        _outcome(
            event_id="outcome-2",
            episode_id="episode-2",
            direction="DOWN",
            available_at="2026-08-21T14:15:00+00:00",
        ),
    ]

    result = evaluate_shadow(predictions, outcomes, minimum_paired_support=2)

    assert [(group["model_id"], group["matched"]) for group in result["groups"]] == [
        (baseline_id, 2),
        (gru_id, 2),
    ]
    comparison = result["comparisons"][0]
    assert comparison["status"] == "ready"
    assert comparison["matched_pairs"] == 2
    assert comparison["gru_minus_baseline"]["brier"] < 0
    assert comparison["gru_minus_baseline"]["log_loss"] < 0
    assert comparison["gru_minus_baseline"]["accuracy"] == 0.0


def test_paired_comparison_withholds_metrics_below_explicit_support() -> None:
    baseline_id = "hierarchical_dirichlet_world_baseline"
    gru_id = "online_gru_world_challenger"
    result = evaluate_shadow(
        [
            _prediction(model_id=baseline_id, prediction_id="baseline"),
            _prediction(model_id=gru_id, prediction_id="gru"),
        ],
        [_outcome()],
        minimum_paired_support=2,
    )

    comparison = result["comparisons"][0]
    assert comparison["status"] == "insufficient_support"
    assert comparison["matched_pairs"] == 1
    assert comparison["minimum_paired_support"] == 2
    assert comparison["baseline"] is None
    assert comparison["gru"] is None
    assert comparison["gru_minus_baseline"] is None


_CAUSAL_IDENTITY = {
    "predicted_at": "2026-08-20T10:00:00+00:00",
    "ready_at": "2026-08-20T10:00:00+00:00",
    "training_cutoff": "2026-08-19T00:00:00+00:00",
    "comparison_batch_id": "batch:v1:shared",
    "comparison_cohort_fingerprint": "1" * 64,
    "study_cohort_id": "world_cohort:v1:" + "a" * 64,
    "manifest_sha256": "b" * 64,
}


def _causal_pair(*, gru_overrides: dict | None = None) -> tuple[dict, dict]:
    baseline = _prediction(
        prediction_id="baseline-causal",
        model_id=BASELINE_MODEL_ID,
        **_CAUSAL_IDENTITY,
    )
    gru_payload = {**_CAUSAL_IDENTITY, **(gru_overrides or {})}
    gru = _prediction(
        prediction_id="gru-causal",
        model_id=GRU_MODEL_ID,
        probabilities={"DOWN": 0.1, "FLAT": 0.1, "UP": 0.8},
        **gru_payload,
    )
    return baseline, gru


def test_paired_comparison_excludes_mismatched_causal_identity_with_reasons() -> None:
    cases = (
        (
            {"predicted_at": "2026-08-20T11:00:00+00:00", "ready_at": "2026-08-20T11:00:00+00:00"},
            "predicted_at_mismatch",
        ),
        ({"training_cutoff": "2026-08-18T00:00:00+00:00"}, "training_cutoff_mismatch"),
        ({"comparison_batch_id": "batch:other"}, "comparison_batch_id_mismatch"),
        ({"comparison_cohort_fingerprint": "2" * 64}, "training_lineage_mismatch"),
        ({"study_cohort_id": "world_cohort:v1:" + "c" * 64}, "study_cohort_id_mismatch"),
        ({"manifest_sha256": "d" * 64}, "manifest_sha256_mismatch"),
    )
    for mutation, reason in cases:
        baseline, gru = _causal_pair(gru_overrides=mutation)
        result = evaluate_shadow([baseline, gru], [_outcome()], minimum_paired_support=1)

        assert result["status"] == "warming_up"
        assert [(group["model_id"], group["matched"]) for group in result["groups"]] == [
            (BASELINE_MODEL_ID, 1),
            (GRU_MODEL_ID, 1),
        ]
        comparison = result["comparisons"][0]
        assert comparison["matched_pairs"] == 0
        assert comparison["status"] == "insufficient_support"
        assert comparison["baseline"] is None
        assert comparison["gru"] is None
        assert comparison["gru_minus_baseline"] is None
        assert result["excluded"][reason] == 1
        assert comparison["excluded"][reason] == 1


def test_generic_eval_dedup_is_order_independent_over_full_causal_proof() -> None:
    replay = _prediction(
        prediction_id="immutable-causal",
        model_id=BASELINE_MODEL_ID,
        **_CAUSAL_IDENTITY,
    )
    replay_copy = dict(replay)
    divergent_proof = _prediction(
        prediction_id="immutable-causal",
        model_id=BASELINE_MODEL_ID,
        **{**_CAUSAL_IDENTITY, "comparison_batch_id": "batch:other"},
    )
    outcome = _outcome()

    forward = evaluate_shadow([replay, replay_copy], [outcome], minimum_paired_support=1)
    reversed_replay = evaluate_shadow([replay_copy, replay], [outcome], minimum_paired_support=1)
    assert forward == reversed_replay
    assert forward["matched"] == 1
    assert forward["excluded"] == {"duplicate_prediction_replay": 1}

    forward_ambiguous = evaluate_shadow([replay, divergent_proof], [outcome], minimum_paired_support=1)
    reversed_ambiguous = evaluate_shadow([divergent_proof, replay], [outcome], minimum_paired_support=1)
    assert forward_ambiguous == reversed_ambiguous
    assert forward_ambiguous["matched"] == 0
    assert forward_ambiguous["status"] == "warming_up"
    assert forward_ambiguous["excluded"] == {"ambiguous_prediction_identity": 2}


def test_generic_eval_counts_absent_members_and_does_not_claim_ready_without_exact_pairs() -> None:
    baseline_only = _prediction(
        prediction_id="baseline-only",
        episode_id="episode-1",
        model_id=BASELINE_MODEL_ID,
        **_CAUSAL_IDENTITY,
    )
    gru_other_slot = _prediction(
        prediction_id="gru-other",
        episode_id="episode-2",
        model_id=GRU_MODEL_ID,
        created_at="2026-08-21T10:00:00+00:00",
        **{
            **_CAUSAL_IDENTITY,
            "predicted_at": "2026-08-21T10:00:00+00:00",
            "ready_at": "2026-08-21T10:00:00+00:00",
        },
    )
    outcomes = [
        _outcome(event_id="outcome-1", episode_id="episode-1"),
        _outcome(
            event_id="outcome-2",
            episode_id="episode-2",
            available_at="2026-08-21T14:15:00+00:00",
        ),
    ]
    forward = evaluate_shadow([baseline_only, gru_other_slot], outcomes, minimum_paired_support=1)
    reversed_rows = evaluate_shadow([gru_other_slot, baseline_only], outcomes, minimum_paired_support=1)

    assert forward == reversed_rows
    assert forward["status"] == "warming_up"
    comparison = forward["comparisons"][0]
    assert comparison["matched_pairs"] == 0
    assert comparison["status"] == "insufficient_support"
    assert comparison["excluded"]["absent_member"] == 2
    assert forward["excluded"]["absent_member"] == 2
    assert "predicted_at_mismatch" not in comparison["excluded"]


def test_paired_comparison_accepts_exact_causal_identity() -> None:
    baseline, gru = _causal_pair()
    result = evaluate_shadow([baseline, gru], [_outcome()], minimum_paired_support=1)

    comparison = result["comparisons"][0]
    assert result["status"] == "ready"
    assert comparison["status"] == "ready"
    assert comparison["matched_pairs"] == 1
    assert comparison["excluded"] == {}
    assert "predicted_at_mismatch" not in result["excluded"]
    assert "training_cutoff_mismatch" not in result["excluded"]
    assert "comparison_batch_id_mismatch" not in result["excluded"]
    assert "training_lineage_mismatch" not in result["excluded"]


def test_directional_shadow_drawdown_is_explicitly_not_a_portfolio_metric() -> None:
    predictions = [
        _prediction(prediction_id="p1", episode_id="episode-1"),
        _prediction(
            prediction_id="p2",
            episode_id="episode-2",
            created_at="2026-08-21T10:00:00+00:00",
        ),
        _prediction(
            prediction_id="p3",
            episode_id="episode-3",
            created_at="2026-08-22T10:00:00+00:00",
        ),
    ]
    outcomes = [
        _outcome(event_id="o1", episode_id="episode-1", simple_return=0.02),
        _outcome(
            event_id="o2",
            episode_id="episode-2",
            simple_return=-0.05,
            available_at="2026-08-21T14:15:00+00:00",
        ),
        _outcome(
            event_id="o3",
            episode_id="episode-3",
            simple_return=0.01,
            available_at="2026-08-22T14:15:00+00:00",
        ),
    ]

    result = evaluate_shadow(predictions, outcomes)
    proxy = result["groups"][0]["directional_shadow_non_portfolio_drawdown"]

    assert proxy["metric"] == "directional_shadow_non_portfolio_drawdown"
    assert proxy["status"] == "available"
    assert proxy["cumulative_directional_simple_return"] == pytest.approx(-0.02)
    assert proxy["max_drawdown"] == pytest.approx(0.05)
    assert proxy["non_portfolio"] is True
    assert proxy["horizons_may_overlap"] is True
    assert "not portfolio PnL" in proxy["caveat"]


def test_directional_shadow_drawdown_orders_by_actual_ready_time() -> None:
    predictions = [
        _prediction(
            prediction_id="p1",
            episode_id="episode-1",
            created_at="2026-08-20T10:00:00+00:00",
            ready_at="2026-08-23T10:00:00+00:00",
        ),
        _prediction(
            prediction_id="p2",
            episode_id="episode-2",
            created_at="2026-08-21T10:00:00+00:00",
            ready_at="2026-08-21T11:00:00+00:00",
        ),
        _prediction(
            prediction_id="p3",
            episode_id="episode-3",
            created_at="2026-08-22T10:00:00+00:00",
            ready_at="2026-08-22T11:00:00+00:00",
        ),
    ]
    outcomes = [
        _outcome(
            event_id="o1",
            episode_id="episode-1",
            simple_return=-0.10,
            available_at="2026-08-23T14:15:00+00:00",
        ),
        _outcome(
            event_id="o2",
            episode_id="episode-2",
            simple_return=0.20,
            available_at="2026-08-21T14:15:00+00:00",
        ),
        _outcome(
            event_id="o3",
            episode_id="episode-3",
            simple_return=-0.05,
            available_at="2026-08-22T14:15:00+00:00",
        ),
    ]

    result = evaluate_shadow(predictions, outcomes)
    proxy = result["groups"][0]["directional_shadow_non_portfolio_drawdown"]

    # Ready order is p2, p3, p1: +0.20, -0.05, -0.10, for a 0.15 drawdown.
    # Logical cutoff order would instead have reported 0.10.
    assert proxy["cumulative_directional_simple_return"] == pytest.approx(0.05)
    assert proxy["max_drawdown"] == pytest.approx(0.15)
