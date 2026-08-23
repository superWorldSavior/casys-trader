from __future__ import annotations

from datetime import datetime, timedelta, timezone
import math

import pytest

from trader.application.world_model.context_capture import attach_world_context
from trader.application.world_model.encoding import (
    FeatureBoundaryError,
    FutureLabelLeakageError,
    OutcomeEventConflictError,
    world_lane_encoder_profile,
)
from trader.application.world_model.gru import OnlineGRUWorldChallenger, cold_gru_challenger
from trader.domain.world_cohort import WorldLaneDefinition
from trader.domain.world_context import SensorEvidence
from trader.domain.world_episode import WorldEpisode, WorldObservation, WorldOutcome, canonical_sha256
from trader.domain.world_feature_contract import (
    WORLD_V3_GRU_MODEL_IDENTITY,
    WORLD_V3_MODEL_VERSION,
    world_v1_feature_contract,
    world_v2_feature_contract,
    world_v3_feature_contract,
)

from tests.application.test_world_context_capture import _FakeSource, _v1_episode


UTC = timezone.utc
HORIZON_4H = "elapsed_4h.v1"
HORIZON_1D = "elapsed_1d.v1"


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _episode(
    hour: int,
    *,
    symbol: str = "SPY",
    market_regime: str = "trend_up",
    macro_regime: str = "quiet",
    return_value: float = 0.006,
    available_after_hours: int = 0,
    feature_contract_version: str = "market_ohlcv_causal.v1",
    sampling_policy_version: str = "first_fresh_bar.v1",
) -> WorldEpisode:
    as_of = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(hours=hour)
    available_at = as_of + timedelta(hours=available_after_hours)
    return WorldEpisode(
        WorldObservation(
            venue="XNYS",
            symbol=symbol,
            bar_interval="1h",
            as_of_bar_ts=_iso(as_of),
            feature_contract_version=feature_contract_version,
            sampling_policy_version=sampling_policy_version,
            anchor={
                "ts": _iso(as_of),
                "open": 100.0,
                "high": 101.0,
                "low": 99.0,
                "close": 100.5,
                "volume": 1000.0,
                "source": "test-market",
            },
            available_at=_iso(available_at),
            captured_at=_iso(available_at + timedelta(minutes=1)),
            freshness={"status": "fresh", "data_age_minutes": 1.0},
            categorical_features={
                "asset_family": "equities",
                "session_phase": "regular",
                "market_regime": market_regime,
                "volatility_state": "normal",
                "macro_regime": macro_regime,
                "geopolitical_risk_bucket": "low",
            },
            numeric_features={
                "return": return_value,
                "atr_pct": 0.01,
                "range_position": 0.72,
                "relative_volume": 1.2,
                "open_close_return": 0.004,
                "high_low_range_pct": 0.018,
                "realized_volatility": 0.012,
            },
        )
    )


def _outcome(
    episode: WorldEpisode,
    *,
    horizon_id: str = HORIZON_4H,
    simple_return: float = 0.01,
    available_after_hours: int = 4,
    source_hash: str | None = None,
) -> WorldOutcome:
    available_at = episode.observation.available_at + timedelta(hours=available_after_hours)  # type: ignore[operator]
    endpoint = 100.5 * (1.0 + simple_return)
    duration = 4 * 60 * 60 if horizon_id == HORIZON_4H else 24 * 60 * 60
    return WorldOutcome(
        episode_id=episode.episode_id,
        horizon={"horizon_id": horizon_id, "duration_seconds": duration},
        status="observed",
        target_at=_iso(available_at),
        available_at=_iso(available_at),
        computed_at=_iso(available_at),
        anchor_close=100.5,
        endpoint_close=endpoint,
        endpoint_bar_ts=_iso(available_at),
        source="test-market",
        source_raw_sha256=source_hash or f"evidence-{episode.episode_id}-{horizon_id}",
        training_eligible=True,
    )


def _model(**overrides: object) -> OnlineGRUWorldChallenger:
    settings: dict[str, object] = {
        "sequence_len": 4,
        "hidden_size": 5,
        "learning_rate": 0.05,
        "gradient_clip": 0.75,
        "minimum_global_support": 2,
        "seed": 17,
    }
    settings.update(overrides)
    return OnlineGRUWorldChallenger(**settings)


def test_cold_start_allows_padded_sequence_and_is_permanently_shadow_only() -> None:
    model = _model()
    episode = _episode(0)

    prediction = model.predict(episode, HORIZON_4H)
    metadata = model.sequence_metadata(episode)
    diagnostics = model.diagnostics(HORIZON_4H)

    assert dict(prediction.probabilities) == {"DOWN": 1 / 3, "FLAT": 1 / 3, "UP": 1 / 3}
    assert prediction.predicted_class == "FLAT"
    assert prediction.status == "warming_up"
    assert prediction.tier == "uniform"
    assert prediction.recommendation == "NO_GO"
    assert prediction.authority == "shadow_only"
    assert prediction.decision_effect == "none"
    assert metadata.episode_ids == (episode.episode_id,)
    assert metadata.observed_steps == 1
    assert metadata.padded_steps == 3
    assert diagnostics["support"] == 0
    assert diagnostics["training_steps"] == 0
    assert diagnostics["recommendation"] == "NO_GO"


def test_observed_world_outcome_trains_online_once_and_is_deterministic() -> None:
    episode = _episode(0)
    outcome = _outcome(episode, simple_return=0.01)
    first = _model()
    second = _model()

    first_update = first.apply_outcome(outcome, episode)
    second_update = second.apply_outcome(outcome, episode)
    first_prediction = first.predict(episode, HORIZON_4H, prediction_at=outcome.available_at)
    second_prediction = second.predict(episode, HORIZON_4H, prediction_at=outcome.available_at)

    assert first_update.applied is True
    assert second_update.applied is True
    assert first.support(HORIZON_4H) == 1
    assert first.training_steps(HORIZON_4H) == 1
    assert dict(first_prediction.probabilities) == dict(second_prediction.probabilities)
    assert first_prediction.model_fingerprint == second_prediction.model_fingerprint
    assert first_prediction.status == "warming_up"
    assert math.isclose(sum(first_prediction.probabilities.values()), 1.0)
    assert all(math.isfinite(value) for value in first_prediction.probabilities.values())

    duplicate = first.apply_outcome(outcome, episode)
    assert duplicate.applied is False
    assert duplicate.reason == "duplicate_outcome_event"
    assert first.support(HORIZON_4H) == 1
    assert first.training_steps(HORIZON_4H) == 1

    conflicting = _outcome(
        episode,
        simple_return=-0.01,
        source_hash=outcome.source_raw_sha256,
    )
    with pytest.raises(OutcomeEventConflictError):
        first.apply_outcome(conflicting, episode)


def test_label_cutoff_and_prediction_cutoff_prevent_future_label_leakage() -> None:
    model = _model()
    episode = _episode(0)
    outcome = _outcome(episode)
    before_label = episode.observation.available_at + timedelta(hours=1)  # type: ignore[operator]

    blocked = model.apply_outcome(outcome, episode, available_through=before_label)
    assert blocked.applied is False
    assert blocked.reason == "label_not_available_at_cutoff"
    assert model.support(HORIZON_4H) == 0

    assert model.apply_outcome(outcome, episode).applied is True
    with pytest.raises(FutureLabelLeakageError):
        model.predict(episode, HORIZON_4H, prediction_at=before_label)

    same_cutoff = model.predict(episode, HORIZON_4H, prediction_at=outcome.available_at)
    assert same_cutoff.global_support == 1


def test_registered_future_episode_cannot_change_an_earlier_sequence() -> None:
    model = _model()
    first = _episode(0)
    middle = _episode(1, market_regime="range")
    future = _episode(2, market_regime="trend_down", macro_regime="event_risk")

    model.observe_episodes((first, middle))
    before = model.predict(middle, HORIZON_4H)
    before_metadata = model.sequence_metadata(middle)
    model.observe_episode(future)
    after = model.predict(middle, HORIZON_4H)
    after_metadata = model.sequence_metadata(middle)

    assert before_metadata.episode_ids == (first.episode_id, middle.episode_id)
    assert after_metadata == before_metadata
    assert after.feature_hash == before.feature_hash
    assert dict(after.probabilities) == dict(before.probabilities)


def test_same_availability_cutoff_never_admits_a_later_market_bar() -> None:
    model = _model()
    delayed_target = _episode(0, available_after_hours=2)
    later_bar = _episode(1)

    model.observe_episodes((delayed_target, later_bar))

    assert model.sequence_metadata(delayed_target).episode_ids == (delayed_target.episode_id,)


def test_sequence_never_mixes_feature_or_sampling_contract_versions() -> None:
    model = _model(accepted_feature_contracts=frozenset({"market_ohlcv_causal.v1", "world_features.v2"}))
    first_v1 = _episode(0)
    incompatible = _episode(1, feature_contract_version="world_features.v2")
    target_v1 = _episode(2)

    model.observe_episodes((first_v1, incompatible, target_v1))

    assert model.sequence_metadata(target_v1).episode_ids == (
        first_v1.episode_id,
        target_v1.episode_id,
    )


def test_online_bptt_can_learn_a_repeated_market_direction_without_authority() -> None:
    model = _model(minimum_global_support=3)
    target = _episode(20)
    cold = model.predict(target, HORIZON_4H)
    assert cold.probabilities["UP"] == pytest.approx(1 / 3)

    for hour in range(6):
        episode = _episode(hour)
        assert model.apply_outcome(_outcome(episode, simple_return=0.01), episode).applied

    warmed = model.predict(target, HORIZON_4H)
    assert model.training_steps(HORIZON_4H) == 6
    assert warmed.status == "shadow_only"
    assert warmed.probabilities["UP"] > warmed.probabilities["DOWN"]
    assert warmed.recommendation == "NO_GO"
    assert warmed.authority == "shadow_only"


def test_horizons_are_isolated_and_replay_is_order_independent() -> None:
    episodes = (_episode(0), _episode(1, return_value=-0.006), _episode(2, market_regime="range"))
    outcomes = (
        _outcome(episodes[0], simple_return=0.01, available_after_hours=4),
        _outcome(episodes[1], simple_return=-0.01, available_after_hours=5),
    )
    direct = _model()
    direct.observe_episodes(episodes)
    for outcome, episode in zip(outcomes, episodes[:2], strict=True):
        assert direct.apply_outcome(outcome, episode).applied

    replayed = _model()
    updates = replayed.replay(reversed(episodes), reversed(outcomes))
    assert [update.applied for update in updates] == [True, True]
    assert replayed.support(HORIZON_4H) == 2
    assert replayed.training_steps(HORIZON_4H) == 2
    assert replayed.model_fingerprint(HORIZON_4H) == direct.model_fingerprint(HORIZON_4H)
    target = _episode(8, market_regime="trend_down")
    assert replayed.predict(target, HORIZON_4H).to_dict() == direct.predict(target, HORIZON_4H).to_dict()

    assert direct.support(HORIZON_1D) == 0
    one_day = direct.predict(episodes[-1], HORIZON_1D)
    assert one_day.tier == "uniform"
    assert dict(one_day.probabilities) == {"DOWN": 1 / 3, "FLAT": 1 / 3, "UP": 1 / 3}


def test_non_observed_or_ineligible_outcomes_never_train() -> None:
    model = _model()
    episode = _episode(0)
    pending = {
        "outcome_event_id": "pending",
        "episode_id": episode.episode_id,
        "horizon_id": HORIZON_4H,
        "status": "pending",
        "training_eligible": True,
        "available_at": "2026-01-01T04:00:00+00:00",
    }
    ineligible = {
        "outcome_event_id": "ineligible",
        "episode_id": episode.episode_id,
        "horizon_id": HORIZON_4H,
        "status": "observed",
        "direction": "UP",
        "training_eligible": False,
        "available_at": "2026-01-01T04:00:00+00:00",
    }

    assert model.apply_outcome(pending, episode).reason == "outcome_not_observed"
    assert model.apply_outcome(ineligible, episode).reason == "outcome_not_training_eligible"
    assert model.support(HORIZON_4H) == 0
    assert model.training_steps(HORIZON_4H) == 0


def test_rejects_non_world_episode_envelope_before_encoding() -> None:
    episode = _episode(0)
    contaminated = {**episode.to_dict(), "decision": {"action": "BUY"}}

    with pytest.raises(FeatureBoundaryError, match="non-World fields"):
        _model().predict(contaminated, HORIZON_4H)


STARTED_EVENT_ID = "world_cohort_started:v1:" + "d" * 64
STUDY_COHORT_ID = "world_cohort:v1:" + "e" * 64
MANIFEST_SHA256 = "f" * 64


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


def _lane(
    kind: str,
    *,
    family: str = "gru",
    sequence_length: int = 4,
    seed: int = 17,
    model_version: str | None = None,
) -> WorldLaneDefinition:
    profile = world_lane_encoder_profile(kind)
    return WorldLaneDefinition(
        lane_id=f"gru.{kind}",
        model_family=family,
        model_id="online_gru_world_challenger",
        model_version=model_version or f"cohort.gru.{kind}.v1",
        feature_contract_id=profile.contract.contract_id,
        feature_contract_fingerprint=profile.contract.fingerprint,
        feature_mask_id=profile.mask.mask_id,
        feature_mask_fingerprint=profile.mask.fingerprint,
        seed=seed,
        sequence_length=sequence_length if family == "gru" else None,
        hyperparameters_sha256=canonical_sha256({"family": family, "lane": f"gru.{kind}"}),
        role="primary_control",
    )


def _cold_gru(kind: str = "market", **overrides: object) -> OnlineGRUWorldChallenger:
    profile = world_lane_encoder_profile(kind)
    settings: dict[str, object] = {
        "lane": _lane(kind),
        "contract": profile.contract,
        "mask": profile.mask,
        "study_cohort_id": STUDY_COHORT_ID,
        "manifest_sha256": MANIFEST_SHA256,
        "started_event_id": STARTED_EVENT_ID,
        "hidden_size": 4,
        "learning_rate": 0.05,
        "gradient_clip": 0.75,
        "minimum_global_support": 2,
    }
    settings.update(overrides)
    return cold_gru_challenger(**settings)  # type: ignore[arg-type]


def test_market_mask_gru_matches_v1_facade_predictions_bit_for_bit() -> None:
    episode = _episode(0)
    outcome = _outcome(episode, simple_return=0.01)
    facade = _model()
    masked = _cold_gru("market", lane=_lane("market", seed=17, model_version="v1"), hidden_size=5)
    assert facade.apply_outcome(outcome, episode).applied
    assert masked.apply_outcome(outcome, episode).applied
    first = facade.predict(episode, HORIZON_4H, prediction_at=outcome.available_at)
    second = masked.predict(episode, HORIZON_4H, prediction_at=outcome.available_at)
    assert dict(first.probabilities) == dict(second.probabilities)
    assert first.model_fingerprint == second.model_fingerprint
    assert first.comparison_batch_id == second.comparison_batch_id
    assert first.comparison_cohort_fingerprint == second.comparison_cohort_fingerprint
    assert masked.feature_mask.mask_id == "market.v1"
    assert masked.feature_contract == world_v1_feature_contract()
    assert masked.sequence_len == 4


def test_joint_mask_gru_matches_v2_include_context_facade() -> None:
    v2 = attach_world_context(
        (_v1_episode(),),
        _FakeSource(
            SensorEvidence(status="missing", reason="no_artifact"),
            SensorEvidence(status="missing", reason="no_artifact"),
        ),
    )[0]
    outcome = _outcome(v2, simple_return=0.01)
    facade = OnlineGRUWorldChallenger(
        include_context=True,
        model_version="cohort.gru.joint.v1",
        hidden_size=4,
        sequence_len=4,
        learning_rate=0.05,
        gradient_clip=0.75,
        minimum_global_support=2,
        seed=17,
    )
    masked = _cold_gru("joint", lane=_lane("joint", seed=17))
    assert facade.apply_outcome(outcome, v2).applied
    assert masked.apply_outcome(outcome, v2).applied
    predicted_at = outcome.available_at
    first = facade.predict(v2, HORIZON_4H, prediction_at=predicted_at)
    second = masked.predict(v2, HORIZON_4H, prediction_at=predicted_at)
    assert dict(first.probabilities) == dict(second.probabilities)
    assert first.feature_hash == second.feature_hash
    assert masked.feature_contract == world_v2_feature_contract()


def test_cold_gru_challengers_match_slots_without_warm_reuse_or_content_leakage() -> None:
    v1 = _episode(0)
    v2 = attach_world_context(
        (v1,),
        _FakeSource(
            SensorEvidence(status="missing", reason="no_artifact"),
            SensorEvidence(status="missing", reason="no_artifact"),
        ),
    )[0]
    intact = _with_context_categoricals(v2, company_thesis_status="intact", context_macro_regime="risk_on")
    other = _with_context_categoricals(v2, company_thesis_status="broken", context_macro_regime="risk_off")
    lanes = {kind: _cold_gru(kind) for kind in ("market", "status_only", "company", "macro", "joint")}
    predicted_at = v1.observation.available_at
    batch_ids = {
        lanes["market"].predict(v1, HORIZON_4H, prediction_at=predicted_at).comparison_batch_id,
        lanes["status_only"].predict(v2, HORIZON_4H, prediction_at=predicted_at).comparison_batch_id,
        lanes["company"].predict(v2, HORIZON_4H, prediction_at=predicted_at).comparison_batch_id,
        lanes["macro"].predict(v2, HORIZON_4H, prediction_at=predicted_at).comparison_batch_id,
        lanes["joint"].predict(v2, HORIZON_4H, prediction_at=predicted_at).comparison_batch_id,
    }
    fingerprints = {
        lanes["market"].predict(v1, HORIZON_4H, prediction_at=predicted_at).comparison_cohort_fingerprint,
        lanes["status_only"].predict(v2, HORIZON_4H, prediction_at=predicted_at).comparison_cohort_fingerprint,
        lanes["joint"].predict(v2, HORIZON_4H, prediction_at=predicted_at).comparison_cohort_fingerprint,
    }
    assert len(batch_ids) == 1
    assert len(fingerprints) == 1
    assert lanes["status_only"].lane_identity.replay_bound_event_id == STARTED_EVENT_ID
    assert lanes["status_only"].support(HORIZON_4H) == 0

    status_intact = _cold_gru("status_only")
    status_other = _cold_gru("status_only")
    joint_intact = _cold_gru("joint")
    joint_other = _cold_gru("joint")
    outcome_intact = _outcome(intact, simple_return=0.01)
    outcome_other = _outcome(other, simple_return=0.01)
    assert status_intact.apply_outcome(outcome_intact, intact).applied
    assert status_other.apply_outcome(outcome_other, other).applied
    assert joint_intact.apply_outcome(outcome_intact, intact).applied
    assert joint_other.apply_outcome(outcome_other, other).applied
    later = intact.observation.available_at + timedelta(hours=5)
    assert dict(status_intact.predict(intact, HORIZON_4H, prediction_at=later).probabilities) == dict(
        status_other.predict(other, HORIZON_4H, prediction_at=later).probabilities
    )
    assert dict(joint_intact.predict(intact, HORIZON_4H, prediction_at=later).probabilities) != dict(
        joint_other.predict(other, HORIZON_4H, prediction_at=later).probabilities
    )

    trained = lanes["market"]
    assert trained.apply_outcome(_outcome(v1), v1).applied
    sibling = _cold_gru("market")
    assert sibling.support(HORIZON_4H) == 0
    with pytest.raises(ValueError, match="warm|trained|reuse"):
        cold_gru_challenger(
            lane=_lane("market"),
            contract=world_v1_feature_contract(),
            mask=world_lane_encoder_profile("market").mask,
            study_cohort_id=STUDY_COHORT_ID,
            manifest_sha256=MANIFEST_SHA256,
            started_event_id=STARTED_EVENT_ID,
            prototype=trained,
            hidden_size=4,
        )
    with pytest.raises(ValueError, match="gru|family|sequence"):
        cold_gru_challenger(
            lane=_lane("market", family="markov"),
            contract=world_v1_feature_contract(),
            mask=world_lane_encoder_profile("market").mask,
            study_cohort_id=STUDY_COHORT_ID,
            manifest_sha256=MANIFEST_SHA256,
            started_event_id=STARTED_EVENT_ID,
            hidden_size=4,
        )


def test_cold_gru_v3_topology_status_only_is_constructible_and_isolated() -> None:
    profile = world_lane_encoder_profile("topology_status_only")
    lane = WorldLaneDefinition(
        lane_id="gru.topology_status_only",
        model_family="gru",
        model_id=WORLD_V3_GRU_MODEL_IDENTITY,
        model_version=WORLD_V3_MODEL_VERSION,
        feature_contract_id=profile.contract.contract_id,
        feature_contract_fingerprint=profile.contract.fingerprint,
        feature_mask_id=profile.mask.mask_id,
        feature_mask_fingerprint=profile.mask.fingerprint,
        seed=17,
        sequence_length=4,
        hyperparameters_sha256=canonical_sha256({"family": "gru", "lane": "topology_status_only"}),
        role="secondary_challenger",
    )
    model = cold_gru_challenger(
        lane=lane,
        contract=profile.contract,
        mask=profile.mask,
        study_cohort_id=STUDY_COHORT_ID,
        manifest_sha256=MANIFEST_SHA256,
        started_event_id=STARTED_EVENT_ID,
        hidden_size=4,
        learning_rate=0.05,
        gradient_clip=0.75,
        minimum_global_support=2,
    )
    v1 = _episode(0)
    v3 = _episode(0, feature_contract_version=profile.contract.accepted_episode_contract)
    assert model.feature_contract == world_v3_feature_contract()
    assert model.feature_mask.mask_id == "topology_status_only.v1"
    assert model.model_id == WORLD_V3_GRU_MODEL_IDENTITY
    assert model.encoder_version == "world_gru_encoder.v3"
    assert model.accepts_episode(v3) is True
    assert model.accepts_episode(v1) is False
    assert model.lane_identity.replay_bound_event_id == STARTED_EVENT_ID
    assert model.support(HORIZON_4H) == 0
    prediction = model.predict(v3, HORIZON_4H, prediction_at=v3.observation.available_at)
    assert prediction.global_support == 0
    with pytest.raises(FeatureBoundaryError):
        model.predict(v1, HORIZON_4H)
    with pytest.raises(ValueError, match="warm|trained|reuse"):
        cold_gru_challenger(
            lane=lane,
            contract=profile.contract,
            mask=profile.mask,
            study_cohort_id=STUDY_COHORT_ID,
            manifest_sha256=MANIFEST_SHA256,
            started_event_id=STARTED_EVENT_ID,
            prototype=model,
            hidden_size=4,
        )
