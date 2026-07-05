"""T1b — orchestration grain-1 `resolve_symbol_decision` (spec queue tool-round §6.7).

Couvre les 4 comportements : décision directe (pas de round), un round + merge des
traces dans domain_tools, blocage tool-loop au tour final, multi-tour (max_rounds=2).
`call_model` est un stub séquentiel pilotant les réponses du LLM.
"""
from datetime import datetime, timezone

import trader.agent.tools as agent_tools
from trader.agent import client as codex_client
from trader.application.tool_round import resolve_symbol_decision

Decision = codex_client.Decision
BatchToolCallRequest = codex_client.BatchToolCallRequest


def _ctx() -> agent_tools.ToolContext:
    return agent_tools.ToolContext(
        now=datetime(2026, 7, 5, tzinfo=timezone.utc),
        allowed_symbols=frozenset({"AAPL"}),
        active_watches_by_symbol={"AAPL": [{"id": "w1", "kind": "stop"}]},
    )


def _seq_call_model(responses: list):
    """Stub `call_model` : renvoie les réponses en séquence, log les allow_tool_calls."""
    log: list[bool] = []
    it = iter(responses)

    def call_model(per_symbol, *, allow_tool_calls):
        log.append(allow_tool_calls)
        return next(it)

    return call_model, log


def _tool_request() -> BatchToolCallRequest:
    return BatchToolCallRequest(calls=[{"id": "c1", "tool": "get_active_plans", "args": {"symbol": "AAPL"}}])


def test_decision_directe_sans_round() -> None:
    decision = Decision.hold("AAPL", "direct")
    call_model, log = _seq_call_model([{"AAPL": decision}])

    result = resolve_symbol_decision(symbol="AAPL", base_facts={}, tool_context=_ctx(), call_model=call_model)

    assert result is decision
    assert result.domain_tools is None
    assert log == [True]  # un seul appel, outils autorisés, pas de round


def test_un_round_puis_decision_merge_les_traces() -> None:
    final = Decision.hold("AAPL", "after tools")
    call_model, log = _seq_call_model([_tool_request(), {"AAPL": final}])

    result = resolve_symbol_decision(symbol="AAPL", base_facts={}, tool_context=_ctx(), call_model=call_model)

    assert result.rationale == "after tools"
    assert result.domain_tools is not None
    assert result.domain_tools["tool_rounds"] == 1
    assert any(tc["tool"] == "get_active_plans" for tc in result.domain_tools["tool_calls"])
    assert log == [True, False]  # round (outils), puis tour final (sans outils)


def test_tool_loop_bloque_au_tour_final() -> None:
    call_model, log = _seq_call_model([_tool_request(), _tool_request()])  # redemande au tour final

    result = resolve_symbol_decision(symbol="AAPL", base_facts={}, tool_context=_ctx(), call_model=call_model)

    assert result.action == "HOLD"
    assert result.rationale == "tool_loop_blocked"
    assert log == [True, False]


def test_max_rounds_2_enchaine_deux_tournees() -> None:
    final = Decision.hold("AAPL", "after 2 rounds")
    call_model, log = _seq_call_model([_tool_request(), _tool_request(), {"AAPL": final}])

    result = resolve_symbol_decision(
        symbol="AAPL", base_facts={}, tool_context=_ctx(), call_model=call_model, max_rounds=2
    )

    assert result.rationale == "after 2 rounds"
    assert result.domain_tools["tool_rounds"] == 2
    assert log == [True, True, False]  # 2 tournées d'outils, puis tour final forcé
