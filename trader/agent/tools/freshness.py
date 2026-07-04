"""Freshness tool handlers."""

from __future__ import annotations

from trader.agent.tools.core import AgentToolCall, ToolContext, ToolSpec

_MAX_FRESHNESS_SYMBOLS = 8


def _validate_get_freshness(args: dict) -> str | None:
    symbols = args.get("symbols")
    if not isinstance(symbols, list) or not symbols or not all(isinstance(s, str) for s in symbols):
        return "symbols: liste non vide de str requise"
    if len(symbols) > _MAX_FRESHNESS_SYMBOLS:
        return f"symbols: {_MAX_FRESHNESS_SYMBOLS} max"
    return None


def _handle_get_freshness(call: AgentToolCall, context: ToolContext) -> dict:
    rows: list[dict] = []
    for sym in call.args["symbols"]:
        if sym not in context.allowed_symbols:
            rows.append({"symbol": sym, "error": "symbol_not_allowed"})
            continue
        age = context.data_age_by_symbol.get(sym)
        mc = context.market_context_by_symbol.get(sym) or {}
        rows.append(
            {
                "symbol": sym,
                "data_age_m": None if age is None else int(round(age)),
                "execution": mc.get("execution"),
                "planning": mc.get("planning"),
            }
        )
    return {"rows": rows}


SPECS = [
    ToolSpec(name="get_freshness", validate_args=_validate_get_freshness, handler=_handle_get_freshness),
]
