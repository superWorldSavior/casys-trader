"""Pure benchmark semantics shared by reporting and delayed learnings."""

from __future__ import annotations

import math
from typing import Any, Mapping

from trader.domain import decision_reason

BENCHMARK_SEMANTICS_VERSION = 3

_MACHINE_SOURCES = {"infra", "infra_hold"}
_MACHINE_REASONS = {
    "quiet_gate",
    "stale_market_data",
    "no_decision_in_batch",
    "model_call_budget_exhausted",
    "model_call_budget_exhausted_after_context",
}
_NON_EXECUTABLE_HOLD_REASONS = {"DATA_STALE", "MARKET_CLOSED"}
_POSITION_EPSILON = 1e-9


def _directional_action(value: Any) -> str | None:
    text = str(value or "").strip().upper()
    if text in {"BUY", "LONG", "OPEN_LONG"}:
        return "BUY"
    if text in {"SELL", "SHORT", "OPEN_SHORT"}:
        return "SELL"
    return None


def _directional_field(mapping: Mapping[str, Any]) -> str | None:
    """Return the first usable direction instead of trusting a truthy bad field."""

    for key in ("action", "intent", "direction", "side"):
        action = _directional_action(mapping.get(key))
        if action is not None:
            return action
    return None


def _row_value(row: Mapping[str, Any], key: str) -> Any:
    """Read a canonical ledger field, with the preserved decision as fallback."""

    value = row.get(key)
    if value is not None:
        return value
    decision = row.get("decision")
    return decision.get(key) if isinstance(decision, Mapping) else None


def _has_llm_error(row: Mapping[str, Any]) -> bool:
    value = _row_value(row, "llm_error")
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    return bool(value)


def _position_action(row: Mapping[str, Any]) -> str | None:
    snapshot = row.get("portfolio_snapshot")
    if not isinstance(snapshot, Mapping):
        return None
    holdings = snapshot.get("holdings")
    if not isinstance(holdings, list):
        return None

    symbol = str(row.get("symbol") or "").strip().upper()
    quantity = 0.0
    found = False
    for holding in holdings:
        if not isinstance(holding, Mapping):
            continue
        if str(holding.get("symbol") or "").strip().upper() != symbol:
            continue
        try:
            parsed_quantity = float(holding.get("quantity"))
        except (TypeError, ValueError):
            return None
        if not math.isfinite(parsed_quantity):
            return None
        quantity += parsed_quantity
        found = True
    if not found:
        return None
    if quantity > _POSITION_EPSILON:
        return "BUY"
    if quantity < -_POSITION_EPSILON:
        return "SELL"
    return None


def _conditional_order_action(row: Mapping[str, Any]) -> str | None:
    runtime = row.get("runtime")
    if isinstance(runtime, Mapping):
        for key in ("indicator_watch_order", "armed_plan_order"):
            order = runtime.get(key)
            if isinstance(order, Mapping):
                action = _directional_field(order)
                if action is not None:
                    return action
    decision = row.get("decision")
    if isinstance(decision, Mapping):
        watch = decision.get("indicator_watch")
        if isinstance(watch, Mapping) and isinstance(watch.get("order"), Mapping):
            return _directional_field(watch["order"])
    return None


def _declared_hold_action(row: Mapping[str, Any]) -> str | None:
    for key in ("opportunity_side", "intended_side", "intended_action", "hold_direction"):
        action = _directional_action(row.get(key))
        if action is not None:
            return action
    action = _directional_action(row.get("intent"))
    if action is not None:
        return action
    decision = row.get("decision")
    if isinstance(decision, Mapping):
        for key in (
            "opportunity_side",
            "intended_side",
            "intended_action",
            "hold_direction",
            "intent",
        ):
            action = _directional_action(decision.get(key))
            if action is not None:
                return action
    return None


def decision_benchmark_context(row: Mapping[str, Any]) -> dict[str, str | None]:
    """Resolve the exposure actually benchmarked without parsing free-form rationale."""

    action = str(row.get("action") or "").strip().upper()
    decision_source = str(_row_value(row, "decision_source") or "").strip().lower()
    reason = str(_row_value(row, "reason") or "").strip().lower()
    if (
        decision_source in _MACHINE_SOURCES
        or reason in _MACHINE_REASONS
        or _has_llm_error(row)
    ):
        return {
            "benchmark_basis": "infra",
            "effective_action": None,
            "intended_action": None,
            "scoreability_reason": "not_agent_decision",
        }
    if action in {"BUY", "SELL"}:
        return {
            "benchmark_basis": "direct_action",
            "effective_action": action,
            "intended_action": action,
            "scoreability_reason": None,
        }
    if action != "HOLD":
        return {
            "benchmark_basis": "unsupported_action",
            "effective_action": None,
            "intended_action": None,
            "scoreability_reason": "unsupported_action",
        }

    reason_code = decision_reason.infer_reason_code(dict(row))
    if reason_code in _NON_EXECUTABLE_HOLD_REASONS:
        return {
            "benchmark_basis": "not_executable",
            "effective_action": None,
            "intended_action": None,
            "scoreability_reason": reason_code.lower(),
        }

    position_action = _position_action(row)
    if position_action is not None:
        return {
            "benchmark_basis": "position_long" if position_action == "BUY" else "position_short",
            "effective_action": position_action,
            "intended_action": None,
            "scoreability_reason": None,
        }

    conditional_action = _conditional_order_action(row)
    if conditional_action is not None:
        return {
            "benchmark_basis": "conditional_order",
            "effective_action": None,
            "intended_action": conditional_action,
            "scoreability_reason": "conditional_trigger_not_evaluated",
        }

    intended_action = _declared_hold_action(row)
    if intended_action is not None:
        return {
            "benchmark_basis": "intended_long" if intended_action == "BUY" else "intended_short",
            "effective_action": None,
            "intended_action": intended_action,
            "scoreability_reason": None,
        }
    return {
        "benchmark_basis": "directionless_hold",
        "effective_action": None,
        "intended_action": None,
        "scoreability_reason": "direction_unknown",
    }


def _verdict(
    action: Any,
    future_return: float | None,
    threshold: float,
    *,
    intended_action: str | None = None,
) -> str:
    if future_return is None:
        return "unknown"
    action_text = str(action or "").strip().upper()
    if action_text == "BUY":
        if future_return >= threshold:
            return "good"
        if future_return <= -threshold:
            return "bad"
        return "neutral"
    if action_text == "SELL":
        if future_return <= -threshold:
            return "good"
        if future_return >= threshold:
            return "bad"
        return "neutral"
    if action_text == "HOLD":
        if abs(future_return) < threshold:
            return "good"
        direction = _directional_action(intended_action)
        if direction == "BUY":
            return "missed" if future_return >= threshold else "good"
        if direction == "SELL":
            return "missed" if future_return <= -threshold else "good"
        return "unknown"
    return "unknown"


def decision_verdict(
    row: Mapping[str, Any],
    future_return: float | None,
    threshold: float,
) -> tuple[str, dict[str, str | None]]:
    """Classify one decision; return and threshold must use the same unit."""

    context = decision_benchmark_context(row)
    basis = context["benchmark_basis"]
    if basis == "infra":
        return "machine", context
    if basis in {"not_executable", "conditional_order", "unsupported_action"}:
        return "unknown", context
    if future_return is None or not math.isfinite(future_return):
        reason = "future_price_missing" if future_return is None else "future_return_invalid"
        return "unknown", {**context, "scoreability_reason": reason}
    if not math.isfinite(threshold) or threshold <= 0.0:
        return "unknown", {**context, "scoreability_reason": "invalid_threshold"}
    effective_action = context.get("effective_action")
    if effective_action in {"BUY", "SELL"}:
        return _verdict(effective_action, future_return, threshold), context
    return (
        _verdict(
            row.get("action"),
            future_return,
            threshold,
            intended_action=context.get("intended_action"),
        ),
        context,
    )


__all__ = [
    "BENCHMARK_SEMANTICS_VERSION",
    "_MACHINE_REASONS",
    "_verdict",
    "decision_benchmark_context",
    "decision_verdict",
]
