"""Core validation, execution, and serialization for bounded agent tools."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, Protocol, TypeAlias

JsonObject: TypeAlias = dict[str, Any]
ToolArgs: TypeAlias = JsonObject
ToolPayload: TypeAlias = JsonObject
ToolOutcome: TypeAlias = Literal["ok", "rejected", "error", "budget_exhausted", "truncated"]

# Enum fermé des issues d'un tool call (invariant design §11 : machine-readable).
OUTCOME_OK: ToolOutcome = "ok"
OUTCOME_REJECTED: ToolOutcome = "rejected"
OUTCOME_ERROR: ToolOutcome = "error"
OUTCOME_BUDGET_EXHAUSTED: ToolOutcome = "budget_exhausted"
OUTCOME_TRUNCATED: ToolOutcome = "truncated"

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
    args: ToolArgs


@dataclass(frozen=True)
class AgentToolResult:
    """Résultat compact réinjecté au LLM (ok=False -> 'error' lisible machine)."""

    id: str
    tool: str
    ok: bool
    result: ToolPayload | None = None
    error: str | None = None


@dataclass(frozen=True)
class AgentToolTrace:
    """Trace d'audit persistée dans decisions.jsonl (runtime.tool_calls)."""

    id: str
    tool: str
    args: ToolArgs
    outcome: ToolOutcome
    detail: ToolPayload = field(default_factory=dict)


class PositionRiskProvider(Protocol):
    def __call__(self, symbol: str) -> ToolPayload | None:
        ...


class RecentDecisionsProvider(Protocol):
    def __call__(self, symbol: str | None, limit: int) -> list[JsonObject]:
        ...


class IndicatorResolver(Protocol):
    def __call__(self, requests: list[Any]) -> ToolPayload:
        ...


class LearningsRecallProvider(Protocol):
    def __call__(self, query: JsonObject) -> ToolPayload:
        ...


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
    position_risk_provider: PositionRiskProvider | None = None
    recent_decisions_provider: RecentDecisionsProvider | None = None
    indicator_resolver: IndicatorResolver | None = None
    learnings_recall_provider: LearningsRecallProvider | None = None


@dataclass(frozen=True)
class ToolSpec:
    """validate_args retourne None si OK, sinon un message d'erreur compact."""

    name: str
    validate_args: Callable[[ToolArgs], str | None]
    handler: Callable[[AgentToolCall, ToolContext], ToolPayload]


def _default_registry() -> Mapping[str, ToolSpec]:
    from trader.agent_tools.registry import TOOL_REGISTRY  # noqa: PLC0415

    return TOOL_REGISTRY


def _scrub_str(s: str, max_len: int = _SCRUB_STR_LEN) -> str:
    return s if len(s) <= max_len else s[:max_len] + "…"


def _scrub_args(args: ToolArgs) -> ToolArgs:
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
            out: ToolPayload = {_scrub_str(str(k)): _rec(v, depth + 1) for k, v in items}
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
    detail: ToolPayload = {"reason": reason}
    if message:
        detail["message"] = message
    raw_args: ToolArgs = data.get("args") if isinstance(data.get("args"), dict) else {}
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

    tools = _default_registry() if registry is None else registry
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
    return AgentToolCall(
        id=_scrub_str(call_id, _SCRUB_ID_LEN),
        tool=_scrub_str(tool, _SCRUB_ID_LEN),
        args=_scrub_args(args),
    )


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

    tools = _default_registry() if registry is None else registry
    try:
        payload = tools[call.tool].handler(call, context)
    except Exception as exc:  # noqa: BLE001 — aucune exception ne remonte au daemon
        message = f"{type(exc).__name__}: {exc}"[:200]
        return (
            AgentToolResult(id=call.id, tool=call.tool, ok=False, error=message),
            AgentToolTrace(
                id=call.id,
                tool=call.tool,
                args=call.args,
                outcome=OUTCOME_ERROR,
                detail={"message": message},
            ),
        )
    detail: ToolPayload = {}
    if isinstance(payload, dict) and "rows" in payload and isinstance(payload["rows"], list):
        detail["result_count"] = len(payload["rows"])
    return (
        AgentToolResult(id=call.id, tool=call.tool, ok=True, result=payload),
        AgentToolTrace(id=call.id, tool=call.tool, args=call.args, outcome=OUTCOME_OK, detail=detail),
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
        AgentToolTrace(
            id=call_id,
            tool=tool,
            args=args,
            outcome=OUTCOME_BUDGET_EXHAUSTED,
            detail={"reason": reason},
        ),
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

    dropped = len(raw_calls) - _MAX_RAW_CALLS
    if dropped > 0:
        raw_calls = raw_calls[:_MAX_RAW_CALLS]
        traces.append(AgentToolTrace(
            id="[sentinel]",
            tool="?",
            args={},
            outcome=OUTCOME_TRUNCATED,
            detail={"dropped": dropped},
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
            results.append(AgentToolResult(
                id=validated.id,
                tool=validated.tool,
                ok=False,
                error=f"rejected:{reason}",
            ))
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
    for result in results:
        item: ToolPayload = {"id": result.id, "tool": result.tool, "ok": result.ok}
        if result.ok:
            item["result"] = result.result
        else:
            item["error"] = result.error
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
