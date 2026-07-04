"""Active plan tool handlers."""

from __future__ import annotations

from trader.agent.tools.core import AgentToolCall, ToolContext, ToolSpec

_MAX_PLAN_ROWS = 20


def _validate_get_active_plans(args: dict) -> str | None:
    symbol = args.get("symbol")
    if symbol is not None and not isinstance(symbol, str):
        return "symbol: str ou absent"
    limit = args.get("limit")
    if limit is not None and (not isinstance(limit, int) or limit < 1):
        return "limit: entier >= 1 ou absent"
    return None


def _handle_get_active_plans(call: AgentToolCall, context: ToolContext) -> dict:
    symbol = call.args.get("symbol")
    limit = min(int(call.args.get("limit") or _MAX_PLAN_ROWS), _MAX_PLAN_ROWS)
    if symbol is not None:
        rows = list(context.active_watches_by_symbol.get(symbol, []))
    else:
        rows = [watch for watches in context.active_watches_by_symbol.values() for watch in watches]
    return {"rows": rows[:limit]}


SPECS = [
    ToolSpec(name="get_active_plans", validate_args=_validate_get_active_plans, handler=_handle_get_active_plans),
]
