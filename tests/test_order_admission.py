from trader.application.order_admission import clamp_exit_quantity, invalid_intent_reason
from trader.codex_client import Decision


def test_invalid_intent_reason_preserves_daemon_reason_for_buy_hold_mismatch() -> None:
    decision = Decision(
        symbol="SPY",
        action="BUY",
        quantity=1.0,
        confidence=0.8,
        rationale="x",
        intent="HOLD",
    )

    assert invalid_intent_reason(decision) == "invalid_intent"


def test_clamp_exit_quantity_preserves_daemon_reason_without_position() -> None:
    qty, reason = clamp_exit_quantity(
        intent="CLOSE",
        action="SELL",
        quantity=10.0,
        position_quantity=0.0,
    )

    assert qty == 0.0
    assert reason == "no_position_to_reduce"
