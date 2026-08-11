from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from trader.agent.client import Decision
from trader.application.execute.cycle_decision import (
    DecisionExecutionContext,
    DecisionExecutionState,
    _capture_plan_effect_readback,
    execute_one_cycle_decision,
)
from trader.market.market_data import Bar
from trader.planning.trade_plan import create_trade_plan
from tests.plan_store_fakes import MemoryTradePlanStore


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


def test_llm_hold_plan_review_has_exact_post_write_readback() -> None:
    store = MemoryTradePlanStore()
    store.upsert(
        create_trade_plan(
            symbol="SPY",
            side="LONG",
            quantity=1.0,
            entry_price=100.0,
            opened_at=_NOW.isoformat(),
            raw_exit_plan={"hard_stop": 95.0},
        )
    )
    ctx, records = _context(
        plan_store=store,
        held_symbols={"SPY"},
    )
    state = DecisionExecutionState(snap=SimpleNamespace(equity=100_000.0), gross=0.0)

    execute_one_cycle_decision(
        sym="SPY",
        index=1,
        total=1,
        decision=Decision(
            symbol="SPY",
            action="HOLD",
            quantity=0.0,
            confidence=0.8,
            rationale="thesis intact",
            intent="HOLD",
            llm_provider="test-provider",
            llm_model="test-model",
        ),
        state=state,
        ctx=ctx,
    )

    assert records[0]["plan_review_recorded"] is True
    assert records[0]["plan_effect"]["status"] == "verified"
    assert records[0]["plan_effect"]["expected_mutation"]["kind"] == "last_llm_review"
    assert (
        records[0]["plan_effect"]["expected_last_llm_review"]
        == records[0]["plan_effect"]["observed_last_llm_reviews"][0]["last_llm_review"]
    )
    assert store.open_plans()[0].last_llm_review["decision_id"] == records[0]["decision_id"]


def test_llm_hold_with_exit_update_retains_both_plan_receipts() -> None:
    initial_plan = create_trade_plan(
        symbol="SPY",
        side="LONG",
        quantity=1.0,
        entry_price=100.0,
        opened_at=_NOW.isoformat(),
        raw_exit_plan={"hard_stop": 95.0},
    )

    class DropFirstUpsertStore(MemoryTradePlanStore):
        def __init__(self) -> None:
            super().__init__([initial_plan])
            self.upsert_calls = 0

        def upsert(self, plan) -> None:
            self.upsert_calls += 1
            if self.upsert_calls == 1:
                return
            super().upsert(plan)

    store = DropFirstUpsertStore()
    ctx, records = _context(plan_store=store, held_symbols={"SPY"})
    state = DecisionExecutionState(snap=SimpleNamespace(equity=100_000.0), gross=0.0)

    execute_one_cycle_decision(
        sym="SPY",
        index=1,
        total=1,
        decision=Decision(
            symbol="SPY",
            action="HOLD",
            quantity=0.0,
            confidence=0.8,
            rationale="review and tighten",
            intent="HOLD",
            llm_provider="test-provider",
            llm_model="test-model",
            exit_update={"hard_stop": 97.0},
        ),
        state=state,
        ctx=ctx,
    )

    assert store.open_plans()[0].last_llm_review is None
    assert [effect["status"] for effect in records[0]["plan_effects"]] == [
        "mismatch",
        "verified",
    ]


def test_plan_receipt_rejects_a_created_plan_absent_from_readback() -> None:
    entry = {
        "trade_plan_created": True,
        "trade_plan": {"id": "plan-1", "symbol": "SPY", "remaining_quantity": 2.0},
        "plan_receipt_expectation": {
            "kind": "upsert",
            "plan": {"id": "plan-1", "symbol": "SPY", "remaining_quantity": 2.0},
        },
    }
    plan_store = SimpleNamespace(open_plans=lambda: [])

    _capture_plan_effect_readback(entry=entry, plan_store=plan_store, symbol="SPY")

    assert entry["plan_effect"] == {
        "status": "mismatch",
        "reason": "plan_receipt_mismatch",
        "open_plans": [],
        "expected_mutation": {
            "kind": "upsert",
            "plan": {"id": "plan-1", "symbol": "SPY", "remaining_quantity": 2.0},
        },
        "expected_plan": {"id": "plan-1", "symbol": "SPY", "remaining_quantity": 2.0},
    }


def test_plan_receipt_accepts_expected_absence_for_flip_without_replacement() -> None:
    entry = {"plan_receipt_expectation": {"kind": "no_open_plans"}}
    plan_store = SimpleNamespace(open_plans=lambda: [])

    _capture_plan_effect_readback(entry=entry, plan_store=plan_store, symbol="SPY")

    assert entry["plan_effect"]["status"] == "verified"
    assert entry["plan_effect"]["expected_mutation"] == {"kind": "no_open_plans"}


def test_plan_receipt_accepts_zero_remaining_quantity_without_plan() -> None:
    entry = {"plan_receipt_expectation": {"kind": "remaining_quantity", "quantity": 0.0}}
    plan_store = SimpleNamespace(open_plans=lambda: [])

    _capture_plan_effect_readback(entry=entry, plan_store=plan_store, symbol="SPY")

    assert entry["plan_effect"]["status"] == "verified"
    assert entry["plan_effect"]["expected_remaining_quantity"] == 0.0


def test_execute_one_cycle_decision_applies_exit_update_with_current_price(tmp_path) -> None:
    records: list[dict] = []
    store = MemoryTradePlanStore()
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
    assert records[0]["plan_effect"]["status"] == "verified"
    assert records[0]["plan_effect"]["expected_mutation"]["kind"] == "upsert"
    assert records[0]["plan_effect"]["expected_plan"] == records[0]["plan_effect"]["observed_plan"]


def test_rejected_exit_update_without_open_plan_has_verified_non_effect_receipt() -> None:
    store = MemoryTradePlanStore()
    ctx, records = _context(plan_store=store)
    state = DecisionExecutionState(snap=SimpleNamespace(equity=100_000.0), gross=0.0)

    execute_one_cycle_decision(
        sym="SPY",
        index=1,
        total=1,
        decision=Decision(
            symbol="SPY",
            action="HOLD",
            quantity=0.0,
            confidence=0.8,
            rationale="tighten stop",
            intent="HOLD",
            exit_update={"hard_stop": 97.0},
        ),
        state=state,
        ctx=ctx,
    )

    assert records[0]["exit_update_applied"] is False
    assert records[0]["exit_update_reason"] == "no_open_plan"
    assert records[0]["plan_effect"]["status"] == "verified"
    assert records[0]["plan_effect"]["expected_mutation"] == {"kind": "no_open_plans"}


def test_rejected_exit_update_proves_existing_plan_unchanged() -> None:
    store = MemoryTradePlanStore()
    store.upsert(
        create_trade_plan(
            symbol="SPY",
            side="LONG",
            quantity=1.0,
            entry_price=100.0,
            opened_at=_NOW.isoformat(),
            raw_exit_plan={},
        )
    )
    ctx, records = _context(plan_store=store)
    state = DecisionExecutionState(snap=SimpleNamespace(equity=100_000.0), gross=0.0)

    execute_one_cycle_decision(
        sym="SPY",
        index=1,
        total=1,
        decision=Decision(
            symbol="SPY",
            action="HOLD",
            quantity=0.0,
            confidence=0.8,
            rationale="set target",
            intent="HOLD",
            exit_update={"take_profits": [{"type": "risk_multiple", "r": 2.0}]},
        ),
        state=state,
        ctx=ctx,
    )

    assert records[0]["exit_update_applied"] is False
    assert records[0]["exit_update_reason"].startswith("resolve_failed:")
    assert records[0]["plan_effect"]["status"] == "verified"
    assert records[0]["plan_effect"]["expected_mutation"]["kind"] == "unchanged"
    assert records[0]["plan_effect"]["expected_plans"] == records[0]["plan_effect"]["observed_plans"]
