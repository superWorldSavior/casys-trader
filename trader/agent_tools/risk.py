"""Position risk tool handlers."""
from __future__ import annotations

from trader.agent_tools.core import AgentToolCall, ToolContext, ToolSpec


def _validate_symbol_only(args: dict) -> str | None:
    if not isinstance(args.get("symbol"), str) or not args["symbol"]:
        return "symbol: str non vide requis"
    return None


def _handle_get_position_risk(call: AgentToolCall, context: ToolContext) -> dict:
    sym = call.args["symbol"]
    if sym not in context.allowed_symbols:
        return {"symbol": sym, "error": "symbol_not_allowed"}
    if context.position_risk_provider is None:
        return {"symbol": sym, "error": "unavailable"}
    payload = context.position_risk_provider(sym)
    return payload if payload is not None else {"symbol": sym, "error": "no_position"}


SPECS = [
    ToolSpec(name="get_position_risk", validate_args=_validate_symbol_only, handler=_handle_get_position_risk),
]
