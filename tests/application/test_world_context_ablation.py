from __future__ import annotations

from trader.reporting.read_models.world_evaluation import (
    BASELINE_MODEL_ID,
    GRU_MODEL_ID,
    evaluate_context_ablation,
    evaluate_shadow,
)

BATCH = "world-comparison-batch:v1:shared"
COHORT = "world-comparison-cohort:v1:shared"


def _prediction(**overrides):
    payload = {
        "episode_id": "episode-market",
        "horizon_id": "elapsed_4h.v1",
        "model_id": BASELINE_MODEL_ID,
        "model_version": "v1",
        "probabilities": {"DOWN": 0.2, "FLAT": 0.2, "UP": 0.6},
        "created_at": "2026-08-20T10:00:00+00:00",
        "ready_at": "2026-08-20T10:00:00+00:00",
        "comparison_batch_id": BATCH,
        "comparison_cohort_fingerprint": COHORT,
        "training_cutoff": None,
        "input": {
            "venue": "XTAI",
            "symbol": "AAA",
            "bar_interval": "1h",
            "as_of_bar_ts": "2026-08-20T09:00:00+00:00",
            "feature_contract_version": "world_feature.market.v1",
        },
    }
    payload.update(overrides)
    return payload


def _outcome(**overrides):
    payload = {
        "event_id": "outcome-1",
        "episode_id": "episode-market",
        "horizon_id": "elapsed_4h.v1",
        "status": "observed",
        "direction": "UP",
        "training_eligible": True,
        "available_at": "2026-08-20T14:15:00+00:00",
    }
    payload.update(overrides)
    return payload


def test_existing_baseline_gru_comparison_remains_compatible() -> None:
    result = evaluate_shadow(
        [
            _prediction(prediction_id="b1"),
            _prediction(
                prediction_id="g1",
                episode_id="episode-market",
                model_id=GRU_MODEL_ID,
                probabilities={"DOWN": 0.1, "FLAT": 0.1, "UP": 0.8},
            ),
        ],
        [_outcome()],
        minimum_paired_support=1,
    )
    assert result["comparisons"][0]["status"] == "ready"
    assert result["context_ablation"]["status"] in {"not_applicable", "insufficient_support"}


def test_context_ablation_pairs_by_market_anchor_not_episode_id() -> None:
    market = _prediction(prediction_id="markov-v1")
    context = _prediction(
        prediction_id="markov-v2",
        episode_id="episode-context",
        model_version="context.v1",
        probabilities={"DOWN": 0.05, "FLAT": 0.05, "UP": 0.9},
        input={
            "venue": "XTAI",
            "symbol": "AAA",
            "bar_interval": "1h",
            "as_of_bar_ts": "2026-08-20T09:00:00+00:00",
            "feature_contract_version": "world_feature.context.v1",
            "context": {"status": "complete"},
        },
    )
    gru_market = _prediction(
        prediction_id="gru-v1",
        model_id=GRU_MODEL_ID,
        probabilities={"DOWN": 0.3, "FLAT": 0.3, "UP": 0.4},
    )
    gru_context = _prediction(
        prediction_id="gru-v2",
        episode_id="episode-context",
        model_id=GRU_MODEL_ID,
        model_version="context.v1",
        probabilities={"DOWN": 0.1, "FLAT": 0.1, "UP": 0.8},
        input=context["input"],
    )
    result = evaluate_shadow(
        [market, context, gru_market, gru_context],
        [
            _outcome(),
            _outcome(event_id="outcome-context", episode_id="episode-context"),
        ],
        minimum_paired_support=1,
    )
    ablation = result["context_ablation"]
    assert ablation["causal_claim"] is False
    assert ablation["pnl_claim"] is False
    families = {item["model_family"]: item for item in ablation["families"]}
    assert families["markov"]["matched_pairs"] == 1
    assert families["gru"]["matched_pairs"] == 1
    assert families["markov"]["context_minus_market"]["log_loss"] < 0
    assert families["markov"]["primary_delta"] == "paired_multiclass_log_loss"
    assert ablation["coverage"]["complete"] >= 1


def test_ablation_rejects_label_mismatch_and_withholds_below_support() -> None:
    market = _prediction(prediction_id="v1")
    context = _prediction(
        prediction_id="v2",
        episode_id="episode-context",
        model_version="context.v1",
        input={
            "venue": "XTAI",
            "symbol": "AAA",
            "bar_interval": "1h",
            "as_of_bar_ts": "2026-08-20T09:00:00+00:00",
            "feature_contract_version": "world_feature.context.v1",
        },
    )
    result = evaluate_shadow(
        [market, context],
        [
            _outcome(direction="UP"),
            _outcome(event_id="other", episode_id="episode-context", direction="DOWN"),
        ],
        minimum_paired_support=5,
    )
    markov = result["context_ablation"]["families"][0]
    assert markov["matched_pairs"] == 0
    assert markov["status"] == "insufficient_support"
    assert markov["conclusion"] is None
    assert result["context_ablation"]["excluded"]["label_mismatch"] == 1


def test_ablation_respects_custom_model_ids_and_valid_pair() -> None:
    market = _prediction(prediction_id="custom-v1", model_id="custom_markov")
    context = _prediction(
        prediction_id="custom-v2",
        episode_id="episode-context",
        model_id="custom_markov",
        model_version="context.v1",
        probabilities={"DOWN": 0.05, "FLAT": 0.05, "UP": 0.9},
        input={
            "venue": "XTAI",
            "symbol": "AAA",
            "bar_interval": "1h",
            "as_of_bar_ts": "2026-08-20T09:00:00+00:00",
            "feature_contract_version": "world_feature.context.v1",
            "context": {"status": "complete"},
        },
    )
    ablation = evaluate_context_ablation(
        [market, context],
        [
            _outcome(),
            _outcome(event_id="outcome-context", episode_id="episode-context"),
        ],
        minimum_paired_support=1,
        baseline_model_id="custom_markov",
        gru_model_id="unused_gru",
    )
    families = {item["model_family"]: item for item in ablation["families"]}
    assert families["markov"]["market_model_id"] == "custom_markov"
    assert families["markov"]["matched_pairs"] == 1
    assert families["markov"]["status"] == "ready"


def test_ablation_rejects_duplicate_anchors_and_mismatched_target_evidence() -> None:
    market = _prediction(prediction_id="v1-a")
    duplicate = _prediction(
        prediction_id="v1-b",
        episode_id="episode-market-2",
        probabilities={"DOWN": 0.4, "FLAT": 0.2, "UP": 0.4},
    )
    context = _prediction(
        prediction_id="v2",
        episode_id="episode-context",
        model_version="context.v1",
        input={
            "venue": "XTAI",
            "symbol": "AAA",
            "bar_interval": "1h",
            "as_of_bar_ts": "2026-08-20T09:00:00+00:00",
            "feature_contract_version": "world_feature.context.v1",
        },
    )
    result = evaluate_shadow(
        [market, duplicate, context],
        [
            _outcome(),
            _outcome(event_id="outcome-market-2", episode_id="episode-market-2"),
            _outcome(event_id="outcome-context", episode_id="episode-context"),
        ],
        minimum_paired_support=1,
    )
    assert result["context_ablation"]["excluded"]["ambiguous_market_anchor"] == 2

    market_only = _prediction(prediction_id="v1")
    context_other_target = _prediction(
        prediction_id="v2-target",
        episode_id="episode-context",
        model_version="context.v1",
        input={
            "venue": "XTAI",
            "symbol": "AAA",
            "bar_interval": "1h",
            "as_of_bar_ts": "2026-08-20T09:00:00+00:00",
            "feature_contract_version": "world_feature.context.v1",
        },
    )
    mismatched = evaluate_shadow(
        [market_only, context_other_target],
        [
            _outcome(target_at="2026-08-20T14:00:00+00:00", source_raw_sha256="aaa"),
            _outcome(
                event_id="outcome-context",
                episode_id="episode-context",
                target_at="2026-08-20T18:00:00+00:00",
                source_raw_sha256="bbb",
            ),
        ],
        minimum_paired_support=1,
    )
    assert mismatched["context_ablation"]["excluded"]["label_evidence_mismatch"] == 1
    assert mismatched["context_ablation"]["families"][0]["matched_pairs"] == 0


def test_ablation_excludes_asynchronous_and_training_cohort_mismatches() -> None:
    market = _prediction(prediction_id="v1")
    later = _prediction(
        prediction_id="v2-later",
        episode_id="episode-context",
        model_version="context.v1",
        created_at="2026-08-20T11:00:00+00:00",
        comparison_batch_id="world-comparison-batch:v1:other",
        input={
            "venue": "XTAI",
            "symbol": "AAA",
            "bar_interval": "1h",
            "as_of_bar_ts": "2026-08-20T09:00:00+00:00",
            "feature_contract_version": "world_feature.context.v1",
        },
    )
    later_result = evaluate_shadow(
        [market, later],
        [_outcome(), _outcome(event_id="outcome-context", episode_id="episode-context")],
        minimum_paired_support=1,
    )
    assert later_result["context_ablation"]["excluded"]["asynchronous_lane_mismatch"] == 1
    assert later_result["context_ablation"]["families"][0]["status"] != "ready"

    cutoff_mismatch = _prediction(
        prediction_id="v2-cutoff",
        episode_id="episode-context",
        model_version="context.v1",
        training_cutoff="2026-08-19T00:00:00+00:00",
        input={
            "venue": "XTAI",
            "symbol": "AAA",
            "bar_interval": "1h",
            "as_of_bar_ts": "2026-08-20T09:00:00+00:00",
            "feature_contract_version": "world_feature.context.v1",
        },
    )
    cutoff_result = evaluate_shadow(
        [market, cutoff_mismatch],
        [_outcome(), _outcome(event_id="outcome-context", episode_id="episode-context")],
        minimum_paired_support=1,
    )
    assert cutoff_result["context_ablation"]["excluded"]["training_cohort_mismatch"] == 1

    missing_cohort = _prediction(
        prediction_id="v2-missing-cohort",
        episode_id="episode-context",
        model_version="context.v1",
        comparison_cohort_fingerprint=None,
        input={
            "venue": "XTAI",
            "symbol": "AAA",
            "bar_interval": "1h",
            "as_of_bar_ts": "2026-08-20T09:00:00+00:00",
            "feature_contract_version": "world_feature.context.v1",
        },
    )
    missing_result = evaluate_shadow(
        [market, missing_cohort],
        [_outcome(), _outcome(event_id="outcome-context", episode_id="episode-context")],
        minimum_paired_support=1,
    )
    assert missing_result["context_ablation"]["excluded"]["training_cohort_mismatch"] == 1

    different_cohort = _prediction(
        prediction_id="v2-other-cohort",
        episode_id="episode-context",
        model_version="context.v1",
        comparison_cohort_fingerprint="world-comparison-cohort:v1:other",
        input={
            "venue": "XTAI",
            "symbol": "AAA",
            "bar_interval": "1h",
            "as_of_bar_ts": "2026-08-20T09:00:00+00:00",
            "feature_contract_version": "world_feature.context.v1",
        },
    )
    different_result = evaluate_shadow(
        [market, different_cohort],
        [_outcome(), _outcome(event_id="outcome-context", episode_id="episode-context")],
        minimum_paired_support=1,
    )
    assert different_result["context_ablation"]["excluded"]["training_cohort_mismatch"] == 1


def test_ablation_pairs_store_shaped_rows_and_rejects_nested_evidence_mismatch() -> None:
    market = {
        "episode_id": "episode-market",
        "horizon_code": "elapsed_4h.v1",
        "model_kind": BASELINE_MODEL_ID,
        "model_id": BASELINE_MODEL_ID,
        "model_version": "v1",
        "predicted_at": "2026-08-20T10:00:00+00:00",
        "ready_at": "2026-08-20T10:00:00+00:00",
        "prediction": {
            "probabilities": {"DOWN": 0.2, "FLAT": 0.2, "UP": 0.6},
            "created_at": "2026-08-20T10:00:00+00:00",
            "comparison_batch_id": BATCH,
            "comparison_cohort_fingerprint": COHORT,
            "training_cutoff": None,
        },
        "prediction_record": {
            "input": {
                "venue": "XTAI",
                "symbol": "AAA",
                "bar_interval": "1h",
                "as_of_bar_ts": "2026-08-20T09:00:00+00:00",
                "feature_contract_version": "world_feature.market.v1",
            },
            "prediction": {
                "probabilities": {"DOWN": 0.2, "FLAT": 0.2, "UP": 0.6},
                "comparison_batch_id": BATCH,
                "comparison_cohort_fingerprint": COHORT,
            },
        },
    }
    context = {
        "episode_id": "episode-context",
        "horizon_code": "elapsed_4h.v1",
        "model_kind": BASELINE_MODEL_ID,
        "model_id": BASELINE_MODEL_ID,
        "model_version": "context.v1",
        "predicted_at": "2026-08-20T10:00:00+00:00",
        "ready_at": "2026-08-20T10:00:00+00:00",
        "prediction": {
            "probabilities": {"DOWN": 0.05, "FLAT": 0.05, "UP": 0.9},
            "created_at": "2026-08-20T10:00:00+00:00",
            "comparison_batch_id": BATCH,
            "comparison_cohort_fingerprint": COHORT,
            "training_cutoff": None,
        },
        "prediction_record": {
            "input": {
                "venue": "XTAI",
                "symbol": "AAA",
                "bar_interval": "1h",
                "as_of_bar_ts": "2026-08-20T09:00:00+00:00",
                "feature_contract_version": "world_feature.context.v1",
                "context": {"status": "complete"},
            },
            "prediction": {
                "probabilities": {"DOWN": 0.05, "FLAT": 0.05, "UP": 0.9},
                "comparison_batch_id": BATCH,
                "comparison_cohort_fingerprint": COHORT,
            },
        },
    }
    matched_outcome_market = {
        "event_id": "outcome-1",
        "episode_id": "episode-market",
        "horizon_id": "elapsed_4h.v1",
        "status": "observed",
        "training_eligible": True,
        "available_at": "2026-08-20T14:15:00+00:00",
        "outcome": {"status": "observed"},
        "label": {"direction": "UP", "target_at": "2026-08-20T14:00:00+00:00", "simple_return": 0.01},
        "evidence": {"target_at": "2026-08-20T14:00:00+00:00", "source_raw_sha256": "aaa"},
    }
    matched_outcome_context = {
        **matched_outcome_market,
        "event_id": "outcome-context",
        "episode_id": "episode-context",
    }
    matched = evaluate_shadow(
        [market, context],
        [matched_outcome_market, matched_outcome_context],
        minimum_paired_support=1,
    )
    assert matched["context_ablation"]["families"][0]["matched_pairs"] == 1
    assert matched["context_ablation"]["families"][0]["status"] == "ready"

    mismatched_context = {
        **matched_outcome_context,
        "label": {"direction": "UP", "target_at": "2026-08-20T18:00:00+00:00", "simple_return": 0.01},
        "evidence": {"target_at": "2026-08-20T18:00:00+00:00", "source_raw_sha256": "bbb"},
    }
    mismatched = evaluate_shadow(
        [market, context],
        [matched_outcome_market, mismatched_context],
        minimum_paired_support=1,
    )
    assert mismatched["context_ablation"]["excluded"]["label_evidence_mismatch"] == 1
    assert mismatched["context_ablation"]["families"][0]["matched_pairs"] == 0


def test_unknown_feature_contract_is_excluded_from_context_ablation() -> None:
    market = _prediction(prediction_id="v1", feature_contract_version=None)
    market["input"] = {
        "venue": "XTAI",
        "symbol": "AAA",
        "bar_interval": "1h",
        "as_of_bar_ts": "2026-08-20T09:00:00+00:00",
    }
    context = _prediction(
        prediction_id="v2",
        episode_id="episode-context",
        model_version="context.v1",
        input={
            "venue": "XTAI",
            "symbol": "AAA",
            "bar_interval": "1h",
            "as_of_bar_ts": "2026-08-20T09:00:00+00:00",
            "feature_contract_version": "world_feature.context.v1",
        },
    )
    result = evaluate_shadow(
        [market, context],
        [_outcome(), _outcome(event_id="outcome-context", episode_id="episode-context")],
        minimum_paired_support=1,
    )
    assert result["context_ablation"]["excluded"]["feature_contract_mismatch"] == 1
    assert result["context_ablation"]["families"][0]["matched_pairs"] == 0
