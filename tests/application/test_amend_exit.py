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


def test_apply_amend_exit_to_open_plan_applies_hard_stop_hors_borne_with_warning(tmp_path) -> None:
    store = _store_with_plan(tmp_path, _plan("SPY", hard_stop_price=95.0))
    entry: dict = {}
    amend_exit = {
        "hard_stop": {
            "type": "structural",
            "anchor": "swing_low",
            "window": 1,
            "max_pct": 0.08,
        }
    }

    apply_amend_exit_to_open_plan(
        plan_store=store,
        symbol="SPY",
        amend_exit=amend_exit,
        bars=[{"ts": "t1", "open": 100.0, "high": 101.0, "low": 80.0, "close": 100.0, "volume": 1000.0}],
        entry=entry,
    )

    assert entry["amend_exit_applied"] is True
    assert "amend_exit_reason" not in entry
    assert store.open_plans()[0].hard_stop_price == pytest.approx(80.0)
    assert entry["amend_exit_warnings"] == [
        {
            "code": "hard_stop_above_max_pct",
            "field": "max_pct",
            "distance": 20.0,
            "distance_pct": 0.2,
            "limit_distance": 8.0,
            "limit_pct": 0.08,
        }
    ]
    assert entry["amend_exit_trace"]["hard_stop"]["warnings"] == entry["amend_exit_warnings"]


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
