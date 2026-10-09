from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest

from trader.application.world_model.dynamics_capture import (
    MAX_PROVIDER_ROWS_PER_SERIES,
    capture_dynamics_bars,
)
from trader.domain.world_dynamics import ObservedDynamicsBar
from tests.domain.test_world_dynamics import T0, _episode


def _payload(hour: int, **changes: object) -> dict[str, object]:
    payload = {
        **_episode(hour).observation.anchor.to_dict(), "interval": "1h",
        "available_at": (T0 + timedelta(hours=20)).isoformat(),
    }
    payload.update(changes)
    return payload


def _capture(payloads: list[object], **changes: object):
    kwargs = {
        "episodes": (_episode(19),), "bars_by_symbol": {"2330.TW": payloads},
        "captured_at": T0 + timedelta(hours=20),
    }
    kwargs.update(changes)
    return capture_dynamics_bars(**kwargs)


def test_historical_bars_are_first_observed_now_never_backdated_episodes() -> None:
    result = _capture([_payload(hour, available_at=(T0 + timedelta(hours=hour, seconds=1)).isoformat())
                       for hour in range(12)])
    assert len(result.bars) == 12
    assert all(type(bar) is ObservedDynamicsBar for bar in result.bars)
    assert all(bar.first_seen_at == T0 + timedelta(hours=20) for bar in result.bars)
    assert all(bar.recorded_at == bar.first_seen_at for bar in result.bars)
    assert result.bars[0].completed_end_at == T0
    assert "freshness" not in result.bars[0].to_dict()


def test_capture_validates_completion_geometry_and_feed_provenance() -> None:
    result = _capture([
        _payload(1),
        _payload(2, high=1),
        _payload(3, source="another_feed"),
        _payload(4, interval="15m"),
        _payload(5, ts=(T0 + timedelta(hours=5, minutes=1)).isoformat()),
        _payload(21, available_at=(T0 + timedelta(hours=21)).isoformat()),
        _payload(6, available_at=None),
    ])
    assert [bar.anchor.ts for bar in result.bars] == [T0 + timedelta(hours=1)]
    assert dict(result.exclusions) == {
        "bar_series_mismatch": 2, "future_or_incomplete_bar": 1,
        "invalid_market_bar": 2, "noncanonical_or_unavailable_bar": 1,
    }


def test_start_stamped_unfinished_bar_is_not_a_completed_history_target() -> None:
    episode = _episode(19)
    episode = replace(episode, observation=replace(
        episode.observation, anchor=replace(episode.observation.anchor, timestamp_semantics="bar_start"),
        available_at=T0 + timedelta(hours=20), captured_at=T0 + timedelta(hours=20),
    ))
    result = _capture([
        _payload(18, timestamp_semantics="bar_start"),
        _payload(20, timestamp_semantics="bar_start", available_at=(T0 + timedelta(hours=21)).isoformat()),
    ], episodes=(episode,))
    assert len(result.bars) == 1
    assert result.bars[0].completed_end_at == T0 + timedelta(hours=19)
    assert dict(result.exclusions)["future_or_incomplete_bar"] == 1


def test_equal_clock_conflicts_are_excluded_and_identical_duplicates_collapsed() -> None:
    result = _capture([_payload(1), _payload(1), _payload(2), _payload(2, volume=9000), _payload(3)])
    assert [bar.anchor.ts for bar in result.bars] == [T0 + timedelta(hours=1), T0 + timedelta(hours=3)]
    assert dict(result.exclusions) == {"ambiguous_bar_slot": 2, "duplicate_identical_bar": 1}


def test_retention_is_chronological_and_independent_of_provider_order() -> None:
    payloads = [_payload(hour) for hour in range(12)]
    first = _capture(payloads, max_bars_per_series=8)
    second = _capture(list(reversed(payloads)), max_bars_per_series=8)
    assert first == second
    assert first.bars[0].anchor.ts == T0 + timedelta(hours=4)
    assert dict(first.exclusions) == {"history_retention_limit": 4}


def test_pinned_series_take_priority_over_new_lexically_earlier_symbols() -> None:
    pinned = _episode(19, symbol="ZZZ")
    other = _episode(19, symbol="AAA")
    bar = ObservedDynamicsBar("XTAI", "ZZZ", "1h", pinned.observation.anchor,
                              T0 + timedelta(hours=20), T0 + timedelta(hours=20))
    result = _capture([_payload(1)], episodes=(other, pinned),
                      bars_by_symbol={"AAA": [_payload(1)], "ZZZ": [_payload(1)]},
                      preferred_series=(bar.series_key,), max_series=1)
    assert {captured.symbol for captured in result.bars} == {"ZZZ"}
    assert dict(result.exclusions)["scope_limit"] == 1


def test_unproven_or_ambiguous_scope_metadata_never_bootstraps_training() -> None:
    episode = _episode(19)
    ineligible = replace(episode, training_eligible=False)
    other = replace(episode, observation=replace(
        episode.observation, anchor=replace(episode.observation.anchor, source="other_feed")))
    result = _capture([_payload(1)], episodes=(episode, other, ineligible, episode.to_dict()))
    assert not result.bars
    assert dict(result.exclusions) == {"ambiguous_scope_metadata": 2, "ineligible_scope_episode": 2}


def test_oversized_and_nonsequence_snapshots_are_explicitly_refused() -> None:
    result = _capture([_payload(1)] * (MAX_PROVIDER_ROWS_PER_SERIES + 1))
    assert not result.bars
    assert dict(result.exclusions) == {"provider_row_limit": 1}
    malformed = _capture([], bars_by_symbol={"2330.TW": _payload(1)})
    assert dict(malformed.exclusions) == {"invalid_bar_snapshot": 1}


@pytest.mark.parametrize("options", [dict(max_series=0), dict(max_series=True), dict(max_bars_per_series=1025)])
def test_capture_budget_cannot_be_unbounded(options: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        _capture([_payload(1)], **options)
