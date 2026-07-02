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
    # Contenu du résultat get_freshness : ok=True et result non vide (§5 test #5).
    tool_results = final_kwargs["per_symbol"]["2330.TW"]["tool_results"]
    assert len(tool_results) >= 1
    freshness_result = next(r for r in tool_results if r["tool"] == "get_freshness")
    assert freshness_result["ok"] is True
    assert freshness_result.get("result") is not None
    assert freshness_result["result"] != {}
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
    # Le blocage consomme bien 2 appels modèle (tournée + tour final bloqué).
    assert calls == 2


def test_indicator_resolver_passe_les_bornes(monkeypatch):
    """_indicator_resolver dans _batch_decide passe max_context_requests et max_indicators."""
    spy_calls: list[dict] = []

    def _spy_resolve_indicator_requests(
        requests,
        tradable_bars_by_symbol,
        *,
        symbols,
        max_requests,
        max_indicators,
        cached_interval,
        cached_lookback,
    ):
        spy_calls.append({"max_requests": max_requests, "max_indicators": max_indicators})
        return {"requests": [], "indicators_by_symbol": {}}

    monkeypatch.setattr(daemon, "resolve_indicator_requests", _spy_resolve_indicator_requests)

    def _fake_decide_batch(**kwargs):
        if kwargs.get("allow_tool_calls"):
            return codex_client.BatchToolCallRequest(
                calls=[{
                    "id": "c1",
                    "tool": "get_indicator_context",
                    "args": {"symbol": "2330.TW", "indicators": ["rsi", "macd"]},
                }]
            )
        return {sym: _hold(sym) for sym in kwargs["symbols"]}

    monkeypatch.setattr(daemon.codex_client, "decide_batch", _fake_decide_batch)
    daemon._batch_decide(
        **_kwargs(
            max_context_requests_per_symbol=2,
            max_indicators_per_request=4,
            agent_tools_enabled=True,
        )
    )

    # resolve_indicator_requests doit être appelé exactement une fois (une tournée).
    assert len(spy_calls) == 1
    assert spy_calls[0]["max_requests"] == 2
    assert spy_calls[0]["max_indicators"] == 4


def test_multi_symboles_chunk_filtrage_per_symbol(monkeypatch):
    """Un chunk de 2 symboles : les tool_results par symbole sont bien filtrés.

    c1 cible A, c2 cible B, c3 est global (scope summary, pas de symbol).
    per_symbol["A"] doit avoir c1+c3, per_symbol["B"] doit avoir c2+c3.
    """
    calls_seen = []

    def _fake_decide_batch(**kwargs):
        calls_seen.append(kwargs)
        if kwargs.get("allow_tool_calls"):
            return codex_client.BatchToolCallRequest(
                calls=[
                    {"id": "c1", "tool": "get_freshness", "args": {"symbols": ["A"]}},
                    {"id": "c2", "tool": "get_freshness", "args": {"symbols": ["B"]}},
                    # get_attribution scope=summary : aucun symbol → call global
                    {"id": "c3", "tool": "get_attribution", "args": {"scope": "summary"}},
                ]
            )
        return {sym: _hold(sym) for sym in kwargs["symbols"]}

    monkeypatch.setattr(daemon.codex_client, "decide_batch", _fake_decide_batch)
    daemon._batch_decide(
        **_kwargs(
            decidable=["A", "B"],
            data_age_by_symbol={"A": 5.0, "B": 10.0},
            tradable_symbols=["A", "B"],
            decision_batch_size=5,  # un seul chunk [A, B]
            agent_tools_enabled=True,
        )
    )

    # Deuxième appel = tour final ; capturer per_symbol réinjecté.
    final_call = next(c for c in calls_seen if not c.get("allow_tool_calls"))
    results_a = {r["id"] for r in final_call["per_symbol"]["A"]["tool_results"]}
    results_b = {r["id"] for r in final_call["per_symbol"]["B"]["tool_results"]}

    # A reçoit ses résultats + le global, pas ceux de B.
    assert "c1" in results_a
    assert "c3" in results_a
    assert "c2" not in results_a

    # B reçoit ses résultats + le global, pas ceux de A.
    assert "c2" in results_b
    assert "c3" in results_b
    assert "c1" not in results_b


def test_hold_tool_loop_blocked_defaults_sains(monkeypatch):
    """Un HOLD tool_loop_blocked n'écrase pas next_wake ni indicator_watch (défauts None)."""
    def _fake_decide_batch(**kwargs):
        return codex_client.BatchToolCallRequest(
            calls=[{"id": "c1", "tool": "get_freshness", "args": {"symbols": ["2330.TW"]}}]
        )

    monkeypatch.setattr(daemon.codex_client, "decide_batch", _fake_decide_batch)
    decisions, _ = daemon._batch_decide(**_kwargs(), agent_tools_enabled=True)
    d = decisions["2330.TW"]
    assert d.action == "HOLD"
    assert d.rationale == "tool_loop_blocked"
    # Champs par défaut préservés : le daemon en aval ne doit pas crasher sur ces None.
    assert d.next_wake_in_minutes is None
    assert d.indicator_watch is None
    assert d.domain_tools is None  # pas de traces (bloc immédiat, pas de tournée aboutie)


def test_contexte_run_tool_round_filtre_au_chunk(monkeypatch):
    """_run_tool_round ne livre pas les données des symboles hors chunk au ToolContext."""
    from trader import agent_tools

    captured: list[agent_tools.ToolContext] = []
    real_execute = agent_tools.execute_tool_round

    def spy_execute(raw_calls, *, context, limits, allowed_tools, registry=None):
        captured.append(context)
        return real_execute(
            raw_calls, context=context, limits=limits, allowed_tools=allowed_tools, registry=registry
        )

    monkeypatch.setattr(daemon.agent_tools, "execute_tool_round", spy_execute)

    request = codex_client.BatchToolCallRequest(
        calls=[{"id": "c1", "tool": "get_freshness", "args": {"symbols": ["A"]}}]
    )
    daemon._run_tool_round(
        request,
        chunk=["A"],
        now=NOW,
        data_age_by_symbol={"A": 5.0, "B": 10.0},
        market_contexts={"A": {}, "B": {"execution": True}},
        active_watches_by_symbol={"A": [{"id": "wA"}], "B": [{"id": "wB"}]},
        shared_context={},
        indicator_resolver=None,
    )

    assert len(captured) == 1
    ctx = captured[0]
    # Seul A est dans le chunk — B ne doit pas fuiter dans le contexte.
    assert "B" not in ctx.data_age_by_symbol
    assert "B" not in ctx.market_context_by_symbol
    assert "B" not in ctx.active_watches_by_symbol
    assert "A" in ctx.data_age_by_symbol


def test_budget_mode_tournee_limite_les_chunks(monkeypatch):
    """Budget=3, 2 chunks avec agent_tools → budget//2=1 seul chunk autorisé.

    Pire cas : chunk 1 consomme 2 appels (tournée + final). Chunk 2 reçoit HOLD.
    Total calls ≤ 3 (invariant budget).
    """
    def _fake_decide_batch(**kwargs):
        if kwargs.get("allow_tool_calls"):
            return codex_client.BatchToolCallRequest(
                calls=[{
                    "id": "c1",
                    "tool": "get_freshness",
                    "args": {"symbols": list(kwargs["symbols"])},
                }]
            )
        return {sym: _hold(sym) for sym in kwargs["symbols"]}

    monkeypatch.setattr(daemon.codex_client, "decide_batch", _fake_decide_batch)
    decisions, calls = daemon._batch_decide(
        **_kwargs(
            decidable=["A", "B", "C", "D"],
            data_age_by_symbol={"A": 1.0, "B": 2.0, "C": 3.0, "D": 4.0},
            tradable_symbols=["A", "B", "C", "D"],
            decision_batch_size=2,    # 2 chunks : [A,B] et [C,D]
            decision_batch_parallelism=1,
            max_model_calls=3,        # budget//2 = 1 chunk max avec tournées
            agent_tools_enabled=True,
        )
    )

    # Seul le 1er chunk [A, B] est autorisé (consomme 2 appels : tournée + final).
    # [C, D] est hors budget → HOLD model_call_budget_exhausted (0 appel LLM).
    assert calls == 2
    assert decisions["C"].rationale == "model_call_budget_exhausted"
    assert decisions["D"].rationale == "model_call_budget_exhausted"
    # A et B ont reçu des décisions réelles (HOLD de notre fake)
    assert decisions["A"].action == "HOLD"
    assert decisions["B"].action == "HOLD"


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
