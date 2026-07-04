"""Attribution and recent decision tool handlers."""

from __future__ import annotations

from trader.agent.tools.core import AgentToolCall, ToolContext, ToolSpec

_ATTRIBUTION_SCOPES = {"summary", "confidence", "exit_reason", "symbol"}
_MAX_DECISION_ROWS = 10


def _validate_get_attribution(args: dict) -> str | None:
    if args.get("scope") not in _ATTRIBUTION_SCOPES:
        return f"scope: un de {sorted(_ATTRIBUTION_SCOPES)}"
    if args["scope"] == "symbol" and not isinstance(args.get("symbol"), str):
        return "symbol requis quand scope=symbol"
    return None


def _handle_get_attribution(call: AgentToolCall, context: ToolContext) -> dict:
    if context.attribution is None:
        return {"error": "unavailable"}
    scope = call.args["scope"]
    if scope == "summary":
        return {"summary": context.attribution.get("summary")}
    if scope == "confidence":
        return {"rows": list(context.attribution.get("by_confidence") or [])}
    if scope == "exit_reason":
        return {"rows": list(context.attribution.get("by_exit_reason") or [])}
    sym = call.args.get("symbol")
    if sym not in context.allowed_symbols:
        return {"symbol": sym, "error": "symbol_not_allowed"}
    rows = [row for row in (context.attribution.get("by_symbol") or []) if row.get("symbol") == sym]
    return {"rows": rows}


def _validate_get_recent_decisions(args: dict) -> str | None:
    symbol = args.get("symbol")
    if symbol is not None and not isinstance(symbol, str):
        return "symbol: str ou absent"
    limit = args.get("limit")
    if limit is not None and (not isinstance(limit, int) or limit < 1):
        return "limit: entier >= 1 ou absent"
    return None


def _handle_get_recent_decisions(call: AgentToolCall, context: ToolContext) -> dict:
    if context.recent_decisions_provider is None:
        return {"error": "unavailable", "rows": []}
    limit = min(int(call.args.get("limit") or 5), _MAX_DECISION_ROWS)
    rows = context.recent_decisions_provider(call.args.get("symbol"), limit)
    return {"rows": rows}


SPECS = [
    ToolSpec(name="get_attribution", validate_args=_validate_get_attribution, handler=_handle_get_attribution),
    ToolSpec(
        name="get_recent_decisions",
        validate_args=_validate_get_recent_decisions,
        handler=_handle_get_recent_decisions,
    ),
]
