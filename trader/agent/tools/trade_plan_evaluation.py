"""Read-only tool exposing deterministic entry-plan evaluation to the agent."""

from __future__ import annotations

import math

from trader.agent.tools.core import AgentToolCall, ToolContext, ToolSpec
from trader.application.execute.trade_plan_evaluation import TradePlanCandidate


def _validate_evaluate_trade_plan(args: dict) -> str | None:
    symbol = args.get("symbol")
    if not isinstance(symbol, str) or not symbol:
        return "symbol: str non vide requis"
    if args.get("direction") not in {"long", "short"}:
        return "direction: long|short requis"
    confidence = _finite_float(args.get("confidence"))
    if confidence is None or not 0.0 <= confidence <= 1.0:
        return "confidence: nombre dans [0,1] requis"
    quantity = args.get("qty", args.get("quantity"))
    risk_pct = args.get("risk_pct")
    if (quantity is None) == (risk_pct is None):
        return "exactement un de qty|quantity ou risk_pct est requis"
    selected = quantity if quantity is not None else risk_pct
    selected_value = _finite_float(selected)
    if selected_value is None or selected_value <= 0.0:
        return "qty/quantity/risk_pct doit être un nombre fini > 0"
    raw_exit = args.get("exit_plan", args.get("exit"))
    if raw_exit is not None and not isinstance(raw_exit, dict):
        return "exit|exit_plan doit être un objet"
    if args.get("thesis") is not None and not isinstance(args.get("thesis"), dict):
        return "thesis doit être un objet"
    return None


def _handle_evaluate_trade_plan(
    call: AgentToolCall,
    context: ToolContext,
) -> dict:
    from trader.agent.protocol.parsing import compact_trade_exit_plan
    from trader.agent.protocol.strategy_language import compile_strategy_call
    from trader.domain.strategy_language import normalize_trade_thesis

    symbol = str(call.args["symbol"])
    if symbol not in context.allowed_symbols:
        return {"valid": False, "symbol": symbol, "reasons": ["symbol_not_allowed"]}
    evaluator = context.trade_plan_evaluator
    if evaluator is None and context.trade_plan_evaluator_provider is not None:
        evaluator = context.trade_plan_evaluator_provider(symbol)
    if evaluator is None:
        return {"valid": False, "symbol": symbol, "reasons": ["unavailable"]}
    quantity_raw = call.args.get("qty", call.args.get("quantity"))
    entry_args = {
        "direction": call.args["direction"],
        "exit": call.args.get("exit_plan", call.args.get("exit")),
        "thesis": call.args.get("thesis"),
    }
    compiled_entry = compile_strategy_call("strategy_entry", entry_args).args
    candidate = TradePlanCandidate(
        symbol=symbol,
        direction=str(call.args["direction"]),  # type: ignore[arg-type]
        confidence=float(call.args["confidence"]),
        quantity=None if quantity_raw is None else float(quantity_raw),
        risk_pct=(
            None
            if call.args.get("risk_pct") is None
            else float(call.args["risk_pct"])
        ),
        exit_plan=compact_trade_exit_plan(compiled_entry.get("exit")),
        thesis=normalize_trade_thesis(compiled_entry.get("thesis")),
        rationale=str(call.args.get("rationale") or "")[:500],
    )
    return evaluator.evaluate(candidate).to_tool_payload()


def _finite_float(value: object) -> float | None:
    try:
        parsed = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


SPECS = [
    ToolSpec(
        name="evaluate_trade_plan",
        validate_args=_validate_evaluate_trade_plan,
        handler=_handle_evaluate_trade_plan,
    )
]

