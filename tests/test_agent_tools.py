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


# ---------------------------------------------------------------------------
# Task 3 : get_freshness + get_active_plans (handlers lecture-seule)
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Task 4 : get_position_risk, get_attribution, get_recent_decisions
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Task 5 : get_indicator_context (cube borné, successeur REQUEST_CONTEXT)
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Task 6 : payloads runtime/prompt + calls_for_symbol
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Task 11 : outils sémantiques describe_data + find_indicators
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Finding 1 : sérialisation bornée (_MAX_RAW_CALLS, _scrub_args, _scrub_str)
# ---------------------------------------------------------------------------

def test_execute_tool_round_cap_40_appels_a_32_plus_sentinel():
    """40 calls → 32 traités + 1 trace sentinel truncated, len(traces) == 33."""
    registry = _registry_with_echo()
    raw = [{"id": f"c{i}", "tool": "echo", "args": {"text": "x"}} for i in range(40)]
    results, traces = agent_tools.execute_tool_round(
        raw, context=_context(), limits=agent_tools.ToolRoundLimits(max_total_calls=40),
        allowed_tools=frozenset({"echo"}), registry=registry,
    )
    assert len(results) == agent_tools._MAX_RAW_CALLS
    assert len(traces) == agent_tools._MAX_RAW_CALLS + 1
    sentinel = next(t for t in traces if t.outcome == agent_tools.OUTCOME_TRUNCATED)
    assert sentinel.detail["dropped"] == 8


def test_execute_tool_round_scrub_args_geants():
    """Un call avec args de 10k chars → sérialisation JSON < 2k chars."""
    import json
    registry = _registry_with_echo()
    huge = "x" * 10_000
    raw = [{"id": "c1", "tool": "echo", "args": {"text": huge}}]
    results, traces = agent_tools.execute_tool_round(
        raw, context=_context(), limits=agent_tools.ToolRoundLimits(),
        allowed_tools=frozenset({"echo"}), registry=registry,
    )
    assert len(traces) == 1
    serialized = json.dumps({"r": [r.__dict__ for r in results], "t": [t.__dict__ for t in traces]})
    assert len(serialized) < 2_000


def test_execute_tool_round_scrub_id_geant():
    """Un id de 200 chars est tronqué à _SCRUB_ID_LEN dans la trace."""
    registry = _registry_with_echo()
    big_id = "a" * 200
    raw = [{"id": big_id, "tool": "echo", "args": {"text": "hi"}}]
    results, traces = agent_tools.execute_tool_round(
        raw, context=_context(), limits=agent_tools.ToolRoundLimits(),
        allowed_tools=frozenset({"echo"}), registry=registry,
    )
    assert len(traces) == 1
    assert len(traces[0].id) <= agent_tools._SCRUB_ID_LEN + 1  # +1 pour le marqueur "…"
    assert len(results[0].id) <= agent_tools._SCRUB_ID_LEN + 1


# ---------------------------------------------------------------------------
# Finding 2 : get_attribution(scope="symbol") vérifie l'allowlist
# ---------------------------------------------------------------------------

def test_get_attribution_scope_symbol_hors_allowlist():
    """scope='symbol' avec un symbole hors allowed_symbols → error symbol_not_allowed."""
    ctx = _providers_context()  # allowed_symbols = frozenset({"2330.TW"})
    result, _ = agent_tools.execute_tool_call(
        AgentToolCall(id="c1", tool="get_attribution", args={"scope": "symbol", "symbol": "EVIL"}),
        ctx,
    )
    assert result.ok is True
    assert result.result == {"symbol": "EVIL", "error": "symbol_not_allowed"}
