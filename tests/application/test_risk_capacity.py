from __future__ import annotations

from trader.execution.broker import Order, SimBroker
from trader.execution.risk import RiskLimits


def test_risk_capacity_context_exposes_remaining_gross_as_native_quantity(tmp_path) -> None:
    from trader.application import risk_capacity

    broker = SimBroker(tmp_path / "broker.json", starting_cash=100_000.0)
    broker.submit(
        Order("SPY", "BUY", 925.0),
        100.0,
        "2026-06-29T04:00:00+00:00",
        dry_run=False,
        fx_rate=1.0,
    )
    prices = {"SPY": 100.0, "2892.TW": 32.95}
    rates = {"SPY": 1.0, "2892.TW": 0.031}
    limits = RiskLimits(
        max_order_value=10_000.0,
        max_risk_per_trade_pct=0.01,
        max_position_value=30_000.0,
        max_gross_exposure=100_000.0,
        max_orders_per_cycle=5,
        min_equity=50_000.0,
    )

    gross = risk_capacity.gross_exposure(broker, prices, rate_of=rates.__getitem__)
    context = risk_capacity.risk_capacity_context(
        symbols=["2892.TW"],
        prices=prices,
        broker=broker,
        gross_exposure=gross,
        limits=limits,
        equity=98_700.0,
        rate_of=rates.__getitem__,
        currency_of=lambda symbol: "TWD" if symbol.endswith(".TW") else "USD",
    )

    expected_remaining_usd = 7_500.0
    expected_qty = expected_remaining_usd / (32.95 * 0.031)
    per_symbol = context["per_symbol"]["2892.TW"]
    assert context["gross_remaining_usd"] == expected_remaining_usd
    assert per_symbol["ccy"] == "TWD"
    assert per_symbol["max_buy_qty"] == expected_qty
    assert per_symbol["max_buy_notional_native"] == expected_remaining_usd / 0.031


def test_gross_exposure_keeps_legacy_native_mode_without_rate(tmp_path) -> None:
    from trader.application import risk_capacity

    broker = SimBroker(tmp_path / "broker.json", starting_cash=100_000.0)
    broker.submit(
        Order("2379.TW", "BUY", 100.0),
        800.0,
        "2026-06-24T09:00:00+00:00",
        dry_run=False,
    )

    assert risk_capacity.gross_exposure(broker, {"2379.TW": 820.0}) == 82_000.0
