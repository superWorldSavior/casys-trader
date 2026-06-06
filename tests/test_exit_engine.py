from datetime import datetime, timezone

from trader.exit_engine import evaluate_plan
from trader.trade_plan import create_trade_plan


def _plan():
    return create_trade_plan(
        symbol="SPY",
        side="LONG",
        quantity=10.0,
        entry_price=100.0,
        opened_at="2026-06-05T12:00:00+00:00",
        raw_exit_plan={
            "hard_stop": {"type": "price", "price": 95.0},
            "take_profits": [
                {"name": "tp1", "price": 105.0, "fraction": 0.5, "after_fill": "move_stop_to_breakeven"},
                {"name": "tp2", "price": 110.0, "fraction": 0.5, "after_fill": "close"},
            ],
            "trailing_stop": {"enabled_after": "tp1", "trail_type": "price", "trail_value": 2.0},
            "max_hold_minutes": 45,
        },
    )


def test_evaluate_plan_declenche_tp1_et_deplace_stop_a_breakeven() -> None:
    result = evaluate_plan(
        _plan(),
        price=106.0,
        now=datetime(2026, 6, 5, 12, 10, tzinfo=timezone.utc),
    )

    assert result.signal is not None
    assert result.signal.reason == "take_profit:tp1"
    assert result.signal.quantity == 5.0
    assert result.updated_plan.remaining_quantity == 5.0
    assert result.updated_plan.hard_stop_price == 100.0
    assert "tp1" in result.updated_plan.filled_take_profits


def test_evaluate_plan_after_fill_close_cloture_toute_la_quantite_restante() -> None:
    plan = create_trade_plan(
        symbol="SPY",
        side="LONG",
        quantity=10.0,
        entry_price=100.0,
        opened_at="2026-06-05T12:00:00+00:00",
        raw_exit_plan={
            "take_profits": [
                {"name": "tp1", "price": 105.0, "fraction": 0.5, "after_fill": "close"},
            ],
        },
    )

    result = evaluate_plan(
        plan,
        price=106.0,
        now=datetime(2026, 6, 5, 12, 10, tzinfo=timezone.utc),
    )

    assert result.signal is not None
    assert result.signal.reason == "take_profit:tp1"
    assert result.signal.quantity == 10.0
    assert result.updated_plan.remaining_quantity == 0.0
    assert result.close_plan is True


def test_evaluate_plan_declenche_trailing_stop_apres_tp1() -> None:
    plan = _plan()
    first = evaluate_plan(plan, price=106.0, now=datetime(2026, 6, 5, 12, 10, tzinfo=timezone.utc))
    second = evaluate_plan(
        first.updated_plan,
        price=104.0,
        now=datetime(2026, 6, 5, 12, 15, tzinfo=timezone.utc),
    )

    assert second.signal is not None
    assert second.signal.reason == "trailing_stop"
    assert second.signal.quantity == 5.0
    assert second.updated_plan.remaining_quantity == 0.0
    assert second.close_plan is True


def test_evaluate_plan_declenche_max_hold() -> None:
    result = evaluate_plan(
        _plan(),
        price=101.0,
        now=datetime(2026, 6, 5, 12, 46, tzinfo=timezone.utc),
    )

    assert result.signal is not None
    assert result.signal.reason == "max_hold"
    assert result.signal.quantity == 10.0
    assert result.close_plan is True


def test_evaluate_plan_protege_un_short_apres_gain_puis_giveback() -> None:
    plan = create_trade_plan(
        symbol="NVDA",
        side="SHORT",
        quantity=48.0,
        entry_price=207.74,
        opened_at="2026-06-05T17:51:12+00:00",
        raw_exit_plan={
            "hard_stop": 213.50,
            "take_profits": [{"name": "tp1", "price": 201.0, "fraction": 0.5}],
            "profit_protection": True,
        },
    )

    armed = evaluate_plan(
        plan,
        price=204.74,
        now=datetime(2026, 6, 5, 18, 10, tzinfo=timezone.utc),
    )
    protected = evaluate_plan(
        armed.updated_plan,
        price=205.96,
        now=datetime(2026, 6, 5, 18, 20, tzinfo=timezone.utc),
    )

    assert armed.signal is None
    assert protected.signal is not None
    assert protected.signal.reason == "profit_protection"
    assert protected.signal.side == "BUY"
    assert protected.signal.quantity == 16.0
    assert protected.updated_plan.remaining_quantity == 32.0
    assert protected.updated_plan.hard_stop_price == 207.74
    assert protected.updated_plan.profit_protection is not None
    assert protected.updated_plan.profit_protection.triggered is True


def test_evaluate_plan_ne_protege_pas_deux_fois() -> None:
    plan = create_trade_plan(
        symbol="SPY",
        side="LONG",
        quantity=9.0,
        entry_price=100.0,
        opened_at="2026-06-05T12:00:00+00:00",
        raw_exit_plan={
            "hard_stop": 95.0,
            "take_profits": [{"price": 110.0, "fraction": 1.0}],
            "profit_protection": True,
        },
    )

    armed = evaluate_plan(plan, price=103.0, now=datetime(2026, 6, 5, 12, 20, tzinfo=timezone.utc))
    protected = evaluate_plan(armed.updated_plan, price=101.5, now=datetime(2026, 6, 5, 12, 25, tzinfo=timezone.utc))
    again = evaluate_plan(protected.updated_plan, price=101.0, now=datetime(2026, 6, 5, 12, 30, tzinfo=timezone.utc))

    assert protected.signal is not None
    assert protected.signal.reason == "profit_protection"
    assert protected.signal.quantity == 3.0
    assert again.signal is None


def test_create_plan_ne_met_pas_protection_defaut_si_trailing_existe() -> None:
    assert _plan().profit_protection is None


def test_create_plan_ne_met_pas_protection_defaut_sans_demande_agent() -> None:
    plan = create_trade_plan(
        symbol="SPY",
        side="LONG",
        quantity=10.0,
        entry_price=100.0,
        opened_at="2026-06-05T12:00:00+00:00",
        raw_exit_plan={"hard_stop": 95.0},
    )

    assert plan.profit_protection is None
