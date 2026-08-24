from __future__ import annotations

from datetime import datetime, timezone

import pytest

from trader.domain.world_episode import (
    AnchorBar,
    DEFAULT_WORLD_HORIZONS,
    Freshness,
    MARKET_FEATURE_CONTRACT_ID,
    WorldEpisode,
    WorldObservation,
    WorldOutcome,
    WorldPrediction,
    canonical_json,
    canonical_sha256,
    is_eligible_completed_bar,
    world_episode_id,
)


def _observation(**overrides: object) -> WorldObservation:
    values: dict[str, object] = {
        "venue": "XTAI",
        "symbol": "2330.TW",
        "bar_interval": "1h",
        "as_of_bar_ts": "2026-08-22T02:00:00Z",
        "feature_contract_version": MARKET_FEATURE_CONTRACT_ID,
        "sampling_policy_version": "active_fresh_bar_v1",
        "anchor": AnchorBar(
            ts="2026-08-22T02:00:00+00:00",
            open=100.0,
            high=103.0,
            low=99.0,
            close=102.0,
            volume=1_000.0,
            source="analysis_bars",
            timestamp_semantics="bar_close",
        ),
        "available_at": "2026-08-22T02:01:00+00:00",
        "captured_at": "2026-08-22T02:02:00+00:00",
        "freshness": Freshness(status="fresh", data_age_minutes=2.0),
        "categorical_features": {"market_regime": "trending_up", "session_phase": "regular"},
        "numeric_features": {"return": 0.01, "atr_pct": 0.02, "range_position": 0.7},
    }
    values.update(overrides)
    return WorldObservation(**values)  # type: ignore[arg-type]


def test_action_portfolio_and_scheduler_fields_cannot_enter_features() -> None:
    for key in ("action", "portfolio", "scheduler", "nested_action"):
        with pytest.raises(ValueError, match="forbidden"):
            _observation(numeric_features={key: 1.0})

    with pytest.raises(ValueError, match="forbidden"):
        _observation(categorical_features={"market_regime": {"task_id": "42"}})

    for key in ("mandate_role", "memory_available", "macro_event_bucket"):
        with pytest.raises(ValueError, match="whitelisted"):
            _observation(categorical_features={key: "present"})


def test_observation_rejects_naive_timestamps_and_nonfinite_features() -> None:
    with pytest.raises(ValueError, match="timezone"):
        _observation(as_of_bar_ts=datetime(2026, 8, 22, 2, 0, 0))

    with pytest.raises(ValueError, match="finite"):
        _observation(numeric_features={"return": float("nan")})

    with pytest.raises(ValueError, match="equal"):
        _observation(
            anchor=AnchorBar(
                ts="2026-08-22T01:00:00+00:00",
                open=100.0,
                high=103.0,
                low=99.0,
                close=102.0,
                volume=1_000.0,
                source="analysis_bars",
            )
        )


def test_episode_identity_and_hash_are_stable_across_equivalent_utc_inputs() -> None:
    first = _observation()
    second = _observation(as_of_bar_ts=datetime(2026, 8, 22, 2, 0, tzinfo=timezone.utc))

    assert first.episode_id == second.episode_id
    assert first.feature_hash == second.feature_hash
    assert first.episode_id == world_episode_id(
        venue="XTAI",
        symbol="2330.TW",
        bar_interval="1h",
        as_of_bar_ts="2026-08-22T02:00:00Z",
        feature_contract_version=MARKET_FEATURE_CONTRACT_ID,
        sampling_policy_version="active_fresh_bar_v1",
    )
    assert canonical_json({"b": 2, "a": 1}) == canonical_json({"a": 1, "b": 2})
    assert canonical_sha256({"b": 2, "a": 1}) == canonical_sha256({"a": 1, "b": 2})


def test_legacy_or_missing_point_in_time_evidence_is_explicitly_non_trainable() -> None:
    observation = _observation(
        available_at=None,
        captured_at=None,
        freshness="unknown",
        categorical_features={},
        numeric_features={},
    )

    episode = WorldEpisode(observation=observation)

    assert episode.training_eligible is False
    assert episode.training_reason == "missing_available_at"
    assert episode.to_dict()["training_eligible"] is False
    with pytest.raises(ValueError, match="cannot mark incomplete"):
        WorldEpisode(observation=observation, training_eligible=True)

    with pytest.raises(ValueError, match="schema_version"):
        WorldEpisode(observation=_observation(), schema_version="world_episode.v0")


def test_episode_rejects_old_feature_contracts_and_missing_schema() -> None:
    with pytest.raises(ValueError, match="feature_contract_version"):
        _observation(feature_contract_version="market_ohlcv_causal.v1")
    with pytest.raises(ValueError, match="feature_contract_version"):
        _observation(feature_contract_version="market_ohlcv_context.v2")
    with pytest.raises(ValueError, match="feature_contract_version"):
        _observation(feature_contract_version="market_ohlcv_graph.v3")
    with pytest.raises(ValueError, match="schema_version"):
        WorldEpisode.from_dict(
            {
                "observation": _observation().to_dict(),
                "training_eligible": False,
                "training_reason": "explicitly_ineligible",
            }
        )


def test_episode_copies_feature_maps_and_exposes_a_replayable_payload() -> None:
    numeric = {"return": 0.01}
    observation = _observation(numeric_features=numeric)
    numeric["return"] = 0.9

    assert observation.numeric_features["return"] == 0.01
    with pytest.raises(TypeError):
        observation.numeric_features["return"] = 0.2  # type: ignore[index]
    assert WorldEpisode.from_dict(WorldEpisode(observation=observation).to_dict()).payload_hash == (
        WorldEpisode(observation=observation).payload_hash
    )
    payload = WorldEpisode(observation=observation).to_dict()
    payload["episode_id"] = "world-episode:v1:not-the-derived-id"
    with pytest.raises(ValueError, match="does not match"):
        WorldEpisode.from_dict(payload)


def test_prediction_is_three_class_and_permanently_shadow_only() -> None:
    prediction = WorldPrediction(
        episode_id=_observation().episode_id,
        horizon_id="elapsed_4h.v1",
        model_id="baseline",
        model_version="v1",
        feature_hash="abc",
        created_at="2026-08-22T02:02:00+00:00",
        probabilities={"DOWN": 0.2, "FLAT": 0.5, "UP": 0.3},
        status="shadow_only",
        tier="global",
        support=12,
        global_support=12,
        training_cutoff="2026-08-22T01:00:00+00:00",
        model_fingerprint="baseline:abc",
    )

    assert prediction.predicted_class == "FLAT"
    assert prediction.to_dict()["decision_effect"] == "none"
    with pytest.raises(ValueError, match="probabilities"):
        WorldPrediction(
            episode_id=prediction.episode_id,
            horizon_id=prediction.horizon_id,
            model_id="baseline",
            model_version="v1",
            feature_hash="abc",
            created_at="2026-08-22T02:02:00+00:00",
            probabilities={"DOWN": 0.5, "FLAT": 0.5},
            status="shadow_only",
        )


def test_world_outcome_from_dict_adapts_nested_labeler_payload() -> None:
    payload = {
        "episode_id": "episode-nested",
        "horizon_code": "elapsed_4h.v1",
        "status": "observed",
        "training_eligible": True,
        "label": {
            "direction": "UP",
            "target_at": "2026-08-22T06:00:00+00:00",
            "available_at": "2026-08-22T06:00:00+00:00",
            "computed_at": "2026-08-22T06:01:00+00:00",
            "anchor_close": 100.0,
            "endpoint_close": 101.0,
            "horizon": {
                "horizon_id": "elapsed_4h.v1",
                "duration_seconds": 4 * 60 * 60,
                "endpoint_rule": "first_fully_available_bar_at_or_after_target.v1",
                "max_lateness_seconds": 3600,
            },
        },
        "evidence": {
            "target_bar": {
                "ts": "2026-08-22T06:00:00+00:00",
                "close": 101.0,
                "source": "yahoo",
                "fingerprint": "c" * 64,
            },
            "anchor_bar": {"close": 100.0},
        },
        "supersedes_outcome_event_id": "previous-event",
    }

    outcome = WorldOutcome.from_dict(payload)

    assert outcome.direction == "UP"
    assert outcome.source == "yahoo"
    assert outcome.source_raw_sha256 == "c" * 64
    assert outcome.endpoint_bar_ts.isoformat() == "2026-08-22T06:00:00+00:00"
    assert outcome.horizon.horizon_id == "elapsed_4h.v1"
    assert outcome.horizon.duration_seconds == 4 * 60 * 60
    assert outcome.supersedes_event_id == "previous-event"
    with pytest.raises(ValueError, match="prediction class"):
        WorldOutcome.from_dict({**payload, "label": {**payload["label"], "direction": "up"}})
    assert outcome.training_eligible is True


def test_world_outcome_from_dict_does_not_invent_missing_source_evidence() -> None:
    with pytest.raises(ValueError, match="immutable source evidence"):
        WorldOutcome.from_dict(
            {
                "episode_id": "episode-missing-source",
                "horizon_id": "elapsed_4h.v1",
                "status": "observed",
                "target_at": "2026-08-22T06:00:00+00:00",
                "available_at": "2026-08-22T06:00:00+00:00",
                "anchor_close": 100.0,
                "endpoint_close": 101.0,
                "endpoint_bar_ts": "2026-08-22T06:00:00+00:00",
                "source": "yahoo",
                "training_eligible": True,
            }
        )


def test_eligible_completed_bar_requires_canonical_grid_not_flatness() -> None:
    aligned = datetime(2026, 8, 22, 9, 45, tzinfo=timezone.utc)
    trailing_quote = datetime(2026, 8, 22, 9, 52, tzinfo=timezone.utc)

    assert is_eligible_completed_bar(
        ts=aligned,
        bar_interval="15m",
        timestamp_semantics="bar_start",
    )
    assert is_eligible_completed_bar(
        ts=aligned,
        bar_interval="15m",
        timestamp_semantics="bar_start",
        end_at=datetime(2026, 8, 22, 10, 0, tzinfo=timezone.utc),
    )
    assert not is_eligible_completed_bar(
        ts=trailing_quote,
        bar_interval="15m",
        timestamp_semantics="bar_start",
    )
    assert not is_eligible_completed_bar(
        ts=datetime(2026, 8, 22, 9, 45, 1, tzinfo=timezone.utc),
        bar_interval="15m",
        timestamp_semantics="bar_start",
    )
    assert not is_eligible_completed_bar(
        ts=aligned,
        bar_interval="15m",
        timestamp_semantics="bar_start",
        available_at=datetime(2026, 8, 22, 9, 50, tzinfo=timezone.utc),
    )


def test_observation_rejects_availability_before_completed_bar_end() -> None:
    with pytest.raises(ValueError, match="completed bar"):
        _observation(available_at="2026-08-22T01:59:00+00:00")

    with pytest.raises(ValueError, match="completed bar"):
        _observation(
            bar_interval="15m",
            as_of_bar_ts="2026-08-22T02:00:00Z",
            available_at="2026-08-22T02:10:00+00:00",
            captured_at="2026-08-22T02:20:00+00:00",
            anchor=AnchorBar(
                ts="2026-08-22T02:00:00+00:00",
                open=100.0,
                high=103.0,
                low=99.0,
                close=102.0,
                volume=1_000.0,
                source="analysis_bars",
                timestamp_semantics="bar_start",
            ),
        )


def test_observation_accepts_availability_at_completed_bar_end() -> None:
    observation = _observation(
        bar_interval="15m",
        as_of_bar_ts="2026-08-22T02:00:00Z",
        available_at="2026-08-22T02:15:00+00:00",
        captured_at="2026-08-22T02:16:00+00:00",
        anchor=AnchorBar(
            ts="2026-08-22T02:00:00+00:00",
            open=100.0,
            high=100.0,
            low=100.0,
            close=100.0,
            volume=0.0,
            source="analysis_bars",
            timestamp_semantics="bar_start",
        ),
    )

    assert observation.available_at is not None
    assert observation.available_at.isoformat() == "2026-08-22T02:15:00+00:00"
    assert observation.completed_bar_end_at == datetime(2026, 8, 22, 2, 15, tzinfo=timezone.utc)


def test_observation_preserves_proven_completed_bar_end() -> None:
    bar_start = _observation(
        bar_interval="15m",
        as_of_bar_ts="2026-08-24T05:15:00Z",
        available_at="2026-08-24T05:30:00+00:00",
        captured_at="2026-08-24T05:31:00+00:00",
        anchor=AnchorBar(
            ts="2026-08-24T05:15:00+00:00",
            open=100.0,
            high=103.0,
            low=99.0,
            close=102.0,
            volume=1_000.0,
            source="analysis_bars",
            timestamp_semantics="bar_start",
        ),
    )
    bar_close = _observation(
        bar_interval="15m",
        as_of_bar_ts="2026-08-24T05:15:00Z",
        available_at="2026-08-24T05:15:00+00:00",
        captured_at="2026-08-24T05:16:00+00:00",
        anchor=AnchorBar(
            ts="2026-08-24T05:15:00+00:00",
            open=100.0,
            high=103.0,
            low=99.0,
            close=102.0,
            volume=1_000.0,
            source="analysis_bars",
            timestamp_semantics="bar_close",
        ),
    )
    unknown = _observation(
        bar_interval="15m",
        as_of_bar_ts="2026-08-24T05:15:00Z",
        available_at="2026-08-24T05:15:00+00:00",
        captured_at="2026-08-24T05:16:00+00:00",
        anchor=AnchorBar(
            ts="2026-08-24T05:15:00+00:00",
            open=100.0,
            high=103.0,
            low=99.0,
            close=102.0,
            volume=1_000.0,
            source="analysis_bars",
            timestamp_semantics="unknown",
        ),
    )

    assert bar_start.completed_bar_end_at == datetime(2026, 8, 24, 5, 30, tzinfo=timezone.utc)
    assert bar_close.completed_bar_end_at == bar_close.as_of_bar_ts
    assert unknown.completed_bar_end_at is None


def _outcome(**overrides: object) -> WorldOutcome:
    values: dict[str, object] = {
        "episode_id": "episode-causal",
        "horizon": DEFAULT_WORLD_HORIZONS[0],
        "status": "observed",
        "target_at": "2026-08-22T06:00:00+00:00",
        "available_at": "2026-08-22T06:00:00+00:00",
        "computed_at": "2026-08-22T06:01:00+00:00",
        "anchor_close": 100.0,
        "endpoint_close": 101.0,
        "endpoint_bar_ts": "2026-08-22T06:00:00+00:00",
        "source": "yahoo",
        "source_raw_sha256": "c" * 64,
    }
    values.update(overrides)
    return WorldOutcome(**values)  # type: ignore[arg-type]


def test_world_outcome_rejects_impossible_causal_timings() -> None:
    with pytest.raises(ValueError, match="endpoint_bar_ts"):
        _outcome(endpoint_bar_ts="2026-08-22T05:45:00+00:00")
    with pytest.raises(ValueError, match="available_at"):
        _outcome(available_at="2026-08-22T05:59:00+00:00")
    with pytest.raises(ValueError, match="computed_at"):
        _outcome(computed_at="2026-08-22T05:59:00+00:00")


def test_malformed_legacy_outcome_without_causal_proof_is_not_trainable() -> None:
    outcome = _outcome(computed_at=None, training_eligible=None)

    assert outcome.training_eligible is False
    with pytest.raises(ValueError, match="causal"):
        _outcome(computed_at=None, training_eligible=True)


def test_pending_outcome_may_be_computed_before_target() -> None:
    pending = WorldOutcome(
        episode_id="episode-pending",
        horizon=DEFAULT_WORLD_HORIZONS[0],
        status="pending",
        target_at="2026-08-22T06:00:00+00:00",
        computed_at="2026-08-22T05:00:00+00:00",
    )

    assert pending.training_eligible is False
    assert pending.status == "pending"
