from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from trader.application.world_model.baseline import HierarchicalDirichletWorldBaseline
from trader.application.world_model.context_capture import attach_world_context
from trader.application.world_model.encoding import FeatureBoundaryError
from trader.application.world_model.gru import OnlineGRUWorldChallenger
from trader.domain.world_context import CONTEXT_FEATURE_CONTRACT_VERSION, SensorEvidence
from trader.domain.world_episode import MARKET_FEATURE_CONTRACT_VERSION

from tests.application.test_world_context_capture import _FakeSource, _v1_episode
from tests.application.test_world_gru import HORIZON_4H, _episode, _outcome


def _v2_episode():
    return attach_world_context(
        (_v1_episode(),),
        _FakeSource(
            SensorEvidence(status="missing", reason="no_artifact"),
            SensorEvidence(status="missing", reason="no_artifact"),
        ),
    )[0]


def test_default_market_models_accept_only_market_v1() -> None:
    v1 = _episode(0)
    unknown = _episode(1, feature_contract_version="world_features.v1")
    markov = HierarchicalDirichletWorldBaseline()
    gru = OnlineGRUWorldChallenger(hidden_size=4, sequence_len=4)
    assert markov.accepts_episode(v1) is True
    assert gru.accepts_episode(v1) is True
    assert markov.accepts_episode(unknown) is False
    assert gru.accepts_episode(unknown) is False
    with pytest.raises(FeatureBoundaryError):
        markov.predict(unknown, HORIZON_4H)
    with pytest.raises(FeatureBoundaryError):
        gru.predict(unknown, HORIZON_4H)
    with pytest.raises(FeatureBoundaryError):
        gru.observe_episode(unknown)
    rejected = markov.apply_outcome(_outcome(unknown), unknown)
    assert rejected.applied is False
    assert rejected.reason == "feature_contract_rejected"
    gru_rejected = gru.apply_outcome(_outcome(unknown), unknown)
    assert gru_rejected.applied is False
    assert gru.support(HORIZON_4H) == 0
    replay = gru.replay((unknown,), (_outcome(unknown),))
    assert replay[0].applied is False


def test_default_context_models_require_v2_and_reject_v1() -> None:
    v1 = _episode(0)
    v2 = _v2_episode()
    markov = HierarchicalDirichletWorldBaseline(include_context=True, model_version="context.v2")
    gru = OnlineGRUWorldChallenger(
        include_context=True,
        model_version="context.v2",
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


def test_explicit_accepted_feature_contracts_remain_an_override() -> None:
    unknown = _episode(0, feature_contract_version="custom.v9")
    markov = HierarchicalDirichletWorldBaseline(accepted_feature_contracts=frozenset({"custom.v9"}))
    gru = OnlineGRUWorldChallenger(
        accepted_feature_contracts=frozenset({"custom.v9"}),
        hidden_size=4,
        sequence_len=4,
    )
    assert markov.accepts_episode(unknown) is True
    assert gru.accepts_episode(unknown) is True
    markov.predict(unknown, HORIZON_4H)
    gru.observe_episode(unknown)
    assert MARKET_FEATURE_CONTRACT_VERSION == "market_ohlcv_causal.v1"
    assert CONTEXT_FEATURE_CONTRACT_VERSION == "market_ohlcv_context.v2"


def test_comparison_lineage_matches_same_evidence_and_splits_on_extra_labels() -> None:
    v1 = _v1_episode()
    v2 = _v2_episode()
    markov_v1 = HierarchicalDirichletWorldBaseline()
    markov_v2 = HierarchicalDirichletWorldBaseline(include_context=True, model_version="context.v2")
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
    v2 = _v2_episode()
    spoof_nested_v2_top_v1 = _spoof_envelope(v2, MARKET_FEATURE_CONTRACT_VERSION)
    spoof_nested_v1_top_v2 = _spoof_envelope(v1, CONTEXT_FEATURE_CONTRACT_VERSION)
    markov_v1 = HierarchicalDirichletWorldBaseline()
    gru_v1 = OnlineGRUWorldChallenger(hidden_size=4, sequence_len=4)
    markov_v2 = HierarchicalDirichletWorldBaseline(include_context=True, model_version="context.v2")
    gru_v2 = OnlineGRUWorldChallenger(
        include_context=True,
        model_version="context.v2",
        hidden_size=4,
        sequence_len=4,
    )
    models = (markov_v1, gru_v1, markov_v2, gru_v2)
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
            gru_v1.observe_episode(payload)
        with pytest.raises(FeatureBoundaryError, match="contradict"):
            gru_v2.observe_episode(payload)
        with pytest.raises(FeatureBoundaryError, match="contradict"):
            gru_v1.replay((payload,), (_outcome(v1),))
        with pytest.raises(FeatureBoundaryError, match="contradict"):
            gru_v2.replay((payload,), (_outcome(v2),))


def test_coherent_custom_envelope_still_honors_accepted_feature_contracts() -> None:
    unknown = _episode(0, feature_contract_version="custom.v9")
    payload = unknown.to_dict()
    payload["feature_contract_version"] = "custom.v9"
    markov = HierarchicalDirichletWorldBaseline(accepted_feature_contracts=frozenset({"custom.v9"}))
    gru = OnlineGRUWorldChallenger(
        accepted_feature_contracts=frozenset({"custom.v9"}),
        hidden_size=4,
        sequence_len=4,
    )
    assert markov.accepts_episode(payload) is True
    assert gru.accepts_episode(payload) is True
    markov.predict(payload, HORIZON_4H)
    gru.observe_episode(payload)


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


def test_tied_availability_replays_in_common_order_for_v1_and_v2() -> None:
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
    gru_v2_a = OnlineGRUWorldChallenger(
        include_context=True,
        model_version="context.v2",
        hidden_size=4,
        sequence_len=4,
        seed=17,
    )
    gru_v2_b = OnlineGRUWorldChallenger(
        include_context=True,
        model_version="context.v2",
        hidden_size=4,
        sequence_len=4,
        seed=17,
    )
    gru_v2_a.replay((v2_a, v2_b), (v2_oa, v2_ob))
    gru_v2_b.replay((v2_b, v2_a), (v2_ob, v2_oa))
    v2_pred_a = gru_v2_a.predict(v2_a, HORIZON_4H, prediction_at=later)
    v2_pred_b = gru_v2_b.predict(v2_a, HORIZON_4H, prediction_at=later)
    assert v2_pred_a.comparison_cohort_fingerprint == v2_pred_b.comparison_cohort_fingerprint
    assert first_pred.comparison_cohort_fingerprint == v2_pred_a.comparison_cohort_fingerprint


def test_mismatched_gru_historical_windows_fail_ablation_training_cohort() -> None:
    from trader.reporting.read_models.world_evaluation import GRU_MODEL_ID, evaluate_shadow

    long = OnlineGRUWorldChallenger(hidden_size=4, sequence_len=4, model_id=GRU_MODEL_ID)
    short = OnlineGRUWorldChallenger(
        hidden_size=4,
        sequence_len=4,
        model_id=GRU_MODEL_ID,
        model_version="context.v2",
        include_context=True,
    )
    first = _episode(0)
    second = _episode(1)
    v2_second = _v2_like(second)
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
                    "feature_contract_version": "market_ohlcv_causal.v1",
                },
            },
            {
                "prediction_id": "gru-v2",
                "episode_id": v2_second.episode_id,
                "horizon_id": HORIZON_4H,
                "model_id": GRU_MODEL_ID,
                "model_version": "context.v2",
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
                    "feature_contract_version": CONTEXT_FEATURE_CONTRACT_VERSION,
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


def _v2_like(episode):
    return attach_world_context(
        (episode,),
        _FakeSource(
            SensorEvidence(status="missing", reason="no_artifact"),
            SensorEvidence(status="missing", reason="no_artifact"),
        ),
    )[0]
