"""Couche d'outils domaine bornés pour le LLM runtime (design 2026-06-29).

Le LLM n'exécute rien : il émet des tool_calls JSON, le daemon valide et
exécute via ce module (V0 = lecture seule, contexte étroit du cycle courant),
puis réinjecte des résultats compacts. Toute erreur devient un résultat
compact à outcome fermé — jamais une exception dans la boucle live.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

# Enum fermé des issues d'un tool call (invariant design §11 : machine-readable).
OUTCOME_OK = "ok"
OUTCOME_REJECTED = "rejected"
OUTCOME_ERROR = "error"
OUTCOME_BUDGET_EXHAUSTED = "budget_exhausted"


@dataclass(frozen=True)
class AgentToolCall:
    id: str
    tool: str
    args: dict[str, Any]


@dataclass(frozen=True)
class AgentToolResult:
    """Résultat compact réinjecté au LLM (ok=False -> 'error' lisible machine)."""
    id: str
    tool: str
    ok: bool
    result: dict[str, Any] | None = None
    error: str | None = None


@dataclass(frozen=True)
class AgentToolTrace:
    """Trace d'audit persistée dans decisions.jsonl (runtime.tool_calls)."""
    id: str
    tool: str
    args: dict[str, Any]
    outcome: str
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolContext:
    """Contexte étroit construit par le daemon pour LE cycle courant.

    Données déjà calculées passées telles quelles ; les sources lourdes
    (ledger, portefeuille) arrivent en providers injectés — les handlers
    restent purs et testables sans daemon.
    """
    now: datetime
    allowed_symbols: frozenset[str]
    data_age_by_symbol: Mapping[str, float] = field(default_factory=dict)
    market_context_by_symbol: Mapping[str, dict] = field(default_factory=dict)
    active_watches_by_symbol: Mapping[str, list] = field(default_factory=dict)
    attribution: Mapping[str, Any] | None = None
    position_risk_provider: Callable[[str], dict | None] | None = None
    recent_decisions_provider: Callable[[str | None, int], list[dict]] | None = None
    indicator_resolver: Callable[[list], dict] | None = None


@dataclass(frozen=True)
class ToolSpec:
    """validate_args retourne None si OK, sinon un message d'erreur compact."""
    name: str
    validate_args: Callable[[dict], str | None]
    handler: Callable[[AgentToolCall, ToolContext], dict]


# Rempli par les tâches suivantes (get_freshness, get_active_plans, ...).
TOOL_REGISTRY: dict[str, ToolSpec] = {}


def _rejected(raw: object, *, reason: str, message: str = "") -> AgentToolTrace:
    data = raw if isinstance(raw, dict) else {}
    detail: dict[str, Any] = {"reason": reason}
    if message:
        detail["message"] = message
    return AgentToolTrace(
        id=str(data.get("id") or "?"),
        tool=str(data.get("tool") or "?"),
        args=data.get("args") if isinstance(data.get("args"), dict) else {},
        outcome=OUTCOME_REJECTED,
        detail=detail,
    )


def validate_tool_call(
    raw: object,
    *,
    allowed_tools: frozenset[str],
    registry: Mapping[str, ToolSpec] | None = None,
) -> AgentToolCall | AgentToolTrace:
    """Frontière d'entrée (AX fast-fail) : rejette AVANT toute exécution."""
    tools = TOOL_REGISTRY if registry is None else registry
    if not isinstance(raw, dict):
        return _rejected(raw, reason="invalid_call", message="tool call non-objet")
    call_id = raw.get("id")
    tool = raw.get("tool")
    if not isinstance(call_id, str) or not call_id or not isinstance(tool, str) or not tool:
        return _rejected(raw, reason="invalid_call", message="id/tool requis")
    if tool not in tools:
        return _rejected(raw, reason="unknown_tool")
    if tool not in allowed_tools:
        return _rejected(raw, reason="tool_not_allowed")
    args = raw.get("args")
    if args is None:
        args = {}
    if not isinstance(args, dict):
        return _rejected(raw, reason="invalid_args", message="args doit être un objet")
    error = tools[tool].validate_args(args)
    if error:
        return _rejected(raw, reason="invalid_args", message=error)
    return AgentToolCall(id=call_id, tool=tool, args=args)


# ---------------------------------------------------------------------------
# Task 2 : exécution bornée (budgets total + par-symbole, erreurs compactes)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ToolRoundLimits:
    """Bornes design §6.2 : petites, explicites, non contournables par le LLM."""
    max_total_calls: int = 24
    max_calls_per_symbol: int = 3


def _symbols_mentioned(args: dict) -> list[str]:
    """Symboles qu'un call consomme pour le budget par-symbole."""
    out: list[str] = []
    if isinstance(args.get("symbol"), str):
        out.append(args["symbol"])
    if isinstance(args.get("symbols"), list):
        out.extend(str(s) for s in args["symbols"])
    return out


def execute_tool_call(
    call: AgentToolCall,
    context: ToolContext,
    *,
    registry: Mapping[str, ToolSpec] | None = None,
) -> tuple[AgentToolResult, AgentToolTrace]:
    tools = TOOL_REGISTRY if registry is None else registry
    try:
        payload = tools[call.tool].handler(call, context)
    except Exception as exc:  # noqa: BLE001 — aucune exception ne remonte au daemon
        message = f"{type(exc).__name__}: {exc}"[:200]
        return (
            AgentToolResult(id=call.id, tool=call.tool, ok=False, error=message),
            AgentToolTrace(id=call.id, tool=call.tool, args=call.args,
                           outcome=OUTCOME_ERROR, detail={"message": message}),
        )
    detail: dict[str, Any] = {}
    if isinstance(payload, dict) and "rows" in payload and isinstance(payload["rows"], list):
        detail["result_count"] = len(payload["rows"])
    return (
        AgentToolResult(id=call.id, tool=call.tool, ok=True, result=payload),
        AgentToolTrace(id=call.id, tool=call.tool, args=call.args,
                       outcome=OUTCOME_OK, detail=detail),
    )


def _budget_exhausted(call_or_raw: object, *, reason: str) -> tuple[AgentToolResult, AgentToolTrace]:
    data = call_or_raw if isinstance(call_or_raw, dict) else {}
    if isinstance(call_or_raw, AgentToolCall):
        call_id, tool, args = call_or_raw.id, call_or_raw.tool, call_or_raw.args
    else:
        call_id = str(data.get("id") or "?")
        tool = str(data.get("tool") or "?")
        args = data.get("args") if isinstance(data.get("args"), dict) else {}
    return (
        AgentToolResult(id=call_id, tool=tool, ok=False, error="budget_exhausted"),
        AgentToolTrace(id=call_id, tool=tool, args=args,
                       outcome=OUTCOME_BUDGET_EXHAUSTED, detail={"reason": reason}),
    )


def execute_tool_round(
    raw_calls: list,
    *,
    context: ToolContext,
    limits: ToolRoundLimits,
    allowed_tools: frozenset[str],
    registry: Mapping[str, ToolSpec] | None = None,
) -> tuple[list[AgentToolResult], list[AgentToolTrace]]:
    """UNE tournée bornée : chaque call rend TOUJOURS un (result, trace) —
    même rejeté ou hors budget — pour que le LLM voie ce qui s'est passé."""
    results: list[AgentToolResult] = []
    traces: list[AgentToolTrace] = []
    executed_total = 0
    per_symbol: dict[str, int] = {}

    for raw in raw_calls:
        if executed_total >= limits.max_total_calls:
            result, trace = _budget_exhausted(raw, reason="max_total_calls")
            results.append(result)
            traces.append(trace)
            continue
        validated = validate_tool_call(raw, allowed_tools=allowed_tools, registry=registry)
        if isinstance(validated, AgentToolTrace):
            reason = validated.detail.get("reason", "rejected")
            results.append(AgentToolResult(id=validated.id, tool=validated.tool,
                                           ok=False, error=f"rejected:{reason}"))
            traces.append(validated)
            continue
        symbols = _symbols_mentioned(validated.args)
        if any(per_symbol.get(sym, 0) >= limits.max_calls_per_symbol for sym in symbols):
            result, trace = _budget_exhausted(validated, reason="max_calls_per_symbol")
            results.append(result)
            traces.append(trace)
            continue
        for sym in symbols:
            per_symbol[sym] = per_symbol.get(sym, 0) + 1
        executed_total += 1
        result, trace = execute_tool_call(validated, context, registry=registry)
        results.append(result)
        traces.append(trace)

    return results, traces


# ---------------------------------------------------------------------------
# Task 3 : get_freshness + get_active_plans (handlers lecture-seule)
# ---------------------------------------------------------------------------

_MAX_FRESHNESS_SYMBOLS = 8
_MAX_PLAN_ROWS = 20


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
        rows.append({
            "symbol": sym,
            "data_age_m": None if age is None else int(round(age)),
            "execution": mc.get("execution"),
            "planning": mc.get("planning"),
        })
    return {"rows": rows}


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
        rows = [w for watches in context.active_watches_by_symbol.values() for w in watches]
    return {"rows": rows[:limit]}


TOOL_REGISTRY["get_freshness"] = ToolSpec(
    name="get_freshness", validate_args=_validate_get_freshness, handler=_handle_get_freshness)
TOOL_REGISTRY["get_active_plans"] = ToolSpec(
    name="get_active_plans", validate_args=_validate_get_active_plans, handler=_handle_get_active_plans)
