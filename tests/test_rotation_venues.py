"""Tests for per-venue rotation state helpers."""

from __future__ import annotations

from copy import deepcopy

import yaml

from trader.rotation_venues import (
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
    res = tick(
        str(config_dir),
        str(state_dir),
        "2026-06-16T00:30:00+00:00",
        rank_fn=lambda: {"ranked": [], "gap_adverse": frozenset(), "ineligible": {}, "components_by_symbol": {}},
        sticky_fn=lambda: set(),
    )

    written = yaml.safe_load((config_dir / "universe.yaml").read_text(encoding="utf-8"))["symbols"]
    assert "2330.TW" in written
    assert "2317.TW" in written


# ---------------------------------------------------------------------------
# A4 : invariant zéro-churn preopen→open
# ---------------------------------------------------------------------------


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
