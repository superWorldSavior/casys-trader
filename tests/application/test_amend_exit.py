from __future__ import annotations

import pytest

from trader.application.amend_exit import apply_amend_exit_to_open_plan
from trader.planning.trade_plan import TradePlan, TradePlanStore, create_trade_plan


def _plan(
    symbol: str = "SPY",
    *,
    hard_stop_price: float | None = 95.0,
) -> TradePlan:
    raw_exit = None if hard_stop_price is None else {"hard_stop": hard_stop_price}
    return create_trade_plan(
        symbol=symbol,
        side="LONG",
        quantity=10.0,
        entry_price=100.0,
        opened_at="2026-07-03T10:00:00+00:00",
        raw_exit_plan=raw_exit,
    )


def _store_with_plan(tmp_path, plan: TradePlan) -> TradePlanStore:
    store = TradePlanStore(tmp_path / "trade_plans.json")
    store.upsert(plan)
    return store


def test_apply_amend_exit_to_open_plan_records_no_open_plan(tmp_path) -> None:
    store = _store_with_plan(tmp_path, _plan("AAPL"))
    entry: dict = {}

    apply_amend_exit_to_open_plan(
        plan_store=store,
        symbol="SPY",
        amend_exit={"hard_stop": 97.0},
        bars=None,
        entry=entry,
    )

    assert entry == {
        "amend_exit": {"hard_stop": 97.0},
        "amend_exit_applied": False,
        "amend_exit_reason": "no_open_plan",
    }
    assert store.open_plans()[0].symbol == "AAPL"


def test_apply_amend_exit_to_open_plan_patches_store(tmp_path) -> None:
    store = _store_with_plan(tmp_path, _plan("SPY", hard_stop_price=95.0))
    entry: dict = {}

    apply_amend_exit_to_open_plan(
        plan_store=store,
        symbol="SPY",
        amend_exit={"hard_stop": 97.5},
        bars=None,
        entry=entry,
    )

    assert entry["amend_exit_applied"] is True
    assert entry["amend_exit"] == {"hard_stop": 97.5}
    assert "amend_exit_reason" not in entry
    assert store.open_plans()[0].hard_stop_price == pytest.approx(97.5)


def test_apply_amend_exit_to_open_plan_records_resolve_failed(tmp_path) -> None:
    store = _store_with_plan(tmp_path, _plan("SPY", hard_stop_price=None))
    entry: dict = {}

    apply_amend_exit_to_open_plan(
        plan_store=store,
        symbol="SPY",
        amend_exit={"take_profits": [{"type": "risk_multiple", "r": 2.0}]},
        bars=None,
        entry=entry,
    )

    assert entry["amend_exit_applied"] is False
    assert entry["amend_exit_reason"].startswith("resolve_failed:")
    assert store.open_plans()[0].hard_stop_price is None


def test_apply_amend_exit_to_open_plan_records_empty_amend(tmp_path) -> None:
    store = _store_with_plan(tmp_path, _plan("SPY", hard_stop_price=95.0))
    entry: dict = {}

    apply_amend_exit_to_open_plan(
        plan_store=store,
        symbol="SPY",
        amend_exit={},
        bars=None,
        entry=entry,
    )

    assert entry == {
        "amend_exit": {},
        "amend_exit_applied": False,
        "amend_exit_reason": "empty_amend",
    }
    assert store.open_plans()[0].hard_stop_price == pytest.approx(95.0)
