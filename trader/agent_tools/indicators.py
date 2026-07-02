"""Semantic data and indicator context tool handlers."""
from __future__ import annotations

from trader.agent_tools.core import AgentToolCall, ToolContext, ToolSpec
from trader.semantic import catalog as semantic_catalog

_MAX_INDICATORS_PER_CALL = 6
_MAX_INDICATOR_MATCHES = 12


def _validate_get_indicator_context(args: dict) -> str | None:
    if not isinstance(args.get("symbol"), str) or not args["symbol"]:
        return "symbol: str non vide requis"
    indicators = args.get("indicators")
    if not isinstance(indicators, list) or not indicators or not all(isinstance(i, str) for i in indicators):
        return "indicators: liste non vide de str requise"
    if len(indicators) > _MAX_INDICATORS_PER_CALL:
        return f"indicators: {_MAX_INDICATORS_PER_CALL} max"
    window = args.get("window")
    if window is not None and (not isinstance(window, int) or window < 1):
        return "window: entier >= 1 ou absent"
    return None


def _handle_get_indicator_context(call: AgentToolCall, context: ToolContext) -> dict:
    from trader.codex_client import IndicatorRequest  # noqa: PLC0415

    sym = call.args["symbol"]
    if sym not in context.allowed_symbols:
        return {"symbol": sym, "error": "symbol_not_allowed"}
    if context.indicator_resolver is None:
        return {"symbol": sym, "error": "unavailable"}
    request = IndicatorRequest(
        symbol=sym,
        indicators=[str(i) for i in call.args["indicators"]],
        timeframe=str(call.args.get("timeframe") or "1h"),
        lookback=None if call.args.get("lookback") is None else str(call.args["lookback"]),
        window=int(call.args.get("window") or 48),
        as_of=str(call.args.get("as_of") or "latest"),
    )
    return context.indicator_resolver([request])


def _handle_describe_data(call: AgentToolCall, context: ToolContext) -> dict:
    """Auto-description du cube sémantique (compacte : la découverte, pas les specs)."""

    return {
        "levels": list(semantic_catalog.LEVELS),
        "timeframes": {
            name: {
                "lookbacks": spec["lookbacks"],
                "default_lookback": spec["default_lookback"],
                "default_window": spec["default_window"],
                "style": spec["style"],
            }
            for name, spec in semantic_catalog.TIMEFRAMES.items()
        },
        "windows": list(semantic_catalog.WINDOWS),
        "as_of_modes": list(semantic_catalog.AS_OF_MODES),
        "indicators": [
            {key: ind[key] for key in ("name", "label", "category", "concepts") if key in ind}
            for ind in semantic_catalog.list_indicators()
        ],
    }


def _validate_find_indicators(args: dict) -> str | None:
    if not isinstance(args.get("concept"), str) or not args["concept"].strip():
        return "concept: str non vide requis (ex. 'momentum', 'volatilité')"
    return None


def _handle_find_indicators(call: AgentToolCall, context: ToolContext) -> dict:
    matches = semantic_catalog.find_indicators(call.args["concept"])
    rows = [
        {key: ind[key] for key in ("name", "label", "category", "concepts", "description") if key in ind}
        for ind in matches[:_MAX_INDICATOR_MATCHES]
    ]
    return {"rows": rows, "truncated": len(matches) > _MAX_INDICATOR_MATCHES}


SPECS = [
    ToolSpec(
        name="get_indicator_context",
        validate_args=_validate_get_indicator_context,
        handler=_handle_get_indicator_context,
    ),
    ToolSpec(name="describe_data", validate_args=lambda args: None, handler=_handle_describe_data),
    ToolSpec(name="find_indicators", validate_args=_validate_find_indicators, handler=_handle_find_indicators),
]
