from trader.application.order_admission import clamp_exit_quantity, invalid_intent_reason


def test_invalid_intent_reason_preserves_daemon_reason_for_buy_hold_mismatch() -> None:
    assert invalid_intent_reason(
        action="BUY",
        quantity=1.0,
        intent="HOLD",
    ) == "invalid_intent"


def test_clamp_exit_quantity_preserves_daemon_reason_without_position() -> None:
    qty, reason = clamp_exit_quantity(
        intent="CLOSE",
        action="SELL",
        quantity=10.0,
        position_quantity=0.0,
    )

    assert qty == 0.0
    assert reason == "no_position_to_reduce"
