from __future__ import annotations

from datetime import datetime, timezone

import pytest

from trader.domain.world_episode import (
    AnchorBar,
    Freshness,
    WorldEpisode,
    WorldObservation,
    WorldPrediction,
    canonical_json,
    canonical_sha256,
    world_episode_id,
)


def _observation(**overrides: object) -> WorldObservation:
    values: dict[str, object] = {
        "venue": "XTAI",
        "symbol": "2330.TW",
        "bar_interval": "1h",
        "as_of_bar_ts": "2026-08-22T02:00:00Z",
        "feature_contract_version": "market_state.v1",
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
        feature_contract_version="market_state.v1",
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

    legacy = WorldEpisode(observation=_observation(), schema_version="world_episode.v0")
    assert legacy.training_eligible is False
    assert legacy.training_reason == "unsupported_schema_version"


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
