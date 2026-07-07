from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from trader.agent.client import Decision
from trader.application.cycle_decision import (
    DecisionExecutionContext,
    DecisionExecutionState,
    execute_one_cycle_decision,
)
from trader.market.market_data import Bar
from trader.planning.trade_plan import TradePlanStore, create_trade_plan


_NOW = datetime(2026, 6, 15, 14, 30, tzinfo=timezone.utc)


def _context(**overrides):
    records: list[dict] = overrides.pop("records", [])
    base = dict(
        now=_NOW,
        min_wake_minutes=None,
        max_wake_minutes=None,
        macro_next=None,
        broker=SimpleNamespace(positions=lambda: {}),
        plan_store=SimpleNamespace(),
        gate=SimpleNamespace(),
        sched=None,
        prices={"SPY": 100.0},
        execution_eligibility={},
        tradable_bars_by_symbol={},
        data_age_by_symbol={},
        runtime_data_source_by_sym={},
        armed_plan_ids={},
        armed_plan_orders={},
        armed_reference_volatilities={},
        held_symbols=set(),
        cockpit={},
        runtime_interval="15m",
        starting_equity=100_000.0,
        require_hard_stop=True,
        dry_run=True,
        queue_execute_enabled=False,
        execute_ledger=None,
        record_decision=records.append,
        rate_for_symbol=lambda _symbol: 1.0,
    )
    base.update(overrides)
    return DecisionExecutionContext(**base), records


def test_execute_one_cycle_decision_records_hold_without_mutating_state() -> None:
    snap = SimpleNamespace(equity=100_000.0)
    state = DecisionExecutionState(snap=snap, gross=1234.0)
    ctx, records = _context()

    new_state = execute_one_cycle_decision(
        sym="SPY",
        index=1,
        total=1,
        decision=Decision.hold("SPY", "no_decision_in_batch"),
        state=state,
        ctx=ctx,
    )

    assert new_state is state
    assert new_state.snap is snap
    assert new_state.gross == 1234.0
    assert len(records) == 1
    assert records[0]["symbol"] == "SPY"
    assert records[0]["executed"] is False
    assert records[0]["reason"] == "no_decision_in_batch"


def test_execute_one_cycle_decision_applies_exit_update_with_current_price(tmp_path) -> None:
    records: list[dict] = []
    store = TradePlanStore(tmp_path / "plans.json")
    store.upsert(
        create_trade_plan(
            symbol="INGA.AS",
            side="LONG",
            quantity=45.0,
            entry_price=27.450000762939453,
            opened_at="2026-07-01T09:33:53.439732+00:00",
            raw_exit_plan={"hard_stop": 28.24},
        )
    )
    current_price = 28.495
    ctx, records = _context(
        records=records,
        plan_store=store,
        prices={"INGA.AS": current_price},
        held_symbols={"INGA.AS"},
        tradable_bars_by_symbol={
            "INGA.AS": [
                Bar(
                    ts="2026-07-06T12:45:00+00:00",
                    open=28.45,
                    high=28.55,
                    low=28.065,
                    close=current_price,
                    volume=1000.0,
                )
            ]
        },
    )
    state = DecisionExecutionState(snap=SimpleNamespace(equity=100_000.0), gross=0.0)

    execute_one_cycle_decision(
        sym="INGA.AS",
        index=1,
        total=1,
        decision=Decision(
            symbol="INGA.AS",
            action="HOLD",
            quantity=0.0,
            confidence=0.84,
            rationale="protect runner",
            intent="HOLD",
            exit_update={
                "hard_stop": {
                    "type": "structural",
                    "anchor": "swing_low",
                    "window": 48,
                    "buffer_pct": 0.002,
                }
            },
        ),
        state=state,
        ctx=ctx,
    )

    assert records[0]["price"] == pytest.approx(current_price)
    assert records[0]["exit_update_applied"] is True
    assert "exit_update_reason" not in records[0]
    assert store.open_plans()[0].hard_stop_price == pytest.approx(28.065 - 27.450000762939453 * 0.002)
