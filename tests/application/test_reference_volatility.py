from __future__ import annotations

import math

from trader.application.reference_volatility import (
    cockpit_vol_fraction,
    feature_vol_fraction,
    positive_finite_float,
    reference_volatility_for_symbol,
)


def test_positive_finite_float_accepts_only_strictly_positive_finite_values() -> None:
    assert positive_finite_float("1.25") == 1.25
    assert positive_finite_float(0) is None
    assert positive_finite_float(-1) is None
    assert positive_finite_float(math.inf) is None
    assert positive_finite_float(math.nan) is None
    assert positive_finite_float("bad") is None


def test_cockpit_vol_fraction_prefers_daily_vol_over_intraday_vol() -> None:
    cockpit = {
        "cols": ["s", "vol", "vol_d"],
        "rows": [["SPY", 0.01, 0.08]],
    }

    assert cockpit_vol_fraction(cockpit, "SPY") == 0.08
    assert (
        reference_volatility_for_symbol(
            "SPY",
            entry_price=100.0,
            cockpit=cockpit,
            tradable_bars_by_symbol={},
        )
        == 8.0
    )


def test_cockpit_vol_fraction_falls_back_to_intraday_vol_when_daily_is_invalid() -> None:
    cockpit = {
        "cols": ["s", "vol", "vol_d"],
        "rows": [["SPY", 0.01, None]],
    }

    assert cockpit_vol_fraction(cockpit, "SPY") == 0.01


def test_cockpit_vol_fraction_keeps_first_matching_symbol_row_semantics() -> None:
    cockpit = {
        "cols": ["s", "vol", "vol_d"],
        "rows": [
            ["SPY", None, None],
            ["SPY", 0.01, 0.08],
        ],
    }

    assert cockpit_vol_fraction(cockpit, "SPY") is None


def test_feature_vol_fraction_uses_indicator_snapshot_when_cockpit_has_no_symbol() -> None:
    calls: list[dict] = []

    def build_snapshot(bars_by_symbol, *, symbols, names, window):
        calls.append(
            {
                "bars_by_symbol": bars_by_symbol,
                "symbols": symbols,
                "names": names,
                "window": window,
            }
        )
        return {"SPY": {"indicators": {"volatility": 0.02}}}

    bars = {"SPY": [object()]}

    assert (
        feature_vol_fraction(
            "SPY",
            bars,
            indicator_snapshot_builder=build_snapshot,
        )
        == 0.02
    )
    assert (
        reference_volatility_for_symbol(
            "SPY",
            entry_price=100.0,
            cockpit={},
            tradable_bars_by_symbol=bars,
            indicator_snapshot_builder=build_snapshot,
        )
        == 2.0
    )
    assert calls == [
        {
            "bars_by_symbol": bars,
            "symbols": ["SPY"],
            "names": ["volatility"],
            "window": 48,
        },
        {
            "bars_by_symbol": bars,
            "symbols": ["SPY"],
            "names": ["volatility"],
            "window": 48,
        },
    ]


def test_reference_volatility_returns_none_for_missing_or_invalid_inputs() -> None:
    assert (
        reference_volatility_for_symbol(
            "SPY",
            entry_price=0.0,
            cockpit={"cols": ["s", "vol"], "rows": [["SPY", 0.01]]},
            tradable_bars_by_symbol={},
        )
        is None
    )
    assert (
        reference_volatility_for_symbol(
            "SPY",
            entry_price=100.0,
            cockpit={"cols": ["s", "vol"], "rows": [["SPY", math.nan]]},
            tradable_bars_by_symbol={},
        )
        is None
    )
