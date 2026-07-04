from __future__ import annotations

import pytest

from trader.application.armed_plans import (
    ArmedPlanEvent,
    resolve_armed_plan_triggers,
)


def _armed_trigger(
    *,
    symbol: str = "SPY",
    watch_id: str = "SPY:abc123",
    intent: str = "OPEN_LONG",
    exit_plan: dict | None = None,
) -> dict:
    action = "BUY" if intent == "OPEN_LONG" else "SELL"
    return {
        "watch_id": watch_id,
        "symbol": symbol,
        "on_trigger": "EXECUTE_ORDER",
        "order": {
            "intent": intent,
            "action": action,
            "qty": 10.0,
            "confidence": 0.9,
            "exit_plan": exit_plan or {"hard_stop": {"type": "price", "price": 95.0}},
            "rationale": "scenario breakout",
        },
    }


def test_resolve_armed_plan_triggers_builds_decision_and_resolved_event() -> None:
    trigger = _armed_trigger(
        exit_plan={"hard_stop": {"type": "volatility_multiple", "multiple": 1.5}},
    )
    volatility_calls: list[dict] = []

    def reference_volatility(symbol, *, entry_price, cockpit, tradable_bars_by_symbol):
        volatility_calls.append(
            {
                "symbol": symbol,
                "entry_price": entry_price,
                "has_cockpit": bool(cockpit),
                "has_bars": symbol in tradable_bars_by_symbol,
            }
        )
        return 2.0

    result = resolve_armed_plan_triggers(
        indicator_triggers=[trigger],
        symbols_to_decide=["SPY"],
        prices={"SPY": 100.0},
        stale_market_data={},
        positions={},
        cockpit={"rows": []},
        tradable_bars_by_symbol={"SPY": []},
        reference_volatility_for_symbol=reference_volatility,
    )

    assert set(result.decisions) == {"SPY"}
    decision = result.decisions["SPY"]
    assert decision.action == "BUY"
    assert decision.intent == "OPEN_LONG"
    assert decision.quantity == pytest.approx(10.0)
    assert decision.decision_reason_code == "ARMED_PLAN"
    assert decision.rationale == "armed_plan:SPY:abc123 \u2014 scenario breakout"
    assert result.plan_ids == {"SPY": "SPY:abc123"}
    assert result.plan_orders["SPY"]["exit_plan"]["hard_stop"]["price"] == pytest.approx(97.0)
    assert result.reference_volatilities == {"SPY": 2.0}
    assert volatility_calls == [{"symbol": "SPY", "entry_price": 100.0, "has_cockpit": True, "has_bars": True}]
    assert len(result.events) == 1
    assert result.events[0].name == "armed_plan_resolved"
    assert result.events[0].payload["symbol"] == "SPY"
    assert result.events[0].payload["plan_id"] == "SPY:abc123"
    assert result.events[0].payload["trace"]["hard_stop"]["resolved_price"] == pytest.approx(97.0)
    assert result.events[0].payload["exit_plan"]["hard_stop"]["price"] == pytest.approx(97.0)


def test_resolve_armed_plan_triggers_marks_conflicts_for_planner_review() -> None:
    first = _armed_trigger(watch_id="SPY:abc123", intent="OPEN_LONG")
    second = _armed_trigger(watch_id="SPY:def456", intent="OPEN_SHORT")

    result = resolve_armed_plan_triggers(
        indicator_triggers=[first, second],
        symbols_to_decide=["SPY"],
        prices={"SPY": 100.0},
        stale_market_data={},
        positions={},
        cockpit={},
        tradable_bars_by_symbol={},
        reference_volatility_for_symbol=lambda *args, **kwargs: None,
    )

    assert result.decisions == {}
    assert first["armed_conflict"] is True
    assert second["armed_conflict"] is True
    assert result.events == [
        ArmedPlanEvent(
            "armed_plan_conflict",
            {"symbol": "SPY", "plan_ids": ["SPY:abc123", "SPY:def456"]},
        )
    ]
    assert result.progress_logs[0].message == (
        "[armed_plan] %s conflit (%d plans d\u00e9clench\u00e9s) \u2014 r\u00e9veil planificateur"
    )
    assert result.progress_logs[0].args == ("SPY", 2)


def test_resolve_armed_plan_triggers_records_stale_cancelled_plan() -> None:
    trigger = _armed_trigger()

    result = resolve_armed_plan_triggers(
        indicator_triggers=[trigger],
        symbols_to_decide=["SPY"],
        prices={},
        stale_market_data={"SPY": {"stale_reason": "runtime_old"}},
        positions={},
        cockpit={},
        tradable_bars_by_symbol={},
        reference_volatility_for_symbol=lambda *args, **kwargs: None,
    )

    assert result.decisions == {}
    assert trigger["armed_cancelled"] == "armed_plan_cancelled:stale"
    assert result.stale_plans == {"SPY": {"id": "SPY:abc123", "order": trigger["order"]}}
    assert result.events == [
        ArmedPlanEvent(
            "armed_plan_cancelled",
            {
                "symbol": "SPY",
                "plan_id": "SPY:abc123",
                "reason": "armed_plan_cancelled:stale",
                "exit_plan": {"hard_stop": {"type": "price", "price": 95.0}},
            },
        )
    ]
