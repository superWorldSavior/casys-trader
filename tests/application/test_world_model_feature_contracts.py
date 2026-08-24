from __future__ import annotations

import ast
import inspect
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from trader.application.world_model.baseline import HierarchicalDirichletWorldBaseline
from trader.application.world_model.context_capture import attach_world_context
from trader.application.world_model.encoding import (
    FEATURE_CONTRACT_FINGERPRINT,
    FEATURE_CONTRACT_FINGERPRINT_CONTEXT,
    FEATURE_CONTRACT_FINGERPRINT_GRAPH,
    FeatureBoundaryError,
    WorldEncoderProfile,
    bound_encoder_profile,
    build_feature_state,
    world_lane_encoder_profile,
)
from trader.application.world_model.gru import OnlineGRUWorldChallenger
from trader.domain.world_context import CONTEXT_FEATURE_CONTRACT_ID, SensorEvidence
from trader.domain.world_episode import MARKET_FEATURE_CONTRACT_ID, WorldEpisode, WorldObservation
from trader.domain.world_feature_contract import (
    GRAPH_CONTENT_CATEGORICAL_FEATURES,
    GRAPH_FEATURE_CONTRACT_ID,
    GRAPH_STATUS_CATEGORICAL_FEATURES,
    GRAPH_ENCODER_IDENTITY,
    WorldFeatureContract,
    WorldFeatureMask,
    market_feature_contract,
    context_feature_contract,
    graph_feature_contract,
    topology_status_only_mask,
)

from tests.application.test_world_context_capture import _FakeSource, _market_episode
from tests.application.test_world_gru import HORIZON_4H, _episode, _outcome

FROZEN_MARKET_ENCODER_FINGERPRINT = "2b4023b7bab99cd39f3592c45b7b8147ad94a7de18684b7896f6daf1a454603c"
FROZEN_CONTEXT_ENCODER_FINGERPRINT = "a66a8a399f0ee6562366def6f9a231a0f0871137e9c838692f3e4c20ee6d9700"
FROZEN_GRAPH_ENCODER_FINGERPRINT = "36177bc807404e0ae8eabb2f1a6b91ddf4e0ff2fecab287372b0d35ddaa2fff4"
_STATUS_CONTENT = frozenset(
    {
        "context_status",
        "macro_status",
        "company_status",
        "company_coverage_status",
        "company_freshness_status",
        "company_source_count_bucket",
    }
)
_COMPANY_CONTENT = frozenset({"company_thesis_status"})
_MACRO_CONTENT = frozenset({"context_macro_regime", "context_rates_regime", "context_usd_regime"})
_ENCODER_SOURCES = (
    Path("trader/application/world_model/encoding.py"),
    Path("trader/application/world_model/baseline.py"),
    Path("trader/application/world_model/gru.py"),
)


def _context_episode():
    return attach_world_context(
        (_market_episode(),),
        _FakeSource(
            SensorEvidence(status="missing", reason="no_artifact"),
            SensorEvidence(status="missing", reason="no_artifact"),
        ),
    )[0]


def test_default_market_models_accept_only_market() -> None:
    v1 = _episode(0)
    companion = _context_episode()
    markov = HierarchicalDirichletWorldBaseline()
    gru = OnlineGRUWorldChallenger(hidden_size=4, sequence_len=4)
    assert markov.accepts_episode(v1) is True
    assert gru.accepts_episode(v1) is True
    assert markov.accepts_episode(companion) is False
    assert gru.accepts_episode(companion) is False
    with pytest.raises(FeatureBoundaryError):
        markov.predict(companion, HORIZON_4H)
    with pytest.raises(FeatureBoundaryError):
        gru.predict(companion, HORIZON_4H)
    with pytest.raises(FeatureBoundaryError):
        gru.observe_episode(companion)
    rejected = markov.apply_outcome(_outcome(companion), companion)
    assert rejected.applied is False
    assert rejected.reason == "feature_contract_rejected"
    gru_rejected = gru.apply_outcome(_outcome(companion), companion)
    assert gru_rejected.applied is False
    assert gru.support(HORIZON_4H) == 0
    replay = gru.replay((companion,), (_outcome(companion),))
    assert replay[0].applied is False


def test_default_context_models_require_context_and_reject_market() -> None:
    v1 = _episode(0)
    v2 = _context_episode()
    markov = HierarchicalDirichletWorldBaseline(include_context=True, model_version="context.v1")
    gru = OnlineGRUWorldChallenger(
        include_context=True,
        model_version="context.v1",
        hidden_size=4,
        sequence_len=4,
    )
    assert markov.accepts_episode(v2) is True
    assert gru.accepts_episode(v2) is True
    assert markov.accepts_episode(v1) is False
    assert gru.accepts_episode(v1) is False
    with pytest.raises(FeatureBoundaryError):
        markov.predict(v1, HORIZON_4H)
    with pytest.raises(FeatureBoundaryError):
        gru.predict(v1, HORIZON_4H)
    with pytest.raises(FeatureBoundaryError):
        gru.observe_episode(v1)
    update = gru.apply_outcome(_outcome(v1), v1)
    assert update.applied is False
    assert update.reason == "feature_contract_rejected"
    replay = gru.replay((v1, v2), (_outcome(v1),))
    assert all(item.applied is False for item in replay)
    markov.predict(v2, HORIZON_4H)
    gru.predict(v2, HORIZON_4H)


def test_explicit_accepted_feature_contracts_select_among_current_capabilities() -> None:
    market = _episode(0)
    context = _context_episode()
    markov = HierarchicalDirichletWorldBaseline(
        accepted_feature_contracts=frozenset({CONTEXT_FEATURE_CONTRACT_ID}),
        include_context=True,
        model_version="context.v1",
    )
    gru = OnlineGRUWorldChallenger(
        accepted_feature_contracts=frozenset({CONTEXT_FEATURE_CONTRACT_ID}),
        include_context=True,
        model_version="context.v1",
        hidden_size=4,
        sequence_len=4,
    )
    assert markov.accepts_episode(context) is True
    assert gru.accepts_episode(context) is True
    assert markov.accepts_episode(market) is False
    assert gru.accepts_episode(market) is False
    markov.predict(context, HORIZON_4H)
    gru.observe_episode(context)
    assert MARKET_FEATURE_CONTRACT_ID == "world_feature.market.v1"
    assert CONTEXT_FEATURE_CONTRACT_ID == "world_feature.context.v1"


def test_comparison_lineage_matches_same_evidence_and_splits_on_extra_labels() -> None:
    v1 = _market_episode()
    v2 = _context_episode()
    markov_v1 = HierarchicalDirichletWorldBaseline()
    markov_v2 = HierarchicalDirichletWorldBaseline(include_context=True, model_version="context.v1")
    predicted_at = v1.observation.available_at
    first = markov_v1.predict(v1, HORIZON_4H, prediction_at=predicted_at)
    second = markov_v2.predict(v2, HORIZON_4H, prediction_at=predicted_at)
    assert first.comparison_batch_id == second.comparison_batch_id
    assert first.comparison_cohort_fingerprint == second.comparison_cohort_fingerprint
    outcome = {
        "outcome_event_id": "evt-1",
        "horizon_id": HORIZON_4H,
        "status": "observed",
        "training_eligible": True,
        "available_at": "2026-08-22T14:05:00+00:00",
        "direction": "UP",
        "target_at": "2026-08-22T14:00:00+00:00",
        "source_raw_sha256": "abc",
    }
    later = datetime(2026, 8, 22, 15, 0, tzinfo=timezone.utc)
    assert markov_v1.apply_outcome(outcome, v1).applied is True
    trained = markov_v1.predict(v1, HORIZON_4H, prediction_at=later)
    untrained = markov_v2.predict(v2, HORIZON_4H, prediction_at=later)
    assert trained.comparison_batch_id == untrained.comparison_batch_id
    assert trained.comparison_cohort_fingerprint != untrained.comparison_cohort_fingerprint
    assert markov_v2.apply_outcome({**outcome, "outcome_event_id": "evt-v2"}, v2).applied is True
    matched = markov_v2.predict(v2, HORIZON_4H, prediction_at=later)
    assert matched.comparison_cohort_fingerprint == trained.comparison_cohort_fingerprint


def _spoof_envelope(episode, envelope_version: str) -> dict:
    payload = episode.to_dict()
    payload["feature_contract_version"] = envelope_version
    return payload


def test_contradictory_feature_contract_envelopes_are_rejected_on_all_paths() -> None:
    v1 = _episode(0)
    v2 = _context_episode()
    spoof_nested_v2_top_v1 = _spoof_envelope(v2, MARKET_FEATURE_CONTRACT_ID)
    spoof_nested_v1_top_v2 = _spoof_envelope(v1, CONTEXT_FEATURE_CONTRACT_ID)
    markov_v1 = HierarchicalDirichletWorldBaseline()
    gru_market = OnlineGRUWorldChallenger(hidden_size=4, sequence_len=4)
    markov_v2 = HierarchicalDirichletWorldBaseline(include_context=True, model_version="context.v1")
    gru_context = OnlineGRUWorldChallenger(
        include_context=True,
        model_version="context.v1",
        hidden_size=4,
        sequence_len=4,
    )
    models = (markov_v1, gru_market, markov_v2, gru_context)
    spoofs = (spoof_nested_v2_top_v1, spoof_nested_v1_top_v2)
    for model in models:
        for payload in spoofs:
            with pytest.raises(FeatureBoundaryError, match="contradict"):
                model.accepts_episode(payload)
            with pytest.raises(FeatureBoundaryError, match="contradict"):
                model.predict(payload, HORIZON_4H)
            with pytest.raises(FeatureBoundaryError, match="contradict"):
                model.apply_outcome(_outcome(v1), payload)
    for payload in spoofs:
        with pytest.raises(FeatureBoundaryError, match="contradict"):
            gru_market.observe_episode(payload)
        with pytest.raises(FeatureBoundaryError, match="contradict"):
            gru_context.observe_episode(payload)
        with pytest.raises(FeatureBoundaryError, match="contradict"):
            gru_market.replay((payload,), (_outcome(v1),))
        with pytest.raises(FeatureBoundaryError, match="contradict"):
            gru_context.replay((payload,), (_outcome(v2),))


def test_old_feature_contract_payloads_are_rejected_before_encoding() -> None:
    market = _episode(0)
    payload = market.to_dict()
    payload["observation"]["feature_contract_version"] = "market_ohlcv_causal.v1"
    payload["feature_contract_version"] = "market_ohlcv_causal.v1"
    with pytest.raises(ValueError, match="feature_contract_version"):
        WorldEpisode.from_dict(payload)
    markov = HierarchicalDirichletWorldBaseline()
    gru = OnlineGRUWorldChallenger(hidden_size=4, sequence_len=4)
    assert markov.accepts_episode(payload) is False
    assert gru.accepts_episode(payload) is False


def test_gru_historical_windows_remain_in_cohort_after_current_windows_converge() -> None:
    long = OnlineGRUWorldChallenger(hidden_size=4, sequence_len=4)
    short = OnlineGRUWorldChallenger(hidden_size=4, sequence_len=4)
    first = _episode(0)
    second = _episode(1)
    long.observe_episodes((first, second))
    short.observe_episode(second)
    assert long.apply_outcome(_outcome(second), second).applied is True
    assert short.apply_outcome(_outcome(second), second).applied is True
    short.observe_episode(first)
    predicted_at = second.observation.available_at + timedelta(hours=5)
    long_pred = long.predict(second, HORIZON_4H, prediction_at=predicted_at)
    short_pred = short.predict(second, HORIZON_4H, prediction_at=predicted_at)
    assert long_pred.comparison_batch_id == short_pred.comparison_batch_id
    assert long_pred.comparison_cohort_fingerprint != short_pred.comparison_cohort_fingerprint


def test_tied_availability_replays_in_common_order_for_market_and_context() -> None:
    v1_a = _episode(0, symbol="AAA")
    v1_b = _episode(0, symbol="BBB")
    v1_oa = _outcome(v1_a, source_hash="shared-aaa")
    v1_ob = _outcome(v1_b, source_hash="shared-bbb")
    first = OnlineGRUWorldChallenger(hidden_size=4, sequence_len=4, seed=17)
    second = OnlineGRUWorldChallenger(hidden_size=4, sequence_len=4, seed=17)
    first.replay((v1_a, v1_b), (v1_oa, v1_ob))
    second.replay((v1_b, v1_a), (v1_ob, v1_oa))
    later = v1_oa.available_at
    first_pred = first.predict(v1_a, HORIZON_4H, prediction_at=later)
    second_pred = second.predict(v1_a, HORIZON_4H, prediction_at=later)
    assert first_pred.comparison_cohort_fingerprint == second_pred.comparison_cohort_fingerprint
    assert first.model_fingerprint(HORIZON_4H) == second.model_fingerprint(HORIZON_4H)

    first.reset_for_replay()
    first.replay((v1_b, v1_a), (v1_ob, v1_oa))
    reset_pred = first.predict(v1_a, HORIZON_4H, prediction_at=later)
    assert reset_pred.comparison_cohort_fingerprint == second_pred.comparison_cohort_fingerprint

    v2_a = attach_world_context(
        (v1_a,),
        _FakeSource(
            SensorEvidence(status="missing", reason="no_artifact"),
            SensorEvidence(status="missing", reason="no_artifact"),
        ),
    )[0]
    v2_b = attach_world_context(
        (v1_b,),
        _FakeSource(
            SensorEvidence(status="missing", reason="no_artifact"),
            SensorEvidence(status="missing", reason="no_artifact"),
        ),
    )[0]
    v2_oa = _outcome(v2_a, source_hash="shared-aaa")
    v2_ob = _outcome(v2_b, source_hash="shared-bbb")
    gru_context_a = OnlineGRUWorldChallenger(
        include_context=True,
        model_version="context.v1",
        hidden_size=4,
        sequence_len=4,
        seed=17,
    )
    gru_context_b = OnlineGRUWorldChallenger(
        include_context=True,
        model_version="context.v1",
        hidden_size=4,
        sequence_len=4,
        seed=17,
    )
    gru_context_a.replay((v2_a, v2_b), (v2_oa, v2_ob))
    gru_context_b.replay((v2_b, v2_a), (v2_ob, v2_oa))
    v2_pred_a = gru_context_a.predict(v2_a, HORIZON_4H, prediction_at=later)
    v2_pred_b = gru_context_b.predict(v2_a, HORIZON_4H, prediction_at=later)
    assert v2_pred_a.comparison_cohort_fingerprint == v2_pred_b.comparison_cohort_fingerprint
    assert first_pred.comparison_cohort_fingerprint == v2_pred_a.comparison_cohort_fingerprint


def test_mismatched_gru_historical_windows_fail_ablation_training_cohort() -> None:
    from trader.reporting.read_models.world_evaluation import GRU_MODEL_ID, evaluate_shadow

    long = OnlineGRUWorldChallenger(hidden_size=4, sequence_len=4, model_id=GRU_MODEL_ID)
    short = OnlineGRUWorldChallenger(
        hidden_size=4,
        sequence_len=4,
        model_id=GRU_MODEL_ID,
        model_version="context.v1",
        include_context=True,
    )
    first = _episode(0)
    second = _episode(1)
    v2_second = _context_like(second)
    long.observe_episodes((first, second))
    short.observe_episode(v2_second)
    assert long.apply_outcome(_outcome(second), second).applied is True
    assert short.apply_outcome(_outcome(v2_second), v2_second).applied is True
    predicted_at = second.observation.available_at + timedelta(hours=5)
    market = long.predict(second, HORIZON_4H, prediction_at=predicted_at)
    context = short.predict(v2_second, HORIZON_4H, prediction_at=predicted_at)
    assert market.comparison_cohort_fingerprint != context.comparison_cohort_fingerprint
    outcome_at = (predicted_at + timedelta(hours=1)).isoformat()
    result = evaluate_shadow(
        [
            {
                "prediction_id": "gru-v1",
                "episode_id": second.episode_id,
                "horizon_id": HORIZON_4H,
                "model_id": GRU_MODEL_ID,
                "model_version": "v1",
                "probabilities": dict(market.probabilities),
                "created_at": predicted_at.isoformat(),
                "ready_at": predicted_at.isoformat(),
                "comparison_batch_id": market.comparison_batch_id,
                "comparison_cohort_fingerprint": market.comparison_cohort_fingerprint,
                "training_cutoff": market.training_cutoff.isoformat() if market.training_cutoff else None,
                "input": {
                    "venue": second.observation.venue,
                    "symbol": second.observation.symbol,
                    "bar_interval": second.observation.bar_interval,
                    "as_of_bar_ts": second.observation.as_of_bar_ts.isoformat(),
                    "feature_contract_version": "world_feature.market.v1",
                },
            },
            {
                "prediction_id": "gru-v2",
                "episode_id": v2_second.episode_id,
                "horizon_id": HORIZON_4H,
                "model_id": GRU_MODEL_ID,
                "model_version": "context.v1",
                "probabilities": dict(context.probabilities),
                "created_at": predicted_at.isoformat(),
                "ready_at": predicted_at.isoformat(),
                "comparison_batch_id": context.comparison_batch_id,
                "comparison_cohort_fingerprint": context.comparison_cohort_fingerprint,
                "training_cutoff": context.training_cutoff.isoformat() if context.training_cutoff else None,
                "input": {
                    "venue": v2_second.observation.venue,
                    "symbol": v2_second.observation.symbol,
                    "bar_interval": v2_second.observation.bar_interval,
                    "as_of_bar_ts": v2_second.observation.as_of_bar_ts.isoformat(),
                    "feature_contract_version": CONTEXT_FEATURE_CONTRACT_ID,
                },
            },
        ],
        [
            {
                "event_id": "outcome-market",
                "episode_id": second.episode_id,
                "horizon_id": HORIZON_4H,
                "status": "observed",
                "direction": "UP",
                "training_eligible": True,
                "available_at": outcome_at,
            },
            {
                "event_id": "outcome-context",
                "episode_id": v2_second.episode_id,
                "horizon_id": HORIZON_4H,
                "status": "observed",
                "direction": "UP",
                "training_eligible": True,
                "available_at": outcome_at,
            },
        ],
        minimum_paired_support=1,
    )
    assert result["context_ablation"]["excluded"]["training_cohort_mismatch"] == 1


def _context_like(episode):
    return attach_world_context(
        (episode,),
        _FakeSource(
            SensorEvidence(status="missing", reason="no_artifact"),
            SensorEvidence(status="missing", reason="no_artifact"),
        ),
    )[0]


def _rich_context_observation() -> dict[str, object]:
    return {
        "available_at": "2026-01-01T00:00:00+00:00",
        "as_of_bar_ts": "2026-01-01T00:00:00+00:00",
        "venue": "XNYS",
        "symbol": "SPY",
        "bar_interval": "1h",
        "feature_contract_version": CONTEXT_FEATURE_CONTRACT_ID,
        "categorical_features": {
            "asset_family": "equities",
            "venue": "XNYS",
            "session_phase": "regular",
            "market_regime": "trend_up",
            "volatility_state": "normal",
            "macro_regime": "quiet",
        },
        "numeric_features": {"return": 0.006, "atr_pct": 0.01, "range_position": 0.72},
        "context": {
            "categorical_features": {
                "context_status": "complete",
                "macro_status": "complete",
                "company_status": "complete",
                "company_coverage_status": "partial",
                "company_freshness_status": "fresh",
                "company_source_count_bucket": "b2",
                "company_thesis_status": "intact",
                "context_macro_regime": "risk_on",
                "context_rates_regime": "easing",
                "context_usd_regime": "strong",
            }
        },
    }


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


def _state_keys(observation: object, profile: WorldEncoderProfile) -> set[str]:
    state = build_feature_state(
        observation,
        feature_contract=profile.contract,
        feature_mask=profile.mask,
    )
    return set(state.features)


def test_encoder_fingerprints_stay_frozen_and_distinct_from_domain_contracts() -> None:
    v1 = market_feature_contract()
    v2 = context_feature_contract()
    v3 = graph_feature_contract()
    assert FEATURE_CONTRACT_FINGERPRINT == FROZEN_MARKET_ENCODER_FINGERPRINT
    assert FEATURE_CONTRACT_FINGERPRINT_CONTEXT == FROZEN_CONTEXT_ENCODER_FINGERPRINT
    assert FEATURE_CONTRACT_FINGERPRINT_GRAPH == FROZEN_GRAPH_ENCODER_FINGERPRINT
    assert v1.fingerprint != FEATURE_CONTRACT_FINGERPRINT
    assert v2.fingerprint != FEATURE_CONTRACT_FINGERPRINT_CONTEXT
    assert v3.fingerprint != FEATURE_CONTRACT_FINGERPRINT_GRAPH
    assert type(v1) is WorldFeatureContract
    assert type(v2) is WorldFeatureContract
    assert type(v3) is WorldFeatureContract


def test_lane_profiles_bind_frozen_market_and_joint_masks() -> None:
    market = world_lane_encoder_profile("market")
    joint = world_lane_encoder_profile("joint")
    assert isinstance(market, WorldEncoderProfile)
    assert isinstance(market.mask, WorldFeatureMask)
    assert market.contract == market_feature_contract()
    assert market.mask.mask_id == "market.v1"
    assert market.include_context is False
    assert market.encoder_fingerprint == FEATURE_CONTRACT_FINGERPRINT
    assert joint.contract == context_feature_contract()
    assert joint.mask.mask_id == "joint.v1"
    assert joint.include_context is True
    assert joint.encoder_fingerprint == FEATURE_CONTRACT_FINGERPRINT_CONTEXT
    assert inspect.signature(WorldFeatureContract).parameters.get("include_context") is None


def test_lane_masks_are_frozen_domain_masks_without_cross_group_leakage() -> None:
    observation = _rich_context_observation()
    profiles = {
        kind: world_lane_encoder_profile(kind) for kind in ("market", "status_only", "company", "macro", "joint")
    }
    assert profiles["status_only"].mask.mask_id == "status_only.v1"
    assert profiles["company"].mask.mask_id == "company.v1"
    assert profiles["macro"].mask.mask_id == "macro.v1"
    assert profiles["market"].contract.contract_id == MARKET_FEATURE_CONTRACT_ID
    for kind in ("status_only", "company", "macro", "joint"):
        assert profiles[kind].contract.contract_id == CONTEXT_FEATURE_CONTRACT_ID
        profiles[kind].mask.assert_compatible_with(profiles[kind].contract)
    fingerprints = {profile.mask.fingerprint for profile in profiles.values()}
    assert len(fingerprints) == 5

    status_keys = _state_keys(observation, profiles["status_only"])
    company_keys = _state_keys(observation, profiles["company"])
    macro_keys = _state_keys(observation, profiles["macro"])
    joint_keys = _state_keys(observation, profiles["joint"])
    assert _STATUS_CONTENT <= status_keys
    assert status_keys.isdisjoint(_COMPANY_CONTENT)
    assert status_keys.isdisjoint(_MACRO_CONTENT)
    assert _COMPANY_CONTENT <= company_keys
    assert company_keys.isdisjoint(_MACRO_CONTENT)
    assert _MACRO_CONTENT <= macro_keys
    assert macro_keys.isdisjoint(_COMPANY_CONTENT)
    assert _STATUS_CONTENT | _COMPANY_CONTENT | _MACRO_CONTENT <= joint_keys
    v2_facade = build_feature_state(observation, include_context=True)
    assert set(v2_facade.features) == joint_keys
    assert (
        v2_facade.feature_hash
        == build_feature_state(
            observation,
            feature_contract=profiles["joint"].contract,
            feature_mask=profiles["joint"].mask,
        ).feature_hash
    )


def test_market_mask_matches_market_facade_and_ignores_context_content() -> None:
    v1 = {
        "available_at": "2026-01-01T00:00:00+00:00",
        "feature_contract_version": MARKET_FEATURE_CONTRACT_ID,
        "categorical_features": {
            "asset_family": "equities",
            "venue": "XNYS",
            "session_phase": "regular",
            "market_regime": "trend_up",
            "volatility_state": "normal",
            "macro_regime": "quiet",
        },
        "numeric_features": {"return": 0.006, "atr_pct": 0.01, "range_position": 0.72},
    }
    market = world_lane_encoder_profile("market")
    facade = build_feature_state(v1)
    masked = build_feature_state(v1, feature_contract=market.contract, feature_mask=market.mask)
    assert facade.feature_hash == masked.feature_hash
    assert facade.exact_state == masked.exact_state
    context_keys = _state_keys(_rich_context_observation(), world_lane_encoder_profile("status_only"))
    assert not (_STATUS_CONTENT <= set(masked.features))
    assert "company_thesis_status" not in masked.features
    assert "context_macro_regime" not in masked.features
    assert "context_status" in context_keys


def test_encoding_and_models_stay_free_of_networkx() -> None:
    for path in _ENCODER_SOURCES:
        source = path.read_text(encoding="utf-8")
        assert "networkx" not in source.lower()
        tree = ast.parse(source, filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert all(alias.name.split(".", 1)[0] != "networkx" for alias in node.names)
            if isinstance(node, ast.ImportFrom) and node.module:
                assert node.module.split(".", 1)[0] != "networkx"


def test_market_feature_fingerprint_stays_frozen_after_macro_source_only() -> None:
    from trader.application.world_model.context_capture import attach_world_context as capture_fn

    assert FEATURE_CONTRACT_FINGERPRINT == FROZEN_MARKET_ENCODER_FINGERPRINT
    assert FEATURE_CONTRACT_FINGERPRINT_CONTEXT == FROZEN_CONTEXT_ENCODER_FINGERPRINT
    v1 = _market_episode()
    v2 = attach_world_context(
        (v1,),
        _FakeSource(
            SensorEvidence(status="missing", reason="no_artifact"),
            SensorEvidence(status="missing", reason="no_artifact"),
        ),
    )[0]
    assert v1.observation.feature_contract_version == MARKET_FEATURE_CONTRACT_ID
    assert v2.observation.feature_contract_version == CONTEXT_FEATURE_CONTRACT_ID
    assert capture_fn is attach_world_context


def test_world_context_model_path_excludes_news_macro_brief() -> None:
    paths = (
        Path("trader/application/world_model/context_capture.py"),
        Path("trader/application/world_model/world_scope_resolver.py"),
        Path("trader/infrastructure/state_db/world_context_reader.py"),
        Path("trader/domain/world_context.py"),
    )
    for path in paths:
        source = path.read_text(encoding="utf-8")
        assert "NewsMacroBrief" not in source
        assert "NewsMacroBriefStore" not in source
        assert "gdelt" not in source.lower()
    capture = Path("trader/application/world_model/context_capture.py").read_text(encoding="utf-8")
    assert "news_macro" not in capture


_C1_LANE_KINDS = ("market", "status_only", "company", "macro", "joint")


def _graph_observation(*, path_signature: str = "sig_a", graph_status: str = "complete") -> dict[str, object]:
    payload = _rich_context_observation()
    payload["feature_contract_version"] = GRAPH_FEATURE_CONTRACT_ID
    payload["graph_features"] = {
        "categorical_features": {
            "graph_status": graph_status,
            "graph_scope_status": "resolved",
            "graph_coverage_status": "complete",
            "graph_freshness_status": "0-4h",
            "graph_source_count_bucket": "b1",
            "graph_artifact_count_bucket": "b1",
            "graph_missingness_status": "none",
            "graph_path_count_bucket": "b2",
            "graph_depth_min_bucket": "b1",
            "graph_depth_max_bucket": "b3",
            "graph_path_freshness_min_bucket": "0-4h",
            "graph_path_freshness_max_bucket": "4-24h",
            "graph_path_signature": path_signature,
            "graph_macro_agreement_status": "agree",
            "graph_window_0_4h_count_bucket": "b1",
            "graph_window_4_24h_count_bucket": "b0",
            "graph_window_1_7d_count_bucket": "b0",
            "graph_window_older_count_bucket": "b0",
        }
    }
    return payload


def test_technical_c1_lane_profiles_exclude_graph_and_keep_frozen_encoder_fingerprints() -> None:
    profiles = {kind: world_lane_encoder_profile(kind) for kind in _C1_LANE_KINDS}
    for kind, profile in profiles.items():
        assert GRAPH_FEATURE_CONTRACT_ID not in {profile.contract.contract_id, profile.mask.contract_id}
        assert "graph" not in profile.contract.allowed_feature_groups
        assert "graph_status" not in profile.contract.allowed_feature_groups
        assert profile.encoder_fingerprint in {FEATURE_CONTRACT_FINGERPRINT, FEATURE_CONTRACT_FINGERPRINT_CONTEXT}
        assert kind != "topology_status_only"
    assert FEATURE_CONTRACT_FINGERPRINT == FROZEN_MARKET_ENCODER_FINGERPRINT
    assert FEATURE_CONTRACT_FINGERPRINT_CONTEXT == FROZEN_CONTEXT_ENCODER_FINGERPRINT
    assert FEATURE_CONTRACT_FINGERPRINT_GRAPH == FROZEN_GRAPH_ENCODER_FINGERPRINT
    assert FEATURE_CONTRACT_FINGERPRINT_GRAPH != FEATURE_CONTRACT_FINGERPRINT
    assert FEATURE_CONTRACT_FINGERPRINT_GRAPH != FEATURE_CONTRACT_FINGERPRINT_CONTEXT
    with pytest.raises(ValueError, match="unknown world lane encoder profile"):
        world_lane_encoder_profile("pipeline_pilot")


def test_graph_topology_status_only_profile_is_a_matched_lane_without_graph_content() -> None:
    profile = world_lane_encoder_profile("topology_status_only")
    content = world_lane_encoder_profile("graph_content")
    v3 = graph_feature_contract()
    assert isinstance(profile, WorldEncoderProfile)
    assert profile.contract == v3 == content.contract
    assert profile.mask == topology_status_only_mask()
    assert profile.mask.mask_id == "topology_status_only.v1"
    assert content.mask.mask_id == "graph_content.v1"
    assert profile.contract.encoder_identity == GRAPH_ENCODER_IDENTITY
    assert profile.encoder_fingerprint == FEATURE_CONTRACT_FINGERPRINT_GRAPH == content.encoder_fingerprint
    assert profile.include_context is False
    assert profile.mask.fingerprint != content.mask.fingerprint
    observation = _graph_observation(path_signature="sig_a")
    other = _graph_observation(path_signature="sig_b")
    status_keys = _state_keys(observation, profile)
    content_keys = _state_keys(observation, content)
    assert GRAPH_STATUS_CATEGORICAL_FEATURES <= status_keys
    assert status_keys.isdisjoint(GRAPH_CONTENT_CATEGORICAL_FEATURES)
    assert "graph_path_signature" not in status_keys
    assert GRAPH_CONTENT_CATEGORICAL_FEATURES <= content_keys
    assert (
        build_feature_state(
            observation,
            feature_contract=profile.contract,
            feature_mask=profile.mask,
        ).feature_hash
        == build_feature_state(
            other,
            feature_contract=profile.contract,
            feature_mask=profile.mask,
        ).feature_hash
    )
    assert (
        build_feature_state(
            observation,
            feature_contract=content.contract,
            feature_mask=content.mask,
        ).feature_hash
        != build_feature_state(
            other,
            feature_contract=content.contract,
            feature_mask=content.mask,
        ).feature_hash
    )
    v1 = _rich_context_observation()
    v1["feature_contract_version"] = MARKET_FEATURE_CONTRACT_ID
    market = world_lane_encoder_profile("market")
    v1_hash = build_feature_state(v1, feature_contract=market.contract, feature_mask=market.mask).feature_hash
    assert v1_hash == build_feature_state(v1).feature_hash
    with pytest.raises(ValueError, match="include_context"):
        bound_encoder_profile(profile.contract, profile.mask, include_context=True)


def test_graph_encoder_is_deterministic_bounded_and_rejects_incompatible_profiles() -> None:
    profile = world_lane_encoder_profile("topology_status_only")
    first = build_feature_state(
        _graph_observation(),
        feature_contract=profile.contract,
        feature_mask=profile.mask,
    )
    second = build_feature_state(
        _graph_observation(),
        feature_contract=profile.contract,
        feature_mask=profile.mask,
    )
    assert first.feature_hash == second.feature_hash
    assert first.exact_state == second.exact_state
    assert all(len(value) <= 80 for value in first.features.values())
    v2 = world_lane_encoder_profile("joint")
    with pytest.raises(ValueError, match="contract"):
        profile.mask.assert_compatible_with(v2.contract)
    with pytest.raises(ValueError, match="unsupported WorldFeatureContract|contract"):
        build_feature_state(
            _graph_observation(),
            feature_contract=_contract_like_custom(),
            feature_mask=profile.mask,
        )


def _contract_like_custom() -> WorldFeatureContract:
    return WorldFeatureContract(
        contract_id="custom.v9",
        accepted_episode_contract="custom.v9",
        projection_version="custom.v9",
        encoder_identity="world_feature_encoder.custom.v9",
        groups=(
            {
                "group_id": "market",
                "categorical_features": ["venue"],
                "numeric_features": ["return"],
            },
        ),
        ontology_revision="custom.v9",
        vocabulary_version="custom.v9",
    )


def test_graph_models_reject_market_context_and_c1_models_reject_graph() -> None:
    v1 = _episode(0)
    v2 = _context_episode()
    v3 = _episode(2, feature_contract_version=GRAPH_FEATURE_CONTRACT_ID)
    v3_profile = world_lane_encoder_profile("topology_status_only")
    markov_graph = HierarchicalDirichletWorldBaseline(
        feature_contract=v3_profile.contract,
        feature_mask=v3_profile.mask,
        model_id="hierarchical_dirichlet_world_baseline@graph.v1",
        model_version="graph.v1",
    )
    gru_graph = OnlineGRUWorldChallenger(
        feature_contract=v3_profile.contract,
        feature_mask=v3_profile.mask,
        model_id="online_gru_world_challenger@graph.v1",
        model_version="graph.v1",
        hidden_size=4,
        sequence_len=4,
    )
    markov_v1 = HierarchicalDirichletWorldBaseline()
    gru_market = OnlineGRUWorldChallenger(hidden_size=4, sequence_len=4)
    markov_v2 = HierarchicalDirichletWorldBaseline(include_context=True, model_version="context.v1")
    gru_context = OnlineGRUWorldChallenger(
        include_context=True,
        model_version="context.v1",
        hidden_size=4,
        sequence_len=4,
    )
    assert markov_graph.accepts_episode(v3) is True
    assert gru_graph.accepts_episode(v3) is True
    assert markov_graph.accepts_episode(v1) is False
    assert gru_graph.accepts_episode(v2) is False
    assert markov_v1.accepts_episode(v3) is False
    assert gru_market.accepts_episode(v3) is False
    assert markov_v2.accepts_episode(v3) is False
    assert gru_context.accepts_episode(v3) is False
    for model in (markov_graph, gru_graph):
        with pytest.raises(FeatureBoundaryError):
            model.predict(v1, HORIZON_4H)
        with pytest.raises(FeatureBoundaryError):
            model.predict(v2, HORIZON_4H)
    for model in (markov_v1, gru_market, markov_v2, gru_context):
        with pytest.raises(FeatureBoundaryError):
            model.predict(v3, HORIZON_4H)
    gru_graph.predict(v3, HORIZON_4H)
    markov_graph.predict(v3, HORIZON_4H)
    assert gru_graph.encoder_version == "world_gru_encoder.graph.v1"
    assert markov_graph.feature_contract == graph_feature_contract()
    rejected = markov_v1.apply_outcome(_outcome(v3), v3)
    assert rejected.applied is False
    assert rejected.reason == "feature_contract_rejected"
