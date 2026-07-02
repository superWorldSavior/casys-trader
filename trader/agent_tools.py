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

from trader.semantic import catalog as semantic_catalog

# Enum fermé des issues d'un tool call (invariant design §11 : machine-readable).
OUTCOME_OK = "ok"
OUTCOME_REJECTED = "rejected"
OUTCOME_ERROR = "error"
OUTCOME_BUDGET_EXHAUSTED = "budget_exhausted"
OUTCOME_TRUNCATED = "truncated"

# Bornes de sérialisation — design §6 / findings Codex 2026-07-02.
_MAX_RAW_CALLS = 32    # au-delà : trace sentinel "truncated", surplus ignoré
_SCRUB_ID_LEN = 64     # id/tool tronqués à 64 chars
_SCRUB_STR_LEN = 256   # strings dans args tronquées à 256 chars
_SCRUB_LIST_LEN = 16   # listes dans args plafonnées à 16 éléments
_SCRUB_DICT_KEYS = 16  # dicts dans args plafonnés à 16 clés
_SCRUB_DEPTH = 3       # profondeur de récursion ≤ 3


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


def _scrub_str(s: str, max_len: int = _SCRUB_STR_LEN) -> str:
    return s if len(s) <= max_len else s[:max_len] + "…"


def _scrub_args(args: dict[str, Any]) -> dict[str, Any]:
    """Borne récursive : profondeur ≤ 3, listes ≤ 16, dicts ≤ 16 clés, strings ≤ 256.
    Garantit que les args persistés/réinjectés restent bornés en taille."""
    def _rec(obj: Any, depth: int) -> Any:
        if depth >= _SCRUB_DEPTH:
            return "…"
        if isinstance(obj, str):
            return _scrub_str(obj)
        if isinstance(obj, dict):
            items = list(obj.items())[:_SCRUB_DICT_KEYS]
            # Les clés font partie du JSON persisté : bornées comme les valeurs.
            out: dict[str, Any] = {_scrub_str(str(k)): _rec(v, depth + 1) for k, v in items}
            if len(obj) > _SCRUB_DICT_KEYS:
                out["…"] = f"(+{len(obj) - _SCRUB_DICT_KEYS} clés)"
            return out
        if isinstance(obj, list):
            out_list: list[Any] = [_rec(v, depth + 1) for v in obj[:_SCRUB_LIST_LEN]]
            if len(obj) > _SCRUB_LIST_LEN:
                out_list.append(f"… (+{len(obj) - _SCRUB_LIST_LEN})")
            return out_list
        return obj
    return _rec(args, 0)  # type: ignore[return-value]


def _rejected(raw: object, *, reason: str, message: str = "") -> AgentToolTrace:
    data = raw if isinstance(raw, dict) else {}
    detail: dict[str, Any] = {"reason": reason}
    if message:
        detail["message"] = message
    raw_args: dict[str, Any] = data.get("args") if isinstance(data.get("args"), dict) else {}
    return AgentToolTrace(
        id=_scrub_str(str(data.get("id") or "?"), _SCRUB_ID_LEN),
        tool=_scrub_str(str(data.get("tool") or "?"), _SCRUB_ID_LEN),
        args=_scrub_args(raw_args),
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
    # Scrub après validation : traces/résultats utilisent les valeurs bornées, jamais les bruts.
    return AgentToolCall(
        id=_scrub_str(call_id, _SCRUB_ID_LEN),
        tool=_scrub_str(tool, _SCRUB_ID_LEN),
        args=_scrub_args(args),
    )


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
    """Exécute un tool call validé et retourne un (result, trace) compact.

    Toute exception handler est absorbée en résultat error — jamais une exception
    ne remonte au daemon (AX §4 : machine-readable errors, §8 : structured outputs).
    """
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
    if isinstance(call_or_raw, AgentToolCall):
        call_id, tool, args = call_or_raw.id, call_or_raw.tool, call_or_raw.args
    else:
        data = call_or_raw if isinstance(call_or_raw, dict) else {}
        call_id = _scrub_str(str(data.get("id") or "?"), _SCRUB_ID_LEN)
        tool = _scrub_str(str(data.get("tool") or "?"), _SCRUB_ID_LEN)
        args = _scrub_args(data.get("args") if isinstance(data.get("args"), dict) else {})
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

    # Cap de liste d'entrée : surplus ignoré, résumé par une trace sentinel truncated.
    dropped = len(raw_calls) - _MAX_RAW_CALLS
    if dropped > 0:
        raw_calls = raw_calls[:_MAX_RAW_CALLS]
        traces.append(AgentToolTrace(
            id="[sentinel]", tool="?", args={},
            outcome=OUTCOME_TRUNCATED, detail={"dropped": dropped},
        ))

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


# ---------------------------------------------------------------------------
# Task 4 : get_position_risk, get_attribution, get_recent_decisions
# ---------------------------------------------------------------------------

_ATTRIBUTION_SCOPES = {"summary", "confidence", "exit_reason", "symbol"}
_MAX_DECISION_ROWS = 10


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
    # scope == "symbol" : vérifier l'allowlist (même pattern que get_position_risk).
    sym = call.args.get("symbol")
    if sym not in context.allowed_symbols:
        return {"symbol": sym, "error": "symbol_not_allowed"}
    rows = [r for r in (context.attribution.get("by_symbol") or []) if r.get("symbol") == sym]
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
    return {"rows": rows[:limit]}


TOOL_REGISTRY["get_position_risk"] = ToolSpec(
    name="get_position_risk", validate_args=_validate_symbol_only, handler=_handle_get_position_risk)
TOOL_REGISTRY["get_attribution"] = ToolSpec(
    name="get_attribution", validate_args=_validate_get_attribution, handler=_handle_get_attribution)
TOOL_REGISTRY["get_recent_decisions"] = ToolSpec(
    name="get_recent_decisions", validate_args=_validate_get_recent_decisions,
    handler=_handle_get_recent_decisions)


# ---------------------------------------------------------------------------
# Task 5 : get_indicator_context (cube borné, successeur REQUEST_CONTEXT)
# ---------------------------------------------------------------------------

# Import retardé pour éviter tout cycle si codex_client évolue : on importe
# dans le handler, pas au niveau module. En V0 codex_client n'importe pas
# agent_tools donc l'import module est sûr, mais le handler reste la forme
# canonique recommandée par le design.

_MAX_INDICATORS_PER_CALL = 6


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
    # Import local pour rester indépendant des cycles éventuels (AX composable primitives).
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


TOOL_REGISTRY["get_indicator_context"] = ToolSpec(
    name="get_indicator_context", validate_args=_validate_get_indicator_context,
    handler=_handle_get_indicator_context)


# ---------------------------------------------------------------------------
# Task 6 : sérialisation des traces + helpers prompt/ledger
# ---------------------------------------------------------------------------


def round_runtime_payload(traces: list[AgentToolTrace], *, rounds: int) -> dict:
    """Payload durable (decisions.jsonl runtime.*) — primitives seulement (§8)."""
    return {
        "tool_rounds": rounds,
        "tool_calls": [
            {"id": t.id, "tool": t.tool, "args": t.args, "outcome": t.outcome, "detail": t.detail}
            for t in traces
        ],
    }


def results_prompt_payload(results: list[AgentToolResult]) -> list[dict]:
    """Forme compacte réinjectée au LLM pour le tour final."""
    out: list[dict] = []
    for r in results:
        item: dict[str, Any] = {"id": r.id, "tool": r.tool, "ok": r.ok}
        if r.ok:
            item["result"] = r.result
        else:
            item["error"] = r.error
        out.append(item)
    return out


def calls_for_symbol(payload_calls: list[dict], symbol: str) -> list[dict]:
    """Traces à attacher au row d'UN symbole : ses calls + les calls globaux."""
    mine: list[dict] = []
    for call in payload_calls:
        args = call.get("args") or {}
        mentioned = _symbols_mentioned(args if isinstance(args, dict) else {})
        if not mentioned or symbol in mentioned:
            mine.append(call)
    return mine


# ---------------------------------------------------------------------------
# Task 11 : outils sémantiques — découverte du cube avant get_indicator_context
# ---------------------------------------------------------------------------

_MAX_INDICATOR_MATCHES = 12


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
            {k: ind[k] for k in ("name", "label", "category", "concepts") if k in ind}
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
        {k: ind[k] for k in ("name", "label", "category", "concepts", "description") if k in ind}
        for ind in matches[:_MAX_INDICATOR_MATCHES]
    ]
    return {"rows": rows, "truncated": len(matches) > _MAX_INDICATOR_MATCHES}


TOOL_REGISTRY["describe_data"] = ToolSpec(
    name="describe_data", validate_args=lambda a: None, handler=_handle_describe_data)
TOOL_REGISTRY["find_indicators"] = ToolSpec(
    name="find_indicators", validate_args=_validate_find_indicators, handler=_handle_find_indicators)
