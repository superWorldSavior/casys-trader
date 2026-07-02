"""agent_tools — validation, budgets, handlers lecture-seule (design 2026-06-29)."""
from __future__ import annotations

from datetime import datetime, timezone

from trader import agent_tools  # noqa: F401 — utilisé dans les tâches 2+
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


# ---------------------------------------------------------------------------
# Task 2 : execute_tool_call + execute_tool_round (budgets, erreurs compactes)
# ---------------------------------------------------------------------------

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
