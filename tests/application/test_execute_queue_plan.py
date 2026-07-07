from __future__ import annotations

from trader.application.execute.execute_queue_plan import build_execute_queue_plan_payload
from trader.planning.trade_plan import TradePlan, create_trade_plan_from_order


class _PlanReader:
    def __init__(self, plans: list[TradePlan] | None = None) -> None:
        self._plans = plans or []

    def open_plans(self) -> list[TradePlan]:
        return list(self._plans)


def _exit_plan() -> dict:
    return {
        "hard_stop": {"type": "price", "price": 95.0},
        "take_profits": [{"price": 110.0, "fraction": 1.0}],
    }


def _entry_context() -> dict:
    return {
        "price": 100.0,
        "runtime_interval": "15m",
        "data_age_m": 3,
        "session_open": True,
        "daily_as_of": "2026-07-05",
    }


def _payload(**overrides):
    params = {
        "plan_reader": _PlanReader(),
        "symbol": "SPY",
        "action": "BUY",
        "intent": "OPEN_LONG",
        "quantity": 2.0,
        "price": 100.0,
        "opened_at": "2026-07-05T08:00:00+00:00",
        "runtime_exit_plan": _exit_plan(),
        "reference_volatility": 1.5,
        "rationale": "breakout",
        "entry_context": _entry_context(),
        "position_quantity": 0.0,
        "position_avg_price": 0.0,
        "llm_provider": "acpx",
        "llm_model": "gpt-5.5/medium",
        "llm_fallback_reason": None,
        "llm_confidence": 0.82,
    }
    params.update(overrides)
    return build_execute_queue_plan_payload(**params)


def test_build_execute_queue_plan_payload_open_long_builds_plan_to_upsert() -> None:
    payload = _payload()

    assert payload.symbol_to_close is None
    assert payload.plan_to_upsert is not None
    plan = payload.plan_to_upsert
    assert plan["symbol"] == "SPY"
    assert plan["side"] == "LONG"
    assert plan["quantity"] == 2.0
    assert plan["remaining_quantity"] == 2.0
    assert plan["entry_price"] == 100.0
    assert plan["reference_volatility"] == 1.5
    assert plan["llm_provider"] == "acpx"
    assert plan["llm_model"] == "gpt-5.5/medium"
    assert plan["llm_confidence"] == 0.82
    assert plan["entry_thesis"] == "breakout"
    assert plan["entry_context"] == _entry_context()


def test_build_execute_queue_plan_payload_close_only_closes_existing_symbol() -> None:
    payload = _payload(intent="CLOSE", action="SELL", runtime_exit_plan=None, entry_context=None)

    assert payload.symbol_to_close == "SPY"
    assert payload.plan_to_upsert is None


def test_build_execute_queue_plan_payload_empty_exit_plan_does_not_create_plan() -> None:
    payload = _payload(runtime_exit_plan={}, entry_context=None)

    assert payload.symbol_to_close is None
    assert payload.plan_to_upsert is None


def test_build_execute_queue_plan_payload_add_projects_position_and_preserves_review() -> None:
    review = {"ts": "2026-07-05T07:55:00+00:00", "action": "HOLD"}
    previous = create_trade_plan_from_order(
        symbol="SPY",
        order_side="BUY",
        quantity=3.0,
        entry_price=90.0,
        opened_at="2026-07-05T07:00:00+00:00",
        raw_exit_plan=_exit_plan(),
        llm_provider="acpx",
        llm_model="gpt-5.5/medium",
        llm_confidence=0.7,
    )
    previous = previous.model_copy(update={"last_llm_review": review})

    payload = _payload(
        plan_reader=_PlanReader([previous]),
        intent="SCALE_IN",
        action="BUY",
        quantity=2.0,
        price=105.0,
        position_quantity=3.0,
        position_avg_price=90.0,
    )

    assert payload.symbol_to_close == "SPY"
    assert payload.plan_to_upsert is not None
    plan = payload.plan_to_upsert
    assert plan["quantity"] == 5.0
    assert plan["remaining_quantity"] == 5.0
    assert plan["entry_price"] == 96.0
    assert plan["last_llm_review"] == review


def test_build_execute_queue_plan_payload_reverse_keeps_only_opening_leg_quantity() -> None:
    payload = _payload(
        intent="FLIP",
        action="SELL",
        quantity=7.0,
        price=101.0,
        position_quantity=4.0,
        position_avg_price=99.0,
    )

    assert payload.symbol_to_close == "SPY"
    assert payload.plan_to_upsert is not None
    plan = payload.plan_to_upsert
    assert plan["side"] == "SHORT"
    assert plan["quantity"] == 3.0
    assert plan["remaining_quantity"] == 3.0
    assert plan["entry_price"] == 101.0
