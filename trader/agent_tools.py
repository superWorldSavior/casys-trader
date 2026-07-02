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
