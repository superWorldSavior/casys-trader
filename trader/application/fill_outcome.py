"""Post-fill accounting helpers for executed agent decisions."""

from __future__ import annotations

from trader.execution.contracts import Fill


def llm_exit_reason_for_intent(intent: str | None) -> str | None:
    """Deterministic label for exits requested by the LLM."""
    return "llm_exit" if intent in {"CLOSE", "REDUCE", "REVERSE"} else None


def apply_fill_accounting(
    entry: dict,
    *,
    fill: Fill,
    symbol: str,
    action: str,
    intent: str | None,
    quantity: float,
    price: float,
    confidence: float,
    llm_provider: str | None,
    llm_model: str | None,
    llm_fallback_reason: str | None,
    equity: float,
    cash: float,
    position_quantity: float,
) -> dict:
    """Return model-performance payload and enrich the runtime decision entry."""
    payload = {
        "ts": fill.ts,
        "symbol": symbol,
        "action": action,
        "intent": intent,
        "quantity": quantity,
        "price": price,
        "commission": fill.commission,
        "commission_currency": fill.commission_currency,
        "commission_model": fill.commission_model,
        "fx_rate": fill.fx_rate,
        "confidence": confidence,
        "llm_provider": llm_provider or "unknown",
        "llm_model": llm_model or "unknown",
        "llm_fallback_reason": llm_fallback_reason,
        "equity": equity,
        "cash": cash,
        "position_quantity": position_quantity,
    }
    exit_reason = llm_exit_reason_for_intent(intent)
    if exit_reason is not None:
        payload["exit_reason"] = exit_reason

    entry["model_performance_logged"] = True
    entry["commission"] = fill.commission
    entry["commission_currency"] = fill.commission_currency
    entry["commission_model"] = fill.commission_model
    entry["fx_rate"] = fill.fx_rate
    return payload
