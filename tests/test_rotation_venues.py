"""Tests for per-venue rotation state helpers."""

from __future__ import annotations

import json
from copy import deepcopy

import yaml

from trader.market.rotation.venues import (
    _purge_old_radar_cache,
    compose_active_universe,
    due_venues,
    empty_venue_state,
    load_venue_state,
    run_venue_close,
    save_venue_state,
    tick,
    update_venue_ranking,
    write_universe_if_changed,
)


def _item(symbol: str, attractiveness: float) -> dict:
    return {
        "symbol": symbol,
        "attractiveness": attractiveness,
        "bias": "long",
        "directional_score": attractiveness,
    }


_SESSIONS = {
    "TW": {"open": "01:00", "close": "05:30"},
    "EU": {"open": "07:00", "close": "15:30"},
    "US": {"open": "13:30", "close": "20:00"},
}


def _write_tick_config(config_dir):
    (config_dir / "config").mkdir()
    (config_dir / "config" / "sessions.yaml").write_text(
        'TW: {open: "01:00", close: "05:30"}\n'
        'EU: {open: "07:00", close: "15:30"}\n'
        'US: {open: "13:30", close: "20:00"}\n',
        encoding="utf-8",
    )
    (config_dir / "radar.yaml").write_text(
        "cap_m: 5\n"
        "delta: 0.05\n"
        "dwell_days: 1\n"
        "emergency_score: -1\n",
        encoding="utf-8",
    )


def _tick_rank_obj():
    return {
        "ranked": [
            _item("8299.TWO", 2.0),
            _item("2330.TW", 1.9),
            _item("AAPL", 1.8),
            _item("EURUSD=X", 1.7),
        ],
        "gap_adverse": frozenset(),
        "ineligible": {},
        "components_by_symbol": {},
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


def test_run_venue_close_filters_global_rank_to_requested_venue():
    rank_obj = {
        "ranked": [
            _item("AAPL", 2.0),
            _item("8299.TWO", 1.8),
            _item("EURUSD=X", 1.7),
            _item("2330.TW", 1.6),
        ],
        "gap_adverse": frozenset(),
    }

    result = run_venue_close(
        empty_venue_state(),
        "TW",
        rank_obj,
        cap_per_venue=5,
        fx_cap=2,
        delta=0.0,
        dwell_days=1,
        emergency_floor=0.0,
        as_of="2026-06-15T05:30:00+00:00",
    )

    assert result["venues"]["TW"]["hotlist"] == ["8299.TWO", "2330.TW"]


def test_run_venue_close_injects_news_challengers_from_full_eligible_venue_ranking():
    venue_symbols = [f"{index:04d}.TW" for index in range(41)]
    rank_obj = {
        "ranked": [
            *[
                _item(symbol, 50.0 - index)
                for index, symbol in enumerate(venue_symbols)
            ],
            _item("AAPL", 0.1),
        ],
        "gap_adverse": frozenset(),
    }
    calls = []

    def news_challenger_fn(**kwargs):
        calls.append(kwargs)
        return [
            {
                "symbol": venue_symbols[40],
                "candidate_source": "fresh_news",
                "fresh_news": {"source_refs": ["u-41"]},
            }
        ]

    result = run_venue_close(
        empty_venue_state(),
        "TW",
        rank_obj,
        cap_per_venue=5,
        fx_cap=2,
        delta=0.0,
        dwell_days=1,
        emergency_floor=0.0,
        news_challenger_fn=news_challenger_fn,
        as_of="2026-06-15T05:30:00+00:00",
    )

    assert len(calls) == 1
    assert calls[0]["venue"] == "TW"
    assert {item["symbol"] for item in calls[0]["venue_ranked"]} == set(venue_symbols)
    assert calls[0]["radar_symbols"] == set(venue_symbols[:40])
    challenger = result["venues"]["TW"]["candidates"][-1]
    assert challenger["symbol"] == venue_symbols[40]
    assert challenger["attractiveness"] == rank_obj["ranked"][40]["attractiveness"]
    assert challenger["bias"] == "long"
    assert challenger["fresh_news"]["source_refs"] == ["u-41"]


def test_run_venue_close_keeps_scout_run_lineage_when_no_challenger_is_selected():
    rank_obj = {
        "ranked": [_item("2330.TW", 1.6)],
        "gap_adverse": frozenset(),
    }

    def news_challenger_fn(**_kwargs):
        return []

    news_challenger_fn.candidate_run_ids = {"TW": "candidate-run-empty-tw"}

    result = run_venue_close(
        empty_venue_state(),
        "TW",
        rank_obj,
        cap_per_venue=5,
        fx_cap=2,
        delta=0.0,
        dwell_days=1,
        emergency_floor=0.0,
        news_challenger_fn=news_challenger_fn,
        as_of="2026-06-15T05:30:00+00:00",
    )

    assert result["venues"]["TW"]["candidate_run_ids"] == [
        "candidate-run-empty-tw"
    ]


def test_run_venue_close_uses_fx_cap_for_fx_and_action_cap_otherwise():
    rank_obj = {
        "ranked": [
            _item("EURUSD=X", 2.0),
            _item("JPY=X", 1.9),
            _item("GBPUSD=X", 1.8),
            _item("AAPL", 1.7),
            _item("MSFT", 1.6),
        ],
        "gap_adverse": frozenset(),
    }

    fx_state = run_venue_close(
        empty_venue_state(),
        "FX",
        rank_obj,
        cap_per_venue=5,
        fx_cap=2,
        delta=0.0,
        dwell_days=1,
        emergency_floor=0.0,
        as_of="2026-06-15T22:00:00+00:00",
    )
    us_state = run_venue_close(
        empty_venue_state(),
        "US",
        rank_obj,
        cap_per_venue=1,
        fx_cap=5,
        delta=0.0,
        dwell_days=1,
        emergency_floor=0.0,
        as_of="2026-06-15T20:00:00+00:00",
    )

    assert fx_state["venues"]["FX"]["hotlist"] == ["EURUSD=X", "JPY=X"]
    assert us_state["venues"]["US"]["hotlist"] == ["AAPL"]


def test_run_venue_close_filters_gap_adverse_to_requested_venue():
    rank_obj = {
        "ranked": [
            _item("8299.TWO", 2.0),
            _item("2330.TW", 1.9),
            _item("AAPL", 1.8),
        ],
        "gap_adverse": frozenset({"8299.TWO", "AAPL"}),
    }

    result = run_venue_close(
        empty_venue_state(),
        "TW",
        rank_obj,
        cap_per_venue=3,
        fx_cap=2,
        delta=0.0,
        dwell_days=1,
        emergency_floor=0.0,
        as_of="2026-06-15T05:30:00+00:00",
    )

    assert result["venues"]["TW"]["hotlist"] == ["2330.TW"]
    assert result["venues"]["TW"]["scores"] == {"2330.TW": 1.9}


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


def test_write_universe_if_changed_skips_same_symbol_set(tmp_path):
    path = tmp_path / "universe.yaml"
    original = "symbols: [a, b]\n"
    path.write_text(original, encoding="utf-8")

    changed = write_universe_if_changed(path, ["b", "a"])

    assert changed is False
    assert path.read_text(encoding="utf-8") == original


def test_write_universe_if_changed_rewrites_different_symbol_set(tmp_path):
    path = tmp_path / "universe.yaml"
    path.write_text("symbols: [a, b]\n", encoding="utf-8")

    changed = write_universe_if_changed(path, ["a", "c"])

    assert changed is True
    assert yaml.safe_load(path.read_text(encoding="utf-8")) == {"symbols": ["a", "c"]}


def test_write_universe_if_changed_skips_empty_symbols(tmp_path):
    path = tmp_path / "universe.yaml"
    original = "symbols: [a, b]\n"
    path.write_text(original, encoding="utf-8")

    changed = write_universe_if_changed(path, [])

    assert changed is False
    assert path.read_text(encoding="utf-8") == original


def test_due_venues_empty_state_bootstraps_all_venues_on_weekday():
    result = due_venues(
        "2026-06-15T12:00:00+00:00",
        empty_venue_state(),
        _SESSIONS,
    )

    assert result == ["EU", "FX", "TW", "US"]


def test_due_venues_empty_state_bootstraps_actions_but_not_fx_on_saturday():
    result = due_venues(
        "2026-06-20T12:00:00+00:00",
        empty_venue_state(),
        _SESSIONS,
    )

    assert result == ["EU", "TW", "US"]


def test_due_venues_skips_venue_already_closed_today():
    state = {
        "venues": {
            "TW": {"last_close_at": "2026-06-15T05:30:00+00:00"},
            "EU": {"last_close_at": "2026-06-15T15:30:00+00:00"},
            "US": {"last_close_at": "2026-06-15T20:00:00+00:00"},
            "FX": {"last_close_at": "2026-06-15T22:00:00+00:00"},
        }
    }

    result = due_venues(
        "2026-06-15T16:00:00+00:00",
        state,
        _SESSIONS,
    )

    assert result == []


def test_due_venues_marks_eu_due_after_close_since_yesterday():
    state = {
        "venues": {
            "TW": {"last_close_at": "2026-06-15T05:30:00+00:00"},
            "EU": {"last_close_at": "2026-06-14T15:30:00+00:00"},
            "US": {"last_close_at": "2026-06-14T20:00:00+00:00"},
            "FX": {"last_close_at": "2026-06-15T22:00:00+00:00"},
        }
    }

    result = due_venues(
        "2026-06-15T16:00:00+00:00",
        state,
        _SESSIONS,
    )

    assert result == ["EU"]


def test_due_venues_marks_fx_due_after_refresh_on_weekday():
    state = {
        "venues": {
            "TW": {"last_close_at": "2026-06-15T05:30:00+00:00"},
            "EU": {"last_close_at": "2026-06-15T15:30:00+00:00"},
            "US": {"last_close_at": "2026-06-15T20:00:00+00:00"},
            "FX": {"last_close_at": "2026-06-14T22:00:00+00:00"},
        }
    }

    result = due_venues(
        "2026-06-15T22:01:00+00:00",
        state,
        _SESSIONS,
    )

    assert result == ["FX"]


def test_due_venues_does_not_mark_fx_due_on_saturday():
    state = {"venues": {"FX": {"last_close_at": "2026-06-19T22:00:00+00:00"}}}

    result = due_venues(
        "2026-06-20T22:01:00+00:00",
        state,
        {},
    )

    assert result == []


def test_tick_bootstrap_updates_due_venues_and_writes_active_universe(tmp_path):
    config_dir = tmp_path / "cfg"
    state_dir = tmp_path / "state"
    config_dir.mkdir()
    _write_tick_config(config_dir)
    calls = {"rank": 0}

    def rank_fn():
        calls["rank"] += 1
        return _tick_rank_obj()

    result = tick(
        config_dir,
        state_dir,
        "2026-06-15T03:00:00+00:00",
        rank_fn=rank_fn,
        sticky_fn=lambda: set(),
        fx_cap=3,
    )

    assert calls["rank"] == 1
    assert result["dues"] == ["EU", "FX", "TW", "US"]
    assert result["open"] == ["FX", "TW"]
    assert result["written"] is True
    assert "8299.TWO" in result["final"]
    assert "2330.TW" in result["final"]
    assert load_venue_state(state_dir)["venues"]["TW"]["hotlist"] == ["8299.TWO", "2330.TW"]
    assert yaml.safe_load((config_dir / "universe.yaml").read_text(encoding="utf-8")) == {
        "symbols": result["final"]
    }


def test_tick_keeps_due_venue_close_scope_quantitative(tmp_path):
    config_dir = tmp_path / "cfg"
    state_dir = tmp_path / "state"
    config_dir.mkdir()
    _write_tick_config(config_dir)
    symbols = [f"US{i:02d}" for i in range(41)]
    rank_obj = {
        "ranked": [_item(symbol, 50.0 - index) for index, symbol in enumerate(symbols)],
        "gap_adverse": frozenset(),
        "ineligible": {},
        "components_by_symbol": {},
    }

    def news_challenger_fn(**kwargs):
        if kwargs["venue"] != "US":
            return []
        return [
            {
                "symbol": symbols[40],
                "candidate_source": "fresh_news",
                "fresh_news": {"source_refs": ["u-us-40"]},
            }
        ]

    tick(
        config_dir,
        state_dir,
        "2026-06-15T14:00:00+00:00",
        rank_fn=lambda: rank_obj,
        sticky_fn=lambda: set(),
        news_challenger_fn=news_challenger_fn,
    )

    candidates = load_venue_state(state_dir)["venues"]["US"]["candidates"]
    assert len(candidates) == 40
    assert all(candidate["candidate_source"] == "radar" for candidate in candidates)
    assert load_venue_state(state_dir)["venues"]["US"]["scope_phase"] == "close"


def test_tick_preopen_keeps_newer_retained_challenger_when_scout_falls_back_to_older_ref(tmp_path):
    config_dir = tmp_path / "cfg"
    state_dir = tmp_path / "state"
    config_dir.mkdir()
    _write_tick_config_with_preopen(config_dir)
    symbols = [f"US{i:02d}" for i in range(41)]
    rank_obj = {
        "ranked": [_item(symbol, 50.0 - index) for index, symbol in enumerate(symbols)],
        "gap_adverse": frozenset(),
        "ineligible": {},
        "components_by_symbol": {},
    }
    parent = update_venue_ranking(
        empty_venue_state(),
        "US",
        rank_obj["ranked"],
        cap_per_venue=5,
        delta=0.0,
        dwell_days=1,
        emergency_floor=-1.0,
        as_of="2026-06-15T20:00:00+00:00",
    )
    parent_scope_id = parent["venues"]["US"]["candidate_scope_id"]
    parent["venues"].update(
        {
            "TW": {"last_close_at": "2026-06-16T05:30:00+00:00"},
            "EU": {"last_close_at": "2026-06-15T15:30:00+00:00"},
            "FX": {"last_close_at": "2026-06-15T22:00:00+00:00"},
        }
    )
    save_venue_state(state_dir, parent)

    calls = []

    def news_challenger_fn(**kwargs):
        calls.append(kwargs)
        if len(calls) > 1:
            # Le catalogue macro borné peut masquer la ref la plus récente et
            # faire réapparaître une source plus vieille du même symbole. Cela
            # ne doit pas créer un nouveau scope ni un nouveau brief macro.
            return [
                {
                    "symbol": symbols[40],
                    "candidate_source": "fresh_news",
                    "fresh_news": {
                        "source_refs": ["overnight-us-40-older"],
                        "latest_published_at": "2026-06-16T11:00:00+00:00",
                        "valid_until": "2026-06-16T15:00:00+00:00",
                    },
                }
            ]
        return [
            {
                "symbol": symbols[40],
                "candidate_source": "fresh_news",
                "fresh_news": {
                    "source_refs": ["overnight-us-40-newer"],
                    "latest_published_at": "2026-06-16T12:00:00+00:00",
                    "valid_until": "2026-06-16T15:00:00+00:00",
                },
            },
            {
                "symbol": symbols[0],
                "candidate_source": "fresh_news",
                "fresh_news": {
                    "source_refs": ["overnight-us-00"],
                    "latest_published_at": "2026-06-16T12:00:00+00:00",
                    "valid_until": "2026-06-16T15:00:00+00:00",
                },
            },
        ]

    observed = []

    def observer(record):
        observed.append(record)
        return {"candidate_scope_id": record["candidate_scope_id"]}

    first = tick(
        config_dir,
        state_dir,
        "2026-06-16T12:30:00+00:00",
        rank_fn=lambda: rank_obj,
        sticky_fn=lambda: set(),
        news_challenger_fn=news_challenger_fn,
        candidate_scope_observer=observer,
    )
    first_scope = load_venue_state(state_dir)["venues"]["US"]
    second = tick(
        config_dir,
        state_dir,
        "2026-06-16T12:45:00+00:00",
        rank_fn=lambda: rank_obj,
        sticky_fn=lambda: set(),
        news_challenger_fn=news_challenger_fn,
        candidate_scope_observer=observer,
    )
    second_scope = load_venue_state(state_dir)["venues"]["US"]

    assert first["dues"] == []
    assert second["dues"] == []
    assert len(calls) == 2
    assert len(observed) == 1
    assert first_scope["scope_phase"] == "preopen"
    assert first_scope["parent_candidate_scope_id"] == parent_scope_id
    assert first_scope["parent_close_at"] == "2026-06-15T20:00:00+00:00"
    assert len(first_scope["candidates"]) == 41
    assert first_scope["candidates"][-1]["symbol"] == symbols[40]
    assert first_scope["candidates"][-1]["fresh_news"]["source_refs"] == [
        "overnight-us-40-newer"
    ]
    assert first_scope["candidates"][0]["fresh_news"]["source_refs"] == [
        "overnight-us-00"
    ]
    assert first_scope["candidate_scope_id"] == second_scope["candidate_scope_id"]
    assert second_scope["candidates"][-1]["fresh_news"]["source_refs"] == [
        "overnight-us-40-newer"
    ]
    assert first_scope["dwell"] == second_scope["dwell"]


def test_production_scope_prepares_at_t90_but_activates_only_at_t15(tmp_path):
    config_dir = tmp_path / "cfg"
    state_dir = tmp_path / "state"
    config_dir.mkdir()
    state_dir.mkdir()
    _write_tick_config_with_override(config_dir, override_enabled=True)
    state = json.loads(_make_preopen_state())
    state["venues"]["TW"]["candidate_scope_id"] = "legacy-close-tw"
    save_venue_state(state_dir, state)
    prepared_calls = []

    def observer(record):
        return {"candidate_scope_id": record["candidate_scope_id"]}

    def prepared_universe_fn(**kwargs):
        prepared_calls.append(kwargs)
        return {
            "status": "success",
            "agent_run_id": "agent-tw-t15",
            "selected_hotlist": ["2330.TW"],
        }

    tick(
        config_dir,
        state_dir,
        "2026-06-16T00:30:00+00:00",
        rank_fn=lambda: _tick_rank_obj(),
        sticky_fn=lambda: set(),
        prepared_universe_fn=prepared_universe_fn,
        candidate_scope_observer=observer,
    )
    prepared_scope = load_venue_state(state_dir)["venues"]["TW"]
    assert prepared_scope["scope_phase"] == "preopen"
    assert prepared_calls == []

    tick(
        config_dir,
        state_dir,
        "2026-06-16T00:50:00+00:00",
        rank_fn=lambda: _tick_rank_obj(),
        sticky_fn=lambda: set(),
        prepared_universe_fn=prepared_universe_fn,
        candidate_scope_observer=observer,
    )

    assert len(prepared_calls) == 1
    assert prepared_calls[0]["candidate_scope_id"] == prepared_scope[
        "candidate_scope_id"
    ]


def test_tick_collects_sticky_before_due_rankings_and_keeps_it_outside_quota(tmp_path):
    config_dir = tmp_path / "cfg"
    state_dir = tmp_path / "state"
    config_dir.mkdir()
    _write_tick_config(config_dir)
    sticky_calls = 0

    def sticky_fn():
        nonlocal sticky_calls
        sticky_calls += 1
        return {"AAPL"}

    rank_obj = {
        "ranked": [
            _item("AAPL", 7.0),
            *[_item(f"US{i}", 6.0 - i) for i in range(6)],
        ],
        "gap_adverse": frozenset(),
        "ineligible": {},
        "components_by_symbol": {},
    }

    result = tick(
        config_dir,
        state_dir,
        "2026-06-15T14:00:00+00:00",
        rank_fn=lambda: rank_obj,
        sticky_fn=sticky_fn,
    )

    us = load_venue_state(state_dir)["venues"]["US"]
    assert sticky_calls == 1
    assert us["hotlist"] == [f"US{i}" for i in range(5)]
    assert "AAPL" in {candidate["symbol"] for candidate in us["candidates"]}
    assert result["final"][0] == "AAPL"
    assert set(us["hotlist"]) <= set(result["final"])


def test_tick_keeps_sticky_symbol_when_its_market_is_closed(tmp_path):
    config_dir = tmp_path / "cfg"
    state_dir = tmp_path / "state"
    config_dir.mkdir()
    _write_tick_config(config_dir)

    result = tick(
        config_dir,
        state_dir,
        "2026-06-15T14:00:00+00:00",
        rank_fn=_tick_rank_obj,
        sticky_fn=lambda: {"ZZZ.TW"},
        fx_cap=3,
    )

    assert result["open"] == ["EU", "FX", "US"]
    assert result["final"][0] == "ZZZ.TW"
    assert "ZZZ.TW" in result["final"]
    saved = load_venue_state(state_dir)
    assert all(
        "ZZZ.TW" not in {candidate["symbol"] for candidate in venue.get("candidates", [])}
        for venue in saved["venues"].values()
    )


def test_tick_second_call_same_now_is_idempotent(tmp_path):
    config_dir = tmp_path / "cfg"
    state_dir = tmp_path / "state"
    config_dir.mkdir()
    _write_tick_config(config_dir)

    first = tick(
        config_dir,
        state_dir,
        "2026-06-15T03:00:00+00:00",
        rank_fn=_tick_rank_obj,
        sticky_fn=lambda: set(),
        fx_cap=3,
    )
    second = tick(
        config_dir,
        state_dir,
        "2026-06-15T03:00:00+00:00",
        rank_fn=_tick_rank_obj,
        sticky_fn=lambda: set(),
        fx_cap=3,
    )

    assert first["written"] is True
    assert second["dues"] == []
    assert second["final"] == first["final"]
    assert second["written"] is False


# ---------------------------------------------------------------------------
# A3 : tick admet les venues en pré-open (hotlists persistées) dans l'univers
# ---------------------------------------------------------------------------


def _write_tick_config_with_preopen(config_dir, preopen_window_minutes=90):
    """Fixture helper identique à _write_tick_config mais avec preopen_window_minutes."""
    (config_dir / "config").mkdir(exist_ok=True)
    (config_dir / "config" / "sessions.yaml").write_text(
        'TW: {open: "01:00", close: "05:30"}\n'
        'EU: {open: "07:00", close: "15:30"}\n'
        'US: {open: "13:30", close: "20:00"}\n',
        encoding="utf-8",
    )
    # radar.yaml est à la racine de config_dir (load_radar_params cherche config_dir/radar.yaml)
    (config_dir / "radar.yaml").write_text(
        f"cap_m: 5\n"
        f"delta: 0.05\n"
        f"dwell_days: 1\n"
        f"emergency_score: -1\n"
        f"preopen_window_minutes: {preopen_window_minutes}\n",
        encoding="utf-8",
    )
    (config_dir / "universe.yaml").write_text("symbols: [SEED]\n", encoding="utf-8")


def test_tick_preopen_admet_la_hotlist_de_la_venue_fermee(tmp_path):
    """Venue TW fermée à 00:30 UTC mais en pré-open (ouvre 01:00) → ses symboles entrent."""
    import json

    config_dir = tmp_path / "cfg"
    state_dir = tmp_path / "state"
    config_dir.mkdir()
    state_dir.mkdir()
    _write_tick_config_with_preopen(config_dir, preopen_window_minutes=90)

    # venue TW déjà classée à sa dernière clôture (hotlist persistée)
    initial_state = {
        "venues": {
            "TW": {
                "hotlist": ["2330.TW", "2317.TW"],
                "scores": {"2330.TW": 1.9, "2317.TW": 1.7},
                "dwell": {"2330.TW": 1, "2317.TW": 1},
                "last_close_at": "2026-06-15T05:30:00+00:00",
                "stale": False,
            }
        }
    }
    (state_dir / "venue_state.json").write_text(
        json.dumps(initial_state), encoding="utf-8"
    )

    # 00:30 UTC mardi : TW fermée mais en pré-open (ouvre 01:00) → ses symboles entrent
    tick(
        str(config_dir),
        str(state_dir),
        "2026-06-16T00:30:00+00:00",
        rank_fn=lambda: {"ranked": [], "gap_adverse": frozenset(), "ineligible": {}, "components_by_symbol": {}},
        sticky_fn=lambda: set(),
    )

    written = yaml.safe_load((config_dir / "universe.yaml").read_text(encoding="utf-8"))["symbols"]
    assert "2330.TW" in written
    assert "2317.TW" in written


def test_tick_makes_candidate_scope_observer_failure_visible(tmp_path):
    config_dir = tmp_path / "cfg"
    state_dir = tmp_path / "state"
    config_dir.mkdir()
    state_dir.mkdir()
    _write_tick_config_with_preopen(config_dir)

    def observer(record):
        if record["venue"] == "TW":
            raise OSError("scope disk unavailable")
        return {"candidate_scope_id": record["candidate_scope_id"]}

    tick(
        config_dir,
        state_dir,
        "2026-06-15T20:00:00+00:00",
        rank_fn=lambda: _tick_rank_obj(),
        sticky_fn=lambda: set(),
        candidate_scope_observer=observer,
    )

    saved = load_venue_state(state_dir)["venues"]
    assert saved["TW"]["candidate_scope_observation_status"] == "error"
    assert saved["TW"]["candidate_scope_observation_error"] == "OSError"
    assert saved["US"]["candidate_scope_observation_status"] == "persisted"
    assert saved["US"]["candidate_scope_observation_ref"] == {
        "candidate_scope_id": saved["US"]["candidate_scope_id"]
    }


# ---------------------------------------------------------------------------
# A4 : invariant zéro-churn preopen→open
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# B3 : hook override pré-open dans tick (override_fn injectable)
# ---------------------------------------------------------------------------


def _write_tick_config_with_override(config_dir, override_enabled=True):
    """Config avec override_enabled et preopen."""
    (config_dir / "config").mkdir(exist_ok=True)
    (config_dir / "config" / "sessions.yaml").write_text(
        'TW: {open: "01:00", close: "05:30"}\n'
        'EU: {open: "07:00", close: "15:30"}\n'
        'US: {open: "13:30", close: "20:00"}\n',
        encoding="utf-8",
    )
    (config_dir / "radar.yaml").write_text(
        f"cap_m: 5\n"
        f"delta: 0.05\n"
        f"dwell_days: 1\n"
        f"emergency_score: -1\n"
        f"preopen_window_minutes: 90\n"
        f"override_enabled: {'true' if override_enabled else 'false'}\n",
        encoding="utf-8",
    )
    (config_dir / "universe.yaml").write_text("symbols: [SEED]\n", encoding="utf-8")


def _make_preopen_state(extra_candidates=None):
    """État initial avec TW ayant une hotlist et des candidats persistés.

    extra_candidates : liste de dicts {"symbol", "attractiveness"} à ajouter
    aux candidats (permet aux tests d'injecter des symboles dans la shortlist).
    """
    import json
    candidates = [{"symbol": "2330.TW", "attractiveness": 1.9}]
    if extra_candidates:
        candidates.extend(extra_candidates)
    # Schéma réel : chaque candidat porte un bias (radar.py). build_override_prompt
    # l'affiche ; le droper casse le prompt prod. On normalise pour coller au prod.
    for c in candidates:
        c.setdefault("bias", "long")
    return json.dumps({
        "venues": {
            "TW": {
                "candidates": candidates,
                "default_hotlist": ["2330.TW"],
                "hotlist": ["2330.TW"],
                "scores": {"2330.TW": 1.9},
                "dwell": {"2330.TW": 1},
                "last_close_at": "2026-06-15T05:30:00+00:00",
                "stale": False,
            }
        }
    })


def test_tick_override_preopen_appelle_override_fn_et_ajoute_symbole(tmp_path):
    """venue TW en pré-open (00:30 UTC) + override_fn injectée qui ajoute 2454.TW
    (dans les candidats) → le symbole ajouté est dans l'univers final.
    Le LLM choisit dans la shortlist candidats, pas le pool brut."""

    config_dir = tmp_path / "cfg"
    state_dir = tmp_path / "state"
    config_dir.mkdir()
    state_dir.mkdir()
    _write_tick_config_with_override(config_dir, override_enabled=True)
    # 2454.TW doit être dans les candidats pour que l'add passe la garde out_of_pool
    (state_dir / "venue_state.json").write_text(
        _make_preopen_state(extra_candidates=[{"symbol": "2454.TW", "attractiveness": 1.5}]),
        encoding="utf-8",
    )

    calls = []

    def override_fn(payload):
        calls.append(payload)
        # ranked contient les vrais scores, pas 0.0
        assert any(c["attractiveness"] > 0 for c in payload["ranked"]), "ranked doit avoir de vrais scores"
        return {"add": ["2454.TW"], "remove": []}

    res = tick(
        str(config_dir),
        str(state_dir),
        "2026-06-16T00:30:00+00:00",
        rank_fn=lambda: {"ranked": [], "gap_adverse": frozenset(), "ineligible": {}, "components_by_symbol": {}},
        sticky_fn=lambda: set(),
        override_fn=override_fn,
    )

    assert len(calls) == 1, "override_fn doit être appelée une fois pour TW en pré-open"
    assert "2330.TW" in res["final"]
    assert "2454.TW" in res["final"]


def test_tick_preopen_activates_full_prepared_selection_and_traces_agent_run(tmp_path):
    config_dir = tmp_path / "cfg"
    state_dir = tmp_path / "state"
    config_dir.mkdir()
    state_dir.mkdir()
    _write_tick_config_with_override(config_dir, override_enabled=True)
    state = json.loads(
        _make_preopen_state(extra_candidates=[{"symbol": "2454.TW", "attractiveness": 1.5}])
    )
    state["venues"]["TW"]["candidate_scope_id"] = "scope-tw-1"
    (state_dir / "venue_state.json").write_text(json.dumps(state), encoding="utf-8")
    calls = []

    def prepared_universe_fn(**kwargs):
        calls.append(kwargs)
        return {
            "status": "success",
            "agent_run_id": "agent-tw-1",
            "candidate_scope_id": "scope-tw-1",
            "brief_ref": {"venue": "TW", "brief_id": "brief-tw-1"},
            "contract_version": "selected_hotlist_v2",
            "selected_hotlist": ["2454.TW"],
        }

    result = tick(
        config_dir,
        state_dir,
        "2026-06-16T00:30:00+00:00",
        rank_fn=lambda: _tick_rank_obj(),
        sticky_fn=lambda: set(),
        prepared_universe_fn=prepared_universe_fn,
    )

    assert len(calls) == 1
    assert calls[0]["venue"] == "TW"
    assert calls[0]["candidate_scope_id"] == "scope-tw-1"
    assert "2454.TW" in result["final"]
    assert "2330.TW" not in result["final"]
    saved = load_venue_state(state_dir)["venues"]["TW"]
    assert saved["hotlist"] == ["2454.TW"]
    assert saved["last_universe_agent_run_id"] == "agent-tw-1"
    assert saved["last_universe_fallback_used"] is False
    row = json.loads((state_dir / "rotation_ledger.jsonl").read_text().splitlines()[-1])
    assert row["overrides"]["selected_hotlist"] == ["2454.TW"]
    assert row["overrides"]["fallback_used"] is False
    assert row["overrides"]["brief_ref"] == {"venue": "TW", "brief_id": "brief-tw-1"}


def test_tick_preopen_prepared_missing_keeps_baseline_and_deduplicates_ledger(tmp_path):
    config_dir = tmp_path / "cfg"
    state_dir = tmp_path / "state"
    config_dir.mkdir()
    state_dir.mkdir()
    _write_tick_config_with_override(config_dir, override_enabled=True)
    state = json.loads(_make_preopen_state())
    state["venues"]["TW"]["candidate_scope_id"] = "scope-tw-missing"
    (state_dir / "venue_state.json").write_text(json.dumps(state), encoding="utf-8")
    calls = []

    def prepared_universe_fn(**kwargs):
        calls.append(kwargs)
        return {"status": "missing", "reason": "brief_missing"}

    first = tick(
        config_dir,
        state_dir,
        "2026-06-16T00:30:00+00:00",
        rank_fn=lambda: _tick_rank_obj(),
        sticky_fn=lambda: set(),
        prepared_universe_fn=prepared_universe_fn,
    )
    second = tick(
        config_dir,
        state_dir,
        "2026-06-16T00:45:00+00:00",
        rank_fn=lambda: _tick_rank_obj(),
        sticky_fn=lambda: set(),
        prepared_universe_fn=prepared_universe_fn,
    )

    assert "2330.TW" in first["final"]
    assert "2330.TW" in second["final"]
    assert len(calls) == 2
    saved = load_venue_state(state_dir)["venues"]["TW"]
    assert saved["last_universe_fallback_used"] is True
    assert saved["last_universe_fallback_reason"] == "brief_missing"
    rows = (state_dir / "rotation_ledger.jsonl").read_text().splitlines()
    assert len(rows) == 1
    assert json.loads(rows[0])["alerts"] == [
        {"code": "universe_fallback", "reason": "brief_missing"}
    ]


def test_tick_preopen_activates_a_prepared_run_that_arrives_after_pending_fallback(tmp_path):
    config_dir = tmp_path / "cfg"
    state_dir = tmp_path / "state"
    config_dir.mkdir()
    state_dir.mkdir()
    _write_tick_config_with_override(config_dir, override_enabled=True)
    state = json.loads(
        _make_preopen_state(extra_candidates=[{"symbol": "2454.TW", "attractiveness": 1.5}])
    )
    state["venues"]["TW"]["candidate_scope_id"] = "scope-tw-late"
    (state_dir / "venue_state.json").write_text(json.dumps(state), encoding="utf-8")
    calls = 0

    def prepared_universe_fn(**_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return {"status": "pending", "reason": "prepare_pending"}
        return {
            "status": "success",
            "agent_run_id": "agent-tw-late",
            "brief_ref": {"venue": "TW", "brief_id": "brief-late"},
            "contract_version": "universe.v1",
            "selected_hotlist": ["2454.TW"],
        }

    first = tick(
        config_dir,
        state_dir,
        "2026-06-16T00:30:00+00:00",
        rank_fn=lambda: _tick_rank_obj(),
        sticky_fn=lambda: set(),
        prepared_universe_fn=prepared_universe_fn,
    )
    second = tick(
        config_dir,
        state_dir,
        "2026-06-16T00:45:00+00:00",
        rank_fn=lambda: _tick_rank_obj(),
        sticky_fn=lambda: set(),
        prepared_universe_fn=prepared_universe_fn,
    )

    assert "2330.TW" in first["final"]
    assert "2454.TW" in second["final"]
    saved = load_venue_state(state_dir)["venues"]["TW"]
    assert saved["last_universe_activation_scope_id"] == "scope-tw-late"
    assert saved["last_universe_activation_status"] == "success"
    assert saved["last_universe_fallback_used"] is False
    assert len((state_dir / "rotation_ledger.jsonl").read_text().splitlines()) == 2


def test_tick_preopen_revalidates_prepared_selection_and_keeps_sticky_outside_quota(tmp_path):
    config_dir = tmp_path / "cfg"
    state_dir = tmp_path / "state"
    config_dir.mkdir()
    state_dir.mkdir()
    _write_tick_config_with_override(config_dir, override_enabled=True)
    state = json.loads(
        _make_preopen_state(extra_candidates=[{"symbol": "2454.TW", "attractiveness": 1.5}])
    )
    state["venues"]["TW"]["candidate_scope_id"] = "scope-tw-sticky"
    (state_dir / "venue_state.json").write_text(json.dumps(state), encoding="utf-8")

    result = tick(
        config_dir,
        state_dir,
        "2026-06-16T00:30:00+00:00",
        rank_fn=lambda: _tick_rank_obj(),
        sticky_fn=lambda: {"2330.TW"},
        prepared_universe_fn=lambda **_kwargs: {
            "status": "success",
            "agent_run_id": "agent-tw-sticky",
            "selected_hotlist": ["2330.TW", "2454.TW"],
        },
    )

    assert "2330.TW" in result["final"]
    assert "2454.TW" in result["final"]
    saved = load_venue_state(state_dir)["venues"]["TW"]
    assert saved["hotlist"] == ["2454.TW"]
    row = json.loads((state_dir / "rotation_ledger.jsonl").read_text().splitlines()[-1])
    assert {item["reason"] for item in row["rejects"]["rejects"]} == {
        "sticky_outside_quota",
    }
    assert row["overrides"]["fallback_used"] is False


def test_tick_preopen_structurally_invalid_prepared_selection_falls_back_wholly(tmp_path):
    config_dir = tmp_path / "cfg"
    state_dir = tmp_path / "state"
    config_dir.mkdir()
    state_dir.mkdir()
    _write_tick_config_with_override(config_dir, override_enabled=True)
    state = json.loads(
        _make_preopen_state(extra_candidates=[{"symbol": "2454.TW", "attractiveness": 1.5}])
    )
    state["venues"]["TW"]["candidate_scope_id"] = "scope-tw-corrupt"
    (state_dir / "venue_state.json").write_text(json.dumps(state), encoding="utf-8")

    result = tick(
        config_dir,
        state_dir,
        "2026-06-16T00:30:00+00:00",
        rank_fn=lambda: _tick_rank_obj(),
        sticky_fn=lambda: set(),
        prepared_universe_fn=lambda **_kwargs: {
            "status": "success",
            "agent_run_id": "agent-tw-corrupt",
            "selected_hotlist": ["2454.TW", "OUTSIDE", "2454.TW"],
        },
    )

    assert "2330.TW" in result["final"]
    assert "2454.TW" not in result["final"]
    saved = load_venue_state(state_dir)["venues"]["TW"]
    assert saved["last_universe_fallback_used"] is True
    assert saved["last_universe_fallback_reason"] == "prepared_invalid_selection"
    assert "last_universe_activation_scope_id" not in saved
    row = json.loads((state_dir / "rotation_ledger.jsonl").read_text().splitlines()[-1])
    assert row["overrides"]["fallback_used"] is True
    assert {item["reason"] for item in row["rejects"]["rejects"]} == {
        "out_of_pool",
        "duplicate",
    }


def test_tick_override_preopen_chemin_prompt_reel(tmp_path):
    """INTÉGRATION : câble le VRAI make_llm_override_fn (donc build_override_prompt)
    contre les candidats persistés. Garde anti-régression du contrat candidates↔prompt
    (le bug 'bias' manquant levait KeyError AVANT l'appel LLM → fail-safe silencieux).
    """
    from trader.market.rotation.override import make_llm_override_fn

    config_dir = tmp_path / "cfg"
    state_dir = tmp_path / "state"
    config_dir.mkdir()
    state_dir.mkdir()
    _write_tick_config_with_override(config_dir, override_enabled=True)
    (state_dir / "venue_state.json").write_text(
        _make_preopen_state(extra_candidates=[{"symbol": "2454.TW", "attractiveness": 1.5}]),
        encoding="utf-8",
    )

    seen_prompts = []

    def fake_complete(prompt, *, timeout_s=None):
        # Si build_override_prompt avait levé (bias manquant), on n'arriverait jamais ici.
        seen_prompts.append(prompt)
        return '{"add": ["2454.TW"], "remove": []}'

    override_fn = make_llm_override_fn(fake_complete)

    res = tick(
        str(config_dir),
        str(state_dir),
        "2026-06-16T00:30:00+00:00",
        rank_fn=lambda: {"ranked": [], "gap_adverse": frozenset(), "ineligible": {}, "components_by_symbol": {}},
        sticky_fn=lambda: set(),
        override_fn=override_fn,
    )

    assert len(seen_prompts) == 1, "le vrai prompt doit avoir été construit puis envoyé"
    assert "bias=" in seen_prompts[0], "le prompt affiche le bias des candidats"
    assert "2454.TW" in res["final"], "l'override réel doit s'appliquer (add dans la shortlist)"


def test_tick_override_preopen_candidats_legacy_sans_bias(tmp_path):
    """Migration : un venue_state.json legacy avec candidats SANS bias ne casse pas le
    vrai prompt (normalisation défensive du bias dans le hook)."""
    import json

    from trader.market.rotation.override import make_llm_override_fn

    config_dir = tmp_path / "cfg"
    state_dir = tmp_path / "state"
    config_dir.mkdir()
    state_dir.mkdir()
    _write_tick_config_with_override(config_dir, override_enabled=True)
    # candidats SANS bias (état d'avant l'ajout du champ)
    (state_dir / "venue_state.json").write_text(
        json.dumps({
            "venues": {
                "TW": {
                    "candidates": [{"symbol": "2330.TW", "attractiveness": 1.9}],
                    "default_hotlist": ["2330.TW"],
                    "hotlist": ["2330.TW"],
                    "scores": {"2330.TW": 1.9},
                    "dwell": {"2330.TW": 1},
                    "last_close_at": "2026-06-15T05:30:00+00:00",
                    "stale": False,
                }
            }
        }),
        encoding="utf-8",
    )

    seen = []

    def fake_complete(prompt, *, timeout_s=None):
        seen.append(prompt)
        return '{"add": [], "remove": []}'

    res = tick(
        str(config_dir),
        str(state_dir),
        "2026-06-16T00:30:00+00:00",
        rank_fn=lambda: {"ranked": [], "gap_adverse": frozenset(), "ineligible": {}, "components_by_symbol": {}},
        sticky_fn=lambda: set(),
        override_fn=make_llm_override_fn(fake_complete),
    )

    assert len(seen) == 1, "le prompt doit se construire malgré l'absence de bias legacy"
    assert "2330.TW" in res["final"]


def test_tick_override_preopen_failsafe_sur_exception(tmp_path):
    """Si override_fn lève une exception → fail-safe : hotlist par défaut conservée."""

    config_dir = tmp_path / "cfg"
    state_dir = tmp_path / "state"
    config_dir.mkdir()
    state_dir.mkdir()
    _write_tick_config_with_override(config_dir, override_enabled=True)
    # candidates présents pour que l'override soit tenté (et lève l'exception)
    (state_dir / "venue_state.json").write_text(_make_preopen_state(), encoding="utf-8")

    def boom(payload):
        raise RuntimeError("llm down")

    res = tick(
        str(config_dir),
        str(state_dir),
        "2026-06-16T00:30:00+00:00",
        rank_fn=lambda: {"ranked": [], "gap_adverse": frozenset(), "ineligible": {}, "components_by_symbol": {}},
        sticky_fn=lambda: set(),
        override_fn=boom,
    )

    # Défaut conservé, rotation jamais bloquée
    assert "2330.TW" in res["final"]


def test_tick_override_preopen_once_per_day(tmp_path):
    """L'override ne tourne qu'une fois par jour par venue (last_override_at persisté)."""

    config_dir = tmp_path / "cfg"
    state_dir = tmp_path / "state"
    config_dir.mkdir()
    state_dir.mkdir()
    _write_tick_config_with_override(config_dir, override_enabled=True)
    # candidates présents pour que l'override soit tenté
    (state_dir / "venue_state.json").write_text(_make_preopen_state(), encoding="utf-8")

    calls = []

    def override_fn(payload):
        calls.append(payload)
        return {"add": [], "remove": []}

    # Premier tick : doit appeler override_fn
    tick(
        str(config_dir),
        str(state_dir),
        "2026-06-16T00:30:00+00:00",
        rank_fn=lambda: {"ranked": [], "gap_adverse": frozenset(), "ineligible": {}, "components_by_symbol": {}},
        sticky_fn=lambda: set(),
        override_fn=override_fn,
    )
    assert len(calls) == 1

    # Second tick même jour (00:45) : pas de nouvel appel
    tick(
        str(config_dir),
        str(state_dir),
        "2026-06-16T00:45:00+00:00",
        rank_fn=lambda: {"ranked": [], "gap_adverse": frozenset(), "ineligible": {}, "components_by_symbol": {}},
        sticky_fn=lambda: set(),
        override_fn=override_fn,
    )
    assert len(calls) == 1, "override_fn ne doit pas être rappelée le même jour"


def test_tick_override_preopen_ne_retire_pas_sticky(tmp_path):
    """apply_override protège les sticky : même si override_fn tente de retirer un sticky,
    il reste dans la hotlist finale."""

    config_dir = tmp_path / "cfg"
    state_dir = tmp_path / "state"
    config_dir.mkdir()
    state_dir.mkdir()
    _write_tick_config_with_override(config_dir, override_enabled=True)
    # candidates présents pour que l'override soit tenté
    (state_dir / "venue_state.json").write_text(_make_preopen_state(), encoding="utf-8")

    def override_fn(payload):
        return {"add": [], "remove": ["2330.TW"]}

    res = tick(
        str(config_dir),
        str(state_dir),
        "2026-06-16T00:30:00+00:00",
        rank_fn=lambda: {"ranked": [], "gap_adverse": frozenset(), "ineligible": {}, "components_by_symbol": {}},
        sticky_fn=lambda: {"2330.TW"},  # 2330.TW est sticky
        override_fn=override_fn,
    )

    # Le sticky doit rester présent malgré le remove
    assert "2330.TW" in res["final"]


# ---------------------------------------------------------------------------
# B4 : override_enabled=false → aucun appel override (rotation déterministe)
# ---------------------------------------------------------------------------


def test_override_disabled_aucun_appel_llm(tmp_path):
    """Avec override_enabled=false dans radar.yaml, tick ne doit PAS appeler override_fn."""

    config_dir = tmp_path / "cfg"
    state_dir = tmp_path / "state"
    config_dir.mkdir()
    state_dir.mkdir()
    _write_tick_config_with_override(config_dir, override_enabled=False)
    # candidates présents mais override_enabled=false → aucun appel
    (state_dir / "venue_state.json").write_text(_make_preopen_state(), encoding="utf-8")

    calls = []

    def spy(payload):
        calls.append(payload)
        return {"add": [], "remove": []}

    tick(
        str(config_dir),
        str(state_dir),
        "2026-06-16T00:30:00+00:00",
        rank_fn=lambda: {"ranked": [], "gap_adverse": frozenset(), "ineligible": {}, "components_by_symbol": {}},
        sticky_fn=lambda: set(),
        override_fn=spy,
    )

    assert calls == [], "override_fn ne doit jamais être appelée quand override_enabled=false"


def test_preopen_vers_open_sans_churn(tmp_path):
    """À 00:30 (pré-open) puis 01:30 (ouvert), les .TW restent présents : zéro churn."""
    import json

    config_dir = tmp_path / "cfg"
    state_dir = tmp_path / "state"
    config_dir.mkdir()
    state_dir.mkdir()
    _write_tick_config_with_preopen(config_dir, preopen_window_minutes=90)

    initial_state = {
        "venues": {
            "TW": {
                "hotlist": ["2330.TW", "2317.TW"],
                "scores": {"2330.TW": 1.9, "2317.TW": 1.7},
                "dwell": {"2330.TW": 1, "2317.TW": 1},
                "last_close_at": "2026-06-15T05:30:00+00:00",
                "stale": False,
            }
        }
    }

    def null_rank():
        return {"ranked": [], "gap_adverse": frozenset(), "ineligible": {}, "components_by_symbol": {}}

    # Premier tick à 00:30 (pré-open)
    (state_dir / "venue_state.json").write_text(json.dumps(initial_state), encoding="utf-8")
    res_preopen = tick(
        str(config_dir),
        str(state_dir),
        "2026-06-16T00:30:00+00:00",
        rank_fn=null_rank,
        sticky_fn=lambda: set(),
    )
    syms_preopen = set(res_preopen["final"])

    # Second tick à 01:30 (TW désormais ouverte)
    res_open = tick(
        str(config_dir),
        str(state_dir),
        "2026-06-16T01:30:00+00:00",
        rank_fn=null_rank,
        sticky_fn=lambda: set(),
    )
    syms_open = set(res_open["final"])

    assert {"2330.TW", "2317.TW"} <= syms_preopen
    assert {"2330.TW", "2317.TW"} <= syms_open  # toujours là → pas de churn reconcile


# ---------------------------------------------------------------------------
# Garde-fou #1 : sticky fraîchement recalculé à chaque tick
# → un symbole devenu sticky entre deux ticks est protégé au tick suivant
# ---------------------------------------------------------------------------


def test_garde_fou_1_sticky_recalcule_a_chaque_tick(tmp_path):
    """Un plan créé APRÈS le premier tick est vu comme sticky au tick suivant
    sans avoir à redémarrer le daemon. sticky_fn est appelée à chaque tick —
    donc la protection est immédiate, pas différée à la prochaine venue close."""
    import json

    config_dir = tmp_path / "cfg"
    state_dir = tmp_path / "state"
    config_dir.mkdir()
    state_dir.mkdir()
    _write_tick_config_with_preopen(config_dir, preopen_window_minutes=90)

    initial_state = {
        "venues": {
            "TW": {
                "hotlist": ["2330.TW", "2317.TW"],
                "scores": {"2330.TW": 1.9, "2317.TW": 1.7},
                "dwell": {"2330.TW": 1, "2317.TW": 1},
                "last_close_at": "2026-06-15T05:30:00+00:00",
                "stale": False,
            }
        }
    }

    def null_rank():
        return {"ranked": [], "gap_adverse": frozenset(), "ineligible": {}, "components_by_symbol": {}}

    # Tick 1 : pas encore de sticky pour 2330.TW
    (state_dir / "venue_state.json").write_text(json.dumps(initial_state), encoding="utf-8")
    sticky_set = set()  # mutable : on peut l'enrichir entre ticks

    res1 = tick(
        str(config_dir),
        str(state_dir),
        "2026-06-16T00:30:00+00:00",
        rank_fn=null_rank,
        sticky_fn=lambda: set(sticky_set),  # capture mutable
    )
    assert "2330.TW" in res1["final"]

    # Entre les ticks : 2330.TW crée un plan → devient sticky
    sticky_set.add("2330.TW")

    # Tick 2 (même pré-open, override tente de retirer 2330.TW)
    def aggressive_override(payload):
        # Tente de retirer le symbole sticky
        return {"add": [], "remove": ["2330.TW"]}

    res2 = tick(
        str(config_dir),
        str(state_dir),
        "2026-06-16T00:45:00+00:00",
        rank_fn=null_rank,
        sticky_fn=lambda: set(sticky_set),
        override_fn=aggressive_override,
    )
    # 2330.TW est sticky → apply_override rejette le retrait → reste dans final
    # Note : l'override ne retourne pas today car last_override_at est déjà set
    # (testé dans test_tick_override_preopen_once_per_day). Ce test vérifie
    # que sticky_fn est recalculée fraîchement, pas figée.
    assert "2330.TW" in res2["final"]


# ---------------------------------------------------------------------------
# Garde-fou #2 : symbole admis en pré-open → dû au scheduler avant l'ouverture
# → reconcile_universe + due_symbols (test via Scheduler direct)
# ---------------------------------------------------------------------------


def test_garde_fou_2_symbole_preopen_dans_univers_est_due_scheduler(tmp_path):
    """Un symbole admis dans l'univers via pré-open (tick) est vu par
    reconcile_universe + due_symbols du scheduler → le daemon l'analysera
    avant l'ouverture de sa venue."""
    import json
    from datetime import datetime, timezone

    from trader.planning.scheduler import Scheduler

    config_dir = tmp_path / "cfg"
    state_dir = tmp_path / "state"
    config_dir.mkdir()
    state_dir.mkdir()
    _write_tick_config_with_preopen(config_dir, preopen_window_minutes=90)
    (state_dir / "venue_state.json").write_text(json.dumps({
        "venues": {
            "TW": {
                "hotlist": ["2330.TW", "2317.TW"],
                "scores": {"2330.TW": 1.9, "2317.TW": 1.7},
                "dwell": {"2330.TW": 1, "2317.TW": 1},
                "last_close_at": "2026-06-15T05:30:00+00:00",
                "stale": False,
            }
        }
    }), encoding="utf-8")

    # tick à 00:30 (pré-open TW) → symboles TW entrent dans l'univers
    res = tick(
        str(config_dir),
        str(state_dir),
        "2026-06-16T00:30:00+00:00",
        rank_fn=lambda: {"ranked": [], "gap_adverse": frozenset(), "ineligible": {}, "components_by_symbol": {}},
        sticky_fn=lambda: set(),
    )
    universe = res["final"]
    assert "2330.TW" in universe

    # Simuler ce que le daemon fait après tick : reconcile_universe
    sched = Scheduler(state_dir / "scheduler.json")
    sched.reconcile_universe(universe)

    # due_symbols à 00:30 (avant l'ouverture TW à 01:00) → 2330.TW doit être dû
    now_dt = datetime(2026, 6, 16, 0, 30, tzinfo=timezone.utc)
    due = sched.due_symbols(universe, now=now_dt)
    assert "2330.TW" in due, (
        "Un symbole admis en pré-open doit être dû avant l'ouverture "
        "(sinon il est présent dans l'univers mais jamais analysé)"
    )


# ---------------------------------------------------------------------------
# D13 : candidats persistés par venue + pool réel dans le hook override
# ---------------------------------------------------------------------------


def test_update_venue_ranking_persiste_top40_plus_tous_les_challengers_sans_cap():
    state = empty_venue_state()
    venue_ranked = [_item(f"SYM{i:02d}", 60.0 - i) for i in range(60)]
    news_challengers = [
        {
            "symbol": f"SYM{i:02d}",
            "candidate_source": "fresh_news",
            "fresh_news": {"score": 90 - i, "source_refs": [f"u-{i}"]},
        }
        for i in range(40, 60)
    ]
    as_of = "2026-06-17T05:30:00+00:00"

    result = update_venue_ranking(
        state,
        "TW",
        venue_ranked,
        cap_per_venue=5,
        delta=0.0,
        dwell_days=1,
        emergency_floor=-1.0,
        news_challengers=[*news_challengers, news_challengers[0]],
        as_of=as_of,
    )

    candidates = result["venues"]["TW"]["candidates"]
    assert [candidate["symbol"] for candidate in candidates[:40]] == [
        f"SYM{i:02d}" for i in range(40)
    ]
    assert [candidate["symbol"] for candidate in candidates[40:]] == [
        f"SYM{i:02d}" for i in range(40, 60)
    ]
    assert len(candidates) == 60
    assert len({candidate["symbol"] for candidate in candidates}) == 60
    assert all(c["attractiveness"] > 0 for c in candidates), "scores doivent être réels"
    assert all("bias" in c for c in candidates), "chaque candidat doit porter son bias"
    assert all(candidate["candidate_source"] == "radar" for candidate in candidates[:40])
    assert all(candidate["candidate_source"] == "fresh_news" for candidate in candidates[40:])
    assert candidates[40]["attractiveness"] == venue_ranked[40]["attractiveness"]
    assert candidates[40]["fresh_news"]["source_refs"] == ["u-40"]
    assert "hotlist" in result["venues"]["TW"]
    assert "scores" in result["venues"]["TW"]
    assert "dwell" in result["venues"]["TW"]
    assert result["venues"]["TW"]["candidate_scope_id"].startswith("candidate_scope:v1:TW:")
    assert result["venues"]["TW"]["sticky_context_at_close"] == []
    assert result["venues"]["TW"]["last_close_at"] == as_of
    assert result["venues"]["TW"]["stale"] is False


def test_update_venue_ranking_drops_incumbent_outside_composed_candidate_pool():
    state = {
        "venues": {
            "US": {
                "default_hotlist": ["SYM45"],
                "hotlist": ["SYM45"],
                "dwell": {"SYM45": 9},
            }
        }
    }
    venue_ranked = [_item(f"SYM{i:02d}", 60.0 - i) for i in range(60)]

    result = update_venue_ranking(
        state,
        "US",
        venue_ranked,
        cap_per_venue=2,
        delta=0.0,
        dwell_days=1,
        emergency_floor=-1.0,
        as_of="2026-06-17T20:00:00+00:00",
    )

    us = result["venues"]["US"]
    assert us["hotlist"] == ["SYM00", "SYM01"]
    assert "SYM45" not in us["hotlist"]
    assert set(us["hotlist"]) <= {candidate["symbol"] for candidate in us["candidates"]}


def test_update_venue_ranking_keeps_ranked_sticky_outside_hotlist_quota():
    venue_ranked = [_item("STICKY", 3.0), _item("A", 2.0), _item("B", 1.0)]

    result = update_venue_ranking(
        empty_venue_state(),
        "US",
        venue_ranked,
        cap_per_venue=2,
        delta=0.0,
        dwell_days=1,
        emergency_floor=-1.0,
        sticky={"STICKY"},
        as_of="2026-06-17T20:00:00+00:00",
    )

    us = result["venues"]["US"]
    assert us["hotlist"] == ["A", "B"]
    assert us["sticky_context_at_close"] == ["STICKY"]
    assert "STICKY" in {candidate["symbol"] for candidate in us["candidates"]}
    assert compose_active_universe(result, ["US"], sticky={"STICKY"}) == ["STICKY", "A", "B"]


def test_update_venue_ranking_retains_unexpired_eligible_news_challenger():
    venue_ranked = [_item(f"SYM{i:02d}", 60.0 - i) for i in range(60)]
    state = {
        "venues": {
            "US": {
                "candidates": [
                    {
                        "symbol": "SYM45",
                        "attractiveness": 15.0,
                        "bias": "long",
                        "candidate_source": "fresh_news",
                        "candidate_sources": ["fresh_news"],
                        "fresh_news": {
                            "valid_until": "2026-06-18T20:00:00+00:00",
                            "source_refs": ["old-uuid"],
                        },
                    }
                ],
                "default_hotlist": [],
                "hotlist": [],
                "dwell": {},
            }
        }
    }

    result = update_venue_ranking(
        state,
        "US",
        venue_ranked,
        cap_per_venue=2,
        delta=0.0,
        dwell_days=1,
        emergency_floor=-1.0,
        as_of="2026-06-17T20:00:00+00:00",
    )

    challengers = [
        candidate
        for candidate in result["venues"]["US"]["candidates"]
        if candidate["candidate_source"] == "fresh_news"
    ]
    assert [candidate["symbol"] for candidate in challengers] == ["SYM45"]
    assert challengers[0]["fresh_news"]["source_refs"] == ["old-uuid"]
    assert challengers[0]["attractiveness"] == venue_ranked[45]["attractiveness"]


def test_update_venue_ranking_refreshes_retained_challenger_without_duplicate():
    venue_ranked = [_item(f"SYM{i:02d}", 60.0 - i) for i in range(60)]
    previous_candidate = {
        "symbol": "SYM45",
        "attractiveness": 15.0,
        "bias": "long",
        "candidate_source": "fresh_news",
        "candidate_sources": ["fresh_news"],
        "fresh_news": {
            "valid_until": "2026-06-18T20:00:00+00:00",
            "source_refs": ["old-uuid"],
        },
    }
    state = {
        "venues": {
            "US": {
                "candidates": [previous_candidate],
                "default_hotlist": [],
                "hotlist": [],
                "dwell": {},
            }
        }
    }
    refreshed = {
        "symbol": "SYM45",
        "candidate_source": "fresh_news",
        "fresh_news": {
            "valid_until": "2026-06-20T20:00:00+00:00",
            "source_refs": ["new-uuid"],
        },
    }

    result = update_venue_ranking(
        state,
        "US",
        venue_ranked,
        cap_per_venue=2,
        delta=0.0,
        dwell_days=1,
        emergency_floor=-1.0,
        news_challengers=[refreshed],
        as_of="2026-06-17T20:00:00+00:00",
    )

    matches = [
        candidate
        for candidate in result["venues"]["US"]["candidates"]
        if candidate["symbol"] == "SYM45"
    ]
    assert len(matches) == 1
    assert matches[0]["fresh_news"]["source_refs"] == ["new-uuid"]
    assert matches[0]["fresh_news"]["valid_until"] == "2026-06-20T20:00:00+00:00"


def test_update_venue_ranking_drops_expired_or_ineligible_retained_challenger():
    previous_candidate = {
        "symbol": "SYM45",
        "attractiveness": 15.0,
        "bias": "long",
        "candidate_source": "fresh_news",
        "candidate_sources": ["fresh_news"],
        "fresh_news": {
            "valid_until": "2026-06-17T20:00:00+00:00",
            "source_refs": ["old-uuid"],
        },
    }
    state = {
        "venues": {
            "US": {
                "candidates": [previous_candidate],
                "default_hotlist": [],
                "hotlist": [],
                "dwell": {},
            }
        }
    }
    full_ranking = [_item(f"SYM{i:02d}", 60.0 - i) for i in range(60)]

    expired = update_venue_ranking(
        state,
        "US",
        full_ranking,
        cap_per_venue=2,
        delta=0.0,
        dwell_days=1,
        emergency_floor=-1.0,
        as_of="2026-06-17T20:00:00+00:00",
    )
    ineligible = update_venue_ranking(
        state,
        "US",
        full_ranking[:40],
        cap_per_venue=2,
        delta=0.0,
        dwell_days=1,
        emergency_floor=-1.0,
        as_of="2026-06-16T20:00:00+00:00",
    )

    assert "SYM45" not in {
        candidate["symbol"] for candidate in expired["venues"]["US"]["candidates"]
    }
    assert "SYM45" not in {
        candidate["symbol"] for candidate in ineligible["venues"]["US"]["candidates"]
    }


def test_override_rejette_add_hors_candidats(tmp_path):
    """Un add d'un symbole absent des candidats est rejeté out_of_pool (la garde est réelle)."""

    config_dir = tmp_path / "cfg"
    state_dir = tmp_path / "state"
    config_dir.mkdir()
    state_dir.mkdir()
    _write_tick_config_with_override(config_dir, override_enabled=True)

    # Candidats : seulement 2330.TW — 9999.TW est hors pool
    (state_dir / "venue_state.json").write_text(
        _make_preopen_state(),  # candidates = [{"symbol": "2330.TW", ...}]
        encoding="utf-8",
    )

    def override_fn(payload):
        # Tente d'ajouter un symbole hors candidats
        return {"add": ["9999.TW"], "remove": []}

    res = tick(
        str(config_dir),
        str(state_dir),
        "2026-06-16T00:30:00+00:00",
        rank_fn=lambda: {"ranked": [], "gap_adverse": frozenset(), "ineligible": {}, "components_by_symbol": {}},
        sticky_fn=lambda: set(),
        override_fn=override_fn,
    )

    # 9999.TW hors pool → rejeté → absent de l'univers final
    assert "9999.TW" not in res["final"], "symbole hors candidats doit être rejeté out_of_pool"
    # 2330.TW (hotlist défaut) toujours présent
    assert "2330.TW" in res["final"]


def test_preopen_migration_drops_default_hotlist_symbol_outside_candidate_pool(tmp_path):
    import json

    config_dir = tmp_path / "cfg"
    state_dir = tmp_path / "state"
    config_dir.mkdir()
    state_dir.mkdir()
    _write_tick_config_with_override(config_dir, override_enabled=True)
    payload = json.loads(_make_preopen_state())
    payload["venues"]["TW"]["default_hotlist"] = ["2330.TW", "9999.TW"]
    payload["venues"]["TW"]["hotlist"] = ["2330.TW", "9999.TW"]
    (state_dir / "venue_state.json").write_text(json.dumps(payload), encoding="utf-8")

    tick(
        str(config_dir),
        str(state_dir),
        "2026-06-16T00:30:00+00:00",
        rank_fn=lambda: {
            "ranked": [],
            "gap_adverse": frozenset(),
            "ineligible": {},
            "components_by_symbol": {},
        },
        sticky_fn=lambda: set(),
        override_fn=lambda _payload: {"add": [], "remove": []},
    )

    saved = load_venue_state(state_dir)["venues"]["TW"]
    assert saved["hotlist"] == ["2330.TW"]
    assert set(saved["hotlist"]) <= {
        candidate["symbol"] for candidate in saved["candidates"]
    }


def test_override_accepte_add_dans_candidats_apres_remove(tmp_path):
    """Swap : remove 2330.TW (non-sticky) puis add 2454.TW (dans candidats) → accepté."""

    config_dir = tmp_path / "cfg"
    state_dir = tmp_path / "state"
    config_dir.mkdir()
    state_dir.mkdir()
    _write_tick_config_with_override(config_dir, override_enabled=True)

    (state_dir / "venue_state.json").write_text(
        _make_preopen_state(extra_candidates=[{"symbol": "2454.TW", "attractiveness": 1.5}]),
        encoding="utf-8",
    )

    def override_fn(payload):
        return {"add": ["2454.TW"], "remove": ["2330.TW"]}

    res = tick(
        str(config_dir),
        str(state_dir),
        "2026-06-16T00:30:00+00:00",
        rank_fn=lambda: {"ranked": [], "gap_adverse": frozenset(), "ineligible": {}, "components_by_symbol": {}},
        sticky_fn=lambda: set(),
        override_fn=override_fn,
    )

    # Swap accepté : 2454.TW entre, 2330.TW sort
    assert "2454.TW" in res["final"], "2454.TW dans candidats doit être accepté après remove"
    assert "2330.TW" not in res["final"], "2330.TW retiré par le swap"


def test_override_ranked_contient_vrais_scores(tmp_path):
    """Le payload ranked transmis au LLM contient les vrais scores, pas 0.0."""

    config_dir = tmp_path / "cfg"
    state_dir = tmp_path / "state"
    config_dir.mkdir()
    state_dir.mkdir()
    _write_tick_config_with_override(config_dir, override_enabled=True)
    (state_dir / "venue_state.json").write_text(
        _make_preopen_state(extra_candidates=[{"symbol": "2454.TW", "attractiveness": 1.5}]),
        encoding="utf-8",
    )

    captured = []

    def override_fn(payload):
        captured.append(payload["ranked"])
        return {"add": [], "remove": []}

    tick(
        str(config_dir),
        str(state_dir),
        "2026-06-16T00:30:00+00:00",
        rank_fn=lambda: {"ranked": [], "gap_adverse": frozenset(), "ineligible": {}, "components_by_symbol": {}},
        sticky_fn=lambda: set(),
        override_fn=override_fn,
    )

    assert len(captured) == 1, "override_fn doit être appelée"
    ranked = captured[0]
    assert all(isinstance(c["attractiveness"], (int, float)) for c in ranked)
    assert all(c["attractiveness"] != 0.0 for c in ranked), "scores 0.0 interdits (valeurs bidon)"
    sym_to_score = {c["symbol"]: c["attractiveness"] for c in ranked}
    assert sym_to_score.get("2330.TW") == 1.9
    assert sym_to_score.get("2454.TW") == 1.5


# ---------------------------------------------------------------------------
# D13 (backtestabilité) : séparation default_hotlist / hotlist
# ---------------------------------------------------------------------------


def test_update_venue_ranking_persiste_default_hotlist_egal_hotlist_a_la_cloture():
    """À la clôture, default_hotlist et hotlist doivent être identiques
    (l'override ne s'est pas encore exécuté)."""
    state = empty_venue_state()
    as_of = "2026-06-17T05:30:00+00:00"

    result = update_venue_ranking(
        state,
        "TW",
        [_item("2330.TW", 1.9), _item("2317.TW", 1.7)],
        cap_per_venue=5,
        delta=0.0,
        dwell_days=1,
        emergency_floor=-1.0,
        as_of=as_of,
    )

    tw = result["venues"]["TW"]
    assert "default_hotlist" in tw, "default_hotlist doit être persisté à la clôture"
    assert tw["default_hotlist"] == tw["hotlist"], (
        "À la clôture, default_hotlist == hotlist (avant tout override)"
    )


def test_update_venue_ranking_migration_fallback_pas_de_default_hotlist():
    """Un venue_state.json sans default_hotlist (ancien format) ne casse pas :
    l'hystérésis se base sur hotlist (fallback) et default_hotlist est créé."""
    state = {
        "venues": {
            "TW": {
                # Ancien format : pas de default_hotlist
                "hotlist": ["2330.TW"],
                "scores": {"2330.TW": 1.9},
                "dwell": {"2330.TW": 3},
                "last_close_at": "2026-06-16T05:30:00+00:00",
                "stale": False,
            }
        }
    }

    result = update_venue_ranking(
        state,
        "TW",
        [_item("2330.TW", 1.9), _item("2317.TW", 1.7)],
        cap_per_venue=5,
        delta=0.0,
        dwell_days=1,
        emergency_floor=-1.0,
        as_of="2026-06-17T05:30:00+00:00",
    )

    tw = result["venues"]["TW"]
    assert "default_hotlist" in tw, "default_hotlist doit être créé même depuis un ancien état"
    assert "2330.TW" in tw["default_hotlist"], "2330.TW doit survivre à la migration"


def test_override_necrit_pas_default_hotlist(tmp_path):
    """L'override pré-open ne touche que hotlist et last_override_at :
    default_hotlist reste le déterministe pur."""
    import json

    config_dir = tmp_path / "cfg"
    state_dir = tmp_path / "state"
    config_dir.mkdir()
    state_dir.mkdir()
    _write_tick_config_with_override(config_dir, override_enabled=True)
    (state_dir / "venue_state.json").write_text(
        _make_preopen_state(extra_candidates=[{"symbol": "2454.TW", "attractiveness": 1.5}]),
        encoding="utf-8",
    )

    def override_fn(payload):
        return {"add": ["2454.TW"], "remove": ["2330.TW"]}

    tick(
        str(config_dir),
        str(state_dir),
        "2026-06-16T00:30:00+00:00",
        rank_fn=lambda: {"ranked": [], "gap_adverse": frozenset(), "ineligible": {}, "components_by_symbol": {}},
        sticky_fn=lambda: set(),
        override_fn=override_fn,
    )

    saved = json.loads((state_dir / "venue_state.json").read_text(encoding="utf-8"))
    tw = saved["venues"]["TW"]
    assert tw["default_hotlist"] == ["2330.TW"], (
        "default_hotlist doit rester déterministe pur après override"
    )
    assert tw["hotlist"] == ["2454.TW"], "hotlist reflète l'override"


def test_purity_chain_override_nexfluence_pas_hysteresis_cloture_suivante(tmp_path):
    """Test de pureté de chaîne D13 :
    - Clôture C1 → default_hotlist = hotlist = [A, B]
    - Pré-open override → hotlist = [A, C]  (C dans candidats, B retiré)
    - Clôture C2 (mêmes venue_ranked) → l'hystérésis part de default_hotlist=[A,B],
      pas de hotlist=[A,C] → le résultat déterministe est identique à C1.
    Vérifie que la baseline ne dérive pas avec les overrides LLM.
    """
    # Clôture C1 — état vierge
    state_c1_before = empty_venue_state()
    state_c1 = update_venue_ranking(
        state_c1_before,
        "TW",
        [_item("A.TW", 2.0), _item("B.TW", 1.8), _item("C.TW", 1.5)],
        cap_per_venue=2,
        delta=0.0,
        dwell_days=1,
        emergency_floor=-1.0,
        as_of="2026-06-17T05:30:00+00:00",
    )
    tw_c1 = state_c1["venues"]["TW"]
    assert tw_c1["default_hotlist"] == ["A.TW", "B.TW"]
    assert tw_c1["hotlist"] == ["A.TW", "B.TW"]

    # Simuler l'override pré-open : hotlist → [A.TW, C.TW], default_hotlist inchangé
    from copy import deepcopy
    state_post_override = deepcopy(state_c1)
    state_post_override["venues"]["TW"]["hotlist"] = ["A.TW", "C.TW"]
    # default_hotlist doit rester ["A.TW", "B.TW"]
    assert state_post_override["venues"]["TW"]["default_hotlist"] == ["A.TW", "B.TW"]

    # Clôture C2 — mêmes venue_ranked
    state_c2 = update_venue_ranking(
        state_post_override,
        "TW",
        [_item("A.TW", 2.0), _item("B.TW", 1.8), _item("C.TW", 1.5)],
        cap_per_venue=2,
        delta=0.0,
        dwell_days=1,
        emergency_floor=-1.0,
        as_of="2026-06-18T05:30:00+00:00",
    )
    tw_c2 = state_c2["venues"]["TW"]

    # L'hystérésis repart de default_hotlist=[A.TW, B.TW] → résultat stable
    assert tw_c2["default_hotlist"] == ["A.TW", "B.TW"], (
        "La baseline déterministe ne doit pas dériver (override C1 ignoré)"
    )
    assert tw_c2["hotlist"] == ["A.TW", "B.TW"], (
        "hotlist C2 = default_hotlist C2 (avant override C2)"
    )
    # Si l'override avait contaminé l'hystérésis, C.TW serait entré en C2
    # (car il était dans hotlist post-override C1) au détriment de B.TW
    assert "C.TW" not in tw_c2["default_hotlist"], (
        "C.TW NE doit PAS être dans la baseline C2 : l'override C1 ne doit pas contaminer"
    )


def test_purge_old_radar_cache_supprime_les_anciens(tmp_path) -> None:
    """Fichier 45j → supprimé ; fichier 29j et aujourd'hui → conservés."""
    cache_dir = tmp_path / "radar_cache"
    cache_dir.mkdir()

    now_iso = "2026-07-02T10:00:00+00:00"

    # Fichier de 45 jours (2026-05-18) — doit être supprimé
    old_file = cache_dir / "2026-05-18.json"
    old_file.write_text("{}")

    # Fichier de 29 jours (2026-06-03) — doit être conservé
    recent_file = cache_dir / "2026-06-03.json"
    recent_file.write_text("{}")

    # Fichier du jour — doit être conservé
    today_file = cache_dir / "2026-07-02.json"
    today_file.write_text("{}")

    # Fichier au nom non-conforme — doit être conservé
    noise_file = cache_dir / "2026-06-15T05:30:28.json"
    noise_file.write_text("{}")

    _purge_old_radar_cache(cache_dir, now_iso)

    assert not old_file.exists(), "Le fichier 45j doit être supprimé"
    assert recent_file.exists(), "Le fichier 29j doit être conservé"
    assert today_file.exists(), "Le fichier du jour doit être conservé"
    assert noise_file.exists(), "Le fichier au nom non-conforme doit être conservé"


def test_tick_persists_score_audit_without_using_it_for_selection(tmp_path) -> None:
    config_dir = tmp_path / "config"
    state_dir = tmp_path / "state"
    config_dir.mkdir()
    state_dir.mkdir()
    _write_tick_config(config_dir)
    observed: list[dict] = []
    rank_obj = _tick_rank_obj()
    rank_obj["score_audit"] = {
        "status": "shadow_only",
        "selection_effect": "none",
        "input_signature": "score-input-1",
        "venues": {},
    }

    result = tick(
        str(config_dir),
        str(state_dir),
        "2026-06-16T01:30:00+00:00",
        rank_fn=lambda: rank_obj,
        sticky_fn=lambda: set(),
        radar_score_audit_observer=lambda payload: observed.append(payload)
        or {"input_signature": payload["input_signature"]},
    )

    assert observed == [rank_obj["score_audit"]]
    assert "2330.TW" in result["final"]
    saved = load_venue_state(state_dir)
    assert saved["radar_score_audit_observation"] == {
        "status": "persisted",
        "as_of": "2026-06-16T01:30:00+00:00",
        "ref": {"input_signature": "score-input-1"},
        "error": None,
        "selection_effect": "none",
    }


def test_tick_score_audit_failure_never_blocks_rotation(tmp_path) -> None:
    config_dir = tmp_path / "config"
    state_dir = tmp_path / "state"
    config_dir.mkdir()
    state_dir.mkdir()
    _write_tick_config(config_dir)
    rank_obj = _tick_rank_obj()
    rank_obj["score_audit"] = {"status": "shadow_only", "selection_effect": "none"}

    def broken_observer(_payload):
        raise OSError("audit disk unavailable")

    result = tick(
        str(config_dir),
        str(state_dir),
        "2026-06-16T01:30:00+00:00",
        rank_fn=lambda: rank_obj,
        sticky_fn=lambda: set(),
        radar_score_audit_observer=broken_observer,
    )

    assert "2330.TW" in result["final"]
    observation = load_venue_state(state_dir)["radar_score_audit_observation"]
    assert observation["status"] == "error"
    assert observation["error"] == "OSError"
    assert observation["selection_effect"] == "none"


def test_load_sessions_warns_once_when_sessions_yaml_absent(tmp_path, caplog):
    import logging

    import trader.market.rotation.schedule as schedule

    schedule._MISSING_SESSIONS_YAML_WARNED = False
    with caplog.at_level(logging.WARNING, logger="trader.market.rotation.schedule"):
        first = schedule.load_sessions(str(tmp_path))
        second = schedule.load_sessions(str(tmp_path))

    assert first == schedule._DEFAULT_SESSIONS
    assert second == schedule._DEFAULT_SESSIONS
    warnings = [record for record in caplog.records if "sessions.yaml" in record.getMessage()]
    assert len(warnings) == 1
    assert "hardcoded default sessions" in warnings[0].getMessage()
