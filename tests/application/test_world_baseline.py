from __future__ import annotations

from copy import deepcopy

import pytest

from trader.application.world_model.baseline import (
    FeatureBoundaryError,
    FutureLabelLeakageError,
    HierarchicalDirichletWorldBaseline,
    OutcomeEventConflictError,
    build_feature_state,
)
from trader.domain.world_episode import WorldEpisode, WorldObservation


def _observation(
    *,
    at: str = "2026-01-01T00:00:00+00:00",
    macro: str = "quiet",
    regime: str = "trend_up",
) -> dict:
    return {
        "available_at": at,
        "categorical_features": {
            "asset_family": "equities",
            "venue": "NYSE",
            "session_phase": "regular",
            "market_regime": regime,
            "volatility_state": "normal",
            "macro_regime": macro,
        },
        "numeric_features": {
            "return": 0.006,
            "atr_pct": 0.01,
            "range_position": 0.72,
            "open_close_return": 0.004,
            "high_low_range_pct": 0.018,
            "realized_volatility": 0.012,
        },
    }


def _outcome(
    event_id: str,
    *,
    horizon: str = "elapsed_4h.v1",
    direction: str = "UP",
    available_at: str = "2026-01-01T04:05:00+00:00",
    status: str = "observed",
    training_eligible: bool = True,
) -> dict:
    return {
        "outcome_event_id": event_id,
        "horizon_id": horizon,
        "status": status,
        "direction": direction,
        "training_eligible": training_eligible,
        "available_at": available_at,
    }


def _ready_model(**overrides: object) -> HierarchicalDirichletWorldBaseline:
    settings: dict[str, object] = {
        "alpha": 1.0,
        "minimum_global_support": 1,
        "minimum_coarse_support": 1,
        "minimum_exact_support": 2,
    }
    settings.update(overrides)
    return HierarchicalDirichletWorldBaseline(**settings)


def test_cold_start_is_uniform_warming_up_and_permanently_shadow_only() -> None:
    model = HierarchicalDirichletWorldBaseline()

    prediction = model.predict(_observation(), "elapsed_4h.v1")

    assert dict(prediction.probabilities) == {"DOWN": 1 / 3, "FLAT": 1 / 3, "UP": 1 / 3}
    assert prediction.tier == "uniform"
    assert prediction.global_support == 0
    assert prediction.status == "warming_up"
    assert prediction.recommendation == "NO_GO"
    assert prediction.authority == "shadow_only"
    assert prediction.decision_effect == "none"
    assert prediction.non_authoritative is True
    with pytest.raises(ValueError, match="unsupported fixed horizon"):
        model.predict(_observation(), "4h")


def test_feature_projection_and_prediction_are_deterministic_without_mutating_input() -> None:
    observation = _observation()
    original = deepcopy(observation)
    first = _ready_model()
    second = _ready_model()

    assert first.apply_outcome(_outcome("event-1"), observation, available_through="2026-01-01T05:00:00+00:00").applied
    assert second.apply_outcome(_outcome("event-1"), observation, available_through="2026-01-01T05:00:00+00:00").applied
    first_prediction = first.predict(_observation(at="2026-01-02T00:00:00+00:00"), "elapsed_4h.v1")
    second_prediction = second.predict(_observation(at="2026-01-02T00:00:00+00:00"), "elapsed_4h.v1")

    assert observation == original
    assert first_prediction.feature_hash == second_prediction.feature_hash
    assert dict(first_prediction.probabilities) == dict(second_prediction.probabilities)
    assert first_prediction.model_fingerprint == second_prediction.model_fingerprint


def test_accepts_immutable_world_episode_and_emits_runner_safe_payload() -> None:
    observation = WorldObservation(
        venue="XNYS",
        symbol="SPY",
        bar_interval="1h",
        as_of_bar_ts="2026-01-01T00:00:00+00:00",
        feature_contract_version="world_features.v1",
        sampling_policy_version="first_fresh_bar.v1",
        anchor={
            "ts": "2026-01-01T00:00:00+00:00",
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": 100.5,
            "volume": 1000.0,
            "source": "test-source",
        },
        available_at="2026-01-01T00:00:00+00:00",
        captured_at="2026-01-01T00:01:00+00:00",
        freshness={"status": "fresh", "data_age_minutes": 1.0},
        categorical_features={"asset_family": "equities", "market_regime": "trend_up"},
        numeric_features={"return": 0.006, "atr_pct": 0.01},
    )
    episode = WorldEpisode(observation)

    prediction = _ready_model().predict(
        episode,
        "elapsed_4h.v1",
        prediction_at="2026-01-01T00:02:00+00:00",
    )
    payload = prediction.to_dict()
    state = build_feature_state(episode)

    assert prediction.episode_id == episode.episode_id
    assert payload["prediction_id"] == prediction.prediction_id
    assert payload["created_at"] == "2026-01-01T00:02:00+00:00"
    assert set(payload["probabilities"]) == {"DOWN", "FLAT", "UP"}
    assert state.features["venue"] == "xnys"
    assert state.features["data_freshness"] == "fresh"
    assert "data_age_bucket" in state.features
    with pytest.raises(TypeError):
        state.features["market_regime"] = "mutated"  # type: ignore[index]


def test_learning_uses_hierarchical_coarse_then_exact_backoff() -> None:
    model = _ready_model()
    first = _observation(macro="quiet")
    same_coarse_other_exact = _observation(at="2026-01-02T00:00:00+00:00", macro="event_risk")

    assert model.apply_outcome(_outcome("event-1"), first, available_through="2026-01-01T05:00:00+00:00").applied
    coarse = model.predict(same_coarse_other_exact, "elapsed_4h.v1")
    assert coarse.tier == "coarse"
    assert coarse.coarse_support == 1
    assert coarse.probabilities["UP"] > coarse.probabilities["DOWN"]

    assert model.apply_outcome(
        _outcome("event-2", available_at="2026-01-02T04:05:00+00:00"),
        same_coarse_other_exact,
        available_through="2026-01-02T05:00:00+00:00",
    ).applied
    assert model.apply_outcome(
        _outcome("event-3", available_at="2026-01-03T04:05:00+00:00"),
        same_coarse_other_exact,
        available_through="2026-01-03T05:00:00+00:00",
    ).applied
    exact = model.predict(
        _observation(at="2026-01-04T00:00:00+00:00", macro="event_risk"),
        "elapsed_4h.v1",
    )
    assert exact.tier == "exact"
    assert exact.exact_support == 2
    assert exact.probabilities["UP"] > exact.probabilities["DOWN"]


def test_horizons_are_isolated() -> None:
    model = _ready_model(minimum_exact_support=1)
    observation = _observation()

    assert model.apply_outcome(
        _outcome("four-hour-up", horizon="elapsed_4h.v1", direction="UP"), observation
    ).applied
    one_day_before = model.predict(_observation(at="2026-01-02T00:00:00+00:00"), "elapsed_1d.v1")
    four_hour_before = model.predict(_observation(at="2026-01-02T00:00:00+00:00"), "elapsed_4h.v1")
    assert one_day_before.tier == "uniform"
    assert four_hour_before.probabilities["UP"] > four_hour_before.probabilities["DOWN"]

    assert model.apply_outcome(
        _outcome(
            "one-day-down",
            horizon="elapsed_1d.v1",
            direction="DOWN",
            available_at="2026-01-02T00:05:00+00:00",
        ),
        observation,
    ).applied
    four_hour_after = model.predict(_observation(at="2026-01-03T00:00:00+00:00"), "elapsed_4h.v1")
    one_day_after = model.predict(_observation(at="2026-01-03T00:00:00+00:00"), "elapsed_1d.v1")
    assert dict(four_hour_after.probabilities) == dict(four_hour_before.probabilities)
    assert one_day_after.probabilities["DOWN"] > one_day_after.probabilities["UP"]


def test_outcome_event_is_idempotent_and_conflicts_fail_closed() -> None:
    model = _ready_model()
    observation = _observation()
    outcome = _outcome("durable-event")

    assert model.apply_outcome(outcome, observation).applied
    replay = model.apply_outcome(outcome, observation)
    assert replay.applied is False
    assert replay.reason == "duplicate_outcome_event"
    assert replay.global_support == 1

    changed = {**outcome, "direction": "DOWN"}
    with pytest.raises(OutcomeEventConflictError):
        model.apply_outcome(changed, observation)


def test_forbidden_controls_and_annotations_are_rejected_and_unknown_fields_are_not_features() -> None:
    safe = _observation()
    for key, value, feature_group in (
        ("action", "BUY", "categorical_features"),
        ("mandate_role", "core", "categorical_features"),
        ("memory_available", "true", "categorical_features"),
        ("macro_event_distance_hours", 1.0, "numeric_features"),
    ):
        forbidden = deepcopy(safe)
        forbidden[feature_group][key] = value
        with pytest.raises(FeatureBoundaryError, match="forbidden World feature"):
            build_feature_state(forbidden)

    harmless_unknown = deepcopy(safe)
    harmless_unknown["categorical_features"]["unreviewed_experiment_tag"] = "ignored"
    assert build_feature_state(harmless_unknown) == build_feature_state(safe)


def test_pending_or_ineligible_outcomes_do_not_train() -> None:
    model = _ready_model()
    observation = _observation()

    pending = model.apply_outcome(_outcome("pending", status="pending"), observation)
    ineligible = model.apply_outcome(_outcome("ineligible", training_eligible=False), observation)
    unsealed = model.apply_outcome({**_outcome("unsealed"), "sealed": False}, observation)

    assert pending.applied is False
    assert pending.reason == "outcome_not_observed"
    assert ineligible.applied is False
    assert ineligible.reason == "outcome_not_training_eligible"
    assert unsealed.applied is False
    assert unsealed.reason == "outcome_not_sealed"
    assert model.predict(_observation(), "elapsed_4h.v1").tier == "uniform"


def test_prediction_rejects_labels_that_were_not_available_at_t0() -> None:
    model = _ready_model()
    observation = _observation()
    blocked = model.apply_outcome(
        _outcome("not-yet-available"),
        observation,
        available_through="2026-01-01T01:00:00+00:00",
    )
    assert blocked.applied is False
    assert blocked.reason == "label_not_available_at_cutoff"
    assert model.predict(observation, "elapsed_4h.v1").tier == "uniform"

    assert model.apply_outcome(_outcome("future-label"), observation).applied
    same_cutoff = model.predict(
        observation,
        "elapsed_4h.v1",
        prediction_at="2026-01-01T04:05:00+00:00",
    )
    assert same_cutoff.global_support == 1

    with pytest.raises(FutureLabelLeakageError):
        model.predict(observation, "elapsed_4h.v1", prediction_at="2026-01-01T01:00:00+00:00")
