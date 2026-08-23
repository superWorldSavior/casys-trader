from __future__ import annotations

from copy import deepcopy

import pytest

from trader.application.world_model.baseline import (
    FeatureBoundaryError,
    FutureLabelLeakageError,
    HierarchicalDirichletWorldBaseline,
    OutcomeEventConflictError,
    build_feature_state,
    cold_markov_challenger,
)
from trader.application.world_model.context_capture import attach_world_context
from trader.application.world_model.encoding import world_lane_encoder_profile
from trader.domain.world_cohort import WorldLaneDefinition
from trader.domain.world_context import SensorEvidence
from trader.domain.world_episode import WorldEpisode, WorldObservation, canonical_sha256
from trader.domain.world_feature_contract import (
    WORLD_V3_MARKOV_MODEL_IDENTITY,
    WORLD_V3_MODEL_VERSION,
    world_v1_feature_contract,
    world_v2_feature_contract,
    world_v3_feature_contract,
)

from tests.application.test_world_context_capture import _FakeSource, _v1_episode
from tests.application.test_world_gru import HORIZON_4H, _outcome as _gru_outcome

STARTED_EVENT_ID = "world_cohort_started:v1:" + "a" * 64
STUDY_COHORT_ID = "world_cohort:v1:" + "b" * 64
MANIFEST_SHA256 = "c" * 64


def _with_context_categoricals(episode: WorldEpisode, **updates: str) -> WorldEpisode:
    context = dict(episode.observation.to_dict()["context"])
    context.pop("content_sha256", None)
    context.pop("context_id", None)
    features = dict(context.get("categorical_features") or {})
    features.update(updates)
    context["categorical_features"] = features
    payload = episode.observation.to_dict()
    payload["context"] = context
    return WorldEpisode(WorldObservation.from_dict(payload))


def _observation(
    *,
    at: str = "2026-01-01T00:00:00+00:00",
    macro: str = "quiet",
    regime: str = "trend_up",
) -> dict:
    return {
        "available_at": at,
        "feature_contract_version": "market_ohlcv_causal.v1",
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
        feature_contract_version="market_ohlcv_causal.v1",
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

    assert model.apply_outcome(_outcome("four-hour-up", horizon="elapsed_4h.v1", direction="UP"), observation).applied
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


def _lane(lane_id: str, *, family: str = "markov", profile=None, sequence_length: int | None = None) -> WorldLaneDefinition:
    resolved = profile or world_lane_encoder_profile("market")
    return WorldLaneDefinition(
        lane_id=lane_id,
        model_family=family,
        model_id="hierarchical_dirichlet_world_baseline",
        model_version=f"cohort.{lane_id}.v1",
        feature_contract_id=resolved.contract.contract_id,
        feature_contract_fingerprint=resolved.contract.fingerprint,
        feature_mask_id=resolved.mask.mask_id,
        feature_mask_fingerprint=resolved.mask.fingerprint,
        seed=0,
        sequence_length=sequence_length,
        hyperparameters_sha256=canonical_sha256({"family": family, "lane": lane_id}),
        role="primary_control",
    )


def _cold_markov(kind: str = "market", **overrides: object) -> HierarchicalDirichletWorldBaseline:
    profile = world_lane_encoder_profile(kind)
    settings: dict[str, object] = {
        "lane": _lane(f"markov.{kind}", profile=profile),
        "contract": profile.contract,
        "mask": profile.mask,
        "study_cohort_id": STUDY_COHORT_ID,
        "manifest_sha256": MANIFEST_SHA256,
        "started_event_id": STARTED_EVENT_ID,
        "alpha": 1.0,
        "minimum_global_support": 1,
        "minimum_coarse_support": 1,
        "minimum_exact_support": 2,
    }
    settings.update(overrides)
    return cold_markov_challenger(**settings)  # type: ignore[arg-type]


def test_market_mask_predictions_match_v1_facade_bit_for_bit() -> None:
    observation = _observation()
    facade = _ready_model()
    masked = _cold_markov("market")
    assert facade.apply_outcome(_outcome("event-1"), observation).applied
    assert masked.apply_outcome(_outcome("event-1"), observation).applied
    later = _observation(at="2026-01-02T00:00:00+00:00")
    first = facade.predict(later, "elapsed_4h.v1")
    second = masked.predict(later, "elapsed_4h.v1")
    assert first.feature_hash == second.feature_hash
    assert dict(first.probabilities) == dict(second.probabilities)
    assert first.comparison_batch_id == second.comparison_batch_id
    assert first.comparison_cohort_fingerprint == second.comparison_cohort_fingerprint
    assert masked.feature_mask.mask_id == "market.v1"
    assert masked.feature_contract == world_v1_feature_contract()
    assert masked.predict(later, "elapsed_4h.v1").global_support == 1


def test_cold_markov_challengers_are_independent_matched_and_refuse_warm_reuse() -> None:
    v1 = _v1_episode()
    v2 = attach_world_context(
        (v1,),
        _FakeSource(
            SensorEvidence(status="missing", reason="no_artifact"),
            SensorEvidence(status="missing", reason="no_artifact"),
        ),
    )[0]
    lanes = {kind: _cold_markov(kind) for kind in ("market", "status_only", "company", "macro", "joint")}
    predicted_at = v1.observation.available_at
    market = lanes["market"].predict(v1, HORIZON_4H, prediction_at=predicted_at)
    status = lanes["status_only"].predict(v2, HORIZON_4H, prediction_at=predicted_at)
    company = lanes["company"].predict(v2, HORIZON_4H, prediction_at=predicted_at)
    macro = lanes["macro"].predict(v2, HORIZON_4H, prediction_at=predicted_at)
    joint = lanes["joint"].predict(v2, HORIZON_4H, prediction_at=predicted_at)
    assert len({market.comparison_batch_id, status.comparison_batch_id, company.comparison_batch_id, macro.comparison_batch_id, joint.comparison_batch_id}) == 1
    assert len({item.comparison_cohort_fingerprint for item in (market, status, company, macro, joint)}) == 1
    assert all(item.global_support == 0 for item in (market, status, company, macro, joint))
    assert lanes["status_only"].lane_identity.started_event_id == STARTED_EVENT_ID
    assert lanes["status_only"].lane_identity.replay_bound_event_id == STARTED_EVENT_ID
    assert lanes["status_only"].lane_identity.prior_training_lineage == ()

    outcome = _gru_outcome(v1)
    assert lanes["market"].apply_outcome(outcome, v1).applied
    sibling = _cold_markov("market")
    assert sibling.predict(v1, HORIZON_4H, prediction_at=outcome.available_at).global_support == 0
    with pytest.raises(ValueError, match="warm|trained|reuse"):
        cold_markov_challenger(
            lane=_lane("markov.market"),
            contract=world_v1_feature_contract(),
            mask=world_lane_encoder_profile("market").mask,
            study_cohort_id=STUDY_COHORT_ID,
            manifest_sha256=MANIFEST_SHA256,
            started_event_id=STARTED_EVENT_ID,
            prototype=lanes["market"],
        )
    gru_lane = WorldLaneDefinition(
        lane_id="gru.market",
        model_family="gru",
        model_id="online_gru_world_challenger",
        model_version="cohort.gru.market.v1",
        feature_contract_id=world_v1_feature_contract().contract_id,
        feature_contract_fingerprint=world_v1_feature_contract().fingerprint,
        feature_mask_id=world_lane_encoder_profile("market").mask.mask_id,
        feature_mask_fingerprint=world_lane_encoder_profile("market").mask.fingerprint,
        seed=0,
        sequence_length=4,
        hyperparameters_sha256=canonical_sha256({"family": "gru", "lane": "gru.market"}),
        role="primary_control",
    )
    with pytest.raises(ValueError, match="markov|family"):
        cold_markov_challenger(
            lane=gru_lane,
            contract=world_v1_feature_contract(),
            mask=world_lane_encoder_profile("market").mask,
            study_cohort_id=STUDY_COHORT_ID,
            manifest_sha256=MANIFEST_SHA256,
            started_event_id=STARTED_EVENT_ID,
        )


def test_status_only_markov_does_not_see_company_or_macro_content() -> None:
    v2 = attach_world_context(
        (_v1_episode(),),
        _FakeSource(
            SensorEvidence(status="missing", reason="no_artifact"),
            SensorEvidence(status="missing", reason="no_artifact"),
        ),
    )[0]
    intact = _with_context_categoricals(v2, company_thesis_status="intact", context_macro_regime="risk_on")
    other = _with_context_categoricals(v2, company_thesis_status="broken", context_macro_regime="risk_off")
    status = _cold_markov("status_only")
    company = _cold_markov("company")
    macro = _cold_markov("macro")
    joint = _cold_markov("joint")
    predicted_at = v2.observation.available_at
    assert status.predict(intact, HORIZON_4H, prediction_at=predicted_at).feature_hash == status.predict(
        other, HORIZON_4H, prediction_at=predicted_at
    ).feature_hash
    assert company.predict(intact, HORIZON_4H, prediction_at=predicted_at).feature_hash != company.predict(
        other, HORIZON_4H, prediction_at=predicted_at
    ).feature_hash
    assert macro.predict(intact, HORIZON_4H, prediction_at=predicted_at).feature_hash != macro.predict(
        other, HORIZON_4H, prediction_at=predicted_at
    ).feature_hash
    assert joint.predict(intact, HORIZON_4H, prediction_at=predicted_at).feature_hash != joint.predict(
        other, HORIZON_4H, prediction_at=predicted_at
    ).feature_hash
    assert company.feature_contract == world_v2_feature_contract()
    facade = HierarchicalDirichletWorldBaseline(include_context=True, model_version="context.v2", alpha=1.0)
    masked_joint = _cold_markov("joint")
    assert facade.predict(intact, HORIZON_4H, prediction_at=predicted_at).feature_hash == masked_joint.predict(
        intact, HORIZON_4H, prediction_at=predicted_at
    ).feature_hash


def test_cold_markov_v3_topology_status_only_is_constructible_and_isolated() -> None:
    profile = world_lane_encoder_profile("topology_status_only")
    lane = WorldLaneDefinition(
        lane_id="markov.topology_status_only",
        model_family="markov",
        model_id=WORLD_V3_MARKOV_MODEL_IDENTITY,
        model_version=WORLD_V3_MODEL_VERSION,
        feature_contract_id=profile.contract.contract_id,
        feature_contract_fingerprint=profile.contract.fingerprint,
        feature_mask_id=profile.mask.mask_id,
        feature_mask_fingerprint=profile.mask.fingerprint,
        seed=0,
        sequence_length=None,
        hyperparameters_sha256=canonical_sha256({"family": "markov", "lane": "topology_status_only"}),
        role="secondary_challenger",
    )
    model = cold_markov_challenger(
        lane=lane,
        contract=profile.contract,
        mask=profile.mask,
        study_cohort_id=STUDY_COHORT_ID,
        manifest_sha256=MANIFEST_SHA256,
        started_event_id=STARTED_EVENT_ID,
        alpha=1.0,
        minimum_global_support=1,
        minimum_coarse_support=1,
        minimum_exact_support=2,
    )
    v1 = _v1_episode()
    v3 = WorldEpisode(
        WorldObservation.from_dict(
            {
                **v1.observation.to_dict(),
                "feature_contract_version": profile.contract.accepted_episode_contract,
            }
        )
    )
    assert model.feature_contract == world_v3_feature_contract()
    assert model.feature_mask.mask_id == "topology_status_only.v1"
    assert model.model_id == WORLD_V3_MARKOV_MODEL_IDENTITY
    assert model.accepts_episode(v3) is True
    assert model.accepts_episode(v1) is False
    assert model.lane_identity.prior_training_lineage == ()
    prediction = model.predict(v3, HORIZON_4H, prediction_at=v3.observation.available_at)
    assert prediction.global_support == 0
    with pytest.raises(FeatureBoundaryError):
        model.predict(v1, HORIZON_4H)
    mapping = {
        "available_at": "2026-01-01T00:00:00+00:00",
        "as_of_bar_ts": "2026-01-01T00:00:00+00:00",
        "venue": "XNYS",
        "symbol": "SPY",
        "bar_interval": "1h",
        "feature_contract_version": profile.contract.accepted_episode_contract,
        "categorical_features": {
            "asset_family": "equities",
            "venue": "XNYS",
            "session_phase": "regular",
            "market_regime": "trend_up",
            "volatility_state": "normal",
        },
        "numeric_features": {"return": 0.006, "atr_pct": 0.01, "range_position": 0.72},
        "graph_features": {
            "categorical_features": {
                "graph_status": "complete",
                "graph_missingness_status": "none",
                "graph_path_signature": "sig_a",
            }
        },
    }
    other = {
        **mapping,
        "graph_features": {
            "categorical_features": {
                "graph_status": "complete",
                "graph_missingness_status": "none",
                "graph_path_signature": "sig_b",
            }
        },
    }
    first = model.predict(mapping, HORIZON_4H, prediction_at="2026-01-01T00:00:00+00:00")
    second = model.predict(other, HORIZON_4H, prediction_at="2026-01-01T00:00:00+00:00")
    assert first.feature_hash == second.feature_hash
