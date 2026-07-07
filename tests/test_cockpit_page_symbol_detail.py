"""Tests de la page symbol_detail (drill-down modal 2f).

Couvre :
- Builders purs (state synthétique + now fixe) : header, position, why, decisions,
  realized, exit plan, watch, market.
- Cas limites : state vide, valeurs None, listes vides.
- Montage Textual du modal : push_screen(SymbolDetailScreen) + Esc ferme.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console
from textual.widgets import Static

import trader.interfaces.cockpit.app as cockpit_module
from trader.interfaces.cockpit.app import CockpitApp
from trader.interfaces.cockpit.pages.symbol_detail import (
    SymbolDetailScreen,
    _exit_update_rejected_info,
    _earnings_label,
    _realized_total,
    _short_ts,
    _symbol_watch,
    _venue_bias,
    build_symbol_body,
    build_symbol_header,
)

UTC = timezone.utc
NOW = datetime(2026, 7, 6, 2, 1, 0, tzinfo=UTC)
SYMBOL = "3443.TW"


# ─── Helpers ─────────────────────────────────────────────────────────────────


def _render(renderable, width: int = 120) -> str:
    console = Console(width=width, legacy_windows=False, highlight=False)
    with console.capture() as cap:
        console.print(renderable)
    return cap.get()


def _patch_paths(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(cockpit_module, "_STATE_DIR", tmp_path)
    monkeypatch.setattr(cockpit_module, "_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(cockpit_module, "_EVENTS_FILE", tmp_path / "events.jsonl")
    monkeypatch.setattr(cockpit_module, "_AGENT_TRACE_FILE", tmp_path / "agent_trace.log")
    monkeypatch.setattr(cockpit_module, "_KILL_FILE", tmp_path / "KILL")


def _minimal_state_files(tmp_path: Path) -> None:
    (tmp_path / "current_report.json").write_text(
        json.dumps(
            {
                "ts": "2026-07-06T02:00:00+00:00",
                "dry_run": True,
                "portfolio": {"cash": 100_000.0, "equity": 100_000.0, "holdings": []},
                "decisions": [],
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "daemon_status.json").write_text(
        json.dumps(
            {
                "phase": "idle",
                "decisions_done": 0,
                "symbols_total": 0,
                "model_calls_used": 0,
                "max_model_calls_per_cycle": 25,
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "decisions.jsonl").write_text("{}\n", encoding="utf-8")


# ─── Synthetic state fixtures ─────────────────────────────────────────────────


_STATE_FULL: dict = {
    "company_map": {"3443.TW": "Global Unichip"},
    "sessions": {
        "TW": {"open": "01:00", "close": "05:30"},
        "EU": {"open": "07:00", "close": "15:30"},
        "US": {"open": "13:30", "close": "20:00"},
    },
    "prices": {"3443.TW": 4825.0},
    "stale_market_data": {},
    "portfolio": {
        "cash": 78_000.0,
        "equity": 101_506.0,
        "holdings": [
            {
                "symbol": "3443.TW",
                "quantity": 10,
                "avg_price": 5140.0,
                "last_price": 4825.0,
                "unrealized_pnl_net": -103.0,
                "fx_rate": 0.031,
            }
        ],
    },
    "trade_plans": [
        {
            "symbol": "3443.TW",
            "side": "LONG",
            "quantity": 10,
            "remaining_quantity": 10,
            "entry_price": 5140.0,
            "opened_at": "2026-07-04T09:20:00+00:00",
            "hard_stop_price": 4700.0,
            "take_profits": [],
            "trailing_stop": None,
            "profit_protection": None,
        }
    ],
    "indicator_watches": [
        {
            "id": "3443.TW:abc123",
            "symbol": "3443.TW",
            "created_at": "2026-07-06T00:00:00+00:00",
            "expires_at": "2026-07-06T06:00:00+00:00",
            "logic": "all",
            "on_trigger": "WAKE",
            "conditions": [
                {"indicator": "breakout", "op": "<=", "value": -1, "timeframe": "1h"}
            ],
        }
    ],
    "recent_decisions": [
        {
            "symbol": "3443.TW",
            "cycle_ts": "2026-07-04T09:20:00+00:00",
            "action": "BUY",
            "confidence": 0.69,
            "rationale": "breakout thesis — RS turning positive",
            "executed": True,
            "qty": 10,
            "price": 5140.0,
            "runtime": {
                "trade_plan_created": True,
                "tool_calls": [],
            },
            "news": {"earnings_in_h": 576.0},
        },
        {
            "symbol": "3443.TW",
            "cycle_ts": "2026-07-06T02:01:00+00:00",
            "action": "HOLD",
            "confidence": 0.72,
            "rationale": "range-bound just above swing low, capping risk",
            "executed": False,
            "runtime": {
                "tool_calls": [
                    {
                        "tool": "strategy_exit",
                        "outcome": "rejected",
                        "detail": {"warnings": [{"code": "hard_stop_above_max_pct"}]},
                    }
                ]
            },
            "news": {"earnings_in_h": 24.0},
        },
    ],
    "recent_trips": [
        {"symbol": "3443.TW", "pnl": 212.0, "exit_reason": "take_profit"},
        {"symbol": "OTHER.SYM", "pnl": 50.0, "exit_reason": "take_profit"},
    ],
    "venue_state": {
        "venues": {
            "TW": {
                "candidates": [
                    {"symbol": "3443.TW", "attractiveness": 1.2, "bias": "long"},
                    {"symbol": "2330.TW", "attractiveness": 0.8, "bias": "long"},
                ]
            }
        }
    },
}


# ─── Unit tests — build_symbol_header ─────────────────────────────────────────


def test_header_shows_symbol() -> None:
    rendered = _render(build_symbol_header(_STATE_FULL, SYMBOL, now=NOW))
    assert SYMBOL in rendered


def test_header_shows_company() -> None:
    rendered = _render(build_symbol_header(_STATE_FULL, SYMBOL, now=NOW))
    assert "Global Unichip" in rendered


def test_header_shows_esc_close() -> None:
    rendered = _render(build_symbol_header(_STATE_FULL, SYMBOL, now=NOW))
    assert "esc close" in rendered


def test_header_shows_last_price() -> None:
    rendered = _render(build_symbol_header(_STATE_FULL, SYMBOL, now=NOW))
    assert "4,825" in rendered


def test_header_stale_flag() -> None:
    state = {**_STATE_FULL, "stale_market_data": {"3443.TW": {"data_age_minutes": 200.0}}}
    rendered = _render(build_symbol_header(state, SYMBOL, now=NOW))
    assert "stale" in rendered or "▲" in rendered


def test_header_empty_state() -> None:
    # Must not crash on empty state
    rendered = _render(build_symbol_header({}, "UNKNOWN.SYM", now=NOW))
    assert "UNKNOWN.SYM" in rendered
    assert "esc close" in rendered


# ─── Unit tests — build_symbol_body ───────────────────────────────────────────


def test_body_shows_position() -> None:
    rendered = _render(build_symbol_body(_STATE_FULL, SYMBOL, now=NOW), width=140)
    assert "position" in rendered


def test_body_shows_why_rationale() -> None:
    rendered = _render(build_symbol_body(_STATE_FULL, SYMBOL, now=NOW), width=140)
    assert "WHY" in rendered
    assert "range-bound" in rendered


def test_body_shows_recent_decisions() -> None:
    rendered = _render(build_symbol_body(_STATE_FULL, SYMBOL, now=NOW), width=140)
    assert "RECENT DECISIONS" in rendered
    assert "BUY" in rendered
    assert "HOLD" in rendered


def test_body_shows_realized() -> None:
    rendered = _render(build_symbol_body(_STATE_FULL, SYMBOL, now=NOW), width=140)
    assert "realized" in rendered
    assert "212" in rendered


def test_body_shows_exit_plan() -> None:
    rendered = _render(build_symbol_body(_STATE_FULL, SYMBOL, now=NOW), width=140)
    assert "EXIT PLAN" in rendered
    assert "4,700" in rendered


def test_body_exit_plan_labels_stop_left_and_entry_risk() -> None:
    rendered = _render(build_symbol_body(_STATE_FULL, SYMBOL, now=NOW), width=160)

    assert "left 2.6%" in rendered
    assert "entry risk 8.6%" in rendered


def test_body_shows_exit_update_rejected_warning() -> None:
    rendered = _render(build_symbol_body(_STATE_FULL, SYMBOL, now=NOW), width=140)
    assert "exit update rejected" in rendered
    assert "hard_stop_above_max_pct" in rendered


def test_body_shows_watch() -> None:
    rendered = _render(build_symbol_body(_STATE_FULL, SYMBOL, now=NOW), width=140)
    assert "WATCH" in rendered
    assert "breakout" in rendered


def test_body_shows_market() -> None:
    rendered = _render(build_symbol_body(_STATE_FULL, SYMBOL, now=NOW), width=140)
    assert "MARKET" in rendered
    assert "fresh" in rendered


def test_body_earnings_in_h() -> None:
    rendered = _render(build_symbol_body(_STATE_FULL, SYMBOL, now=NOW), width=140)
    assert "earnings" in rendered
    assert "in 1d" in rendered


def test_body_empty_state_no_crash() -> None:
    rendered = _render(build_symbol_body({}, "UNKNOWN.SYM", now=NOW), width=140)
    assert "UNKNOWN.SYM" in rendered
    assert "no open position" in rendered


def test_body_no_position_no_crash() -> None:
    state = {
        **_STATE_FULL,
        "portfolio": {"cash": 100_000.0, "equity": 100_000.0, "holdings": []},
    }
    rendered = _render(build_symbol_body(state, SYMBOL, now=NOW), width=140)
    assert "no open position" in rendered


def test_body_no_trade_plan_shows_no_exit_plan() -> None:
    state = {**_STATE_FULL, "trade_plans": []}
    rendered = _render(build_symbol_body(state, SYMBOL, now=NOW), width=140)
    assert "EXIT PLAN" in rendered
    assert "no exit plan" in rendered


def test_body_no_watch_shows_no_watch() -> None:
    state = {**_STATE_FULL, "indicator_watches": []}
    rendered = _render(build_symbol_body(state, SYMBOL, now=NOW), width=140)
    assert "WATCH" in rendered
    assert "no watch" in rendered


def test_body_no_recent_decisions_no_crash() -> None:
    state = {**_STATE_FULL, "recent_decisions": []}
    rendered = _render(build_symbol_body(state, SYMBOL, now=NOW), width=140)
    assert "no reasoning recorded" in rendered
    assert "no decisions yet" in rendered


# ─── Unit tests — local helpers ───────────────────────────────────────────────


def test_short_ts_today_shows_time_only() -> None:
    row = {"cycle_ts": "2026-07-06T01:54:00+00:00"}
    assert _short_ts(row, NOW) == "01:54"


def test_short_ts_other_day_shows_weekday() -> None:
    row = {"cycle_ts": "2026-07-04T09:20:00+00:00"}
    result = _short_ts(row, NOW)
    assert "Sat" in result or "09:20" in result  # Sat 09:20


def test_short_ts_missing_returns_dash() -> None:
    assert _short_ts({}, NOW) == "—"


def test_exit_update_rejected_info_found() -> None:
    rows = [
        {
            "cycle_ts": "2026-07-06T02:01:00+00:00",
            "runtime": {
                "tool_calls": [
                    {
                        "tool": "strategy_exit",
                        "outcome": "rejected",
                        "detail": {"warnings": [{"code": "my_code"}]},
                    }
                ]
            },
        }
    ]
    result = _exit_update_rejected_info(rows)
    assert result is not None
    time_str, code = result
    assert "02:01" in time_str
    assert code == "my_code"


def test_exit_update_rejected_info_not_found() -> None:
    rows = [
        {
            "runtime": {
                "tool_calls": [{"tool": "strategy_exit", "outcome": "applied", "detail": {}}]
            }
        }
    ]
    assert _exit_update_rejected_info(rows) is None


def test_exit_update_rejected_info_empty() -> None:
    assert _exit_update_rejected_info([]) is None


def _exit_row(cycle_ts: str, outcome: str) -> dict:
    return {
        "cycle_ts": cycle_ts,
        "runtime": {
            "tool_calls": [
                {"tool": "strategy_exit", "outcome": outcome, "detail": {}}
            ]
        },
    }


def test_exit_update_rejected_info_resolved_by_later_applied() -> None:
    """Un rejet ancien SUIVI d'une ré-application réussie ne doit plus alerter
    (bug de fraîcheur : le warning restait affiché après résolution)."""
    rows = [
        _exit_row("2026-07-06T20:29:00+00:00", "rejected"),  # ancien rejet
        _exit_row("2026-07-06T20:59:00+00:00", "applied"),   # ré-appliqué depuis
    ]
    assert _exit_update_rejected_info(rows) is None


def test_exit_update_rejected_info_latest_is_rejected() -> None:
    """Un applied ancien puis un rejet récent : c'est le dernier état qui compte."""
    rows = [
        _exit_row("2026-07-06T20:29:00+00:00", "applied"),
        _exit_row("2026-07-06T20:59:00+00:00", "rejected"),  # dernier = rejet
    ]
    assert _exit_update_rejected_info(rows) is not None


def test_earnings_label_hours() -> None:
    rows = [{"news": {"earnings_in_h": 12.0}}]
    assert _earnings_label(rows) == "in 12h"


def test_earnings_label_days() -> None:
    rows = [{"news": {"earnings_in_h": 576.0}}]
    assert _earnings_label(rows) == "in 24d"


def test_earnings_label_absent() -> None:
    assert _earnings_label([]) is None
    assert _earnings_label([{"news": {}}]) is None


def test_realized_total_sums_only_symbol() -> None:
    state = {
        "recent_trips": [
            {"symbol": "3443.TW", "pnl": 212.0},
            {"symbol": "OTHER.SYM", "pnl": 50.0},
            {"symbol": "3443.TW", "pnl": -80.0},
        ]
    }
    total, count = _realized_total(state, "3443.TW")
    assert count == 2
    assert abs(total - 132.0) < 0.01


def test_realized_total_empty() -> None:
    total, count = _realized_total({}, "3443.TW")
    assert total == 0.0
    assert count == 0


def test_symbol_watch_returns_first_match() -> None:
    state = {
        "indicator_watches": [
            {"symbol": "OTHER.SYM", "on_trigger": "WAKE"},
            {"symbol": "3443.TW", "on_trigger": "WAKE", "conditions": [{"indicator": "z"}]},
        ]
    }
    watch = _symbol_watch(state, "3443.TW")
    assert watch is not None
    assert watch["conditions"][0]["indicator"] == "z"


def test_symbol_watch_none_when_absent() -> None:
    assert _symbol_watch({"indicator_watches": []}, "3443.TW") is None


def test_venue_bias_returns_long() -> None:
    state = {
        "venue_state": {
            "venues": {
                "TW": {
                    "candidates": [
                        {"symbol": "3443.TW", "bias": "long", "attractiveness": 1.5}
                    ]
                }
            }
        }
    }
    assert _venue_bias(state, "3443.TW") == "long"


def test_venue_bias_none_when_absent() -> None:
    assert _venue_bias({}, "3443.TW") is None


# ─── Textual mounting test ─────────────────────────────────────────────────────


async def test_symbol_detail_screen_mounts_and_shows_body(tmp_path, monkeypatch):
    """push_screen(SymbolDetailScreen) : le static body est rendu sans crash."""
    _patch_paths(monkeypatch, tmp_path)
    _minimal_state_files(tmp_path)

    app = CockpitApp()
    async with app.run_test(size=(140, 50)) as pilot:
        await pilot.pause()
        # Post SymbolChosen to trigger the push_screen in the app
        from trader.interfaces.cockpit.pages._shared import SymbolChosen

        app.post_message(SymbolChosen(SYMBOL))
        await pilot.pause()

        # The modal should now be on the screen stack
        assert isinstance(app.screen, SymbolDetailScreen)
        body = app.screen.query_one("#symbol-detail-body", Static)
        assert body is not None

        # Esc should dismiss the modal
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, SymbolDetailScreen)


async def test_symbol_detail_esc_closes_modal(tmp_path, monkeypatch):
    """Esc renvoie None et retire la modal de la pile."""
    _patch_paths(monkeypatch, tmp_path)
    _minimal_state_files(tmp_path)

    app = CockpitApp()
    async with app.run_test(size=(140, 50)) as pilot:
        await pilot.pause()
        app.push_screen(SymbolDetailScreen(SYMBOL))
        await pilot.pause()
        assert isinstance(app.screen, SymbolDetailScreen)
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, SymbolDetailScreen)


async def test_symbol_detail_body_renders_symbol(tmp_path, monkeypatch):
    """Le contenu du modal contient le symbole (via le renderable Rich du Static)."""
    _patch_paths(monkeypatch, tmp_path)
    _minimal_state_files(tmp_path)

    app = CockpitApp()
    async with app.run_test(size=(140, 50)) as pilot:
        await pilot.pause()
        # Set _last_state with minimal data before push_screen so on_mount sees it
        app._last_state = {
            "recent_decisions": [],
            "portfolio": {"cash": 100_000.0, "equity": 100_000.0, "holdings": []},
            "trade_plans": [],
            "indicator_watches": [],
            "recent_trips": [],
            "prices": {SYMBOL: 4825.0},
            "stale_market_data": {},
        }
        app.push_screen(SymbolDetailScreen(SYMBOL))
        await pilot.pause()

        body = app.screen.query_one("#symbol-detail-body", Static)
        assert body is not None

        # body.content holds the Rich object passed to Static.update()
        # build_symbol_body returns a Group; render it via Rich to check content.
        renderable = body.content
        assert renderable is not None
        rendered_str = _render(renderable, width=140)
        assert SYMBOL in rendered_str

        await pilot.press("escape")
