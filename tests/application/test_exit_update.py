from __future__ import annotations

import pytest

from trader.application import exit_update as exit_update_service
from trader.application.exit_update import apply_exit_update_to_open_plan
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


def _validate_without_store_write(
    store: TradePlanStore,
    *,
    symbol: str,
    exit_update: dict,
    bars: list | None,
    current_price: float | None = None,
):
    before = store.open_plans()
    result = exit_update_service.validate_exit_update(
        plan_store=store,
        symbol=symbol,
        exit_update=exit_update,
        bars=bars,
        current_price=current_price,
    )
    assert store.open_plans() == before
    return result


def test_apply_exit_update_to_open_plan_records_no_open_plan(tmp_path) -> None:
    store = _store_with_plan(tmp_path, _plan("AAPL"))
    entry: dict = {}

    result = apply_exit_update_to_open_plan(
        plan_store=store,
        symbol="SPY",
        exit_update={"hard_stop": 97.0},
        bars=None,
        entry=entry,
    )

    assert result is not None
    assert result.applied is False
    assert result.reason == "no_open_plan"
    assert result.trace == {}
    assert result.warnings == []
    assert entry == {
        "exit_update": {"hard_stop": 97.0},
        "exit_update_applied": False,
        "exit_update_reason": "no_open_plan",
    }
    assert store.open_plans()[0].symbol == "AAPL"


def test_apply_exit_update_to_open_plan_patches_store(tmp_path) -> None:
    store = _store_with_plan(tmp_path, _plan("SPY", hard_stop_price=95.0))
    entry: dict = {}

    result = apply_exit_update_to_open_plan(
        plan_store=store,
        symbol="SPY",
        exit_update={"hard_stop": 97.5},
        bars=None,
        entry=entry,
    )

    assert result is not None
    assert result.applied is True
    assert result.reason is None
    assert result.trace == entry["exit_update_trace"]
    assert result.trace["hard_stop"]["resolved_price"] == pytest.approx(97.5)
    assert result.warnings == []
    assert entry["exit_update_applied"] is True
    assert entry["exit_update"] == {"hard_stop": 97.5}
    assert "exit_update_reason" not in entry
    assert store.open_plans()[0].hard_stop_price == pytest.approx(97.5)


def test_apply_exit_update_to_open_plan_applies_hard_stop_hors_borne_with_warning(tmp_path) -> None:
    store = _store_with_plan(tmp_path, _plan("SPY", hard_stop_price=95.0))
    entry: dict = {}
    exit_update = {
        "hard_stop": {
            "type": "structural",
            "anchor": "swing_low",
            "window": 1,
            "max_pct": 0.08,
        }
    }

    result = apply_exit_update_to_open_plan(
        plan_store=store,
        symbol="SPY",
        exit_update=exit_update,
        bars=[{"ts": "t1", "open": 100.0, "high": 101.0, "low": 80.0, "close": 100.0, "volume": 1000.0}],
        entry=entry,
    )

    assert result is not None
    assert result.applied is True
    assert result.reason is None
    assert result.trace["hard_stop"]["warnings"] == result.warnings
    assert entry["exit_update_applied"] is True
    assert "exit_update_reason" not in entry
    assert store.open_plans()[0].hard_stop_price == pytest.approx(80.0)
    assert entry["exit_update_warnings"] == [
        {
            "code": "hard_stop_above_max_pct",
            "field": "max_pct",
            "distance": 20.0,
            "distance_pct": 0.2,
            "limit_distance": 8.0,
            "limit_pct": 0.08,
        }
    ]
    assert entry["exit_update_trace"]["hard_stop"]["warnings"] == entry["exit_update_warnings"]
    assert result.warnings == entry["exit_update_warnings"]


def test_apply_exit_update_to_open_plan_records_resolve_failed(tmp_path) -> None:
    store = _store_with_plan(tmp_path, _plan("SPY", hard_stop_price=None))
    entry: dict = {}

    result = apply_exit_update_to_open_plan(
        plan_store=store,
        symbol="SPY",
        exit_update={"take_profits": [{"type": "risk_multiple", "r": 2.0}]},
        bars=None,
        entry=entry,
    )

    assert result is not None
    assert result.applied is False
    assert result.reason is not None
    assert result.reason.startswith("resolve_failed:")
    assert result.trace == {}
    assert result.warnings == []
    assert entry["exit_update_applied"] is False
    assert entry["exit_update_reason"].startswith("resolve_failed:")
    assert store.open_plans()[0].hard_stop_price is None


def test_apply_exit_update_to_open_plan_records_empty_update(tmp_path) -> None:
    store = _store_with_plan(tmp_path, _plan("SPY", hard_stop_price=95.0))
    entry: dict = {}

    result = apply_exit_update_to_open_plan(
        plan_store=store,
        symbol="SPY",
        exit_update={},
        bars=None,
        entry=entry,
    )

    assert result is not None
    assert result.applied is False
    assert result.reason == "empty_update"
    assert result.trace == {}
    assert result.warnings == []
    assert entry == {
        "exit_update": {},
        "exit_update_applied": False,
        "exit_update_reason": "empty_update",
    }
    assert store.open_plans()[0].hard_stop_price == pytest.approx(95.0)


def test_validate_exit_update_records_no_open_plan_without_store_write(tmp_path) -> None:
    store = _store_with_plan(tmp_path, _plan("AAPL"))

    result = _validate_without_store_write(
        store,
        symbol="SPY",
        exit_update={"hard_stop": 97.0},
        bars=None,
    )

    assert result.would_apply is False
    assert result.reason == "no_open_plan"
    assert result.warnings == []


def test_validate_exit_update_would_patch_store_without_store_write(tmp_path) -> None:
    store = _store_with_plan(tmp_path, _plan("SPY", hard_stop_price=95.0))

    result = _validate_without_store_write(
        store,
        symbol="SPY",
        exit_update={"hard_stop": 97.5},
        bars=None,
    )

    assert result.would_apply is True
    assert result.reason is None
    assert result.warnings == []
    assert store.open_plans()[0].hard_stop_price == pytest.approx(95.0)


def test_validate_exit_update_returns_warnings_without_store_write(tmp_path) -> None:
    store = _store_with_plan(tmp_path, _plan("SPY", hard_stop_price=95.0))
    exit_update = {
        "hard_stop": {
            "type": "structural",
            "anchor": "swing_low",
            "window": 1,
            "max_pct": 0.08,
        }
    }

    result = _validate_without_store_write(
        store,
        symbol="SPY",
        exit_update=exit_update,
        bars=[{"ts": "t1", "open": 100.0, "high": 101.0, "low": 80.0, "close": 100.0, "volume": 1000.0}],
    )

    assert result.would_apply is True
    assert result.reason is None
    assert result.warnings == [
        {
            "code": "hard_stop_above_max_pct",
            "field": "max_pct",
            "distance": 20.0,
            "distance_pct": 0.2,
            "limit_distance": 8.0,
            "limit_pct": 0.08,
        }
    ]
    assert store.open_plans()[0].hard_stop_price == pytest.approx(95.0)


def test_validate_exit_update_records_resolve_failed_without_store_write(tmp_path) -> None:
    store = _store_with_plan(tmp_path, _plan("SPY", hard_stop_price=None))

    result = _validate_without_store_write(
        store,
        symbol="SPY",
        exit_update={"take_profits": [{"type": "risk_multiple", "r": 2.0}]},
        bars=None,
    )

    assert result.would_apply is False
    assert result.reason is not None
    assert result.reason.startswith("resolve_failed:")
    assert result.warnings == []
    assert store.open_plans()[0].hard_stop_price is None


def test_validate_exit_update_records_empty_update_without_store_write(tmp_path) -> None:
    store = _store_with_plan(tmp_path, _plan("SPY", hard_stop_price=95.0))

    result = _validate_without_store_write(
        store,
        symbol="SPY",
        exit_update={},
        bars=None,
    )

    assert result.would_apply is False
    assert result.reason == "empty_update"
    assert result.warnings == []
    assert store.open_plans()[0].hard_stop_price == pytest.approx(95.0)
