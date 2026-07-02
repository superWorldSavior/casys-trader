"""agent_tools — validation, budgets, handlers lecture-seule (design 2026-06-29)."""
from __future__ import annotations

from datetime import timezone

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
