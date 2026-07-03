from trader.application.order_admission import clamp_exit_quantity, invalid_intent_reason, VALID_INTENTS, ACTION_INTENTS


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


# ---------------------------------------------------------------------------
# L4 — ADD dans VALID_INTENTS / ACTION_INTENTS
# ---------------------------------------------------------------------------

def test_add_est_dans_valid_intents() -> None:
    """L4 : ADD doit être reconnu comme un intent valide."""
    assert "ADD" in VALID_INTENTS


def test_add_est_dans_action_intents_buy() -> None:
    """L4 : ADD est valide pour BUY (renforcement long)."""
    assert "ADD" in ACTION_INTENTS["BUY"]


def test_add_est_dans_action_intents_sell() -> None:
    """L4 : ADD est valide pour SELL (renforcement short)."""
    assert "ADD" in ACTION_INTENTS["SELL"]


def test_invalid_intent_reason_accepte_add_buy() -> None:
    """L4 : ADD + BUY après résolution de position → pas d'invalid_intent."""
    assert invalid_intent_reason(action="BUY", quantity=5.0, intent="ADD") is None


def test_invalid_intent_reason_accepte_add_sell() -> None:
    """L4 : ADD + SELL après résolution de position → pas d'invalid_intent."""
    assert invalid_intent_reason(action="SELL", quantity=3.0, intent="ADD") is None
