from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
import json

import pytest

from trader.domain.world_dynamics import (
    EpisodeEvidence,
    NextBarTransition,
    ObservedDynamicsBar,
    SimulatedBar,
    WorldTrajectory,
    WORLD_DYNAMICS_TARGET_CONTRACT_VERSION,
)
from trader.domain.world_episode import (
    AnchorBar,
    GRAPH_FEATURE_CONTRACT_ID,
    MARKET_FEATURE_CONTRACT_ID,
    WorldEpisode,
    WorldObservation,
)


T0 = datetime(2026, 10, 9, 6, tzinfo=timezone.utc)


def _episode(hour: int = 0, **observation_changes: object) -> WorldEpisode:
    ts = T0 + timedelta(hours=hour)
    fields = {
        "venue": "XTAI",
        "symbol": "2330.TW",
        "bar_interval": "1h",
        "as_of_bar_ts": ts,
        "feature_contract_version": MARKET_FEATURE_CONTRACT_ID,
        "sampling_policy_version": "active_fresh_bar_v1",
        "anchor": AnchorBar(ts=ts, open=100, high=103, low=99, close=102, volume=1000, source="analysis_bars"),
        "available_at": ts + timedelta(seconds=10),
        "captured_at": ts + timedelta(seconds=20),
        "freshness": "fresh",
        "categorical_features": {"venue": "XTAI"},
        "numeric_features": {"return": 0.01},
    }
    fields.update(observation_changes)
    return WorldEpisode(observation=WorldObservation(**fields))


def _evidence(hour: int = 0, *, episode: WorldEpisode | None = None, delay_seconds: int = 30) -> EpisodeEvidence:
    return EpisodeEvidence(
        episode=_episode(hour) if episode is None else episode,
        recorded_at=T0 + timedelta(hours=hour, seconds=delay_seconds),
    )


def _bar(step_index: int = 1, **changes: object) -> SimulatedBar:
    fields = {
        "step_index": step_index,
        "end_at": T0 + timedelta(hours=step_index),
        "open": 102,
        "high": 104,
        "low": 101,
        "close": 103,
        "volume": 1200,
    }
    fields.update(changes)
    return SimulatedBar(**fields)


def _trajectory(**changes: object) -> WorldTrajectory:
    episode = _episode()
    fields = {
        "origin_evidence_id": episode.episode_id,
        "origin_evidence_hash": episode.payload_hash,
        "origin_evidence_kind": "world_episode",
        "symbol": "2330.TW",
        "venue": "XTAI",
        "bar_interval": "1h",
        "origin_end_at": T0,
        "prediction_at": T0 + timedelta(seconds=30),
        "training_cutoff": T0,
        "model_id": "gru_next_bar.v1",
        "model_fingerprint": "test-model-fingerprint",
        "seed": 3,
        "bars": (_bar(), _bar(2)),
    }
    fields.update(changes)
    return WorldTrajectory(**fields)


def test_episode_evidence_uses_latest_provider_capture_and_persistence_clock() -> None:
    evidence = _evidence(delay_seconds=40)
    assert evidence.effective_available_at == T0 + timedelta(seconds=40)
    assert _evidence(delay_seconds=15).effective_available_at == T0 + timedelta(seconds=20)
    assert _evidence(delay_seconds=40).recorded_at.tzinfo == timezone.utc
    with pytest.raises(FrozenInstanceError):
        evidence.recorded_at = T0


def test_episode_evidence_rejects_unproven_clocks_and_simulated_inputs() -> None:
    with pytest.raises(ValueError, match="available_at and captured_at"):
        _evidence(episode=_episode(available_at=None))
    with pytest.raises(ValueError, match="available_at and captured_at"):
        _evidence(episode=_episode(captured_at=None))
    with pytest.raises(ValueError, match="timezone"):
        EpisodeEvidence(_episode(), datetime(2026, 10, 9, 6))
    for impostor in (_bar(), _episode().to_dict(), _episode().observation.anchor):
        with pytest.raises(TypeError, match="real WorldEpisode"):
            EpisodeEvidence(impostor, T0)


def test_next_bar_target_has_deterministic_provenance_without_target_features() -> None:
    transition = NextBarTransition(_evidence(), _evidence(1, delay_seconds=45))
    assert transition.label_available_at == T0 + timedelta(hours=1, seconds=45)
    assert transition.transition_id == NextBarTransition(_evidence(), _evidence(1, delay_seconds=45)).transition_id
    assert transition.transition_id != NextBarTransition(_evidence(), _evidence(1, delay_seconds=50)).transition_id
    payload = transition.to_dict()
    assert payload["target_contract_version"] == WORLD_DYNAMICS_TARGET_CONTRACT_VERSION
    assert payload["source_evidence_hash"] == transition.source.episode.payload_hash
    assert payload["source_evidence_kind"] == "world_episode"
    assert payload["schema_version"] == "world_next_bar_transition.v2"
    assert payload["target_bar"]["close"] == 102.0
    assert "numeric_features" not in json.dumps(payload)
    assert "observation" not in payload


@pytest.mark.parametrize("gap", [0, 2, 24, -1])
def test_next_bar_target_rejects_same_slot_gaps_nights_and_reverse_time(gap: int) -> None:
    with pytest.raises(ValueError, match="exactly adjacent"):
        NextBarTransition(_evidence(), _evidence(gap))


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({"venue": "XNAS"}, "venue"),
        ({"symbol": "AAPL"}, "symbol"),
        ({"bar_interval": "30m"}, "bar_interval"),
        ({"sampling_policy_version": "another_policy"}, "sampling_policy_version"),
    ],
)
def test_next_bar_target_rejects_series_and_sampling_mismatch(changes: dict[str, object], expected: str) -> None:
    with pytest.raises(ValueError, match=expected):
        NextBarTransition(_evidence(), _evidence(1, episode=_episode(1, **changes)))


@pytest.mark.parametrize(("field", "value"), [("source", "other_feed"), ("timestamp_semantics", "bar_end")])
def test_next_bar_target_rejects_source_and_timestamp_semantics_mismatch(field: str, value: str) -> None:
    target = _episode(1)
    target = replace(target, observation=replace(target.observation, anchor=replace(target.observation.anchor, **{field: value})))
    with pytest.raises(ValueError, match=f"anchor.{field}"):
        NextBarTransition(_evidence(), _evidence(1, episode=target))


def test_next_bar_target_rejects_ineligible_off_grid_and_late_source_evidence() -> None:
    with pytest.raises(ValueError, match="training-eligible"):
        NextBarTransition(_evidence(episode=replace(_episode(), training_eligible=False)), _evidence(1))
    shifted = T0 + timedelta(minutes=1)
    with pytest.raises(ValueError, match="canonical completed"):
        NextBarTransition(
            _evidence(episode=_episode(
                as_of_bar_ts=shifted,
                anchor=replace(_episode().observation.anchor, ts=shifted),
                available_at=shifted + timedelta(seconds=10),
                captured_at=shifted + timedelta(seconds=20),
            )),
            _evidence(1),
        )
    with pytest.raises(ValueError, match="known by target availability"):
        NextBarTransition(_evidence(delay_seconds=4000), _evidence(1))
    with pytest.raises(TypeError, match="EpisodeEvidence"):
        NextBarTransition(_episode(), _evidence(1))


def test_next_bar_target_rejects_graph_contract_and_uses_completed_bar_clock() -> None:
    graph_episode = _episode(feature_contract_version=GRAPH_FEATURE_CONTRACT_ID)
    with pytest.raises(ValueError, match="market-only"):
        NextBarTransition(_evidence(episode=graph_episode), _evidence(1))
    # Start-stamped bars transition from their completion, not their opening.
    source = _episode(
        anchor=replace(_episode().observation.anchor, timestamp_semantics="bar_start"),
        available_at=T0 + timedelta(hours=1, seconds=10),
        captured_at=T0 + timedelta(hours=1, seconds=20),
    )
    target = _episode(
        1,
        anchor=replace(_episode(1).observation.anchor, timestamp_semantics="bar_start"),
        available_at=T0 + timedelta(hours=2, seconds=10),
        captured_at=T0 + timedelta(hours=2, seconds=20),
    )
    transition = NextBarTransition(_evidence(episode=source), _evidence(1, episode=target))
    assert transition.source.episode.observation.completed_bar_end_at == T0 + timedelta(hours=1)
    assert transition.target.episode.observation.completed_bar_end_at == T0 + timedelta(hours=2)


@pytest.mark.parametrize(
    ("changes", "exception", "message"),
    [
        ({"step_index": True}, TypeError, "integer"),
        ({"step_index": 0}, ValueError, "positive"),
        ({"open": 0}, ValueError, "positive"),
        ({"close": float("nan")}, ValueError, "finite"),
        ({"volume": -1}, ValueError, "non-negative"),
        ({"volume": True}, TypeError, "finite number"),
        ({"high": 102}, ValueError, "coherent"),
        ({"end_at": "2026-10-09T07:00:00"}, ValueError, "timezone"),
    ],
)
def test_simulated_bar_enforces_geometry_and_clocks(changes: dict[str, object], exception: type[Exception], message: str) -> None:
    with pytest.raises(exception, match=message):
        _bar(**changes)


def test_simulated_flat_zero_volume_bar_is_valid_but_is_not_a_real_episode() -> None:
    bar = _bar(open=100, high=100, low=100, close=100, volume=0)
    assert bar.simulated is True
    assert not isinstance(bar, AnchorBar)
    assert bar.to_dict()["kind"] == "simulated_bar"
    with pytest.raises(ValueError, match="world_episode.v1"):
        WorldEpisode.from_dict({**_episode().to_dict(), **bar.to_dict()})


def test_trajectory_serialization_is_immutable_and_permanently_shadow_only() -> None:
    input_bars = [_bar(), _bar(2)]
    trajectory = _trajectory(bars=input_bars)
    input_bars.clear()
    assert len(trajectory.bars) == 2
    assert trajectory.trajectory_id == _trajectory().trajectory_id
    assert trajectory.trajectory_id != _trajectory(seed=4).trajectory_id
    payload = trajectory.to_dict()
    assert payload["authority"] == "shadow_only"
    assert payload["decision_effect"] == "none"
    assert payload["recommendation"] == "NO_GO"
    assert payload["context_policy"] == "market_only"
    assert payload["kind"] == "simulated_trajectory"
    assert payload["schema_version"] == "world_trajectory.v2"
    assert payload["origin_evidence_kind"] == "world_episode"
    assert "origin_episode_id" not in payload
    payload["bars"][0]["close"] = 1
    assert trajectory.bars[0].close == 103
    with pytest.raises(FrozenInstanceError):
        trajectory.seed = 4
    with pytest.raises(ValueError, match="world_episode.v1"):
        WorldEpisode.from_dict({**_episode().to_dict(), **payload})


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"prediction_at": T0 - timedelta(seconds=1)}, "origin completion"),
        ({"training_cutoff": T0 + timedelta(seconds=31)}, "training_cutoff"),
        ({"bars": ()}, "non-empty"),
        ({"bars": (_bar(2),)}, "contiguous"),
        ({"bars": (_bar(), _bar(3))}, "contiguous"),
        ({"bars": (_bar(end_at=T0 + timedelta(minutes=90)),)}, "contiguous"),
        ({"origin_end_at": T0 + timedelta(seconds=1)}, "canonical bar grid"),
        ({"bar_interval": "unknown"}, "supported positive"),
        ({"context_policy": "frozen_context"}, "market_only"),
        ({"origin_evidence_kind": "simulated_bar"}, "real dynamics evidence"),
        ({"prediction_at": T0 + timedelta(hours=1)}, "first simulated bar"),
        ({"prediction_at": T0 + timedelta(hours=2)}, "first simulated bar"),
    ],
)
def test_trajectory_rejects_future_training_noncontiguous_paths_and_implicit_context(changes: dict[str, object], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        _trajectory(**changes)


def test_trajectory_rejects_real_anchors_and_episodes_as_simulated_bars() -> None:
    for real in (_episode(), _episode().observation.anchor):
        with pytest.raises(TypeError, match="never real episodes or anchors"):
            _trajectory(bars=(real,))
    # Labels available exactly at the declared forecast cutoff can be consumed.
    assert _trajectory(training_cutoff=T0 + timedelta(seconds=30)).training_cutoff == T0 + timedelta(seconds=30)


def _observed(hour: int = 0, **changes: object) -> ObservedDynamicsBar:
    fields = {
        "venue": "XTAI",
        "symbol": "2330.TW",
        "bar_interval": "1h",
        "anchor": _episode(hour).observation.anchor,
        "first_seen_at": T0 + timedelta(hours=3, seconds=10),
        "recorded_at": T0 + timedelta(hours=3, seconds=20),
    }
    fields.update(changes)
    return ObservedDynamicsBar(**fields)


def test_observed_bar_records_historical_geometry_as_known_now_without_freshness() -> None:
    observed = _observed()
    assert observed.anchor.ts == T0
    assert observed.completed_end_at == T0
    assert observed.effective_available_at == T0 + timedelta(hours=3, seconds=20)
    assert observed.training_eligible is True
    assert observed.kind == "observed_market_bar"
    assert observed.feature_contract_version != MARKET_FEATURE_CONTRACT_ID
    assert observed.sampling_policy_version == "first_seen_completed_market_bar.v1"
    assert not hasattr(observed, "episode")
    assert not hasattr(observed, "freshness")
    payload = observed.to_dict()
    assert payload["schema_version"] == "observed_dynamics_bar.v1"
    assert "available_at" not in payload
    assert "freshness" not in payload
    assert "captured_at" not in payload
    assert "numeric_features" not in payload


def test_observed_bar_serialization_has_stable_identity_and_verified_clock_hash() -> None:
    observed = _observed()
    assert ObservedDynamicsBar.from_dict(json.loads(json.dumps(observed.to_dict()))) == observed
    later_receipt = _observed(first_seen_at=T0 + timedelta(hours=4), recorded_at=T0 + timedelta(hours=4, seconds=10))
    assert later_receipt.evidence_id == observed.evidence_id
    assert later_receipt.payload_hash != observed.payload_hash
    revised = _observed(anchor=replace(observed.anchor, close=102.5))
    assert revised.evidence_id != observed.evidence_id
    assert revised.payload_hash != observed.payload_hash
    with pytest.raises(FrozenInstanceError):
        observed.first_seen_at = T0


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("schema_version", "world_episode.v1", "schema_version"),
        ("kind", "world_episode", "kind"),
        ("feature_contract_version", MARKET_FEATURE_CONTRACT_ID, "feature_contract_version"),
        ("sampling_policy_version", "legacy_policy", "sampling_policy_version"),
        ("evidence_id", "wrong-id", "evidence_id"),
        ("payload_hash", "wrong-hash", "payload_hash"),
    ],
)
def test_observed_bar_from_dict_rejects_contract_or_provenance_tampering(field: str, value: str, message: str) -> None:
    payload = _observed().to_dict()
    payload[field] = value
    with pytest.raises(ValueError, match=message):
        ObservedDynamicsBar.from_dict(payload)


def test_observed_bar_from_dict_rejects_hidden_fields_and_missing_receipt_hash() -> None:
    for change in (lambda payload: payload.update(freshness="fresh"), lambda payload: payload.pop("payload_hash")):
        payload = _observed().to_dict()
        change(payload)
        with pytest.raises(ValueError, match="exactly its schema fields"):
            ObservedDynamicsBar.from_dict(payload)
    payload = _observed().to_dict()
    payload["anchor"]["available_at"] = T0.isoformat()
    with pytest.raises(ValueError, match="exactly the AnchorBar fields"):
        ObservedDynamicsBar.from_dict(payload)


def test_observed_bar_rejects_future_incomplete_offgrid_and_backdated_receipts() -> None:
    with pytest.raises(ValueError, match="completed bar"):
        _observed(4)
    with pytest.raises(ValueError, match="completed bar"):
        _observed(anchor=replace(_observed().anchor, timestamp_semantics="unknown"))
    with pytest.raises(ValueError, match="completed bar"):
        _observed(anchor=replace(_observed().anchor, ts=T0 + timedelta(minutes=1)))
    with pytest.raises(ValueError, match="recorded_at must not precede"):
        _observed(recorded_at=T0 + timedelta(hours=3, seconds=9))
    with pytest.raises(ValueError, match="timezone"):
        _observed(first_seen_at="2026-10-09T09:00:10")
    with pytest.raises(ValueError, match="completed bar"):
        _observed(anchor=replace(_observed().anchor, timestamp_semantics="bar_start"), first_seen_at=T0, recorded_at=T0)


def test_observed_bar_rejects_simulated_and_mapped_anchor_impostors() -> None:
    for impostor in (_bar(), _observed().anchor.to_dict(), _episode()):
        with pytest.raises(TypeError, match="real AnchorBar"):
            _observed(anchor=impostor)


def test_evidence_types_share_read_only_geometry_and_provenance_properties() -> None:
    episode, observed = _evidence(), _observed()
    for evidence in (episode, observed):
        assert evidence.evidence_id
        assert len(evidence.payload_hash) == 64
        assert evidence.venue == "XTAI"
        assert evidence.symbol == "2330.TW"
        assert evidence.bar_interval == "1h"
        assert evidence.anchor.close == 102
        assert evidence.completed_end_at == T0
        assert evidence.training_eligible is True
        assert evidence.feature_contract_version
        assert evidence.sampling_policy_version
        assert evidence.series_key == (
            evidence.venue, evidence.symbol, evidence.bar_interval,
            evidence.feature_contract_version, evidence.sampling_policy_version,
            evidence.anchor.source, evidence.anchor.timestamp_semantics,
        )
        with pytest.raises((FrozenInstanceError, AttributeError)):
            evidence.evidence_id = "changed"


def test_next_bar_transition_accepts_same_receipt_time_bootstrap_but_never_crosses_evidence_types() -> None:
    source, target = _observed(), _observed(1)
    transition = NextBarTransition(source, target)
    assert transition.label_available_at == target.recorded_at
    assert transition.transition_id.startswith("world-next-bar-transition:v2:")
    payload = transition.to_dict()
    assert payload["source_evidence_id"] == source.evidence_id
    assert payload["target_evidence_id"] == target.evidence_id
    assert payload["source_evidence_kind"] == payload["target_evidence_kind"] == "observed_market_bar"
    assert "source_episode_id" not in payload
    with pytest.raises(TypeError, match="same evidence type"):
        NextBarTransition(_evidence(), target)
    with pytest.raises(TypeError, match="same evidence type"):
        NextBarTransition(source, _evidence(1))
    with pytest.raises(ValueError, match="exactly adjacent"):
        NextBarTransition(source, _observed(2))
    with pytest.raises(ValueError, match="known by target availability"):
        NextBarTransition(_observed(recorded_at=T0 + timedelta(hours=3, seconds=30)), target)


def test_trajectory_provenance_distinguishes_observed_bars_from_real_episodes() -> None:
    observed = _observed(first_seen_at=T0 + timedelta(seconds=10), recorded_at=T0 + timedelta(seconds=20))
    trajectory = _trajectory(
        origin_evidence_id=observed.evidence_id,
        origin_evidence_hash=observed.payload_hash,
        origin_evidence_kind=observed.kind,
    )
    assert trajectory.trajectory_id.startswith("world-trajectory:v2:")
    assert trajectory.to_dict()["origin_evidence_kind"] == "observed_market_bar"
    assert not hasattr(trajectory, "origin_episode_id")


def test_observed_market_bar_boundary_rejects_subclass_impostors() -> None:
    class FakeAnchor(AnchorBar):
        pass

    class FakeObserved(ObservedDynamicsBar):
        pass

    anchor_fields = _observed().anchor.to_dict()
    with pytest.raises(TypeError, match="real AnchorBar"):
        _observed(anchor=FakeAnchor(**anchor_fields))
    fake = FakeObserved(
        venue="XTAI", symbol="2330.TW", bar_interval="1h",
        anchor=_observed().anchor, first_seen_at=T0, recorded_at=T0,
    )
    with pytest.raises(TypeError, match="EpisodeEvidence or ObservedDynamicsBar"):
        NextBarTransition(fake, _observed(1))
