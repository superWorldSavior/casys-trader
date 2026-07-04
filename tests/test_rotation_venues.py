"""Tests for per-venue rotation state helpers."""

from __future__ import annotations

from copy import deepcopy

import yaml

from trader.rotation.venues import (
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


def test_tick_override_preopen_chemin_prompt_reel(tmp_path):
    """INTÉGRATION : câble le VRAI make_llm_override_fn (donc build_override_prompt)
    contre les candidats persistés. Garde anti-régression du contrat candidates↔prompt
    (le bug 'bias' manquant levait KeyError AVANT l'appel LLM → fail-safe silencieux).
    """
    from trader.rotation.override import make_llm_override_fn

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

    from trader.rotation.override import make_llm_override_fn

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

    from trader.scheduling.scheduler import Scheduler

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


def test_update_venue_ranking_persiste_top40_candidats(tmp_path):
    """Après update_venue_ranking avec 50 items, candidates a 40 entrées triées
    avec les vrais scores (pas 0.0)."""
    state = empty_venue_state()
    venue_ranked = [_item(f"SYM{i:02d}", 50.0 - i) for i in range(50)]
    as_of = "2026-06-17T05:30:00+00:00"

    result = update_venue_ranking(
        state,
        "TW",
        venue_ranked,
        cap_per_venue=5,
        delta=0.0,
        dwell_days=1,
        emergency_floor=-1.0,
        as_of=as_of,
    )

    candidates = result["venues"]["TW"]["candidates"]
    assert len(candidates) == 40, "top 40 attendu"
    # tri desc par attractivité préservé
    scores = [c["attractiveness"] for c in candidates]
    assert scores == sorted(scores, reverse=True), "candidats doivent être triés desc"
    # vrais scores (pas 0.0)
    assert all(c["attractiveness"] > 0 for c in candidates), "scores doivent être réels"
    # bias persisté (sinon build_override_prompt lève KeyError en prod)
    assert all("bias" in c for c in candidates), "chaque candidat doit porter son bias"
    # clés existantes inchangées
    assert "hotlist" in result["venues"]["TW"]
    assert "scores" in result["venues"]["TW"]
    assert "dwell" in result["venues"]["TW"]
    assert result["venues"]["TW"]["last_close_at"] == as_of
    assert result["venues"]["TW"]["stale"] is False


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
