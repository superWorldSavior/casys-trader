"""Tests de la page Portfolio du cockpit casys (page 2).

Couvre :
- Helpers purs : _pnl_pct, _sort_holdings, _data_cell, _fmt_date_exit
- Projections pures : build_positions_rows, project_portfolio_positions
- Builders Rich purs : build_exposure, build_fx, build_closed_trades
- Widget Textual : montage des panneaux, binding o cycle sort, état vide toléré
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

UTC = timezone.utc
NOW = datetime(2026, 7, 6, 2, 1, 28, tzinfo=UTC)


# ---------------------------------------------------------------------------
# State fixtures
# ---------------------------------------------------------------------------

_HOLDINGS_2 = [
    {
        "symbol": "AAPL",
        "quantity": 10,
        "avg_price": 200.0,
        "last_price": 210.0,
        "unrealized_pnl_net": 100.0,
        "fx_rate": 1.0,
    },
    {
        "symbol": "2330.TW",
        "quantity": 100,
        "avg_price": 800.0,
        "last_price": 750.0,
        "unrealized_pnl_net": -155.0,
        "fx_rate": 0.031,
    },
]

_STATE_BASE = {
    "portfolio": {
        "cash": 80000.0,
        "equity": 100000.0,
        "holdings": _HOLDINGS_2,
    },
    "fx_rates": {"USD": 1.0, "EUR": 1.1432, "CHF": 1.2429, "TWD": 0.0312},
    "ts": "2026-07-06T01:45:00+00:00",
    "stale_market_data": {
        "2330.TW": {"data_age_minutes": 4680.0, "stale_reason": "too_old"},
    },
    "trade_plans": [
        {
            "symbol": "AAPL",
            "side": "LONG",
            "hard_stop_price": 195.0,
            "take_profits": [{"price": 220.0}],
        }
    ],
    "recent_trips": [
        {
            "symbol": "SPY",
            "side": "LONG",
            "quantity": 5,
            "entry_price": 500.0,
            "exit_price": 510.0,
            "pnl": 45.0,
            "entry_ts": "2026-07-03T10:00:00+00:00",
            "exit_ts": "2026-07-03T15:00:00+00:00",
            "holding_minutes": 300.0,
        },
    ],
    "attribution": {
        "realized_pnl": 450.0,
        "total_commissions": 32.0,
        "n_closed_trades": 8,
        "win_rate": 0.625,
    },
}


# ---------------------------------------------------------------------------
# Render helper
# ---------------------------------------------------------------------------


def _render(renderable, width: int = 120) -> str:
    from rich.console import Console

    console = Console(width=width, highlight=False)
    with console.capture() as cap:
        console.print(renderable)
    return cap.get()


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_pnl_pct_long_profit():
    from trader.interfaces.cockpit.pages.portfolio import _pnl_pct

    h = {"quantity": 10, "avg_price": 100.0, "unrealized_pnl_net": 50.0, "fx_rate": 1.0}
    assert abs(_pnl_pct(h) - 5.0) < 0.01


def test_pnl_pct_short_loss():
    from trader.interfaces.cockpit.pages.portfolio import _pnl_pct

    h = {"quantity": -10, "avg_price": 50.0, "unrealized_pnl_net": -25.0, "fx_rate": 1.0}
    assert abs(_pnl_pct(h) - (-5.0)) < 0.01


def test_pnl_pct_zero_cost():
    from trader.interfaces.cockpit.pages.portfolio import _pnl_pct

    h = {"quantity": 0, "avg_price": 0.0, "unrealized_pnl_net": 0.0, "fx_rate": 1.0}
    assert _pnl_pct(h) == 0.0


def test_sort_by_pnl():
    from trader.interfaces.cockpit.pages.portfolio import _sort_holdings

    holdings = [
        {"symbol": "A", "unrealized_pnl_net": 50.0, "quantity": 1, "last_price": 10.0, "fx_rate": 1.0},
        {"symbol": "B", "unrealized_pnl_net": -200.0, "quantity": 1, "last_price": 10.0, "fx_rate": 1.0},
        {"symbol": "C", "unrealized_pnl_net": 100.0, "quantity": 1, "last_price": 10.0, "fx_rate": 1.0},
    ]
    result = _sort_holdings(holdings, sort_mode=0)
    assert result[0]["symbol"] == "B"   # |−200| largest
    assert result[1]["symbol"] == "C"   # |+100|
    assert result[2]["symbol"] == "A"   # |+50|


def test_sort_by_value():
    from trader.interfaces.cockpit.pages.portfolio import _sort_holdings

    holdings = [
        {"symbol": "A", "quantity": 1, "last_price": 500.0, "fx_rate": 1.0, "unrealized_pnl_net": 0.0},
        {"symbol": "B", "quantity": 10, "last_price": 100.0, "fx_rate": 1.0, "unrealized_pnl_net": 0.0},
    ]
    result = _sort_holdings(holdings, sort_mode=1)
    assert result[0]["symbol"] == "B"  # notional 1000 > 500


def test_sort_by_pct():
    from trader.interfaces.cockpit.pages.portfolio import _sort_holdings

    holdings = [
        {
            "symbol": "A",
            "quantity": 10, "avg_price": 100.0, "unrealized_pnl_net": 5.0,
            "fx_rate": 1.0, "last_price": 100.0,
        },  # +0.5%
        {
            "symbol": "B",
            "quantity": 10, "avg_price": 100.0, "unrealized_pnl_net": -30.0,
            "fx_rate": 1.0, "last_price": 100.0,
        },  # -3%
    ]
    result = _sort_holdings(holdings, sort_mode=2)
    assert result[0]["symbol"] == "B"  # |−3%| > |+0.5%|


def test_page_position_helpers_are_canonical_projection_aliases():
    from trader.interfaces.cockpit.pages import portfolio as page
    from trader.interfaces.cockpit.projections import portfolio as projection

    assert page._pnl_pct is projection.pnl_pct
    assert page._sort_holdings is projection.sort_holdings
    assert page.build_positions_rows is projection.build_positions_rows


def test_data_cell_fresh():
    from trader.interfaces.cockpit.pages.portfolio import _data_cell

    cell = _data_cell({"stale_market_data": {}}, "AAPL")
    assert "●" in cell.plain
    assert "▲" not in cell.plain


def test_data_cell_stale_shows_age():
    from trader.interfaces.cockpit.pages.portfolio import _data_cell

    state = {"stale_market_data": {"AAPL": {"data_age_minutes": 4680.0}}}
    cell = _data_cell(state, "AAPL")
    assert "▲" in cell.plain
    assert "78h" in cell.plain  # 4680 min / 60 = 78h


def test_data_cell_absent_key_is_fresh():
    from trader.interfaces.cockpit.pages.portfolio import _data_cell

    cell = _data_cell({}, "MSFT")
    assert "●" in cell.plain


def test_fmt_date_exit_valid():
    from trader.interfaces.cockpit.pages.portfolio import _fmt_date_exit

    assert _fmt_date_exit("2026-07-03T15:00:00+00:00") == "03 Jul"
    assert _fmt_date_exit("2026-01-01T00:00:00+00:00") == "01 Jan"


def test_fmt_date_exit_invalid():
    from trader.interfaces.cockpit.pages.portfolio import _fmt_date_exit

    assert _fmt_date_exit(None) == "—"
    assert _fmt_date_exit("garbage") == "—"


# ---------------------------------------------------------------------------
# build_exposure
# ---------------------------------------------------------------------------


def test_build_exposure_empty_no_crash():
    from trader.interfaces.cockpit.pages.portfolio import build_exposure

    rendered = _render(build_exposure({}))
    assert rendered  # non-empty, doesn't raise


def test_build_exposure_long_short_labels():
    from trader.interfaces.cockpit.pages.portfolio import build_exposure

    state = {
        "portfolio": {
            "cash": 50000.0,
            "equity": 100000.0,
            "holdings": [
                {
                    "symbol": "AAPL",
                    "quantity": 10,
                    "last_price": 200.0,
                    "avg_price": 190.0,
                    "unrealized_pnl_net": 100.0,
                    "fx_rate": 1.0,
                },
                {
                    "symbol": "MSFT",
                    "quantity": -5,
                    "last_price": 300.0,
                    "avg_price": 310.0,
                    "unrealized_pnl_net": 50.0,
                    "fx_rate": 1.0,
                },
            ],
        }
    }
    rendered = _render(build_exposure(state))
    assert "long" in rendered
    assert "short" in rendered
    assert "net" in rendered
    assert "gross" in rendered


def test_build_exposure_bars_filled():
    from trader.interfaces.cockpit.pages.portfolio import build_exposure

    # Single long-only position → long bar should have some █
    state = {
        "portfolio": {
            "cash": 90000.0,
            "equity": 100000.0,
            "holdings": [
                {
                    "symbol": "AAPL",
                    "quantity": 50,
                    "last_price": 200.0,
                    "avg_price": 200.0,
                    "unrealized_pnl_net": 0.0,
                    "fx_rate": 1.0,
                },
            ],
        }
    }
    rendered = _render(build_exposure(state))
    assert "█" in rendered


# ---------------------------------------------------------------------------
# build_fx
# ---------------------------------------------------------------------------


def test_build_fx_empty_state():
    from trader.interfaces.cockpit.pages.portfolio import build_fx

    rendered = _render(build_fx({}, now=NOW))
    assert "not available" in rendered


def test_build_fx_shows_rates():
    from trader.interfaces.cockpit.pages.portfolio import build_fx

    state = {
        "fx_rates": {"USD": 1.0, "EUR": 1.1432, "CHF": 1.2429, "TWD": 0.0312},
        "ts": "2026-07-06T01:45:00+00:00",
    }
    rendered = _render(build_fx(state, now=NOW))
    assert "EUR" in rendered
    assert "1.1432" in rendered
    assert "CHF" in rendered
    assert "TWD" in rendered
    assert "0.0312" in rendered


def test_build_fx_usd_hidden():
    from trader.interfaces.cockpit.pages.portfolio import build_fx

    state = {"fx_rates": {"USD": 1.0, "EUR": 1.14}, "ts": "2026-07-06T01:45:00+00:00"}
    rendered = _render(build_fx(state, now=NOW))
    assert "EUR" in rendered
    # USD should not appear as a rate entry
    assert "USD 1" not in rendered


def test_build_fx_refreshed_timestamp():
    from trader.interfaces.cockpit.pages.portfolio import build_fx

    state = {
        "fx_rates": {"EUR": 1.1432},
        "ts": "2026-07-06T01:45:00+00:00",
    }
    rendered = _render(build_fx(state, now=NOW))
    assert "01:45" in rendered
    assert "UTC" in rendered
    assert "P&L" in rendered


def test_build_fx_fallback_to_now_when_no_ts():
    from trader.interfaces.cockpit.pages.portfolio import build_fx

    state = {"fx_rates": {"EUR": 1.14}}  # no ts key
    rendered = _render(build_fx(state, now=NOW))
    # Should fall back to NOW's time
    assert "02:01" in rendered  # NOW = 02:01:28 UTC


# ---------------------------------------------------------------------------
# build_closed_trades
# ---------------------------------------------------------------------------


def test_build_closed_trades_empty():
    from trader.interfaces.cockpit.pages.portfolio import build_closed_trades

    rendered = _render(build_closed_trades({}, now=NOW))
    assert "no closed trades" in rendered


def test_build_closed_trades_shows_symbol_and_pnl():
    from trader.interfaces.cockpit.pages.portfolio import build_closed_trades

    rendered = _render(build_closed_trades(_STATE_BASE, now=NOW))
    assert "SPY" in rendered
    assert "03 Jul" in rendered
    assert "+45" in rendered


def test_build_closed_trades_footer_stats():
    from trader.interfaces.cockpit.pages.portfolio import build_closed_trades

    rendered = _render(build_closed_trades(_STATE_BASE, now=NOW))
    assert "realized" in rendered
    assert "fees" in rendered
    assert "trips" in rendered
    assert "8 trips" in rendered


def test_build_closed_trades_attribution_fallback():
    from trader.interfaces.cockpit.pages.portfolio import build_closed_trades

    # recent_trips absent at top-level → fall back to attribution.recent_trips
    state = {
        "attribution": {
            "realized_pnl": 120.0,
            "total_commissions": 15.0,
            "n_closed_trades": 4,
            "recent_trips": [
                {
                    "symbol": "XYZ",
                    "side": "SHORT",
                    "pnl": -20.0,
                    "exit_ts": "2026-07-04T10:00:00+00:00",
                    "holding_minutes": 120.0,
                }
            ],
        }
    }
    rendered = _render(build_closed_trades(state, now=NOW))
    assert "XYZ" in rendered
    assert "S" in rendered
    assert "-20" in rendered


def test_build_closed_trades_side_long():
    from trader.interfaces.cockpit.pages.portfolio import build_closed_trades

    state = {
        "recent_trips": [
            {
                "symbol": "BNP",
                "side": "LONG",
                "pnl": 80.0,
                "exit_ts": "2026-07-05T14:00:00+00:00",
                "holding_minutes": 90.0,
            }
        ],
        "attribution": {},
    }
    rendered = _render(build_closed_trades(state, now=NOW))
    assert "BNP" in rendered
    assert "+80" in rendered


def test_build_closed_trades_shows_reason():
    from trader.interfaces.cockpit.pages.portfolio import build_closed_trades

    state = {
        "recent_trips": [
            {
                "symbol": "AAPL",
                "side": "LONG",
                "pnl": 50.0,
                "exit_ts": "2026-07-05T14:00:00+00:00",
                "holding_minutes": 120.0,
                "exit_reason": "take_profit",
            }
        ],
        "attribution": {},
    }
    rendered = _render(build_closed_trades(state, now=NOW))
    assert "take_profit" in rendered


def test_build_closed_trades_reason_missing_shows_dash():
    from trader.interfaces.cockpit.pages.portfolio import build_closed_trades

    state = {
        "recent_trips": [
            {
                "symbol": "MSFT",
                "side": "LONG",
                "pnl": 30.0,
                "exit_ts": "2026-07-05T14:00:00+00:00",
            }
        ],
        "attribution": {},
    }
    rendered = _render(build_closed_trades(state, now=NOW))
    assert "MSFT" in rendered
    assert "—" in rendered  # exit_reason fallback


def test_build_closed_trades_reason_clipped_to_12():
    from trader.interfaces.cockpit.pages.portfolio import build_closed_trades

    long_reason = "very_long_exit_reason_string"
    state = {
        "recent_trips": [
            {
                "symbol": "X",
                "side": "LONG",
                "pnl": 10.0,
                "exit_ts": "2026-07-05T14:00:00+00:00",
                "exit_reason": long_reason,
            }
        ],
        "attribution": {},
    }
    rendered = _render(build_closed_trades(state, now=NOW))
    assert long_reason not in rendered           # full string absent
    assert long_reason[:12] in rendered          # clipped version present


def test_build_closed_trades_medium_drops_dur():
    from trader.interfaces.cockpit.pages.portfolio import build_closed_trades

    state = {
        "recent_trips": [
            {
                "symbol": "AAPL",
                "side": "LONG",
                "pnl": 50.0,
                "exit_ts": "2026-07-05T14:00:00+00:00",
                "holding_minutes": 120.0,
                "exit_reason": "hard_stop",
            }
        ],
        "attribution": {},
    }
    wide_rendered = _render(build_closed_trades(state, now=NOW, wide=True))
    medium_rendered = _render(build_closed_trades(state, now=NOW, wide=False))
    # DUR "2h00" visible in wide, absent in medium
    assert "2h00" in wide_rendered
    assert "2h00" not in medium_rendered
    # REASON present in both layouts
    assert "hard_stop" in wide_rendered
    assert "hard_stop" in medium_rendered


def test_build_closed_trades_limit():
    from trader.interfaces.cockpit.pages.portfolio import build_closed_trades

    trips = [
        {
            "symbol": f"SYM{i}",
            "side": "LONG",
            "pnl": float(i * 10),
            "exit_ts": "2026-07-05T14:00:00+00:00",
            "exit_reason": "hard_stop",
        }
        for i in range(10)
    ]
    state = {"recent_trips": trips, "attribution": {}}
    rendered = _render(build_closed_trades(state, now=NOW, limit=3))
    assert "SYM0" in rendered
    assert "SYM2" in rendered
    assert "SYM9" not in rendered  # beyond limit


def test_build_closed_trades_limit_default_shows_all_when_few():
    from trader.interfaces.cockpit.pages.portfolio import build_closed_trades

    trips = [
        {
            "symbol": f"T{i}",
            "side": "LONG",
            "pnl": 10.0,
            "exit_ts": "2026-07-05T14:00:00+00:00",
            "exit_reason": "take_profit",
        }
        for i in range(3)
    ]
    state = {"recent_trips": trips, "attribution": {}}
    rendered = _render(build_closed_trades(state, now=NOW, limit=20))
    # All 3 trips visible (limit > count)
    assert "T0" in rendered
    assert "T1" in rendered
    assert "T2" in rendered


# ---------------------------------------------------------------------------
# build_positions_rows (pure)
# ---------------------------------------------------------------------------


def test_build_positions_rows_empty():
    from trader.interfaces.cockpit.pages.portfolio import build_positions_rows

    assert build_positions_rows({}) == []


def test_build_positions_rows_count():
    from trader.interfaces.cockpit.pages.portfolio import build_positions_rows

    rows = build_positions_rows(_STATE_BASE)
    assert len(rows) == 2


def test_build_positions_rows_sorted_by_pnl_default():
    from trader.interfaces.cockpit.pages.portfolio import build_positions_rows

    # 2330.TW: |−155| > AAPL: |+100|
    rows = build_positions_rows(_STATE_BASE, sort_mode=0)
    assert rows[0]["symbol"] == "2330.TW"
    assert rows[1]["symbol"] == "AAPL"


def test_build_positions_rows_sorted_by_value():
    from trader.interfaces.cockpit.pages.portfolio import build_positions_rows

    # AAPL: 10 * 210 * 1.0 = 2100
    # 2330.TW: 100 * 750 * 0.031 = 2325
    rows = build_positions_rows(_STATE_BASE, sort_mode=1)
    assert rows[0]["symbol"] == "2330.TW"  # 2325 > 2100


def test_build_positions_rows_stop_for_plan():
    from trader.interfaces.cockpit.pages.portfolio import build_positions_rows

    rows = build_positions_rows(_STATE_BASE)
    aapl = next(r for r in rows if r["symbol"] == "AAPL")
    # Plan has hard_stop=195, last=210 → dist = (195-210)/210 * -1 = negative
    assert aapl["stop_dist"] is not None


def test_build_positions_rows_distinguishes_stop_left_from_entry_risk():
    from trader.interfaces.cockpit.pages.portfolio import build_positions_rows

    state = {
        "portfolio": {
            "holdings": [
                {
                    "symbol": "1326.TW",
                    "quantity": 3000,
                    "avg_price": 69.0,
                    "last_price": 67.4,
                    "unrealized_pnl_net": -159.87,
                    "fx_rate": 0.031178871385847216,
                }
            ]
        },
        "prices": {"1326.TW": 67.4},
        "trade_plans": [
            {
                "symbol": "1326.TW",
                "side": "LONG",
                "entry_price": 69.0,
                "hard_stop_price": 66.9,
            }
        ],
    }

    row = build_positions_rows(state)[0]

    assert row["stop_left_pct"] == pytest.approx(0.7418, abs=0.01)
    assert row["stop_entry_risk_pct"] == pytest.approx(3.0435, abs=0.01)


def test_build_positions_rows_no_stop_without_plan():
    from trader.interfaces.cockpit.pages.portfolio import build_positions_rows

    rows = build_positions_rows(_STATE_BASE)
    tw = next(r for r in rows if r["symbol"] == "2330.TW")
    assert tw["stop_dist"] is None  # no plan for 2330.TW


def test_build_positions_rows_stale_flag():
    from trader.interfaces.cockpit.pages.portfolio import build_positions_rows

    rows = build_positions_rows(_STATE_BASE)
    tw = next(r for r in rows if r["symbol"] == "2330.TW")
    aapl = next(r for r in rows if r["symbol"] == "AAPL")
    assert tw["is_stale"] is True
    assert aapl["is_stale"] is False


def test_build_positions_rows_pnl_pct():
    from trader.interfaces.cockpit.pages.portfolio import build_positions_rows

    rows = build_positions_rows(_STATE_BASE)
    aapl = next(r for r in rows if r["symbol"] == "AAPL")
    # pnl_pct = 100 / (10 * 200 * 1.0) * 100 = 5.0%
    assert abs(aapl["pnl_pct"] - 5.0) < 0.01


def test_project_portfolio_positions_composes_rows_and_aggregates():
    from trader.interfaces.cockpit.projections.portfolio import (
        project_portfolio_positions,
    )

    projection = project_portfolio_positions(_STATE_BASE)

    assert [row.symbol for row in projection.rows] == ["2330.TW", "AAPL"]
    assert projection.gross_long == pytest.approx(4425.0)
    assert projection.gross_short == 0.0
    assert projection.gross == pytest.approx(4425.0)
    assert projection.net_long == pytest.approx(4425.0)
    assert projection.unrealized_total == pytest.approx(-55.0)


def test_project_portfolio_positions_splits_long_and_short_exposure():
    from trader.interfaces.cockpit.projections.portfolio import (
        project_portfolio_positions,
    )

    state = {
        "portfolio": {
            "holdings": [
                {
                    "symbol": "LONG",
                    "quantity": 2,
                    "last_price": 100.0,
                    "fx_rate": 1.0,
                    "unrealized_pnl_net": 10.0,
                },
                {
                    "symbol": "SHORT",
                    "quantity": -3,
                    "last_price": 50.0,
                    "fx_rate": 1.0,
                    "unrealized_pnl_net": -5.0,
                },
            ]
        }
    }

    projection = project_portfolio_positions(state)

    assert projection.gross_long == 200.0
    assert projection.gross_short == 150.0
    assert projection.gross == 350.0
    assert projection.net_long == 50.0
    assert projection.unrealized_total == 5.0


def test_project_portfolio_positions_masque_les_residus_de_cloture_a_valeur_nulle():
    from trader.interfaces.cockpit.projections.portfolio import (
        project_portfolio_positions,
    )

    state = {
        "portfolio": {
            "holdings": [
                {
                    "symbol": "AAPL",
                    "quantity": 2.0,
                    "last_price": 100.0,
                    "fx_rate": 1.0,
                },
                {
                    "symbol": "ZERO_DUST",
                    "quantity": -4.557080046652118e-9,
                    "last_price": 260.3,
                    "fx_rate": 0.1036,
                },
            ]
        }
    }

    projection = project_portfolio_positions(state)

    assert [row.symbol for row in projection.rows] == ["AAPL"]
    assert projection.gross == 200.0


# ---------------------------------------------------------------------------
# Widget — Textual async tests
# ---------------------------------------------------------------------------


def _make_state_dir(tmp_path: Path, pid: int = 0) -> None:
    """Minimal state files for CockpitApp to mount without first-run screen."""
    (tmp_path / "current_report.json").write_text(
        json.dumps(
            {
                "ts": "2026-07-06T02:00:00+00:00",
                "dry_run": True,
                "portfolio": {"cash": 80000.0, "equity": 100000.0, "holdings": []},
                "decisions": [],
                "fx_rates": {"USD": 1.0, "EUR": 1.14},
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "daemon_status.json").write_text(
        json.dumps(
            {
                "ts": NOW.isoformat(),
                "phase": "cycle_completed",
                "pid": pid,
                "decisions_done": 0,
                "symbols_total": 0,
                "model_calls_used": 0,
            }
        ),
        encoding="utf-8",
    )


def _patch_app(monkeypatch, tmp_path: Path, pid: int = 4242) -> None:
    import trader.interfaces.cockpit.app as cockpit_module
    from trader.interfaces.cockpit.supervisor import DaemonVitalState

    _make_state_dir(tmp_path, pid=pid)
    monkeypatch.setattr(cockpit_module, "_STATE_DIR", tmp_path)
    monkeypatch.setattr(cockpit_module, "_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(cockpit_module, "_EVENTS_FILE", tmp_path / "events.jsonl")
    monkeypatch.setattr(cockpit_module, "_AGENT_TRACE_FILE", tmp_path / "agent_trace.log")
    monkeypatch.setattr(cockpit_module, "_KILL_FILE", tmp_path / "KILL")
    # Simulate alive daemon → no FirstRunScreen overlay
    alive = DaemonVitalState(status="alive", since_seconds=5.0, battement_old=False)
    monkeypatch.setattr(cockpit_module, "daemon_vital_state", lambda _path: alive)


@pytest.mark.asyncio
async def test_portfolio_page_panels_in_dom(tmp_path, monkeypatch):
    """La page Portfolio expose tous ses panneaux dans le DOM après navigation."""
    from trader.interfaces.cockpit.app import CockpitApp

    _patch_app(monkeypatch, tmp_path)
    app = CockpitApp()
    async with app.run_test(size=(200, 55)) as pilot:
        await pilot.press("2")
        await pilot.pause()
        assert app._active_page_key == "portfolio"
        assert app.query_one("#portfolio-page").display is True
        assert app.query_one("#positions-panel") is not None
        assert app.query_one("#positions-table") is not None
        assert app.query_one("#positions-footer") is not None
        assert app.query_one("#exposure-panel") is not None
        assert app.query_one("#fx-panel") is not None
        assert app.query_one("#closed-panel") is not None


@pytest.mark.asyncio
async def test_portfolio_page_empty_state_no_crash(tmp_path, monkeypatch):
    """update_state({}) ne lève pas d'exception — panneaux montés sans données."""
    from trader.interfaces.cockpit.app import CockpitApp
    from trader.interfaces.cockpit.pages.portfolio import PortfolioPage

    _patch_app(monkeypatch, tmp_path)
    app = CockpitApp()
    async with app.run_test(size=(200, 55)) as pilot:
        await pilot.press("2")
        await pilot.pause()
        page = app.query_one("#portfolio-page", PortfolioPage)
        page.update_state({})   # Must not raise


@pytest.mark.asyncio
async def test_portfolio_page_update_state_with_data(tmp_path, monkeypatch):
    """update_state avec données réelles remplit les panneaux sans crash."""
    from trader.interfaces.cockpit.app import CockpitApp
    from trader.interfaces.cockpit.pages.portfolio import PortfolioPage

    _patch_app(monkeypatch, tmp_path)
    app = CockpitApp()
    async with app.run_test(size=(200, 55)) as pilot:
        await pilot.press("2")
        await pilot.pause()
        page = app.query_one("#portfolio-page", PortfolioPage)
        page.update_state(_STATE_BASE)   # Must not raise


@pytest.mark.asyncio
async def test_portfolio_page_binding_o_cycles_sort(tmp_path, monkeypatch):
    """La touche o cycle le tri : 0=|pnl| → 1=value → 2=% → 0."""
    from trader.interfaces.cockpit.app import CockpitApp
    from trader.interfaces.cockpit.pages.portfolio import PortfolioPage

    _patch_app(monkeypatch, tmp_path)
    app = CockpitApp()
    async with app.run_test(size=(200, 55)) as pilot:
        await pilot.press("2")
        await pilot.pause()
        page = app.query_one("#portfolio-page", PortfolioPage)
        # Load data so refresh has something to sort
        page.update_state(_STATE_BASE)
        assert page._sort_mode == 0
        await pilot.press("o")
        assert page._sort_mode == 1
        await pilot.press("o")
        assert page._sort_mode == 2
        await pilot.press("o")
        assert page._sort_mode == 0
