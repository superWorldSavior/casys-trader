"""Tests for per-venue rotation state helpers."""

from __future__ import annotations

from copy import deepcopy

from trader.rotation_venues import (
    compose_active_universe,
    empty_venue_state,
    load_venue_state,
    save_venue_state,
    update_venue_ranking,
)


def _item(symbol: str, attractiveness: float) -> dict:
    return {
        "symbol": symbol,
        "attractiveness": attractiveness,
        "bias": "long",
        "directional_score": attractiveness,
    }


def test_empty_venue_state_returns_empty_venues():
    assert empty_venue_state() == {"venues": {}}


def test_load_venue_state_missing_file_returns_empty(tmp_path):
    assert load_venue_state(tmp_path) == {"venues": {}}


def test_load_venue_state_ill_readable_file_returns_empty(tmp_path):
    (tmp_path / "venue_state.json").write_text("{ not json", encoding="utf-8")

    assert load_venue_state(tmp_path) == {"venues": {}}


def test_save_venue_state_round_trips_and_creates_directory(tmp_path):
    state_dir = tmp_path / "state"
    state = {
        "venues": {
            "US": {
                "hotlist": ["AAPL"],
                "scores": {"AAPL": 1.2},
                "dwell": {"AAPL": 3},
                "last_close_at": "2026-06-15T20:00:00+00:00",
                "stale": False,
            }
        }
    }

    save_venue_state(state_dir, state)

    assert load_venue_state(state_dir) == state


def test_update_venue_ranking_creates_venue_with_top_hotlist_and_scores():
    state = empty_venue_state()
    as_of = "2026-06-15T20:00:00+00:00"

    result = update_venue_ranking(
        state,
        "US",
        [_item("AAPL", 1.3), _item("MSFT", 1.1), _item("NVDA", 0.9)],
        cap_per_venue=2,
        delta=0.1,
        dwell_days=2,
        emergency_floor=0.0,
        as_of=as_of,
    )

    assert result["venues"]["US"]["hotlist"] == ["AAPL", "MSFT"]
    assert result["venues"]["US"]["scores"] == {"AAPL": 1.3, "MSFT": 1.1}
    assert result["venues"]["US"]["last_close_at"] == as_of
    assert result["venues"]["US"]["stale"] is False


def test_update_venue_ranking_keeps_incumbent_when_dwell_insufficient():
    state = {
        "venues": {
            "US": {
                "hotlist": ["AAPL"],
                "scores": {"AAPL": 1.0},
                "dwell": {"AAPL": 1},
                "last_close_at": "2026-06-14T20:00:00+00:00",
                "stale": False,
            }
        }
    }

    result = update_venue_ranking(
        state,
        "US",
        [_item("MSFT", 2.0), _item("AAPL", 1.0)],
        cap_per_venue=1,
        delta=0.0,
        dwell_days=2,
        emergency_floor=0.0,
        as_of="2026-06-15T20:00:00+00:00",
    )

    assert result["venues"]["US"]["hotlist"] == ["AAPL"]


def test_update_venue_ranking_evicts_gap_adverse_symbol():
    state = empty_venue_state()

    result = update_venue_ranking(
        state,
        "US",
        [_item("X", 2.0), _item("AAPL", 1.0)],
        cap_per_venue=2,
        delta=0.0,
        dwell_days=1,
        emergency_floor=0.0,
        gap_adverse=frozenset({"X"}),
        as_of="2026-06-15T20:00:00+00:00",
    )

    assert result["venues"]["US"]["hotlist"] == ["AAPL"]
    assert result["venues"]["US"]["scores"] == {"AAPL": 1.0}
    assert result["venues"]["US"]["dwell"] == {"AAPL": 1}


def test_update_venue_ranking_keeps_other_venues_unchanged():
    tw_state = {
        "hotlist": ["2330.TW"],
        "scores": {"2330.TW": 1.4},
        "dwell": {"2330.TW": 7},
        "last_close_at": "2026-06-15T05:30:00+00:00",
        "stale": False,
    }
    state = {"venues": {"TW": deepcopy(tw_state)}}

    result = update_venue_ranking(
        state,
        "US",
        [_item("AAPL", 1.0)],
        cap_per_venue=1,
        delta=0.0,
        dwell_days=1,
        emergency_floor=0.0,
        as_of="2026-06-15T20:00:00+00:00",
    )

    assert result["venues"]["TW"] == tw_state
    assert result["venues"]["US"]["hotlist"] == ["AAPL"]


def test_update_venue_ranking_increments_stayers_and_sets_entrant_dwell_to_one():
    state = {
        "venues": {
            "US": {
                "hotlist": ["AAPL"],
                "scores": {"AAPL": 1.0},
                "dwell": {"AAPL": 4},
                "last_close_at": "2026-06-14T20:00:00+00:00",
                "stale": False,
            }
        }
    }

    result = update_venue_ranking(
        state,
        "US",
        [_item("AAPL", 1.2), _item("MSFT", 1.0)],
        cap_per_venue=2,
        delta=0.0,
        dwell_days=1,
        emergency_floor=0.0,
        as_of="2026-06-15T20:00:00+00:00",
    )

    assert result["venues"]["US"]["hotlist"] == ["AAPL", "MSFT"]
    assert result["venues"]["US"]["dwell"] == {"AAPL": 5, "MSFT": 1}


def test_compose_active_universe_adds_open_action_hotlist_and_fx_cap():
    state = {
        "venues": {
            "TW": {"hotlist": ["a", "b"]},
            "FX": {"hotlist": ["f1", "f2", "f3", "f4"]},
        }
    }

    result = compose_active_universe(state, ["FX", "TW"], sticky=set(), fx_cap=3)

    assert result == ["a", "b", "f1", "f2", "f3"]


def test_compose_active_universe_keeps_sticky_from_closed_market_at_front():
    state = {"venues": {"TW": {"hotlist": ["a"]}}}

    result = compose_active_universe(state, [], sticky={"z"})

    assert result == ["z"]


def test_compose_active_universe_interleaves_open_action_venues():
    state = {
        "venues": {
            "EU": {"hotlist": ["e1", "e2"]},
            "US": {"hotlist": ["u1", "u2"]},
        }
    }

    result = compose_active_universe(state, ["EU", "US"], sticky=set())

    assert result == ["e1", "u1", "e2", "u2"]


def test_compose_active_universe_empty_without_open_venues_or_sticky():
    assert compose_active_universe({"venues": {}}, [], sticky=set()) == []


def test_compose_active_universe_returns_sticky_when_no_venues_are_open():
    assert compose_active_universe({"venues": {}}, [], sticky={"z"}) == ["z"]
