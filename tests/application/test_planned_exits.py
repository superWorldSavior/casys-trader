from __future__ import annotations

from datetime import datetime, timezone

import pytest

from trader.execution.broker import Order, SimBroker
from trader.market.market_data import Bar
from trader.planning.trade_plan import TradePlanStore, create_trade_plan


def test_planned_exits_executes_take_profit_and_records_performance(tmp_path) -> None:
    from trader.application.planned_exits import apply_planned_exits

    state_dir = tmp_path / "state"
    now = datetime(2026, 6, 5, 14, 30, tzinfo=timezone.utc)
    broker = SimBroker(state_dir / "broker.json", starting_cash=100_000)
    broker.submit(Order("SPY", "BUY", 10.0), 100.0, "2026-06-05T14:00:00+00:00", dry_run=False)
    plan_store = TradePlanStore(state_dir / "trade_plans.json")
    plan_store.upsert(
        create_trade_plan(
            symbol="SPY",
            side="LONG",
            quantity=10.0,
            entry_price=100.0,
            opened_at="2026-06-05T14:00:00+00:00",
            raw_exit_plan={"take_profits": [{"name": "tp1", "price": 105.0, "fraction": 0.5}]},
            llm_confidence=0.73,
            llm_provider="codex",
            llm_model="gpt-test",
        )
    )
    performance_rows: list[dict] = []

    entries = apply_planned_exits(
        broker=broker,
        plan_store=plan_store,
        prices={"SPY": 106.0},
        bars_by_symbol={
            "SPY": [
                Bar(
                    ts=now.isoformat(),
                    open=106.0,
                    high=107.0,
                    low=105.0,
                    close=106.0,
                    volume=1000.0,
                )
            ]
        },
        bars_intervals_by_symbol={"SPY": "15m"},
        valuation_prices={"SPY": 106.0},
        now=now,
        dry_run=False,
        starting_equity=100_000,
        execution_eligibility={"SPY": {"execution": {"enabled": True}}},
        runtime_interval="15m",
        exit_check_interval="5m",
        exit_check_window_bars=3,
        append_model_performance=lambda **payload: performance_rows.append(payload),
    )

    assert len(entries) == 1
    entry = entries[0]
    assert entry["reason"] == "take_profit:tp1"
    assert entry["executed"] is True
    assert entry["quantity"] == pytest.approx(5.0)
    assert entry["plan_snapshot"]["entry_price"] == pytest.approx(100.0)
    assert broker.positions()["SPY"].quantity == pytest.approx(5.0)
    assert performance_rows[-1]["intent"] == "PLANNED_EXIT"
    assert performance_rows[-1]["source_plan_id"] == entry["plan_snapshot"]["id"]
    assert performance_rows[-1]["confidence"] == pytest.approx(0.73)
