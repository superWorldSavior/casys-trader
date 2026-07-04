"""Tests TDD — Task 5B : conscience devise dans build_market_cockpit.

Vérifie que chaque ligne du cockpit est estampillée avec sa devise, son taux
FX et les budgets natifs (risk et max-order), sans convertir aucune valeur
d'analyse (prix, indicateurs, swings restent natifs).
"""

from __future__ import annotations

import pytest

from trader.agent.context import build_market_cockpit
from trader.market.market_data import Bar


def _fake_bars(close: float = 100.0, n: int = 12) -> list[Bar]:
    """Série de barres minimale pour alimenter build_market_cockpit."""
    return [
        Bar(
            ts=f"2026-06-{i + 1:02d}T10:00:00+00:00",
            open=close - 0.5,
            high=close + 1.0,
            low=close - 1.0,
            close=close,
            volume=1000.0,
        )
        for i in range(n)
    ]


def _row_as_dict(cockpit: dict, symbol: str) -> dict:
    """Convertit une ligne du cockpit (liste) en dict via les colonnes."""
    cols = cockpit["cols"]
    for row in cockpit["rows"]:
        if row[0] == symbol:
            return dict(zip(cols, row))
    raise KeyError(f"symbole {symbol!r} introuvable dans les rows du cockpit")


def test_symbol_row_is_currency_stamped_and_native_budget() -> None:
    bars = {
        "2379.TW": _fake_bars(close=829.0),
        "MSFT": _fake_bars(close=370.0),
    }
    cockpit = build_market_cockpit(
        bars,
        symbols=["2379.TW", "MSFT"],
        prices={"2379.TW": 829.0, "MSFT": 370.0},
        fx_rate_by_ccy={"TWD": 0.031, "USD": 1.0},
        equity_usd=100_000.0,
        risk_pct=0.01,
        max_order_value=10_000.0,
    )
    tw = _row_as_dict(cockpit, "2379.TW")
    assert tw["ccy"] == "TWD"
    assert tw["fx_usd"] == pytest.approx(0.031)
    # Prix natif NON converti
    assert tw["p"] == pytest.approx(829.0)
    # budget natif = risk_pct * equity_usd / fx_usd = 1000 / 0.031
    assert tw["risk_budget_native"] == pytest.approx(1_000.0 / 0.031)
    assert tw["max_order_native"] == pytest.approx(10_000.0 / 0.031)

    usd = _row_as_dict(cockpit, "MSFT")
    assert usd["ccy"] == "USD"
    assert usd["fx_usd"] == pytest.approx(1.0)
    assert usd["risk_budget_native"] == pytest.approx(1_000.0)  # /1.0
    assert usd["max_order_native"] == pytest.approx(10_000.0)


def test_symbol_row_divide_by_zero_guard() -> None:
    """Rate=0.0 → budget retourne 0.0, pas d'exception."""
    bars = {"SPY": _fake_bars(close=500.0)}
    cockpit = build_market_cockpit(
        bars,
        symbols=["SPY"],
        prices={"SPY": 500.0},
        fx_rate_by_ccy={"USD": 0.0},  # taux invalide
        equity_usd=50_000.0,
        risk_pct=0.01,
        max_order_value=5_000.0,
    )
    spy = _row_as_dict(cockpit, "SPY")
    assert spy["risk_budget_native"] == pytest.approx(0.0)
    assert spy["max_order_native"] == pytest.approx(0.0)


def test_symbol_row_no_fx_dict_defaults_usd() -> None:
    """Sans fx_rate_by_ccy (None), tout est traité comme USD (rate=1.0)."""
    bars = {"AAPL": _fake_bars(close=200.0)}
    cockpit = build_market_cockpit(
        bars,
        symbols=["AAPL"],
        prices={"AAPL": 200.0},
        fx_rate_by_ccy=None,
        equity_usd=10_000.0,
        risk_pct=0.02,
        max_order_value=2_000.0,
    )
    aapl = _row_as_dict(cockpit, "AAPL")
    assert aapl["ccy"] == "USD"
    assert aapl["fx_usd"] == pytest.approx(1.0)
    assert aapl["risk_budget_native"] == pytest.approx(200.0)  # 0.02 * 10_000 / 1.0
    assert aapl["max_order_native"] == pytest.approx(2_000.0)
