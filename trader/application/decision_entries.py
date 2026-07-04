"""Pure builders for runtime decision entries."""

from __future__ import annotations

from typing import Any

from trader.agent_protocol.types import Decision


INFRA_HOLD_REASONS = frozenset(
    {
        "no_decision_in_batch",
        "model_call_budget_exhausted",
        "model_call_budget_exhausted_after_context",
    }
)

DOMAIN_NOOP_HOLD_REASONS = frozenset({"nothing_to_close", "add_without_position"})


def runtime_tool_audit_fields(domain_tools: dict | None) -> dict:
    tools = domain_tools if isinstance(domain_tools, dict) else {}
    fields = {
        "tool_rounds": tools.get("tool_rounds"),
        "tool_calls": tools.get("tool_calls"),
    }
    normalizations = tools.get("normalizations")
    if normalizations is not None:
        fields["tool_normalizations"] = normalizations
    return fields


def decision_source_for(*, decision: Decision, armed_plan_id: str | None) -> str:
    if armed_plan_id is not None:
        return "armed_plan"
    if decision.llm_provider or decision.llm_model:
        return "llm"
    return "infra"


def build_decision_entry(
    *,
    symbol: str,
    decision: Decision,
    effective_quantity: float,
    next_wake_in_minutes: float | None,
    next_wake_event_iso: str | None,
    armed_plan_id: str | None,
    armed_plan_order: dict | None,
    runtime_data_source: object,
) -> dict[str, Any]:
    decision_source = decision_source_for(decision=decision, armed_plan_id=armed_plan_id)
    return {
        "symbol": symbol,
        "action": decision.action,
        "qty": effective_quantity,
        **({"armed_plan_id": armed_plan_id} if armed_plan_id is not None else {}),
        **({"armed_plan_order": armed_plan_order} if armed_plan_order is not None else {}),
        "confidence": decision.confidence,
        "rationale": decision.rationale,
        "next_wake_in_minutes": next_wake_in_minutes,
        "next_wake_requested": decision.next_wake_in_minutes,
        "next_wake_event": decision.next_wake_event,
        "next_wake_event_iso": next_wake_event_iso,
        "context_request": decision.context_request,
        "intent": decision.intent,
        "decision_reason_code": decision.decision_reason_code,
        "decision_source": decision_source,
        "model_called": decision_source == "llm",
        "llm_provider": decision.llm_provider,
        "llm_model": decision.llm_model,
        "llm_fallback_reason": decision.llm_fallback_reason,
        "llm_error": decision.llm_error,
        "learning": decision.learning,
        "thesis": decision.thesis,
        "risk_pct_target": decision.risk_pct_target,
        "trade_plan_created": False,
        "indicator_watch_created": False,
        "indicator_watch_requested": bool(decision.indicator_watch),
        "indicator_watch_rejections": [],
        "data_source": runtime_data_source,
        **runtime_tool_audit_fields(decision.domain_tools),
    }


def hold_reason_for_decision(*, decision_source: str, rationale: str | None) -> str:
    rationale_text = str(rationale or "")
    if decision_source == "infra" and rationale_text in INFRA_HOLD_REASONS:
        return rationale_text
    if rationale_text in DOMAIN_NOOP_HOLD_REASONS:
        return rationale_text
    return "hold"
