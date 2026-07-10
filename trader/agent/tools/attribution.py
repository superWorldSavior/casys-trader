"""Attribution tool handlers.

`get_recent_decisions` a été retiré : les décisions récentes sont désormais
POUSSÉES dans les faits par-symbole (`recent_decisions`, issue #4) plutôt qu'un
outil pull — anti-répétition permanente sans que l'agent ait à la demander.
"""

from __future__ import annotations

from trader.agent.tools.core import AgentToolCall, ToolContext, ToolSpec

_ATTRIBUTION_SCOPES = {"summary", "confidence", "exit_reason", "symbol"}
_SUMMARY_KEYS = (
    "n_closed_trades",
    "realized_pnl",
    "realized_gross_pnl",
    "total_commissions",
    "win_rate",
    "avg_pnl",
    "avg_holding_minutes",
    "regime",
)


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
        summary = context.attribution.get("summary")
        if not isinstance(summary, dict):
            summary = {
                key: context.attribution[key]
                for key in _SUMMARY_KEYS
                if key in context.attribution
            }
        return {"summary": summary}
    if scope == "confidence":
        return {"rows": list(context.attribution.get("by_confidence") or [])}
    if scope == "exit_reason":
        return {"rows": list(context.attribution.get("by_exit_reason") or [])}
    sym = call.args.get("symbol")
    if sym not in context.allowed_symbols:
        return {"symbol": sym, "error": "symbol_not_allowed"}
    source_rows = context.attribution.get("by_symbol")
    if not isinstance(source_rows, list):
        source_rows = context.attribution.get("recent_trips")
    rows = [
        row
        for row in (source_rows or [])
        if isinstance(row, dict) and row.get("symbol") == sym
    ]
    return {"rows": rows}


SPECS = [
    ToolSpec(name="get_attribution", validate_args=_validate_get_attribution, handler=_handle_get_attribution),
]
