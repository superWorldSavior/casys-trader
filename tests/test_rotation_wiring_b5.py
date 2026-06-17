"""Tests B5 : câblage prod override_fn + market_context depuis family_regime."""
from __future__ import annotations


# ---------------------------------------------------------------------------
# B5 : build_market_context_from_regime — helper assemblant market_context
# ---------------------------------------------------------------------------


class TestBuildMarketContextFromRegime:
    def test_retourne_dict_avec_regime_families(self):
        from trader.rotation_wiring import build_market_context_from_regime

        family_bias = {
            "defense": {"dir": "up", "frac": 0.8, "up": 4, "down": 1, "n": 5},
            "semis": {"dir": "down", "frac": 0.6, "up": 2, "down": 3, "n": 5},
        }

        ctx = build_market_context_from_regime(family_bias)
        assert "regime_families" in ctx
        assert ctx["regime_families"]["defense"]["dir"] == "up"
        assert ctx["regime_families"]["semis"]["dir"] == "down"

    def test_retourne_none_si_dict_vide(self):
        """Contrat narrow : None si regime vide (pas d'info à transmettre au LLM)."""
        from trader.rotation_wiring import build_market_context_from_regime

        ctx = build_market_context_from_regime({})
        assert ctx is None

    def test_retourne_none_si_aucun_bias(self):
        """Contrat narrow : None si regime vide (pas d'info à transmettre)."""
        from trader.rotation_wiring import build_market_context_from_regime

        ctx = build_market_context_from_regime(None)
        assert ctx is None


# ---------------------------------------------------------------------------
# B5 : tick (daemon wiring) passe override_fn réel quand override_enabled
# ---------------------------------------------------------------------------


def test_tick_daemon_passe_override_fn_quand_enabled(tmp_path):
    """Smoke test : tick avec override_fn assemblée via build_llm_override_fn mock
    + market_context peuplé → override appelé en pré-open."""
    import json
    import yaml
    from trader.rotation_venues import tick

    config_dir = tmp_path / "cfg"
    state_dir = tmp_path / "state"
    config_dir.mkdir()
    state_dir.mkdir()

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
        "emergency_score: -1\n"
        "preopen_window_minutes: 90\n"
        "override_enabled: true\n",
        encoding="utf-8",
    )
    (config_dir / "universe.yaml").write_text("symbols: [SEED]\n", encoding="utf-8")

    # candidates doit être présent pour que le hook override soit tenté
    (state_dir / "venue_state.json").write_text(json.dumps({
        "venues": {
            "TW": {
                "candidates": [{"symbol": "2330.TW", "attractiveness": 1.9}],
                "hotlist": ["2330.TW"],
                "scores": {"2330.TW": 1.9},
                "dwell": {"2330.TW": 1},
                "last_close_at": "2026-06-15T05:30:00+00:00",
                "stale": False,
            }
        }
    }), encoding="utf-8")

    # Simuler override_fn prod (câblée via make_llm_override_fn)
    calls = []

    def prod_like_override_fn(payload):
        calls.append(payload)
        return {"add": [], "remove": []}

    # Simuler market_context peuplé depuis family_regime
    market_ctx = {"regime_families": {"defense": {"dir": "up", "frac": 0.8}}}

    res = tick(
        str(config_dir),
        str(state_dir),
        "2026-06-16T00:30:00+00:00",
        rank_fn=lambda: {"ranked": [], "gap_adverse": frozenset(), "ineligible": {}, "components_by_symbol": {}},
        sticky_fn=lambda: set(),
        override_fn=prod_like_override_fn,
        market_context=market_ctx,
    )

    assert len(calls) == 1, "override_fn doit être appelée pour TW en pré-open"
    payload = calls[0]
    assert payload.get("market_context") == market_ctx
    assert "2330.TW" in res["final"]
