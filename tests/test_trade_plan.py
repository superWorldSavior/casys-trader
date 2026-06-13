from datetime import datetime, timezone

import pytest

import trader.exit_engine as exit_engine
from trader.trade_plan import (
    InvalidExitPlanError,
    TradePlan,
    TradePlanStore,
    create_trade_plan,
    trade_plan_from_dict,
    validate_exit_plan,
)


def _exit_plan() -> dict:
    return {
        "hard_stop": {"type": "price", "price": 95.0},
        "take_profits": [
            {"name": "tp1", "price": 105.0, "fraction": 0.5, "after_fill": "move_stop_to_breakeven"},
            {"name": "tp2", "price": 110.0, "fraction": 0.5, "after_fill": "close"},
        ],
        "trailing_stop": {
            "enabled_after": "tp1",
            "trail_type": "price",
            "trail_value": 2.0,
        },
        "max_hold_minutes": 45,
    }


def test_create_trade_plan_normalise_exit_plan_agent() -> None:
    plan = create_trade_plan(
        symbol="SPY",
        side="LONG",
        quantity=10.0,
        entry_price=100.0,
        opened_at="2026-06-05T12:00:00+00:00",
        raw_exit_plan=_exit_plan(),
    )

    assert plan.symbol == "SPY"
    assert plan.remaining_quantity == 10.0
    assert plan.hard_stop_price == 95.0
    assert plan.take_profits[0].name == "tp1"
    assert plan.take_profits[0].quantity == 5.0
    assert plan.trailing_stop is not None
    assert plan.max_hold_minutes == 45.0
    assert plan.profit_protection is None


def test_create_trade_plan_accepte_hard_stop_prix_direct() -> None:
    plan = create_trade_plan(
        symbol="SPY",
        side="LONG",
        quantity=10.0,
        entry_price=100.0,
        opened_at="2026-06-05T12:00:00+00:00",
        raw_exit_plan={"hard_stop": 95.0},
    )

    assert plan.hard_stop_price == 95.0
    assert plan.profit_protection is None


def test_create_trade_plan_accepte_profit_protection_agent() -> None:
    plan = create_trade_plan(
        symbol="SPY",
        side="LONG",
        quantity=10.0,
        entry_price=100.0,
        opened_at="2026-06-05T12:00:00+00:00",
        raw_exit_plan={
            "hard_stop": 95.0,
            "profit_protection": {
                "arm_at_r": 0.8,
                "trigger_on_giveback_pct": 0.25,
                "close_fraction": 0.5,
                "move_stop_to": "breakeven",
                "min_hold_minutes": 15,
            },
        },
    )

    assert plan.profit_protection is not None
    assert plan.profit_protection.arm_at_r == 0.8
    assert plan.profit_protection.trigger_on_giveback_pct == 0.25
    assert plan.profit_protection.close_fraction == 0.5


def test_create_trade_plan_accepte_exit_watch_agent() -> None:
    plan = create_trade_plan(
        symbol="SPY",
        side="LONG",
        quantity=10.0,
        entry_price=100.0,
        opened_at="2026-06-05T12:00:00+00:00",
        raw_exit_plan={
            "hard_stop": 95.0,
            "exit_watch": {
                "ttl_minutes": 90,
                "cooldown_minutes": 12,
                "logic": "any",
                "on_trigger": "ORDER",
                "conditions": [
                    {"indicator": "trend_slope", "op": "<", "value": 0, "timeframe": "15m"},
                ],
            },
        },
    )

    assert plan.exit_watch is not None
    assert plan.exit_watch["symbol"] == "SPY"
    assert plan.exit_watch["logic"] == "any"
    assert plan.exit_watch["on_trigger"] == "WAKE"
    assert plan.exit_watch["cooldown_minutes"] == 12.0
    assert plan.exit_watch["conditions"][0]["indicator"] == "trend_slope"


def test_create_trade_plan_normalise_take_profits_prix_directs_et_trailing_numeric() -> None:
    plan = create_trade_plan(
        symbol="SPY",
        side="SHORT",
        quantity=10.0,
        entry_price=100.0,
        opened_at="2026-06-05T12:00:00+00:00",
        raw_exit_plan={
            "hard_stop": 103.0,
            "take_profits": [98.0, 96.0],
            "trailing_stop": 0.5,
            "max_hold_minutes": 30,
        },
    )

    assert [tp.price for tp in plan.take_profits] == [98.0, 96.0]
    assert [tp.fraction for tp in plan.take_profits] == [0.5, 0.5]
    assert [tp.quantity for tp in plan.take_profits] == [5.0, 5.0]
    assert plan.trailing_stop is not None
    assert plan.trailing_stop.trail_value == 0.5


def test_validate_exit_plan_ignore_trailing_stop_inexploitable_si_autres_sorties_valides() -> None:
    validate_exit_plan(
        {
            "hard_stop": 95.0,
            "take_profits": [{"price": 105.0, "fraction": 1.0}],
            "trailing_stop": {"enabled_after": "tp1"},
        }
    )


def test_create_trade_plan_refuse_volatility_multiple_sans_volatilite_reference() -> None:
    with pytest.raises(InvalidExitPlanError, match="trailing_volatility_unavailable"):
        create_trade_plan(
            symbol="SPY",
            side="LONG",
            quantity=10.0,
            entry_price=100.0,
            opened_at="2026-06-05T12:00:00+00:00",
            raw_exit_plan={
                "trailing_stop": {"trail_type": "volatility_multiple", "trail_value": 2.0},
            },
        )


def test_create_trade_plan_refuse_trailing_stop_non_fini() -> None:
    with pytest.raises(InvalidExitPlanError, match="trailing_stop_trail_value_must_be_finite"):
        create_trade_plan(
            symbol="SPY",
            side="LONG",
            quantity=10.0,
            entry_price=100.0,
            opened_at="2026-06-05T12:00:00+00:00",
            raw_exit_plan={
                "trailing_stop": {"trail_type": "price", "trail_value": float("inf")},
            },
        )


def test_create_trade_plan_refuse_reference_volatility_non_finie() -> None:
    with pytest.raises(InvalidExitPlanError, match="reference_volatility_must_be_finite"):
        create_trade_plan(
            symbol="SPY",
            side="LONG",
            quantity=10.0,
            entry_price=100.0,
            opened_at="2026-06-05T12:00:00+00:00",
            raw_exit_plan={
                "trailing_stop": {"trail_type": "volatility_multiple", "trail_value": 2.0},
            },
            reference_volatility=float("nan"),
        )


def test_trade_plan_from_dict_ignore_les_non_finis_au_reload() -> None:
    plan = trade_plan_from_dict(
        {
            "id": "SPY-nonfinite",
            "symbol": "SPY",
            "side": "LONG",
            "quantity": 10.0,
            "remaining_quantity": 10.0,
            "entry_price": 100.0,
            "opened_at": "2026-06-05T12:00:00+00:00",
            "reference_volatility": float("nan"),
            "trailing_stop": {
                "enabled_after": None,
                "trail_type": "price",
                "trail_value": float("inf"),
            },
        }
    )

    assert plan.reference_volatility is None
    assert plan.trailing_stop is None


def test_trade_plan_store_persiste_reference_volatility(tmp_path) -> None:
    store = TradePlanStore(tmp_path / "plans.json")
    plan = create_trade_plan(
        symbol="SPY",
        side="LONG",
        quantity=10.0,
        entry_price=100.0,
        opened_at="2026-06-05T12:00:00+00:00",
        raw_exit_plan={
            "trailing_stop": {"trail_type": "volatility_multiple", "trail_value": 2.0},
        },
        reference_volatility=1.25,
    )

    store.upsert(plan)

    reloaded = TradePlanStore(tmp_path / "plans.json").open_plans()[0]
    assert reloaded.reference_volatility == pytest.approx(1.25)
    assert reloaded == plan


def test_create_trade_plan_clampe_trailing_percent_sous_plancher_volatilite() -> None:
    plan = create_trade_plan(
        symbol="SPY",
        side="LONG",
        quantity=10.0,
        entry_price=100.0,
        opened_at="2026-06-05T12:00:00+00:00",
        raw_exit_plan={
            "trailing_stop": {"trail_type": "percent", "trail_value": 0.005},
        },
        reference_volatility=1.0,
    )

    assert plan.trailing_stop is not None
    assert plan.trailing_stop.trail_floored is True
    assert plan.trailing_stop.trail_value == pytest.approx(0.01)
    assert exit_engine._trail_amount(plan) == pytest.approx(1.0)


def test_create_trade_plan_refuse_entry_price_zero_avant_clamp_trailing_percent() -> None:
    with pytest.raises(InvalidExitPlanError, match="entry_price_must_be_positive"):
        create_trade_plan(
            symbol="SPY",
            side="LONG",
            quantity=10.0,
            entry_price=0.0,
            opened_at="2026-06-05T12:00:00+00:00",
            raw_exit_plan={
                "trailing_stop": {"trail_type": "percent", "trail_value": 0.01},
            },
            reference_volatility=1.0,
        )


def test_create_trade_plan_ne_clampe_pas_trailing_au_dessus_du_plancher() -> None:
    plan = create_trade_plan(
        symbol="SPY",
        side="LONG",
        quantity=10.0,
        entry_price=100.0,
        opened_at="2026-06-05T12:00:00+00:00",
        raw_exit_plan={
            "trailing_stop": {"trail_type": "percent", "trail_value": 0.02},
        },
        reference_volatility=1.0,
    )

    assert plan.trailing_stop is not None
    assert plan.trailing_stop.trail_floored is False
    assert plan.trailing_stop.trail_value == pytest.approx(0.02)
    assert exit_engine._trail_amount(plan) == pytest.approx(2.0)


def test_create_trade_plan_ne_clampe_pas_sans_volatilite_reference() -> None:
    plan = create_trade_plan(
        symbol="EURUSD=X",
        side="LONG",
        quantity=10.0,
        entry_price=1.10,
        opened_at="2026-06-05T12:00:00+00:00",
        raw_exit_plan={
            "trailing_stop": {"trail_type": "price", "trail_value": 0.0007},
        },
    )

    assert plan.trailing_stop is not None
    assert plan.trailing_stop.trail_floored is False
    assert plan.trailing_stop.trail_value == pytest.approx(0.0007)


def test_trade_plan_store_persiste_les_plans_ouverts(tmp_path) -> None:
    store = TradePlanStore(tmp_path / "plans.json")
    plan = create_trade_plan(
        symbol="SPY",
        side="LONG",
        quantity=10.0,
        entry_price=100.0,
        opened_at="2026-06-05T12:00:00+00:00",
        raw_exit_plan=_exit_plan(),
    )

    store.upsert(plan)

    reloaded = TradePlanStore(tmp_path / "plans.json").open_plans()
    assert reloaded == [plan]


def test_trade_plan_store_persiste_profit_protection(tmp_path) -> None:
    store = TradePlanStore(tmp_path / "plans.json")
    plan = create_trade_plan(
        symbol="SPY",
        side="LONG",
        quantity=10.0,
        entry_price=100.0,
        opened_at="2026-06-05T12:00:00+00:00",
        raw_exit_plan={"hard_stop": 95.0, "profit_protection": True},
    )

    store.upsert(plan)

    reloaded = TradePlanStore(tmp_path / "plans.json").open_plans()[0]
    assert reloaded.profit_protection == plan.profit_protection


def test_trade_plan_store_persiste_exit_watch(tmp_path) -> None:
    store = TradePlanStore(tmp_path / "plans.json")
    plan = create_trade_plan(
        symbol="SPY",
        side="LONG",
        quantity=10.0,
        entry_price=100.0,
        opened_at="2026-06-05T12:00:00+00:00",
        raw_exit_plan={
            "hard_stop": 95.0,
            "exit_watch": {
                "conditions": [{"indicator": "trend_slope", "op": "<", "value": 0}],
            },
        },
    )

    store.upsert(plan)

    reloaded = TradePlanStore(tmp_path / "plans.json").open_plans()[0]
    assert reloaded.exit_watch == plan.exit_watch


def test_trade_plan_store_cloture_un_plan(tmp_path) -> None:
    store = TradePlanStore(tmp_path / "plans.json")
    plan = TradePlan(
        id="SPY-2026",
        symbol="SPY",
        side="LONG",
        quantity=10.0,
        remaining_quantity=10.0,
        entry_price=100.0,
        opened_at=datetime(2026, 6, 5, 12, tzinfo=timezone.utc).isoformat(),
    )
    store.upsert(plan)

    store.close(plan.id)

    assert store.open_plans() == []
