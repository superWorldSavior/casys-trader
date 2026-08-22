from __future__ import annotations

from trader.application.world_model.evaluation import evaluate_shadow


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
        [_prediction(created_at="2026-08-20T15:00:00+00:00")],
        [_outcome()],
    )

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
