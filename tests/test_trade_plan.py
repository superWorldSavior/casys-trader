from datetime import datetime, timezone

from trader.trade_plan import TradePlan, TradePlanStore, create_trade_plan, validate_exit_plan


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
