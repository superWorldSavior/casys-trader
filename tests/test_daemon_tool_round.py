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

    # build_decision_row(report, decision, *, sequence, source) — le ts vient
    # de report.get("ts") ou decision.get("ts") ; symbol de decision.get("symbol").
    decision = {
        "symbol": "2330.TW", "action": "HOLD", "qty": 0.0, "confidence": 0.0,
        "rationale": "r", "decision_reason_code": "NO_EDGE",
        "ts": "2026-07-02T10:00:00+00:00",
        "tool_rounds": 1,
        "tool_calls": [{"id": "c1", "tool": "get_freshness", "args": {}, "outcome": "ok", "detail": {}}],
    }
    row = decision_ledger.build_decision_row(
        report={},
        decision=decision,
        sequence=1,
        source="daemon",
    )
    assert row["runtime"]["tool_rounds"] == 1
    assert row["runtime"]["tool_calls"][0]["tool"] == "get_freshness"
