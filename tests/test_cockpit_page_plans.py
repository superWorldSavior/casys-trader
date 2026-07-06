"""Tests de la page Plans (pages/plans.py).

Couvre :
- Builders purs avec states synthétiques (déterministes, now fixe)
- Cas limites : état vide, valeurs None, listes vides
- Détection amend_exit rejeté par symbole
- Montage Textual (touche 4 → PlansPage visible + panneaux présents)
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import trader.interfaces.cockpit.app as cockpit_module
from trader.interfaces.cockpit.app import CockpitApp
from trader.interfaces.cockpit.pages.plans import (
    PlansPage,
    _amend_rejected_symbols,
    _price_fmt,
    _stop_pct,
    _tp_label,
    build_armed,
    build_exit_plans,
    build_exit_watches,
    build_next_to_fire_plans,
    build_watches,
)

UTC = timezone.utc
NOW = datetime(2026, 7, 6, 2, 0, 0, tzinfo=UTC)
FUTURE = "2026-07-06T08:00:00+00:00"  # dans le futur par rapport à NOW


# ---------------------------------------------------------------------------
# Helpers de rendu
# ---------------------------------------------------------------------------


def _render(renderable, width: int = 160) -> str:
    from rich.console import Console

    console = Console(width=width, legacy_windows=False)
    with console.capture() as capture:
        console.print(renderable)
    return capture.get()


def _patch_paths(monkeypatch, tmp_path):
    monkeypatch.setattr(cockpit_module, "_STATE_DIR", tmp_path)
    monkeypatch.setattr(cockpit_module, "_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(cockpit_module, "_EVENTS_FILE", tmp_path / "events.jsonl")
    monkeypatch.setattr(cockpit_module, "_AGENT_TRACE_FILE", tmp_path / "agent_trace.log")
    monkeypatch.setattr(cockpit_module, "_KILL_FILE", tmp_path / "KILL")


def _make_minimal_state(tmp_path) -> None:
    (tmp_path / "current_report.json").write_text(
        json.dumps({
            "ts": "2026-07-06T02:00:00+00:00",
            "dry_run": True,
            "portfolio": {"cash": 80000.0, "equity": 100000.0, "holdings": []},
            "decisions": [],
        }),
        encoding="utf-8",
    )
    (tmp_path / "daemon_status.json").write_text(
        json.dumps({
            "phase": "idle",
            "decisions_done": 0,
            "symbols_total": 0,
            "model_calls_used": 0,
            "max_model_calls_per_cycle": 25,
        }),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Helpers purs locaux
# ---------------------------------------------------------------------------


def test_price_fmt_round_number():
    assert _price_fmt(4700.0) == "4,700"
    assert _price_fmt(100.0) == "100"


def test_price_fmt_decimal():
    assert _price_fmt(256.15) == "256.15"
    assert _price_fmt(45.75) == "45.75"


def test_price_fmt_none():
    assert _price_fmt(None) == "—"


def test_stop_pct_long():
    plan = {"hard_stop_price": 4700.0, "side": "LONG"}
    pct = _stop_pct(plan, 4825.0)
    assert pct is not None
    assert abs(pct - (-2.59)) < 0.1  # ~-2.6% per spec (3443.TW)


def test_stop_pct_short():
    plan = {"hard_stop_price": 46.0, "side": "SHORT"}
    pct = _stop_pct(plan, 45.13)
    assert pct is not None
    assert abs(pct - 1.93) < 0.1  # ~+1.9% per spec (SLB)


def test_stop_pct_no_ref():
    plan = {"hard_stop_price": 100.0}
    assert _stop_pct(plan, None) is None


def test_stop_pct_no_stop():
    plan = {"side": "LONG"}
    assert _stop_pct(plan, 100.0) is None


def test_tp_label_none():
    assert _tp_label({}) == "—"


def test_tp_label_single():
    plan = {"take_profits": [{"price": 268.90}]}
    label = _tp_label(plan)
    assert "268.90" in label


def test_tp_label_two():
    plan = {"take_profits": [{"price": 45.75}, {"price": 47.09}]}
    label = _tp_label(plan)
    assert "→" in label
    assert "45.75" in label
    assert "47.09" in label


def test_tp_label_round_prices():
    plan = {"take_profits": [{"price": 1286.0}, {"price": 1342.0}]}
    label = _tp_label(plan)
    assert "1,286" in label
    assert "→" in label


# ---------------------------------------------------------------------------
# amend rejected detection
# ---------------------------------------------------------------------------


def test_amend_rejected_symbols_empty():
    assert _amend_rejected_symbols({}) == set()


def test_amend_rejected_detects_rejected():
    state = {
        "recent_decisions": [
            {
                "symbol": "3443.TW",
                "cycle_ts": "2026-07-06T02:01:00+00:00",
                "runtime": {
                    "tool_calls": [
                        {"tool": "amend_exit", "outcome": "rejected"},
                    ]
                },
            }
        ]
    }
    result = _amend_rejected_symbols(state)
    assert "3443.TW" in result


def test_amend_rejected_ignores_applied():
    state = {
        "recent_decisions": [
            {
                "symbol": "ABBV",
                "cycle_ts": "2026-07-06T02:00:00+00:00",
                "runtime": {
                    "tool_calls": [
                        {"tool": "amend_exit", "outcome": "applied"},
                    ]
                },
            }
        ]
    }
    result = _amend_rejected_symbols(state)
    assert "ABBV" not in result


def test_amend_rejected_last_wins():
    """La plus récente décision par symbole détermine l'état."""
    state = {
        "recent_decisions": [
            {
                "symbol": "SYM",
                "cycle_ts": "2026-07-06T01:00:00+00:00",
                "runtime": {"tool_calls": [{"tool": "amend_exit", "outcome": "rejected"}]},
            },
            {
                "symbol": "SYM",
                "cycle_ts": "2026-07-06T02:00:00+00:00",
                "runtime": {"tool_calls": [{"tool": "amend_exit", "outcome": "applied"}]},
            },
        ]
    }
    result = _amend_rejected_symbols(state)
    assert "SYM" not in result  # applied est plus récent → pas rejected


def test_amend_rejected_ignores_other_tools():
    state = {
        "recent_decisions": [
            {
                "symbol": "XYZ",
                "cycle_ts": "2026-07-06T01:00:00+00:00",
                "runtime": {
                    "tool_calls": [
                        {"tool": "propose_order", "outcome": "rejected"},
                        {"tool": "set_next_wake", "outcome": "applied"},
                    ]
                },
            }
        ]
    }
    result = _amend_rejected_symbols(state)
    assert "XYZ" not in result


# ---------------------------------------------------------------------------
# build_armed — état vide
# ---------------------------------------------------------------------------


def test_build_armed_empty_state():
    rendered = _render(build_armed({}, now=NOW))
    assert "nothing armed" in rendered
    assert "risk gate still applies" in rendered


def test_build_armed_one_watch():
    state = {
        "indicator_watches": [
            {
                "symbol": "2383.TW",
                "on_trigger": "EXECUTE_ORDER",
                "logic": "and",
                "conditions": [
                    {"indicator": "breakout", "op": ">=", "value": 1, "timeframe": "1h"},
                    {"indicator": "z", "op": "<=", "value": 1.2, "timeframe": "1h"},
                ],
                "expires_at": FUTURE,
                "order": {
                    "action": "BUY",
                    "qty": 2,
                    "exit_plan": {"hard_stop": {"price": 5390}},
                },
            }
        ],
        "prices": {"2383.TW": 5610.0},
    }
    rendered = _render(build_armed(state, now=NOW))
    assert "2383.TW" in rendered
    assert "BUY" in rendered
    assert "breakout" in rendered
    assert "risk gate still applies" in rendered


def test_build_armed_no_conditions():
    """Watch sans conditions : ne plante pas."""
    state = {
        "indicator_watches": [
            {
                "symbol": "TEST",
                "on_trigger": "EXECUTE_ORDER",
                "expires_at": FUTURE,
                "order": {"action": "SELL", "qty": 10},
            }
        ]
    }
    rendered = _render(build_armed(state, now=NOW))
    assert "TEST" in rendered
    assert "SELL" in rendered


# ---------------------------------------------------------------------------
# build_exit_plans — état vide + plans avec données
# ---------------------------------------------------------------------------


def test_build_exit_plans_empty():
    rendered = _render(build_exit_plans({}, now=NOW))
    assert "no exit plans" in rendered


def test_build_exit_plans_with_plan():
    state = {
        "trade_plans": [
            {
                "symbol": "ABBV",
                "side": "LONG",
                "quantity": 12,
                "remaining_quantity": 12,
                "entry_price": 256.15,
                "hard_stop_price": 253.70,
                "take_profits": [{"price": 268.90}],
                "trailing_stop": None,
                "profit_protection": None,
                "last_llm_review": {"ts": "2026-07-05T20:16:00+00:00"},
            }
        ],
        "prices": {"ABBV": 261.05},
    }
    rendered = _render(build_exit_plans(state, now=NOW))
    assert "ABBV" in rendered
    assert "256.15" in rendered
    assert "253.70" in rendered
    assert "268.90" in rendered
    assert "✓ 20:16" in rendered
    assert "every open position" in rendered


def test_build_exit_plans_amend_rejected():
    state = {
        "trade_plans": [
            {
                "symbol": "3443.TW",
                "side": "LONG",
                "quantity": 10,
                "remaining_quantity": 10,
                "entry_price": 5140.0,
                "hard_stop_price": 4700.0,
            }
        ],
        "prices": {"3443.TW": 4825.0},
        "recent_decisions": [
            {
                "symbol": "3443.TW",
                "cycle_ts": "2026-07-06T02:01:00+00:00",
                "runtime": {"tool_calls": [{"tool": "amend_exit", "outcome": "rejected"}]},
            }
        ],
    }
    rendered = _render(build_exit_plans(state, now=NOW))
    assert "3443.TW" in rendered
    assert "amend rejected" in rendered


def test_build_exit_plans_short():
    state = {
        "trade_plans": [
            {
                "symbol": "SLB",
                "side": "SHORT",
                "quantity": 140,
                "remaining_quantity": 140,
                "entry_price": 44.79,
                "hard_stop_price": 46.00,
                "take_profits": [{"price": 42.10}],
            }
        ],
        "prices": {"SLB": 45.13},
    }
    rendered = _render(build_exit_plans(state, now=NOW))
    assert "SLB" in rendered
    assert "S" in rendered  # side Short


def test_build_exit_plans_labels_stop_left_and_entry_risk():
    state = {
        "trade_plans": [
            {
                "symbol": "1326.TW",
                "side": "LONG",
                "quantity": 3000,
                "remaining_quantity": 3000,
                "entry_price": 69.0,
                "hard_stop_price": 66.9,
                "take_profits": [{"price": 73.0}],
            }
        ],
        "prices": {"1326.TW": 67.4},
    }

    rendered = _render(build_exit_plans(state, now=NOW), width=180)

    assert "left 0.7%" in rendered
    assert "entry 3.0%" in rendered


def test_build_exit_plans_sorted_by_distance():
    """Plans triés : stop le plus proche (distance abs min) d'abord."""
    state = {
        "trade_plans": [
            {
                "symbol": "FAR",
                "side": "LONG",
                "remaining_quantity": 10,
                "entry_price": 100.0,
                "hard_stop_price": 90.0,  # -10% distance
            },
            {
                "symbol": "CLOSE",
                "side": "LONG",
                "remaining_quantity": 10,
                "entry_price": 100.0,
                "hard_stop_price": 97.5,  # -2.5% distance
            },
        ],
        "prices": {"FAR": 100.0, "CLOSE": 100.0},
    }
    rendered = _render(build_exit_plans(state, now=NOW))
    # CLOSE doit apparaître avant FAR
    assert rendered.index("CLOSE") < rendered.index("FAR")


def test_build_exit_plans_two_take_profits():
    state = {
        "trade_plans": [
            {
                "symbol": "AMCR",
                "side": "LONG",
                "remaining_quantity": 70,
                "entry_price": 43.89,
                "hard_stop_price": 42.56,
                "take_profits": [{"price": 45.75}, {"price": 47.09}],
            }
        ]
    }
    rendered = _render(build_exit_plans(state, now=NOW))
    assert "→" in rendered
    assert "45.75" in rendered


def test_build_exit_plans_with_protect_trail():
    state = {
        "trade_plans": [
            {
                "symbol": "DASH",
                "side": "LONG",
                "remaining_quantity": 20,
                "entry_price": 192.76,
                "hard_stop_price": 186.12,
                "trailing_stop": {"trail_pct": 3.5},
            }
        ]
    }
    rendered = _render(build_exit_plans(state, now=NOW))
    assert "trail" in rendered


def test_build_exit_plans_no_last_price_falls_back_to_entry():
    """Sans prix courant, utilise entry_price comme référence."""
    state = {
        "trade_plans": [
            {
                "symbol": "NOPR",
                "side": "LONG",
                "remaining_quantity": 5,
                "entry_price": 100.0,
                "hard_stop_price": 95.0,
            }
        ]
        # Pas de prices
    }
    rendered = _render(build_exit_plans(state, now=NOW))
    assert "NOPR" in rendered
    assert "95" in rendered  # stop price visible


# ---------------------------------------------------------------------------
# build_watches
# ---------------------------------------------------------------------------


def test_build_watches_empty():
    rendered = _render(build_watches({}, now=NOW))
    assert "no active watches" in rendered
    assert "bar = time left" in rendered


def test_build_watches_with_active():
    state = {
        "indicator_watches": [
            {
                "symbol": "2633.TW",
                "on_trigger": "WAKE",
                "logic": "all",
                "conditions": [
                    {"indicator": "breakout", "op": ">=", "value": 1, "timeframe": "1h"}
                ],
                "created_at": "2026-07-06T00:00:00+00:00",
                "expires_at": FUTURE,  # dans 6h
            }
        ]
    }
    rendered = _render(build_watches(state, now=NOW))
    assert "2633.TW" in rendered
    assert "breakout" in rendered
    assert "bar = time left" in rendered


def test_build_watches_expires_at_none_skipped():
    """Watch sans expires_at ignorée (pas de TTL)."""
    state = {
        "indicator_watches": [
            {
                "symbol": "NOEXP",
                "on_trigger": "WAKE",
                "conditions": [],
                # pas de expires_at
            }
        ]
    }
    rendered = _render(build_watches(state, now=NOW))
    assert "NOEXP" not in rendered
    assert "no active watches" in rendered


def test_build_watches_expired_skipped():
    """Watch déjà expirée ignorée."""
    state = {
        "indicator_watches": [
            {
                "symbol": "OLD",
                "on_trigger": "WAKE",
                "conditions": [],
                "expires_at": "2026-07-05T00:00:00+00:00",  # passé
            }
        ]
    }
    rendered = _render(build_watches(state, now=NOW))
    assert "OLD" not in rendered


def test_build_watches_armed_not_shown():
    """Plans armés (EXECUTE_ORDER) n'apparaissent pas dans WATCHES."""
    state = {
        "indicator_watches": [
            {
                "symbol": "ARMED_SYM",
                "on_trigger": "EXECUTE_ORDER",
                "expires_at": FUTURE,
                "order": {"action": "BUY", "qty": 5},
                "conditions": [],
            }
        ]
    }
    rendered = _render(build_watches(state, now=NOW))
    assert "ARMED_SYM" not in rendered


# ---------------------------------------------------------------------------
# build_exit_watches
# ---------------------------------------------------------------------------


def test_build_exit_watches_empty():
    rendered = _render(build_exit_watches({}, now=NOW))
    assert "no exit watches" in rendered


def test_build_exit_watches_no_exit_watch_key():
    state = {
        "trade_plans": [
            {"symbol": "NOEW", "side": "LONG", "entry_price": 100.0}
            # exit_watch absent
        ]
    }
    rendered = _render(build_exit_watches(state, now=NOW))
    assert "no exit watches" in rendered


def test_build_exit_watches_with_watch():
    state = {
        "trade_plans": [
            {
                "symbol": "DASH",
                "side": "LONG",
                "entry_price": 192.76,
                "exit_watch": {
                    "logic": "or",
                    "conditions": [
                        {"indicator": "breakout", "op": "<=", "value": -1, "timeframe": "1h"},
                        {"indicator": "RS", "op": "<", "value": 0, "timeframe": "1h"},
                    ],
                    "expires_at": FUTURE,
                },
            }
        ]
    }
    rendered = _render(build_exit_watches(state, now=NOW))
    assert "DASH" in rendered
    assert "breakout" in rendered


# ---------------------------------------------------------------------------
# build_next_to_fire_plans
# ---------------------------------------------------------------------------


def test_build_next_to_fire_empty():
    rendered = _render(build_next_to_fire_plans({}, now=NOW))
    assert "nothing armed" in rendered


def test_build_next_to_fire_with_watches():
    state = {
        "indicator_watches": [
            {
                "symbol": "2633.TW",
                "on_trigger": "WAKE",
                "logic": "all",
                "conditions": [
                    {"indicator": "breakout", "op": ">=", "value": 1, "timeframe": "1h"}
                ],
                "expires_at": FUTURE,
                "created_at": "2026-07-06T00:00:00+00:00",
            }
        ]
    }
    rendered = _render(build_next_to_fire_plans(state, now=NOW))
    assert "2633.TW" in rendered


# ---------------------------------------------------------------------------
# Adaptive limits — build_armed / build_watches / build_exit_watches / fire
# ---------------------------------------------------------------------------


def test_build_armed_limit_truncates():
    """limit=1 avec 3 watches armées → 1 affichée + '+ 2 more'."""
    state = {
        "indicator_watches": [
            {
                "symbol": f"ARM{i}",
                "on_trigger": "EXECUTE_ORDER",
                "expires_at": FUTURE,
                "logic": "and",
                "conditions": [
                    {"indicator": "breakout", "op": ">=", "value": 1, "timeframe": "1h"}
                ],
                "order": {"action": "BUY", "qty": 1},
            }
            for i in range(3)
        ]
    }
    rendered = _render(build_armed(state, now=NOW, limit=1))
    assert "ARM0" in rendered
    assert "ARM1" not in rendered
    assert "+ 2 more" in rendered


def test_build_armed_no_truncation_when_fits():
    """Quand limit >= len(armed), aucun '+ N more'."""
    state = {
        "indicator_watches": [
            {
                "symbol": "A1",
                "on_trigger": "EXECUTE_ORDER",
                "expires_at": FUTURE,
                "logic": "and",
                "conditions": [],
                "order": {"action": "BUY", "qty": 1},
            }
        ]
    }
    rendered = _render(build_armed(state, now=NOW, limit=5))
    assert "A1" in rendered
    assert "more" not in rendered


def test_build_watches_limit_truncates():
    """limit=1 avec 3 watches actives → 1 affichée + '+ 2 more'."""
    state = {
        "indicator_watches": [
            {
                "symbol": f"WATCH{i}",
                "on_trigger": "WAKE",
                "expires_at": FUTURE,
                "created_at": "2026-07-06T00:00:00+00:00",
                "logic": "all",
                "conditions": [
                    {"indicator": "z", "op": "<=", "value": 1.0, "timeframe": "1h"}
                ],
            }
            for i in range(3)
        ]
    }
    rendered = _render(build_watches(state, now=NOW, limit=1))
    assert "WATCH0" in rendered
    assert "WATCH1" not in rendered
    assert "+ 2 more" in rendered


def test_build_watches_no_truncation_when_fits():
    """Quand limit >= active watches, aucun '+ N more'."""
    state = {
        "indicator_watches": [
            {
                "symbol": "W1",
                "on_trigger": "WAKE",
                "expires_at": FUTURE,
                "created_at": "2026-07-06T00:00:00+00:00",
                "logic": "all",
                "conditions": [],
            }
        ]
    }
    rendered = _render(build_watches(state, now=NOW, limit=10))
    assert "W1" in rendered
    assert "more" not in rendered


def test_build_exit_watches_limit_truncates():
    """limit=1 avec 3 exit watches → 1 affichée + '+ 2 more'."""
    state = {
        "trade_plans": [
            {
                "symbol": f"EW{i}",
                "exit_watch": {
                    "logic": "or",
                    "conditions": [
                        {"indicator": "z", "op": "<=", "value": 1.0, "timeframe": "1h"}
                    ],
                    "expires_at": FUTURE,
                },
            }
            for i in range(3)
        ]
    }
    rendered = _render(build_exit_watches(state, now=NOW, limit=1))
    assert "EW0" in rendered
    assert "EW1" not in rendered
    assert "+ 2 more" in rendered


def test_build_next_to_fire_adaptive_limit():
    """build_next_to_fire_plans accepte limit comme paramètre explicite."""
    state = {
        "indicator_watches": [
            {
                "symbol": f"SYM{i}",
                "on_trigger": "WAKE",
                "expires_at": FUTURE,
                "created_at": "2026-07-06T00:00:00+00:00",
                "logic": "all",
                "conditions": [],
            }
            for i in range(5)
        ]
    }
    rendered_small = _render(build_next_to_fire_plans(state, now=NOW, limit=2))
    rendered_large = _render(build_next_to_fire_plans(state, now=NOW, limit=10))
    # Avec limit=10 on voit plus de symboles qu'avec limit=2
    count_small = rendered_small.count("SYM")
    count_large = rendered_large.count("SYM")
    assert count_large >= count_small


# ---------------------------------------------------------------------------
# PlansPage widget — test de montage Textual
# ---------------------------------------------------------------------------


async def test_plans_page_mounts_via_key_4(tmp_path, monkeypatch):
    """La touche 4 navigue vers la PlansPage qui doit monter sans crash."""
    _make_minimal_state(tmp_path)
    _patch_paths(monkeypatch, tmp_path)
    (tmp_path / "decisions.jsonl").write_text("{}\n", encoding="utf-8")

    app = CockpitApp()
    async with app.run_test(size=(220, 60)) as pilot:
        await pilot.pause()
        await pilot.press("4")
        await pilot.pause()
        assert app._active_page_key == "plans"
        plans_page = app.query_one("#plans-page", PlansPage)
        assert plans_page.display is True
        # Les panneaux doivent être dans le DOM
        assert app.query_one("#armed-panel") is not None
        assert app.query_one("#exit-plans-panel") is not None
        assert app.query_one("#watches-panel") is not None
        assert app.query_one("#exit-watches-panel") is not None
        assert app.query_one("#fire-panel") is not None


async def test_plans_page_update_state_etat_vide(tmp_path, monkeypatch):
    """update_state avec state vide ne plante pas."""
    _make_minimal_state(tmp_path)
    _patch_paths(monkeypatch, tmp_path)
    (tmp_path / "decisions.jsonl").write_text("{}\n", encoding="utf-8")

    app = CockpitApp()
    async with app.run_test(size=(220, 60)) as pilot:
        await pilot.pause()
        await pilot.press("4")
        await pilot.pause()
        page = app.query_one("#plans-page", PlansPage)
        # update_state avec des states partiels ne doit jamais lever d'exception
        page.update_state({})
        page.update_state({"indicator_watches": None, "trade_plans": None})
        page.update_state({"indicator_watches": [], "trade_plans": []})


async def test_plans_page_update_state_with_full_data(tmp_path, monkeypatch):
    """update_state avec données complètes met à jour les titres des panneaux."""
    _make_minimal_state(tmp_path)
    _patch_paths(monkeypatch, tmp_path)
    (tmp_path / "decisions.jsonl").write_text("{}\n", encoding="utf-8")

    state = {
        "indicator_watches": [
            {
                "symbol": "TEST",
                "on_trigger": "EXECUTE_ORDER",
                "expires_at": FUTURE,
                "logic": "all",
                "conditions": [
                    {"indicator": "RS", "op": ">", "value": 50, "timeframe": "1h"}
                ],
                "order": {"action": "BUY", "qty": 5},
            },
            {
                "symbol": "WATCH",
                "on_trigger": "WAKE",
                "expires_at": FUTURE,
                "created_at": "2026-07-06T00:00:00+00:00",
                "logic": "all",
                "conditions": [
                    {"indicator": "z", "op": "<=", "value": 1.0, "timeframe": "1h"}
                ],
            },
        ],
        "trade_plans": [
            {
                "symbol": "SPY",
                "side": "LONG",
                "remaining_quantity": 10,
                "entry_price": 500.0,
                "hard_stop_price": 485.0,
                "last_llm_review": {"ts": "2026-07-06T01:30:00+00:00"},
            }
        ],
        "prices": {"SPY": 502.0},
    }

    app = CockpitApp()
    async with app.run_test(size=(220, 60)) as pilot:
        await pilot.pause()
        await pilot.press("4")
        await pilot.pause()
        page = app.query_one("#plans-page", PlansPage)
        # fige l'état de l'app : le mixin ResizeRefresh re-rend depuis
        # app._last_state — sans ça le refresh périodique écrase les données
        app._last_state = state
        page.update_state(state)
        await pilot.pause()

        # Titre ARMED doit être "ARMED — 1"
        armed_panel = app.query_one("#armed-panel")
        assert "1" in (armed_panel.border_title or "")

        # Titre EXIT PLANS contient le count
        ep_panel = app.query_one("#exit-plans-panel")
        assert "1" in (ep_panel.border_title or "")

        # Titre WATCHES contient le count
        wt_panel = app.query_one("#watches-panel")
        assert "1" in (wt_panel.border_title or "")
