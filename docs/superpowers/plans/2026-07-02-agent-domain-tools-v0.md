# Agent Domain Tools V0 — Plan d'implémentation

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implémenter les Phases 1-2 du design `docs/superpowers/specs/2026-06-29-agent-domain-tools-design.md` : le registre d'outils domaine lecture-seule (`trader/agent_tools.py`) et la boucle « 1 tournée d'outils + 1 décision finale » dans le daemon, derrière un feature flag éteint par défaut.

**Architecture:** Le LLM continue de recevoir UN prompt et de rendre UN JSON. Quand le flag est actif, ce JSON peut être `{"tool_calls": [...]}` au lieu de `{"decisions": [...]}` ; le daemon valide et exécute les outils (lecture seule, contexte étroit fourni par le cycle courant), réinjecte des `tool_results` compacts, puis exige la décision finale. Traces persistées sous `runtime.tool_rounds` / `runtime.tool_calls` dans `state/decisions.jsonl`, dérivables par `tool_trace.summarize_tools`.

**Tech Stack:** Python 3.11, dataclasses frozen, pytest. Aucune dépendance nouvelle (validation d'args à la main, pas de jsonschema).

## Global Constraints

Copiées du design (§4, §6.2, §11) — chaque tâche les respecte implicitement :

- acpx reste appelé avec terminal et outils système désactivés (`build_acpx_command` inchangé).
- Le daemon est le SEUL exécuteur d'effets ; les outils V0 sont lecture-seule et ne mutent ni scheduler, ni broker, ni mémoire, ni fichiers.
- Aucune erreur d'outil ne fait crasher la boucle live : erreur = résultat compact, outcome ∈ `{ok, rejected, error, budget_exhausted}` (enum fermé).
- Limites premier jet : 1 tournée d'outils max + 1 tour final ; 3 appels max par symbole ; 24 appels max par lot.
- Tournée finale qui rend encore des `tool_calls` → HOLD par symbole, raison `tool_loop_blocked`.
- Flag `CASYS_AGENT_TOOLS_ENABLED` (défaut 0) : éteint ⇒ ZÉRO changement de comportement (tests le prouvent).
- Compatibilité : `REQUEST_CONTEXT` et les champs JSON legacy restent acceptés tels quels.
- Style repo : docstrings français, commentaires sur le POURQUOI, dataclasses frozen, helpers purs testables sans daemon.

**Vérification transverse après chaque tâche :** `uv run ruff check` puis `uv run pytest -q > /tmp/pytest-plan.log 2>&1; echo "EXIT=$?"` — ne JAMAIS conclure au vert sans lire `EXIT=0` (un pipe vers tail masque l'échec).

---

### Task 1: Types, registre et validation des tool calls

**Files:**
- Create: `trader/agent_tools.py`
- Test: `tests/test_agent_tools.py`

**Interfaces:**
- Produces: `AgentToolCall(id, tool, args)`, `AgentToolResult(id, tool, ok, result, error)`, `AgentToolTrace(id, tool, args, outcome, detail)`, `ToolSpec(name, validate_args, handler)`, `ToolContext(...)`, `TOOL_REGISTRY: dict[str, ToolSpec]`, `validate_tool_call(raw, *, allowed_tools, registry) -> AgentToolCall | AgentToolTrace`.

- [ ] **Step 1: Écrire les tests de validation qui échouent**

```python
# tests/test_agent_tools.py
"""agent_tools — validation, budgets, handlers lecture-seule (design 2026-06-29)."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from trader import agent_tools
from trader.agent_tools import (
    AgentToolCall,
    AgentToolTrace,
    ToolContext,
    ToolSpec,
    validate_tool_call,
)

UTC = timezone.utc


def _registry_with_echo() -> dict[str, ToolSpec]:
    """Registre de test : un outil 'echo' qui rend ses args."""
    def _validate(args: dict) -> str | None:
        if "text" not in args:
            return "champ 'text' requis"
        return None

    def _handler(call: AgentToolCall, context: ToolContext) -> dict:
        return {"echo": call.args["text"]}

    return {"echo": ToolSpec(name="echo", validate_args=_validate, handler=_handler)}


def test_validate_tool_call_happy_path():
    call = validate_tool_call(
        {"id": "c1", "tool": "echo", "args": {"text": "hi"}},
        allowed_tools=frozenset({"echo"}),
        registry=_registry_with_echo(),
    )
    assert isinstance(call, AgentToolCall)
    assert call.id == "c1"
    assert call.tool == "echo"
    assert call.args == {"text": "hi"}


def test_validate_tool_call_args_absents_deviennent_dict_vide():
    registry = {"noargs": ToolSpec(name="noargs", validate_args=lambda a: None, handler=lambda c, ctx: {})}
    call = validate_tool_call(
        {"id": "c1", "tool": "noargs"},
        allowed_tools=frozenset({"noargs"}),
        registry=registry,
    )
    assert isinstance(call, AgentToolCall)
    assert call.args == {}


def test_validate_tool_call_rejette_non_dict():
    trace = validate_tool_call("pas un objet", allowed_tools=frozenset({"echo"}), registry=_registry_with_echo())
    assert isinstance(trace, AgentToolTrace)
    assert trace.outcome == "rejected"
    assert trace.detail["reason"] == "invalid_call"


def test_validate_tool_call_rejette_outil_inconnu():
    trace = validate_tool_call(
        {"id": "c1", "tool": "rm_rf", "args": {}},
        allowed_tools=frozenset({"echo"}),
        registry=_registry_with_echo(),
    )
    assert isinstance(trace, AgentToolTrace)
    assert trace.outcome == "rejected"
    assert trace.detail["reason"] == "unknown_tool"


def test_validate_tool_call_rejette_outil_hors_allowlist():
    trace = validate_tool_call(
        {"id": "c1", "tool": "echo", "args": {"text": "hi"}},
        allowed_tools=frozenset(),  # registre le connaît, allowlist non
        registry=_registry_with_echo(),
    )
    assert isinstance(trace, AgentToolTrace)
    assert trace.detail["reason"] == "tool_not_allowed"


def test_validate_tool_call_rejette_args_invalides():
    trace = validate_tool_call(
        {"id": "c1", "tool": "echo", "args": {}},  # 'text' manquant
        allowed_tools=frozenset({"echo"}),
        registry=_registry_with_echo(),
    )
    assert isinstance(trace, AgentToolTrace)
    assert trace.detail["reason"] == "invalid_args"
    assert "text" in trace.detail["message"]
```

- [ ] **Step 2: Vérifier l'échec**

Run: `uv run pytest tests/test_agent_tools.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'trader.agent_tools'`

- [ ] **Step 3: Implémenter types + validation**

```python
# trader/agent_tools.py
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
```

- [ ] **Step 4: Vérifier le vert**

Run: `uv run pytest tests/test_agent_tools.py -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Commit**

```bash
git add trader/agent_tools.py tests/test_agent_tools.py
git commit -m "feat(agent-tools): types + registre + validation frontière (V0 design 2026-06-29)"
```

---

### Task 2: Exécution d'une tournée bornée (budgets, erreurs)

**Files:**
- Modify: `trader/agent_tools.py` (fin de fichier)
- Test: `tests/test_agent_tools.py` (append)

**Interfaces:**
- Consumes: `validate_tool_call`, `ToolSpec`, `ToolContext` (Task 1).
- Produces: `ToolRoundLimits(max_total_calls=24, max_calls_per_symbol=3)`, `execute_tool_call(call, context, registry) -> tuple[AgentToolResult, AgentToolTrace]`, `execute_tool_round(raw_calls, *, context, limits, allowed_tools, registry) -> tuple[list[AgentToolResult], list[AgentToolTrace]]`.

- [ ] **Step 1: Tests qui échouent**

```python
# append à tests/test_agent_tools.py

def _context(symbols: set[str] = frozenset({"AAA"})) -> ToolContext:
    return ToolContext(now=datetime(2026, 7, 2, 10, 0, tzinfo=UTC), allowed_symbols=frozenset(symbols))


def _registry_boom() -> dict[str, ToolSpec]:
    def _handler(call, ctx):
        raise RuntimeError("kaput")
    return {"boom": ToolSpec(name="boom", validate_args=lambda a: None, handler=_handler)}


def test_execute_tool_call_ok_produit_result_et_trace():
    registry = _registry_with_echo()
    call = AgentToolCall(id="c1", tool="echo", args={"text": "hi"})
    result, trace = agent_tools.execute_tool_call(call, _context(), registry=registry)
    assert result.ok is True
    assert result.result == {"echo": "hi"}
    assert trace.outcome == "ok"


def test_execute_tool_call_exception_handler_devient_error():
    call = AgentToolCall(id="c1", tool="boom", args={})
    result, trace = agent_tools.execute_tool_call(call, _context(), registry=_registry_boom())
    assert result.ok is False
    assert "RuntimeError" in result.error
    assert trace.outcome == "error"


def test_execute_tool_round_melange_valides_et_rejets():
    registry = _registry_with_echo()
    raw = [
        {"id": "c1", "tool": "echo", "args": {"text": "a"}},
        {"id": "c2", "tool": "inconnu", "args": {}},
    ]
    results, traces = agent_tools.execute_tool_round(
        raw, context=_context(), limits=agent_tools.ToolRoundLimits(),
        allowed_tools=frozenset({"echo"}), registry=registry,
    )
    assert len(results) == 2 and len(traces) == 2
    assert results[0].ok is True
    assert results[1].ok is False and results[1].error == "rejected:unknown_tool"
    assert traces[1].outcome == "rejected"


def test_execute_tool_round_budget_total_epuise():
    registry = _registry_with_echo()
    raw = [{"id": f"c{i}", "tool": "echo", "args": {"text": "x"}} for i in range(5)]
    results, traces = agent_tools.execute_tool_round(
        raw, context=_context(), limits=agent_tools.ToolRoundLimits(max_total_calls=2),
        allowed_tools=frozenset({"echo"}), registry=registry,
    )
    assert [r.ok for r in results] == [True, True, False, False, False]
    assert {t.outcome for t in traces[2:]} == {"budget_exhausted"}


def test_execute_tool_round_budget_par_symbole():
    def _validate(args):
        return None if isinstance(args.get("symbol"), str) else "symbol requis"
    registry = {"persym": ToolSpec(name="persym", validate_args=_validate,
                                   handler=lambda c, ctx: {"sym": c.args["symbol"]})}
    raw = [{"id": f"c{i}", "tool": "persym", "args": {"symbol": "AAA"}} for i in range(4)]
    results, traces = agent_tools.execute_tool_round(
        raw, context=_context(), limits=agent_tools.ToolRoundLimits(max_calls_per_symbol=3),
        allowed_tools=frozenset({"persym"}), registry=registry,
    )
    # 3 passent, le 4e sur le même symbole est coupé
    assert [r.ok for r in results] == [True, True, True, False]
    assert traces[3].outcome == "budget_exhausted"
    assert traces[3].detail["reason"] == "max_calls_per_symbol"
```

- [ ] **Step 2: Vérifier l'échec**

Run: `uv run pytest tests/test_agent_tools.py -v`
Expected: FAIL — `AttributeError: ... no attribute 'execute_tool_call'`

- [ ] **Step 3: Implémenter**

```python
# append à trader/agent_tools.py

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
            results.append(result); traces.append(trace)
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
            results.append(result); traces.append(trace)
            continue
        for sym in symbols:
            per_symbol[sym] = per_symbol.get(sym, 0) + 1
        executed_total += 1
        result, trace = execute_tool_call(validated, context, registry=registry)
        results.append(result); traces.append(trace)

    return results, traces
```

- [ ] **Step 4: Vérifier le vert**

Run: `uv run pytest tests/test_agent_tools.py -v`
Expected: PASS (11 tests)

- [ ] **Step 5: Commit**

```bash
git add trader/agent_tools.py tests/test_agent_tools.py
git commit -m "feat(agent-tools): execute_tool_round borné (budgets total/par-symbole, erreurs compactes)"
```

---

### Task 3: Outils `get_freshness` et `get_active_plans`

**Files:**
- Modify: `trader/agent_tools.py`
- Test: `tests/test_agent_tools.py` (append)

**Interfaces:**
- Consumes: `ToolContext.data_age_by_symbol`, `.market_context_by_symbol` (dicts `{"execution": {...}, "planning": {...}}` — même forme que `daemon._symbol_facts`, voir `trader/daemon.py:1323-1345`), `.active_watches_by_symbol`.
- Produces: entrées `TOOL_REGISTRY["get_freshness"]` et `TOOL_REGISTRY["get_active_plans"]`. Résultats : `{"rows": [...]}`.

- [ ] **Step 1: Tests qui échouent**

```python
# append à tests/test_agent_tools.py

def _full_context() -> ToolContext:
    return ToolContext(
        now=datetime(2026, 7, 2, 10, 0, tzinfo=UTC),
        allowed_symbols=frozenset({"2330.TW", "SAF.PA"}),
        data_age_by_symbol={"2330.TW": 12.4},
        market_context_by_symbol={
            "2330.TW": {"execution": {"enabled": False, "reason": "session_closed"},
                        "planning": {"enabled": True}},
        },
        active_watches_by_symbol={
            "2330.TW": [{"watch_id": "2330.TW:w1", "kind": "indicator_watch", "expires_at": "2026-07-03T01:00:00Z"}],
        },
    )


def test_get_freshness_rend_execution_planning_et_age():
    result, trace = agent_tools.execute_tool_call(
        AgentToolCall(id="c1", tool="get_freshness", args={"symbols": ["2330.TW"]}),
        _full_context(),
    )
    assert trace.outcome == "ok"
    row = result.result["rows"][0]
    assert row["symbol"] == "2330.TW"
    assert row["data_age_m"] == 12
    assert row["execution"] == {"enabled": False, "reason": "session_closed"}
    assert row["planning"] == {"enabled": True}


def test_get_freshness_symbole_hors_lot_marque_sans_crasher():
    result, _ = agent_tools.execute_tool_call(
        AgentToolCall(id="c1", tool="get_freshness", args={"symbols": ["EVIL"]}),
        _full_context(),
    )
    assert result.ok is True
    assert result.result["rows"][0] == {"symbol": "EVIL", "error": "symbol_not_allowed"}


def test_get_freshness_valide_ses_args():
    trace = validate_tool_call(
        {"id": "c1", "tool": "get_freshness", "args": {"symbols": []}},
        allowed_tools=frozenset({"get_freshness"}),
    )
    assert isinstance(trace, AgentToolTrace)
    assert trace.detail["reason"] == "invalid_args"


def test_get_active_plans_filtre_par_symbole_et_limite():
    result, _ = agent_tools.execute_tool_call(
        AgentToolCall(id="c1", tool="get_active_plans", args={"symbol": "2330.TW", "limit": 1}),
        _full_context(),
    )
    rows = result.result["rows"]
    assert len(rows) == 1
    assert rows[0]["watch_id"] == "2330.TW:w1"


def test_get_active_plans_sans_symbole_rend_tout_le_lot():
    result, _ = agent_tools.execute_tool_call(
        AgentToolCall(id="c1", tool="get_active_plans", args={}),
        _full_context(),
    )
    assert len(result.result["rows"]) == 1
```

- [ ] **Step 2: Vérifier l'échec**

Run: `uv run pytest tests/test_agent_tools.py -k "freshness or active_plans" -v`
Expected: FAIL — `KeyError: 'get_freshness'` (registre vide)

- [ ] **Step 3: Implémenter les deux handlers + enregistrement**

```python
# append à trader/agent_tools.py

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
```

- [ ] **Step 4: Vérifier le vert**

Run: `uv run pytest tests/test_agent_tools.py -v`
Expected: PASS (16 tests)

- [ ] **Step 5: Commit**

```bash
git add trader/agent_tools.py tests/test_agent_tools.py
git commit -m "feat(agent-tools): get_freshness + get_active_plans (V0 lecture seule)"
```

---

### Task 4: Outils `get_position_risk`, `get_attribution`, `get_recent_decisions`

**Files:**
- Modify: `trader/agent_tools.py`
- Test: `tests/test_agent_tools.py` (append)

**Interfaces:**
- Consumes: `ToolContext.position_risk_provider(sym) -> dict | None`, `.attribution` (payload `trader.attribution` déjà calculé : clés `summary`, `by_confidence`, `by_exit_reason`, `by_symbol`), `.recent_decisions_provider(symbol | None, limit) -> list[dict]`.
- Produces: 3 entrées `TOOL_REGISTRY`. `get_attribution` args: `{"scope": "summary|confidence|exit_reason|symbol", "symbol"?}`.

- [ ] **Step 1: Tests qui échouent**

```python
# append à tests/test_agent_tools.py

def _providers_context() -> ToolContext:
    return ToolContext(
        now=datetime(2026, 7, 2, 10, 0, tzinfo=UTC),
        allowed_symbols=frozenset({"2330.TW"}),
        attribution={
            "summary": {"n_closed_trades": 12, "realized_pnl": -84.2},
            "by_confidence": [{"bucket": "0.6-0.8", "n": 5}],
            "by_exit_reason": [{"reason": "stop", "n": 4}],
            "by_symbol": [{"symbol": "2330.TW", "n": 2}],
        },
        position_risk_provider=lambda sym: {"symbol": sym, "qty": 1000.0, "usd_exposure": 1023.0}
        if sym == "2330.TW" else None,
        recent_decisions_provider=lambda sym, limit: [
            {"cycle_ts": "2026-07-02T01:20:51Z", "symbol": sym or "2330.TW", "action": "HOLD",
             "reason": "quiet_gate", "executed": False}
        ][:limit],
    )


def test_get_position_risk_via_provider():
    result, trace = agent_tools.execute_tool_call(
        AgentToolCall(id="c1", tool="get_position_risk", args={"symbol": "2330.TW"}),
        _providers_context(),
    )
    assert trace.outcome == "ok"
    assert result.result["qty"] == 1000.0


def test_get_position_risk_hors_allowlist_rejete_au_handler():
    result, _ = agent_tools.execute_tool_call(
        AgentToolCall(id="c1", tool="get_position_risk", args={"symbol": "EVIL"}),
        _providers_context(),
    )
    assert result.ok is True
    assert result.result == {"symbol": "EVIL", "error": "symbol_not_allowed"}


def test_get_position_risk_provider_absent():
    ctx = ToolContext(now=datetime(2026, 7, 2, tzinfo=UTC), allowed_symbols=frozenset({"2330.TW"}))
    result, _ = agent_tools.execute_tool_call(
        AgentToolCall(id="c1", tool="get_position_risk", args={"symbol": "2330.TW"}), ctx)
    assert result.result == {"symbol": "2330.TW", "error": "unavailable"}


def test_get_attribution_scope_summary_et_symbol():
    ctx = _providers_context()
    result, _ = agent_tools.execute_tool_call(
        AgentToolCall(id="c1", tool="get_attribution", args={"scope": "summary"}), ctx)
    assert result.result == {"summary": {"n_closed_trades": 12, "realized_pnl": -84.2}}
    result2, _ = agent_tools.execute_tool_call(
        AgentToolCall(id="c2", tool="get_attribution", args={"scope": "symbol", "symbol": "2330.TW"}), ctx)
    assert result2.result == {"rows": [{"symbol": "2330.TW", "n": 2}]}


def test_get_attribution_scope_invalide():
    trace = validate_tool_call(
        {"id": "c1", "tool": "get_attribution", "args": {"scope": "everything"}},
        allowed_tools=frozenset({"get_attribution"}),
    )
    assert isinstance(trace, AgentToolTrace)
    assert trace.detail["reason"] == "invalid_args"


def test_get_recent_decisions_borne_la_limite():
    result, _ = agent_tools.execute_tool_call(
        AgentToolCall(id="c1", tool="get_recent_decisions", args={"symbol": "2330.TW", "limit": 999}),
        _providers_context(),
    )
    assert len(result.result["rows"]) <= agent_tools._MAX_DECISION_ROWS
```

- [ ] **Step 2: Vérifier l'échec**

Run: `uv run pytest tests/test_agent_tools.py -k "position_risk or attribution or recent_decisions" -v`
Expected: FAIL — `KeyError: 'get_position_risk'`

- [ ] **Step 3: Implémenter**

```python
# append à trader/agent_tools.py

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
    rows = [r for r in (context.attribution.get("by_symbol") or [])
            if r.get("symbol") == call.args.get("symbol")]
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
```

- [ ] **Step 4: Vérifier le vert**

Run: `uv run pytest tests/test_agent_tools.py -v`
Expected: PASS (22 tests)

- [ ] **Step 5: Commit**

```bash
git add trader/agent_tools.py tests/test_agent_tools.py
git commit -m "feat(agent-tools): get_position_risk + get_attribution + get_recent_decisions"
```

---

### Task 5: Outil `get_indicator_context` (successeur de REQUEST_CONTEXT)

**Files:**
- Modify: `trader/agent_tools.py`
- Test: `tests/test_agent_tools.py` (append)

**Interfaces:**
- Consumes: `ToolContext.indicator_resolver(requests: list[codex_client.IndicatorRequest]) -> dict` — côté daemon ce callable enveloppera `resolve_indicator_requests` (`trader/daemon.py`, déjà utilisé par le two-pass REQUEST_CONTEXT à `daemon.py:1428-1436`) avec les MÊMES bornes (`max_requests`, `max_indicators`).
- Produces: `TOOL_REGISTRY["get_indicator_context"]`. Args = le cube borné actuel : `{"symbol": str, "indicators": [str], "timeframe"?, "lookback"?, "window"?, "as_of"?}`.

- [ ] **Step 1: Tests qui échouent**

```python
# append à tests/test_agent_tools.py

def test_get_indicator_context_construit_la_requete_et_resout():
    captured: list = []

    def _resolver(requests):
        captured.extend(requests)
        return {"requests": [{"symbol": r.symbol, "indicators": r.indicators} for r in requests]}

    ctx = ToolContext(
        now=datetime(2026, 7, 2, tzinfo=UTC),
        allowed_symbols=frozenset({"2330.TW"}),
        indicator_resolver=_resolver,
    )
    result, trace = agent_tools.execute_tool_call(
        AgentToolCall(id="c1", tool="get_indicator_context",
                      args={"symbol": "2330.TW", "indicators": ["rsi14"], "timeframe": "4h", "window": 24}),
        ctx,
    )
    assert trace.outcome == "ok"
    assert captured[0].symbol == "2330.TW"
    assert captured[0].indicators == ["rsi14"]
    assert captured[0].timeframe == "4h"
    assert captured[0].window == 24
    assert result.result["requests"][0]["symbol"] == "2330.TW"


def test_get_indicator_context_symbole_hors_lot():
    ctx = ToolContext(now=datetime(2026, 7, 2, tzinfo=UTC), allowed_symbols=frozenset({"2330.TW"}),
                      indicator_resolver=lambda reqs: {"requests": []})
    result, _ = agent_tools.execute_tool_call(
        AgentToolCall(id="c1", tool="get_indicator_context",
                      args={"symbol": "EVIL", "indicators": ["rsi14"]}),
        ctx,
    )
    assert result.result == {"symbol": "EVIL", "error": "symbol_not_allowed"}


def test_get_indicator_context_args_invalides():
    trace = validate_tool_call(
        {"id": "c1", "tool": "get_indicator_context", "args": {"symbol": "2330.TW", "indicators": []}},
        allowed_tools=frozenset({"get_indicator_context"}),
    )
    assert isinstance(trace, AgentToolTrace)
    assert trace.detail["reason"] == "invalid_args"
```

- [ ] **Step 2: Vérifier l'échec**

Run: `uv run pytest tests/test_agent_tools.py -k indicator_context -v`
Expected: FAIL — `KeyError: 'get_indicator_context'`

- [ ] **Step 3: Implémenter**

```python
# append à trader/agent_tools.py
# import en tête de fichier (zone imports) :
#   from trader.codex_client import IndicatorRequest
# NOTE: import au niveau module SEULEMENT si codex_client n'importe pas agent_tools
# (vrai en Phase 1 ; en Task 7 codex_client reste indépendant d'agent_tools — le
# daemon fait le pont). Si un cycle apparaît, importer DANS le handler.

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
```

- [ ] **Step 4: Vérifier le vert + non-régression import**

Run: `uv run pytest tests/test_agent_tools.py tests/test_llm.py -v`
Expected: PASS — et aucun ImportError circulaire (agent_tools → codex_client est unidirectionnel).

- [ ] **Step 5: Commit**

```bash
git add trader/agent_tools.py tests/test_agent_tools.py
git commit -m "feat(agent-tools): get_indicator_context (cube borné, successeur REQUEST_CONTEXT)"
```

---

### Task 6: Sérialisation des traces + extension `tool_trace`

**Files:**
- Modify: `trader/agent_tools.py`, `trader/tool_trace.py:97-118`
- Test: `tests/test_agent_tools.py` (append), `tests/test_tool_trace.py` (append — le fichier existe, style : rows dict en entrée, asserts sur `summarize_tools`)

**Interfaces:**
- Produces:
  - `agent_tools.round_runtime_payload(traces, *, rounds) -> dict` → `{"tool_rounds": int, "tool_calls": [{"id","tool","args","outcome","detail"}]}` (schéma design §8).
  - `agent_tools.results_prompt_payload(results) -> list[dict]` → compact `{"id","tool","ok","result"|"error"}` pour réinjection prompt.
  - `agent_tools.calls_for_symbol(payload_calls, symbol) -> list[dict]` → les calls qui mentionnent `symbol` dans args + les calls sans symbole (globaux).
  - `tool_trace.summarize_tools(row)` inclut les domain tools : chaque call de `runtime.tool_calls` devient un item de `trace` et son nom entre dans `tools_used` ; `rounds` = max(rounds context, `runtime.tool_rounds`). `tools_skipped` reste calculé sur les 5 pseudo-tools legacy (compat).

- [ ] **Step 1: Tests qui échouent**

```python
# append à tests/test_agent_tools.py

def test_round_runtime_payload_schema_design():
    traces = [AgentToolTrace(id="c1", tool="get_freshness", args={"symbols": ["2330.TW"]},
                             outcome="ok", detail={"result_count": 1})]
    payload = agent_tools.round_runtime_payload(traces, rounds=1)
    assert payload == {"tool_rounds": 1, "tool_calls": [
        {"id": "c1", "tool": "get_freshness", "args": {"symbols": ["2330.TW"]},
         "outcome": "ok", "detail": {"result_count": 1}}]}


def test_results_prompt_payload_compact():
    results = [
        agent_tools.AgentToolResult(id="c1", tool="echo", ok=True, result={"x": 1}),
        agent_tools.AgentToolResult(id="c2", tool="echo", ok=False, error="rejected:unknown_tool"),
    ]
    payload = agent_tools.results_prompt_payload(results)
    assert payload == [
        {"id": "c1", "tool": "echo", "ok": True, "result": {"x": 1}},
        {"id": "c2", "tool": "echo", "ok": False, "error": "rejected:unknown_tool"},
    ]


def test_calls_for_symbol_filtre_et_garde_les_globaux():
    calls = [
        {"id": "c1", "tool": "get_freshness", "args": {"symbols": ["2330.TW"]}, "outcome": "ok", "detail": {}},
        {"id": "c2", "tool": "get_position_risk", "args": {"symbol": "SAF.PA"}, "outcome": "ok", "detail": {}},
        {"id": "c3", "tool": "get_attribution", "args": {"scope": "summary"}, "outcome": "ok", "detail": {}},
    ]
    mine = agent_tools.calls_for_symbol(calls, "2330.TW")
    assert [c["id"] for c in mine] == ["c1", "c3"]
```

```python
# append à tests/test_tool_trace.py

def test_summarize_tools_inclut_les_domain_tools():
    row = {
        "action": "HOLD",
        "runtime": {
            "tool_rounds": 1,
            "tool_calls": [
                {"id": "c1", "tool": "get_freshness", "args": {"symbols": ["2330.TW"]},
                 "outcome": "ok", "detail": {"result_count": 1}},
                {"id": "c2", "tool": "get_position_risk", "args": {"symbol": "2330.TW"},
                 "outcome": "rejected", "detail": {"reason": "invalid_args"}},
            ],
        },
    }
    summary = summarize_tools(row)
    assert "get_freshness" in summary["tools_used"]
    assert "get_position_risk" in summary["tools_used"]
    assert summary["rounds"] == 1
    domain_items = [item for item in summary["trace"] if item["tool"] == "get_freshness"]
    assert domain_items[0]["outcome"] == "ok"


def test_summarize_tools_sans_domain_tools_inchange():
    summary = summarize_tools({"action": "HOLD", "runtime": {}})
    assert summary["tools_used"] == []
    assert summary["rounds"] == 0
```

- [ ] **Step 2: Vérifier l'échec**

Run: `uv run pytest tests/test_agent_tools.py tests/test_tool_trace.py -v`
Expected: FAIL — `round_runtime_payload` absent + assertion domain tools.

- [ ] **Step 3: Implémenter**

```python
# append à trader/agent_tools.py

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
```

```python
# trader/tool_trace.py — remplacer summarize_tools (lignes 97-118) par :

def _domain_tool_traces(runtime: dict) -> list[dict]:
    calls = runtime.get("tool_calls")
    if not isinstance(calls, list):
        return []
    return [
        {
            "tool": str(call.get("tool") or "?"),
            "invoked": True,
            "args": call.get("args") or {},
            "outcome": call.get("outcome"),
            "detail": call.get("detail") or {},
        }
        for call in calls
        if isinstance(call, dict)
    ]


def summarize_tools(row: dict) -> dict:
    runtime = _runtime(row)
    context_request = runtime.get("context_request")
    context_rounds = (
        _as_int(context_request.get("rounds"))
        if isinstance(context_request, dict)
        else 0
    )
    trace = [
        _context_request_trace(runtime),
        _indicator_watch_trace(runtime),
        _next_wake_trace(row, runtime),
        _order_trace(row),
        _learning_trace(row),
        *_domain_tool_traces(runtime),
    ]
    tools_used = [item["tool"] for item in trace if item["invoked"]]
    return {
        "tools_used": tools_used,
        # compat : le taux d'usage legacy reste calculé sur les 5 pseudo-tools
        "tools_skipped": [tool for tool in TOOLS if tool not in tools_used],
        "rounds": max(context_rounds, _as_int(runtime.get("tool_rounds"))),
        "trace": trace,
    }
```

- [ ] **Step 4: Vérifier le vert + non-régression `tool_usage`**

Run: `uv run pytest tests/test_agent_tools.py tests/test_tool_trace.py tests/test_tool_usage.py -v`
Expected: PASS — les tests tool_usage existants (basés sur les 5 legacy) inchangés.

- [ ] **Step 5: Commit**

```bash
git add trader/agent_tools.py trader/tool_trace.py tests/test_agent_tools.py tests/test_tool_trace.py
git commit -m "feat(agent-tools): payloads runtime/prompt + summarize_tools voit les domain tools"
```

---

### Task 7: Parser `tool_calls` dans codex_client

**Files:**
- Modify: `trader/codex_client.py` (autour de `parse_batch`, ligne 621 ; dataclass `Decision`, ligne 53)
- Test: `tests/test_codex_client.py` (append — fichier existant)

**Interfaces:**
- Consumes: `_extract_json` (`codex_client.py:498`), `parse_batch` existant.
- Produces:
  - Champ `Decision.domain_tools: dict | None = None` (transporte le payload de Task 6 jusqu'au ledger).
  - `BatchToolCallRequest(calls, llm_provider, llm_model, llm_fallback_reason)`.
  - `parse_batch_or_tool_calls(raw_text, symbols, *, allow_context_request) -> dict[str, Decision | ContextResearchRequest] | BatchToolCallRequest`.
  - `parse_batch` refactoré pour partager `_parse_batch_data(data, symbols, allow_context_request)` — comportement STRICTEMENT identique (tests existants inchangés).

- [ ] **Step 1: Tests qui échouent**

```python
# append à tests/test_codex_client.py

def test_parse_batch_or_tool_calls_detecte_une_tournee():
    raw = '{"tool_calls": [{"id": "c1", "tool": "get_freshness", "args": {"symbols": ["2330.TW"]}}]}'
    out = codex_client.parse_batch_or_tool_calls(raw, ["2330.TW"], allow_context_request=True)
    assert isinstance(out, codex_client.BatchToolCallRequest)
    assert out.calls[0]["tool"] == "get_freshness"


def test_parse_batch_or_tool_calls_ignore_les_elements_non_objets():
    raw = '{"tool_calls": [42, {"id": "c1", "tool": "t", "args": {}}]}'
    out = codex_client.parse_batch_or_tool_calls(raw, ["2330.TW"], allow_context_request=True)
    assert isinstance(out, codex_client.BatchToolCallRequest)
    assert len(out.calls) == 1


def test_parse_batch_or_tool_calls_tombe_sur_les_decisions():
    raw = '{"decisions": [{"symbol": "2330.TW", "action": "HOLD", "quantity": 0, "confidence": 0.1, "rationale": "r", "decision_reason_code": "NO_EDGE"}]}'
    out = codex_client.parse_batch_or_tool_calls(raw, ["2330.TW"], allow_context_request=False)
    assert isinstance(out, dict)
    assert out["2330.TW"].action == "HOLD"


def test_parse_batch_or_tool_calls_json_invalide_hold_global():
    out = codex_client.parse_batch_or_tool_calls("pas du json", ["2330.TW"], allow_context_request=False)
    assert isinstance(out, dict)
    assert out["2330.TW"].rationale == "batch_bad_output"


def test_parse_batch_or_tool_calls_tool_calls_vides_ne_declenchent_pas():
    raw = '{"tool_calls": [], "decisions": [{"symbol": "2330.TW", "action": "HOLD", "quantity": 0, "confidence": 0, "rationale": "r", "decision_reason_code": "NO_EDGE"}]}'
    out = codex_client.parse_batch_or_tool_calls(raw, ["2330.TW"], allow_context_request=False)
    assert isinstance(out, dict)
```

- [ ] **Step 2: Vérifier l'échec**

Run: `uv run pytest tests/test_codex_client.py -k tool_calls -v`
Expected: FAIL — `AttributeError: ... 'BatchToolCallRequest'`

- [ ] **Step 3: Implémenter**

```python
# trader/codex_client.py

# 1) Decision (ligne 53) — ajouter le champ transport après `learning`:
#      domain_tools: dict | None = None  # traces tournée d'outils (runtime.tool_*)

# 2) Après ContextResearchRequest (ligne 97) :
@dataclass(frozen=True)
class BatchToolCallRequest:
    """Le lot a répondu par une tournée d'outils au lieu de décisions finales.

    `calls` reste BRUT (list[dict]) : la validation vit dans trader.agent_tools,
    côté daemon — codex_client reste un transport sans dépendance domaine."""
    calls: list[dict]
    llm_provider: str | None = None
    llm_model: str | None = None
    llm_fallback_reason: str | None = None


# 3) Refactor parse_batch (ligne 621) : extraire le corps en
#    _parse_batch_data(data: dict, symbols, *, allow_context_request) qui reçoit
#    le dict déjà extrait ; parse_batch devient :
def parse_batch(
    raw_text: str, symbols: list[str], *, allow_context_request: bool
) -> dict[str, Decision | ContextResearchRequest]:
    try:
        data = _extract_json(raw_text)
    except (ValueError, json.JSONDecodeError):
        return {sym: Decision.hold(sym, "batch_bad_output") for sym in symbols}
    return _parse_batch_data(data, symbols, allow_context_request=allow_context_request)
#    (_parse_batch_data reprend la boucle isolation per-élément + setdefault
#     missing_in_batch EXACTEMENT comme aujourd'hui, en lisant data.get("decisions")
#     — déplacer la levée ValueError de _extract_decisions_array dans le try.)


# 4) Le nouveau point d'entrée :
def parse_batch_or_tool_calls(
    raw_text: str, symbols: list[str], *, allow_context_request: bool
) -> dict[str, Decision | ContextResearchRequest] | BatchToolCallRequest:
    """Réponse batch OU tournée d'outils. tool_calls non vide prime ; toute
    malformation retombe sur le chemin décisions (fail-safe HOLD)."""
    try:
        data = _extract_json(raw_text)
    except (ValueError, json.JSONDecodeError):
        return {sym: Decision.hold(sym, "batch_bad_output") for sym in symbols}
    raw_calls = data.get("tool_calls")
    if isinstance(raw_calls, list):
        calls = [c for c in raw_calls if isinstance(c, dict)]
        if calls:
            return BatchToolCallRequest(calls=calls)
    return _parse_batch_data(data, symbols, allow_context_request=allow_context_request)


# 5) _attach_llm_metadata (ligne 656) : accepter BatchToolCallRequest dans le
#    type hint — `replace()` fonctionne déjà (mêmes noms de champs).
```

- [ ] **Step 4: Vérifier le vert (y compris non-régression parse_batch)**

Run: `uv run pytest tests/test_codex_client.py -v`
Expected: PASS — tous les tests parse_batch existants passent sans modification.

- [ ] **Step 5: Commit**

```bash
git add trader/codex_client.py tests/test_codex_client.py
git commit -m "feat(codex-client): parse des réponses tool_calls (BatchToolCallRequest)"
```

---

### Task 8: Catalogue d'outils dans le prompt + `decide_batch(allow_tool_calls)`

**Files:**
- Modify: `trader/codex_client.py` (`build_batch_prompt` ligne 470, `decide_batch` ligne 718)
- Test: `tests/test_codex_client.py` (append)

**Interfaces:**
- Consumes: `BatchToolCallRequest`, `parse_batch_or_tool_calls` (Task 7).
- Produces: `build_batch_prompt(..., allow_tool_calls: bool = False)` ; `decide_batch(..., allow_tool_calls: bool = False) -> dict[...] | BatchToolCallRequest`.

- [ ] **Step 1: Tests qui échouent**

```python
# append à tests/test_codex_client.py

def test_build_batch_prompt_sans_flag_ne_mentionne_pas_les_outils():
    prompt = codex_client.build_batch_prompt(
        mandate="m", memory="mem", shared_context={}, symbols_payload=[{"symbol": "2330.TW"}],
        allow_context_request=True,
    )
    assert "tool_calls" not in prompt


def test_build_batch_prompt_avec_flag_expose_le_catalogue():
    prompt = codex_client.build_batch_prompt(
        mandate="m", memory="mem", shared_context={}, symbols_payload=[{"symbol": "2330.TW"}],
        allow_context_request=True, allow_tool_calls=True,
    )
    assert '"tool_calls"' in prompt
    assert "get_freshness" in prompt
    assert "get_indicator_context" in prompt


def test_decide_batch_retourne_la_tournee_quand_le_llm_la_demande(monkeypatch):
    class _Router:
        def complete(self, prompt, timeout_s):
            return llm.LlmCompletion(
                text='{"tool_calls": [{"id": "c1", "tool": "get_freshness", "args": {"symbols": ["2330.TW"]}}]}',
                provider="acpx", model="gpt-5.5", fallback_reason=None,
            )
    out = codex_client.decide_batch(
        symbols=["2330.TW"], mandate="m", memory="mem", shared_context={},
        per_symbol={"2330.TW": {}}, llm_router=_Router(), allow_tool_calls=True,
    )
    assert isinstance(out, codex_client.BatchToolCallRequest)
    assert out.llm_provider == "acpx"


def test_decide_batch_sans_flag_ignore_les_tool_calls(monkeypatch):
    class _Router:
        def complete(self, prompt, timeout_s):
            return llm.LlmCompletion(
                text='{"tool_calls": [{"id": "c1", "tool": "get_freshness", "args": {}}]}',
                provider="acpx", model="gpt-5.5", fallback_reason=None,
            )
    out = codex_client.decide_batch(
        symbols=["2330.TW"], mandate="m", memory="mem", shared_context={},
        per_symbol={"2330.TW": {}}, llm_router=_Router(), allow_tool_calls=False,
    )
    # flag éteint => parse_batch classique => pas de clé decisions => HOLD fail-safe
    assert isinstance(out, dict)
    assert out["2330.TW"].action == "HOLD"
```

Note : si `llm.LlmCompletion` a une signature différente (vérifier `trader/llm.py`), adapter la construction du stub aux champs réels (`text`, `provider`, `model`, `fallback_reason`).

- [ ] **Step 2: Vérifier l'échec**

Run: `uv run pytest tests/test_codex_client.py -k "catalogue or tournee or ignore_les_tool" -v`
Expected: FAIL — `TypeError: build_batch_prompt() got an unexpected keyword argument 'allow_tool_calls'`

- [ ] **Step 3: Implémenter**

```python
# trader/codex_client.py — au-dessus de build_batch_prompt :

_TOOL_CATALOG = (
    "# Outils domaine (OPTIONNELS — une seule tournée)\n"
    "Si le cockpit suffit, rends directement le contrat final. Sinon tu peux demander\n"
    "UNE tournée d'outils lecture-seule en répondant À LA PLACE du contrat final :\n"
    '{"tool_calls": [{"id": "c1", "tool": "<nom>", "args": {...}}]}\n'
    "Bornes : 3 appels max par symbole, 24 par lot. Outils :\n"
    "- get_freshness{symbols:[…]} : exécution/planification/âge des données par symbole\n"
    "- get_active_plans{symbol?,limit?} : veilles et plans armés actifs (corrige au lieu d'empiler)\n"
    "- get_position_risk{symbol} : position, exposition USD, bornes de risque\n"
    "- get_attribution{scope:summary|confidence|exit_reason|symbol, symbol?} : perf attribuée compacte\n"
    "- get_recent_decisions{symbol?,limit?} : dernières décisions et blocages du ledger\n"
    "- get_indicator_context{symbol,indicators:[…],timeframe?,lookback?,window?,as_of?} : cube indicateurs borné\n"
    "Après la tournée tu recevras `tool_results` par symbole et tu DEVRAS rendre le contrat final\n"
    "(toute nouvelle tournée sera bloquée en HOLD).\n\n"
)

# build_batch_prompt : ajouter le paramètre `allow_tool_calls: bool = False` et
# insérer entre le vocabulaire et le contexte partagé :
#       f"{_TOOL_CATALOG if allow_tool_calls else ''}"

# decide_batch : ajouter `allow_tool_calls: bool = False`, le passer à
# build_batch_prompt, puis remplacer l'appel parse par :
#     if allow_tool_calls:
#         results = parse_batch_or_tool_calls(completion.text, symbols,
#                                             allow_context_request=allow_context_request)
#         if isinstance(results, BatchToolCallRequest):
#             return _attach_llm_metadata(results, completion)
#     else:
#         results = parse_batch(completion.text, symbols, allow_context_request=allow_context_request)
#     return {sym: _attach_llm_metadata(resp, completion) for sym, resp in results.items()}
```

- [ ] **Step 4: Vérifier le vert**

Run: `uv run pytest tests/test_codex_client.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add trader/codex_client.py tests/test_codex_client.py
git commit -m "feat(codex-client): catalogue d'outils au prompt + decide_batch(allow_tool_calls)"
```

---

### Task 9: Tournée d'outils dans le daemon, derrière le flag

**Files:**
- Modify: `trader/daemon.py` (`_batch_decide` ligne 1286, `_decide_chunks`/`_call` lignes 1358-1397, entry ligne 2396, argparse vers ligne 2882 — repérer les 3 sites de plumbing avec `grep -n "decision_batch_parallelism" trader/daemon.py` et faire pareil), `trader/decision_ledger.py:128-145`
- Test: Create `tests/test_daemon_tool_round.py`

**Interfaces:**
- Consumes: `agent_tools.execute_tool_round`, `.ToolContext`, `.ToolRoundLimits`, `.round_runtime_payload`, `.results_prompt_payload`, `.calls_for_symbol` ; `codex_client.decide_batch(allow_tool_calls=)`, `.BatchToolCallRequest` ; champ `Decision.domain_tools`.
- Produces: `_batch_decide(..., agent_tools_enabled: bool = False)` ; flag CLI `--agent-tools` / env `CASYS_AGENT_TOOLS_ENABLED` (défaut 0) ; rows ledger avec `runtime.tool_rounds` / `runtime.tool_calls`.

- [ ] **Step 1: Tests qui échouent**

S'inspirer des fixtures existantes de `tests/test_daemon_sizing.py` (qui appellent `_batch_decide` avec `monkeypatch.setattr(daemon.codex_client, "decide_batch", ...)`) — reprendre leur construction minimale de kwargs.

```python
# tests/test_daemon_tool_round.py
"""Tournée d'outils domaine dans _batch_decide (flag CASYS_AGENT_TOOLS_ENABLED)."""
from __future__ import annotations

from datetime import datetime, timezone

from trader import codex_client, daemon

UTC = timezone.utc
NOW = datetime(2026, 7, 2, 10, 0, tzinfo=UTC)


def _kwargs(**overrides):
    base = dict(
        decidable=["2330.TW"],
        mandate="m",
        memory="mem",
        shared_context={},
        triggers_by_symbol={},
        tradable_bars_by_symbol={},
        tradable_symbols=["2330.TW"],
        runtime_interval="1h",
        runtime_lookback="1mo",
        max_context_requests_per_symbol=2,
        max_indicators_per_request=4,
        max_model_calls=10,
        now=NOW,
        data_age_by_symbol={"2330.TW": 5.0},
    )
    base.update(overrides)
    return base


def _hold(sym):
    return codex_client.Decision.hold(sym, "test")


def test_flag_off_ne_passe_jamais_allow_tool_calls(monkeypatch):
    seen = []

    def _fake_decide_batch(**kwargs):
        seen.append(kwargs.get("allow_tool_calls", False))
        return {sym: _hold(sym) for sym in kwargs["symbols"]}

    monkeypatch.setattr(daemon.codex_client, "decide_batch", _fake_decide_batch)
    decisions, calls = daemon._batch_decide(**_kwargs())  # défaut : agent_tools_enabled=False
    assert calls == 1
    assert seen == [False]
    assert decisions["2330.TW"].action == "HOLD"


def test_flag_on_tournee_puis_decision_finale(monkeypatch):
    calls_seen = []

    def _fake_decide_batch(**kwargs):
        calls_seen.append(kwargs)
        if kwargs.get("allow_tool_calls"):
            return codex_client.BatchToolCallRequest(
                calls=[{"id": "c1", "tool": "get_freshness", "args": {"symbols": ["2330.TW"]}}],
                llm_provider="acpx", llm_model="gpt-5.5",
            )
        return {sym: _hold(sym) for sym in kwargs["symbols"]}

    monkeypatch.setattr(daemon.codex_client, "decide_batch", _fake_decide_batch)
    decisions, calls = daemon._batch_decide(**_kwargs(), agent_tools_enabled=True)

    assert calls == 2  # la tournée consomme un appel modèle + le tour final
    final_kwargs = calls_seen[-1]
    assert final_kwargs.get("allow_tool_calls") is False
    # tool_results compacts réinjectés au tour final
    assert "tool_results" in final_kwargs["per_symbol"]["2330.TW"]
    # traces attachées à la décision pour persistance ledger
    dt = decisions["2330.TW"].domain_tools
    assert dt["tool_rounds"] == 1
    assert dt["tool_calls"][0]["tool"] == "get_freshness"
    assert dt["tool_calls"][0]["outcome"] == "ok"


def test_flag_on_seconde_tournee_bloquee_en_hold(monkeypatch):
    def _fake_decide_batch(**kwargs):
        # le LLM re-demande des outils même au tour final
        return codex_client.BatchToolCallRequest(
            calls=[{"id": "c1", "tool": "get_freshness", "args": {"symbols": ["2330.TW"]}}])

    monkeypatch.setattr(daemon.codex_client, "decide_batch", _fake_decide_batch)
    decisions, calls = daemon._batch_decide(**_kwargs(), agent_tools_enabled=True)
    assert decisions["2330.TW"].action == "HOLD"
    assert decisions["2330.TW"].rationale == "tool_loop_blocked"


def test_ledger_row_persiste_les_traces():
    """decision_ledger construit runtime.tool_rounds / runtime.tool_calls."""
    from trader import decision_ledger

    entry = {
        "symbol": "2330.TW", "action": "HOLD", "qty": 0.0, "confidence": 0.0,
        "rationale": "r", "decision_reason_code": "NO_EDGE",
        "tool_rounds": 1,
        "tool_calls": [{"id": "c1", "tool": "get_freshness", "args": {}, "outcome": "ok", "detail": {}}],
    }
    row = decision_ledger.build_row(
        decision=entry, report={}, cycle_ts="2026-07-02T10:00:00+00:00",
        sequence=1, source="daemon", code_version={}, stale_market_data={},
    )
    assert row["runtime"]["tool_rounds"] == 1
    assert row["runtime"]["tool_calls"][0]["tool"] == "get_freshness"
```

Note : vérifier la signature réelle du constructeur de row dans `trader/decision_ledger.py` (autour de la ligne 90 — la fonction qui produit le dict des lignes 100-148) et adapter le dernier test à ses kwargs exacts.

- [ ] **Step 2: Vérifier l'échec**

Run: `uv run pytest tests/test_daemon_tool_round.py -v`
Expected: FAIL — `TypeError: _batch_decide() got an unexpected keyword argument 'agent_tools_enabled'`

- [ ] **Step 3: Implémenter côté daemon**

```python
# trader/daemon.py

# 1) import en tête : from . import agent_tools

# 2) _batch_decide : + paramètre `agent_tools_enabled: bool = False`.

# 3) Helper au-dessus de _batch_decide — construit le contexte étroit et joue
#    la tournée (les providers lourds viennent de ce que le cycle a déjà) :
def _run_tool_round(
    request: codex_client.BatchToolCallRequest,
    *,
    chunk: list[str],
    now: datetime,
    data_age_by_symbol: dict[str, float],
    market_contexts: dict[str, dict],
    active_watches_by_symbol: dict[str, list],
    shared_context: dict,
    indicator_resolver,
) -> tuple[list[dict], dict]:
    """Retourne (tool_results compacts pour le prompt, payload runtime durable)."""
    context = agent_tools.ToolContext(
        now=now,
        allowed_symbols=frozenset(chunk),
        data_age_by_symbol=data_age_by_symbol,
        market_context_by_symbol=market_contexts,
        active_watches_by_symbol=active_watches_by_symbol,
        attribution=shared_context.get("attribution"),
        # V0 : pas de providers portefeuille/ledger — les outils répondent
        # "unavailable" proprement ; branchement en suivi quand mesuré utile.
        position_risk_provider=None,
        recent_decisions_provider=None,
        indicator_resolver=indicator_resolver,
    )
    results, traces = agent_tools.execute_tool_round(
        request.calls,
        context=context,
        limits=agent_tools.ToolRoundLimits(),
        allowed_tools=frozenset(agent_tools.TOOL_REGISTRY),
    )
    return (
        agent_tools.results_prompt_payload(results),
        agent_tools.round_runtime_payload(traces, rounds=1),
    )

# 4) Dans _batch_decide, l'indicator_resolver réutilise les bornes REQUEST_CONTEXT :
#    def _indicator_resolver(requests):
#        return resolve_indicator_requests(
#            requests, tradable_bars_by_symbol, symbols=tradable_symbols,
#            max_requests=max_context_requests_per_symbol,
#            max_indicators=max_indicators_per_request,
#            cached_interval=runtime_interval, cached_lookback=runtime_lookback)

# 5) Dans `_call(chunk)` (ligne 1371) : retourner (responses, n_calls) au lieu de
#    responses seul, et gérer la tournée :
#      - 1er appel : decide_batch(..., allow_tool_calls=agent_tools_enabled and allow_context_request)
#      - si BatchToolCallRequest :
#          tool_results, runtime_payload = _run_tool_round(...)
#          per_symbol enrichi : per_symbol_payload[sym] + {"tool_results":
#              [r pour r dans tool_results si r concerne sym ou global]}
#            (réutiliser agent_tools.calls_for_symbol sur runtime_payload["tool_calls"]
#             pour le filtre, même logique pour les results par id)
#          2e appel : decide_batch(..., allow_tool_calls=False, allow_context_request=False)
#          si le 2e retour est ENCORE un BatchToolCallRequest :
#              {sym: Decision.hold(sym, "tool_loop_blocked") for sym in chunk}, n_calls=2
#          sinon : attacher replace(decision, domain_tools={
#              "tool_rounds": 1,
#              "tool_calls": agent_tools.calls_for_symbol(runtime_payload["tool_calls"], sym)})
#          n_calls=2
#      - sinon : comportement actuel, n_calls=1
#    `_decide_chunks` somme les n_calls retournés (remplace `len(allowed_chunks)`).

# 6) entry (ligne 2396) — ajouter après "context_request":
#        "tool_rounds": (decision.domain_tools or {}).get("tool_rounds"),
#        "tool_calls": (decision.domain_tools or {}).get("tool_calls"),

# 7) argparse (modèle ligne 2882) :
#    parser.add_argument("--agent-tools", action=argparse.BooleanOptionalAction,
#        default=_env_int("CASYS_AGENT_TOOLS_ENABLED", 0) == 1,
#        help="tournée d'outils domaine pour le LLM (défaut/env CASYS_AGENT_TOOLS_ENABLED: 0)")
#    puis suivre le plumbing de `decision_batch_parallelism` (grep) pour passer
#    args.agent_tools -> run_cycle(...) -> _batch_decide(agent_tools_enabled=...).
```

```python
# trader/decision_ledger.py — dans le dict "runtime" (lignes 128-145), ajouter :
            "tool_rounds": decision.get("tool_rounds"),
            "tool_calls": decision.get("tool_calls"),
```

- [ ] **Step 4: Vérifier le vert + non-régression complète**

Run: `uv run pytest tests/test_daemon_tool_round.py -v`
Expected: PASS
Run: `uv run pytest -q > /tmp/pytest-task9.log 2>&1; echo "EXIT=$?"`
Expected: `EXIT=0` — notamment `tests/test_daemon_sizing.py` (budget/chunks intacts avec flag off) et les tests stale/session-closed/risk-gate (invariants design §12).

- [ ] **Step 5: Commit**

```bash
git add trader/daemon.py trader/decision_ledger.py tests/test_daemon_tool_round.py
git commit -m "feat(daemon): tournée d'outils domaine derrière CASYS_AGENT_TOOLS_ENABLED (défaut off)"
```

---

### Task 10: Documentation d'exploitation + statut du design

**Files:**
- Modify: `docs/superpowers/specs/2026-06-29-agent-domain-tools-design.md` (ligne 5, `**Status**`), `.env.example`

**Interfaces:**
- Consumes: tout ce qui précède.
- Produces: doc à jour ; aucun code.

- [ ] **Step 1: Mettre à jour le statut du design**

```markdown
**Status**: Phases 1-2 implémentées (registry V0 lecture-seule + tool loop),
flag `CASYS_AGENT_TOOLS_ENABLED` éteint par défaut. Phases 3-5 (migration
REQUEST_CONTEXT, scheduling/watches, propose_order) non commencées.
```

- [ ] **Step 2: Documenter le flag dans `.env.example`**

```bash
# Tournée d'outils domaine pour le LLM runtime (design 2026-06-29).
# 0 (défaut) = comportement historique ; 1 = le LLM peut demander UNE tournée
# d'outils lecture-seule (get_freshness, get_active_plans, ...) avant de décider.
CASYS_AGENT_TOOLS_ENABLED=0
```

- [ ] **Step 3: Vérification finale complète**

Run: `uv run ruff check && uv run pytest -q > /tmp/pytest-final.log 2>&1; echo "EXIT=$?"`
Expected: `All checks passed!` puis `EXIT=0`

- [ ] **Step 4: Commit**

```bash
git add docs/superpowers/specs/2026-06-29-agent-domain-tools-design.md .env.example
git commit -m "docs(agent-tools): statut design + flag CASYS_AGENT_TOOLS_ENABLED documenté"
```

---

### Task 11: Outils sémantiques `describe_data` + `find_indicators` (extension validée par Erwan le 02/07)

Expose la couche sémantique gouvernée (`trader/semantic/catalog.py` — TraderNexus)
comme porte d'entrée de découverte : le LLM peut demander « qu'est-ce qui
existe ? » avant de requêter le cube via `get_indicator_context`.

**Files:**
- Modify: `trader/agent_tools.py`, `trader/codex_client.py` (`_TOOL_CATALOG`)
- Test: `tests/test_agent_tools.py` (append), `tests/test_codex_client.py` (append)

**Interfaces:**
- Consumes: `trader.semantic.catalog.find_indicators(concept: str) -> list[dict]` (recherche plein-texte sur name/label/description/category/concepts ; concept vide = tout), `catalog.TIMEFRAMES`, `catalog.WINDOWS`, `catalog.AS_OF_MODES`, `catalog.LEVELS`, `catalog.list_indicators()`.
- Produces: `TOOL_REGISTRY["describe_data"]` (args: aucun) et `TOOL_REGISTRY["find_indicators"]` (args: `{"concept": str}`), résultats compacts et bornés.

- [ ] **Step 1: Tests qui échouent**

```python
# append à tests/test_agent_tools.py

def test_describe_data_rend_le_cube_compact():
    result, trace = agent_tools.execute_tool_call(
        AgentToolCall(id="c1", tool="describe_data", args={}), _context())
    assert trace.outcome == "ok"
    d = result.result
    assert "1h" in d["timeframes"] and "lookbacks" in d["timeframes"]["1h"]
    assert d["windows"] and d["as_of_modes"] == ["latest"]
    # indicateurs en forme compacte : name + category, pas les specs complètes
    assert all(set(i) <= {"name", "label", "category", "concepts"} for i in d["indicators"])


def test_find_indicators_par_concept_borne():
    result, trace = agent_tools.execute_tool_call(
        AgentToolCall(id="c1", tool="find_indicators", args={"concept": "momentum"}), _context())
    assert trace.outcome == "ok"
    rows = result.result["rows"]
    assert 0 < len(rows) <= agent_tools._MAX_INDICATOR_MATCHES
    assert all("name" in r and "description" in r for r in rows)


def test_find_indicators_concept_requis():
    trace = validate_tool_call(
        {"id": "c1", "tool": "find_indicators", "args": {}},
        allowed_tools=frozenset({"find_indicators"}))
    assert isinstance(trace, AgentToolTrace)
    assert trace.detail["reason"] == "invalid_args"
```

```python
# append à tests/test_codex_client.py

def test_catalogue_prompt_expose_les_outils_semantiques():
    prompt = codex_client.build_batch_prompt(
        mandate="m", memory="mem", shared_context={}, symbols_payload=[{"symbol": "SPY"}],
        allow_context_request=True, allow_tool_calls=True)
    assert "describe_data" in prompt
    assert "find_indicators" in prompt
```

- [ ] **Step 2: Vérifier l'échec**

Run: `uv run pytest tests/test_agent_tools.py -k "describe_data or find_indicators" tests/test_codex_client.py -k semantiques -v`
Expected: FAIL — `KeyError: 'describe_data'`

- [ ] **Step 3: Implémenter**

```python
# append à trader/agent_tools.py
# import en tête : from trader.semantic import catalog as semantic_catalog

_MAX_INDICATOR_MATCHES = 12


def _handle_describe_data(call: AgentToolCall, context: ToolContext) -> dict:
    """Auto-description du cube sémantique (compacte : la découverte, pas les specs)."""
    return {
        "levels": list(semantic_catalog.LEVELS),
        "timeframes": {
            name: {"lookbacks": spec["lookbacks"], "default_lookback": spec["default_lookback"],
                   "default_window": spec["default_window"], "style": spec["style"]}
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
```

```python
# trader/codex_client.py — dans _TOOL_CATALOG, ajouter 2 lignes avant get_indicator_context :
    "- describe_data{} : cube sémantique (timeframes/lookbacks valides, windows, indicateurs)\n"
    "- find_indicators{concept} : cherche des indicateurs par concept (momentum, volatilité, …)\n"
```

- [ ] **Step 4: Vérifier le vert + suite complète**

Run: `uv run ruff check && uv run pytest -q > /tmp/pytest-task11.log 2>&1; echo "EXIT=$?"`
Expected: `All checks passed!` puis `EXIT=0`

- [ ] **Step 5: Commit**

```bash
git add trader/agent_tools.py trader/codex_client.py tests/test_agent_tools.py tests/test_codex_client.py
git commit -m "feat(agent-tools): describe_data + find_indicators — la couche sémantique devient la porte de découverte"
```

---

## Hors périmètre de ce plan (suites du design)

- Migration effective de `REQUEST_CONTEXT` vers `get_indicator_context` (Phase 3) : nécessite d'abord des traces live du tool round flag-on.
- Outils d'action non-broker `set_next_wake` / `propose_indicator_watch` / `cancel_watch` / `record_learning` (Phase 4).
- `propose_order` (Phase 5) — volontairement dernier.
- Branchement des providers `position_risk` / `recent_decisions` réels (broker snapshot, `DecisionLedgerStore`) : les outils répondent `unavailable` en V0 ; à câbler quand la mesure d'usage le justifie.
- Compteurs dédiés `tool_usage` par outil domaine (le canal `summarize_tools` les expose déjà dans `trace`/`tools_used`).

## Décision d'activation

Une fois mergé : laisser le flag OFF quelques jours, puis activer `CASYS_AGENT_TOOLS_ENABLED=1` sur une session de marché calme et mesurer via `runtime.tool_calls` (taux d'usage, rejets, coût en appels modèle ×2 par chunk avec tournée) avant d'envisager la Phase 3.
