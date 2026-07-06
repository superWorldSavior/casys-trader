"""Post-fill accounting helpers for executed agent decisions."""

from __future__ import annotations

from dataclasses import dataclass

from trader.execution.contracts import Fill


@dataclass(frozen=True)
class FillAccounting:
    model_performance: dict
    entry_updates: dict


def llm_exit_reason_for_intent(intent: str | None) -> str | None:
    """Deterministic label for exits requested by the LLM."""
    return "llm_exit" if intent in {"CLOSE", "REDUCE", "FLIP"} else None


def build_fill_accounting(
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
) -> FillAccounting:
    """Return model-performance payload and decision-entry updates."""
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

    return FillAccounting(
        model_performance=payload,
        entry_updates={
            "model_performance_logged": True,
            "commission": fill.commission,
            "commission_currency": fill.commission_currency,
            "commission_model": fill.commission_model,
            "fx_rate": fill.fx_rate,
        },
    )
