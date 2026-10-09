from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
import math

import numpy as np
import pytest

from trader.application.world_model.dynamics_model import OnlineOHLCVDynamicsModel
from trader.application.world_model.dynamics_service import DynamicsReplayRequest, run_dynamics_replay
from trader.domain.world_dynamics import EpisodeEvidence, ObservedDynamicsBar
from trader.domain.world_episode import AnchorBar, WorldEpisode
from tests.domain.test_world_dynamics import T0, _episode


def _series(count: int = 80) -> tuple[EpisodeEvidence, ...]:
    rows = []
    close = 100.0
    for hour in range(count):
        ts = T0 + timedelta(hours=hour)
        opening = close * math.exp(.001 * math.sin(hour))
        close = opening * math.exp(.003 * math.cos(hour / 3))
        anchor = AnchorBar(ts, opening, max(opening, close) * 1.003,
                           min(opening, close) * .997, close, 1000 + hour * 10, "analysis_bars")
        episode = _episode(hour, anchor=anchor)
        rows.append(EpisodeEvidence(episode, ts + timedelta(seconds=30)))
    return tuple(rows)


def _request(rows: tuple[EpisodeEvidence, ...], **kwargs: object) -> DynamicsReplayRequest:
    return DynamicsReplayRequest(as_of=rows[-1].effective_available_at, paths=20, min_support=8, **kwargs)


def test_replay_is_deterministic_and_keeps_learning_before_prediction() -> None:
    rows = _series()
    first = run_dynamics_replay(rows, _request(rows))
    second = run_dynamics_replay(tuple(reversed(rows)), _request(rows))
    assert first == second
    assert first.status == "ready"
    assert first.transitions == 79
    assert first.support == 79
    assert len(first.trajectories) == 20
    assert len(first.scores) == 8
    by_id = {row.episode.episode_id: row for row in rows}
    for origin in first.evaluation_origins:
        assert origin.training_cutoff <= origin.prediction_at
        assert origin.support < first.support
        for target_id in origin.target_evidence_ids:
            target = by_id[target_id]
            assert target.effective_available_at > origin.prediction_at
            assert target.episode.observation.completed_bar_end_at > origin.prediction_at
    assert all(score.return_crps >= 0 for score in first.scores)
    assert all(path.authority == "shadow_only" for path in first.trajectories)
    assert len({path.seed for path in first.trajectories}) == 20


def test_saved_individual_path_seed_reproduces_its_bars() -> None:
    rows = _series(20)
    model = OnlineOHLCVDynamicsModel(minimum_support=8, seed=0)
    result = run_dynamics_replay(rows, _request(rows, steps=2), model=model)
    saved = result.trajectories[7]
    rng = np.random.Generator(np.random.PCG64(saved.seed))
    held = [row.episode.observation.anchor for row in rows]
    reproduced = []
    for bar in saved.bars:
        predicted = model.sample_next(held, rng, end_at=bar.end_at, step_index=bar.step_index)
        reproduced.append(predicted)
        held.append(predicted)
    assert tuple(reproduced) == saved.bars


def test_future_ohlcv_changes_do_not_change_earlier_forecasts_or_weights() -> None:
    rows = _series()
    first = run_dynamics_replay(rows, _request(rows, steps=1))
    original = rows[72]
    observation = original.episode.observation
    anchor = replace(observation.anchor, close=observation.anchor.close * 1.001,
                     high=observation.anchor.high * 1.002)
    changed = EpisodeEvidence(WorldEpisode(replace(observation, anchor=anchor)), original.recorded_at)
    modified = rows[:72] + (changed,) + rows[73:]
    second = run_dynamics_replay(modified, _request(rows, steps=1))
    early_first = tuple(row for row in first.evaluation_origins if row.prediction_at < rows[70].effective_available_at)
    early_second = tuple(row for row in second.evaluation_origins if row.prediction_at < rows[70].effective_available_at)
    assert early_first and early_first == early_second
    assert first.model_fingerprint != second.model_fingerprint


def test_delayed_target_is_not_learned_before_recorded_availability() -> None:
    rows = _series()
    normal = run_dynamics_replay(rows, _request(rows, steps=1))
    delayed = EpisodeEvidence(rows[56].episode, rows[60].recorded_at)
    modified = rows[:56] + (delayed,) + rows[57:]
    result = run_dynamics_replay(modified, _request(rows, steps=1))
    earlier = {origin.evidence_id: origin for origin in result.evaluation_origins}
    before_delay = normal.evaluation_origins[0]
    at_next = next(origin for origin in normal.evaluation_origins
                   if origin.evidence_id == rows[57].episode.episode_id)
    assert earlier[at_next.evidence_id].support < at_next.support
    assert before_delay.prediction_at < delayed.effective_available_at
    assert dict(result.exclusions)["evaluation_target_already_completed"] >= 1


def test_gaps_are_excluded_and_same_bar_duplicates_do_not_add_training() -> None:
    rows = _series()
    gapped = rows[:20] + rows[21:]
    result = run_dynamics_replay(gapped + (rows[10],), _request(rows))
    assert result.transitions == 77
    assert dict(result.exclusions)["nonadjacent_bar"] == 1
    assert dict(result.exclusions)["duplicate_identical_bar"] == 1


def test_conflicting_slot_is_removed_entirely() -> None:
    rows = _series()
    observation = rows[20].episode.observation
    conflict = EpisodeEvidence(WorldEpisode(replace(observation, anchor=replace(
        observation.anchor, volume=5000))), rows[20].recorded_at)
    result = run_dynamics_replay(rows + (conflict,), _request(rows))
    assert result.unique_anchors == 79
    assert dict(result.exclusions)["conflicting_bar_slot"] == 2
    assert result.transitions == 77


def test_later_conflicting_revision_never_rewrites_earlier_learning() -> None:
    rows = _series()
    first = run_dynamics_replay(rows, _request(rows, steps=1))
    observation = rows[20].episode.observation
    revision = EpisodeEvidence(WorldEpisode(replace(observation, anchor=replace(
        observation.anchor, volume=5000))), rows[75].recorded_at)
    second = run_dynamics_replay(rows + (revision,), _request(rows, steps=1))
    assert first.model_fingerprint == second.model_fingerprint
    assert first.evaluation_origins == second.evaluation_origins
    assert first.trajectories == second.trajectories
    assert dict(second.exclusions)["conflicting_later_bar_revision"] == 1


def test_future_evidence_is_excluded_and_stale_origin_has_no_rollout() -> None:
    rows = _series()
    cutoff = rows[60].effective_available_at
    result = run_dynamics_replay(rows, DynamicsReplayRequest(cutoff, paths=20, min_support=8))
    assert result.unique_anchors == 61
    assert result.support == 60
    stale = run_dynamics_replay(rows, DynamicsReplayRequest(
        as_of=rows[-1].episode.observation.completed_bar_end_at + timedelta(hours=1),
        paths=20, min_support=8))
    assert stale.status == "stale_origin"
    assert stale.trajectories == ()
    assert stale.evaluation_origins


def test_empty_cold_and_mixed_source_results_are_explicit() -> None:
    assert run_dynamics_replay((), DynamicsReplayRequest(T0, paths=20)).status == "no_data"
    rows = _series(5)
    assert run_dynamics_replay(rows, _request(rows)).status == "insufficient_support"
    observation = rows[-1].episode.observation
    other = EpisodeEvidence(WorldEpisode(replace(observation, anchor=replace(
        observation.anchor, source="other-feed"))), rows[-1].recorded_at)
    with pytest.raises(ValueError, match="one exact"):
        run_dynamics_replay(rows + (other,), _request(rows))


def test_retrieved_history_trains_now_without_claiming_past_forecasts() -> None:
    episodes = _series(24)
    captured = episodes[-1].effective_available_at
    recorded = captured + timedelta(seconds=5)
    bars = tuple(ObservedDynamicsBar(
        row.venue, row.symbol, row.bar_interval, row.anchor, captured, recorded,
    ) for row in episodes)
    result = run_dynamics_replay(bars, DynamicsReplayRequest(recorded, paths=20, min_support=8))
    assert result.status == "ready"
    assert result.read_evidence == 24 and result.support == 23
    assert result.training_cutoff == recorded
    assert not result.evaluation_origins and not result.scores
    assert all(path.origin_evidence_kind == "observed_market_bar" for path in result.trajectories)
    assert all(path.prediction_at == recorded for path in result.trajectories)
    assert dict(result.exclusions)["evaluation_target_already_completed"] > 0


def test_labels_fetched_together_are_trained_in_completed_bar_order() -> None:
    episodes = _series(24)
    captured = episodes[-1].effective_available_at
    bars = tuple(ObservedDynamicsBar(
        row.venue, row.symbol, row.bar_interval, row.anchor, captured, captured,
    ) for row in episodes)
    expected = OnlineOHLCVDynamicsModel(minimum_support=8, seed=0)
    for target_index in range(1, len(bars)):
        expected.update(tuple(row.anchor for row in bars[max(0, target_index - 20):target_index]),
                        bars[target_index].anchor)
    result = run_dynamics_replay(tuple(reversed(bars)), DynamicsReplayRequest(captured, paths=20, min_support=8))
    assert result.model_fingerprint == expected.fingerprint()


@pytest.mark.parametrize("kwargs", [dict(steps=0), dict(paths=1), dict(min_support=257), dict(seed=-1)])
def test_request_work_budgets_are_bounded(kwargs: dict[str, int]) -> None:
    with pytest.raises(ValueError):
        DynamicsReplayRequest(T0, **kwargs)
