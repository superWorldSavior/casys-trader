"""Outcome-weighted learning recall tool handlers."""
from __future__ import annotations

from trader.agent_tools.core import AgentToolCall, ToolContext, ToolSpec

_MAX_RECALL_ROWS = 8


def _validate_recall_learnings(args: dict) -> str | None:
    """Au moins un de symbol/family/query requis ; limit optionnel ≤ 8."""

    has_symbol = isinstance(args.get("symbol"), str) and bool(args["symbol"])
    has_family = isinstance(args.get("family"), str) and bool(args["family"])
    has_query = isinstance(args.get("query"), str) and bool(args["query"])
    if not (has_symbol or has_family or has_query):
        return "au moins un de symbol, family, query requis"
    limit = args.get("limit")
    if limit is not None and (not isinstance(limit, int) or limit < 1):
        return "limit: entier >= 1 ou absent"
    return None


def _handle_recall_learnings(call: AgentToolCall, context: ToolContext) -> dict:
    """Délègue au provider injecté ; re-tronque rows à 8 par défense."""

    sym = call.args.get("symbol")
    if sym is not None and sym not in context.allowed_symbols:
        return {"symbol": sym, "error": "symbol_not_allowed"}
    if context.learnings_recall_provider is None:
        return {"error": "unavailable"}
    payload = context.learnings_recall_provider(call.args)
    if isinstance(payload.get("rows"), list) and len(payload["rows"]) > _MAX_RECALL_ROWS:
        payload = {**payload, "rows": payload["rows"][:_MAX_RECALL_ROWS]}
    return payload


SPECS = [
    ToolSpec(name="recall_learnings", validate_args=_validate_recall_learnings, handler=_handle_recall_learnings),
]
