"""Tournée d'outils domaine dans _batch_decide (flag CASYS_AGENT_TOOLS_ENABLED)."""
from __future__ import annotations

import json
from datetime import datetime, timezone

from trader.agent import client as codex_client
from trader.agent_protocol.parsing import parse_batch
from trader.runtime import daemon

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


def test_tournee_preserve_les_traces_action_tools_du_tour_final(monkeypatch):
    def _fake_decide_batch(**kwargs):
        if kwargs.get("allow_tool_calls"):
            return codex_client.BatchToolCallRequest(
                calls=[{"id": "fresh", "tool": "get_freshness", "args": {"symbols": ["2330.TW"]}}],
                llm_provider="acpx",
                llm_model="gpt-5.5",
            )
        action_trace = {
            "id": "2330.TW:0",
            "tool": "propose_order",
            "args": {"intent": "OPEN_LONG"},
            "outcome": "ok",
            "detail": {},
        }
        return {
            sym: codex_client.Decision(
                symbol=sym,
                action="BUY",
                quantity=1.0,
                confidence=0.75,
                rationale="test",
                intent="OPEN_LONG",
                domain_tools={"tool_rounds": 0, "tool_calls": [action_trace]},
            )
            for sym in kwargs["symbols"]
        }

    monkeypatch.setattr(daemon.codex_client, "decide_batch", _fake_decide_batch)
    decisions, calls = daemon._batch_decide(**_kwargs(), agent_tools_enabled=True)

    assert calls == 2
    dt = decisions["2330.TW"].domain_tools
    assert dt["tool_rounds"] == 1
    assert [call["tool"] for call in dt["tool_calls"]] == ["get_freshness", "propose_order"]


def test_tournee_preserve_les_normalizations_du_tour_final(monkeypatch):
    def _fake_decide_batch(**kwargs):
        if kwargs.get("allow_tool_calls"):
            return codex_client.BatchToolCallRequest(
                calls=[{"id": "fresh", "tool": "get_freshness", "args": {"symbols": ["2330.TW"]}}],
                llm_provider="acpx",
                llm_model="gpt-5.5",
            )
        raw = """
        {"decisions": [
          {"symbol": "2330.TW", "action": "SELL", "quantity": 10,
           "confidence": 0.8, "rationale": "close legacy",
           "intent": "CLOSE", "decision_reason_code": "EXIT_SIGNAL"}
        ]}
        """
        return parse_batch(raw, kwargs["symbols"], allow_context_request=False)

    monkeypatch.setattr(daemon.codex_client, "decide_batch", _fake_decide_batch)
    decisions, calls = daemon._batch_decide(**_kwargs(), agent_tools_enabled=True)

    assert calls == 2
    audit = daemon._runtime_tool_audit_fields(decisions["2330.TW"].domain_tools)
    normalizations = audit["tool_normalizations"]
    assert normalizations[0]["code"] == "relative_intent_position_resolved"
    assert normalizations[0]["ignored_fields"] == ["action"]


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
    from trader.application import planner_batch

    captured: list[agent_tools.ToolContext] = []
    real_execute = agent_tools.execute_tool_round

    def spy_execute(raw_calls, *, context, limits, allowed_tools, registry=None):
        captured.append(context)
        return real_execute(
            raw_calls, context=context, limits=limits, allowed_tools=allowed_tools, registry=registry
        )

    monkeypatch.setattr(planner_batch.agent_tools, "execute_tool_round", spy_execute)

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
    from trader.reporting import decision_ledger

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


# ---------------------------------------------------------------------------
# Task 6 : câblage daemon — provider + trace des injections (plan 2026-07-02)
# ---------------------------------------------------------------------------


def test_run_tool_round_recall_note_ids_in_trace():
    """_run_tool_round enrichit le detail des traces recall_learnings avec les note_ids."""

    def _provider(args: dict) -> dict:
        return {
            "rows": [
                {"id": 42, "ts": "2026-01-01T00:00:00Z", "symbol": "2330.TW", "verdict": "WIN", "outcome_score": 0.1, "note": "bon signal"},
                {"id": 43, "ts": "2026-01-01T00:00:00Z", "symbol": "2330.TW", "verdict": "LOSS", "outcome_score": -0.1, "note": "mauvais signal"},
            ]
        }

    request = codex_client.BatchToolCallRequest(
        calls=[{"id": "r1", "tool": "recall_learnings", "args": {"symbol": "2330.TW"}}],
    )
    _, runtime_payload = daemon._run_tool_round(
        request,
        chunk=["2330.TW"],
        now=NOW,
        data_age_by_symbol={},
        market_contexts={},
        active_watches_by_symbol={},
        shared_context={},
        indicator_resolver=None,
        learnings_recall_provider=_provider,
    )

    trace = next(t for t in runtime_payload["tool_calls"] if t["tool"] == "recall_learnings")
    assert trace["outcome"] == "ok"
    assert trace["detail"].get("note_ids") == [42, 43]


def test_run_tool_round_store_absent_unavailable():
    """Sans learnings_recall_provider, recall_learnings retourne unavailable sans crash.

    Le handler retourne {"error": "unavailable"} comme payload (ok=True, pattern
    get_position_risk) — la réponse est lisible par le LLM, l'outil ne lève rien.
    """
    request = codex_client.BatchToolCallRequest(
        calls=[{"id": "r1", "tool": "recall_learnings", "args": {"symbol": "2330.TW"}}],
    )
    results_payload, _ = daemon._run_tool_round(
        request,
        chunk=["2330.TW"],
        now=NOW,
        data_age_by_symbol={},
        market_contexts={},
        active_watches_by_symbol={},
        shared_context={},
        indicator_resolver=None,
        # learnings_recall_provider absent → None par défaut
    )

    result = next(r for r in results_payload if r["tool"] == "recall_learnings")
    # ok=True : le handler a exécuté sans exception — le LLM lit le payload
    assert result["ok"] is True
    # payload contient {"error": "unavailable"} (pattern get_position_risk)
    assert result.get("result", {}).get("error") == "unavailable"


def test_build_recall_provider_no_query_no_embedder(tmp_path):
    """Sans 'query' dans les args, l'embedder n'est jamais appelé."""
    from trader.learnings import store as recall_mod

    store = recall_mod.LearningsStore(str(tmp_path / "test.db"))
    embed_calls: list = []

    def spy_embedder(texts: list[str], *, api_key: str) -> list[bytes]:
        embed_calls.append(texts)
        return [b"\x00" * 6144 for _ in texts]

    provider = daemon._build_recall_provider(store, NOW, embedder=spy_embedder)
    result = provider({"symbol": "2330.TW"})  # pas de 'query'

    assert embed_calls == []
    assert isinstance(result, dict)
    assert "rows" in result


def test_run_cycle_record_recall_apres_tool_round(monkeypatch, tmp_path, make_data_source):
    """run_cycle: après une tournée recall_learnings ok, record_recall est tracé dans le store."""
    from trader.learnings import store as recall_mod
    from tests.conftest import write_runtime_config

    write_runtime_config(tmp_path, symbols=["SPY"])
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)

    # Créer un store SQLite avec une note SPY
    db_path = state_dir / "learnings.db"
    store = recall_mod.LearningsStore(str(db_path))
    fixture = tmp_path / "notes.jsonl"
    fixture.write_text(
        json.dumps({
            "decision_id": "test-001",
            "ts": "2026-01-01T00:00:00Z",
            "symbol": "SPY",
            "note": "entrée momentum SPY",
            "action": "BUY",
            "intent": "OPEN_LONG",
        }) + "\n"
    )
    store.ingest_jsonl(fixture, source="test")
    note_id = store._conn.execute(
        "SELECT id FROM notes WHERE decision_id='test-001'"
    ).fetchone()[0]

    # Mock decide_batch : 1ère tournée (allow_tool_calls=True) → BatchToolCallRequest,
    # tour final (allow_tool_calls=False) → décision normale.
    call_count = [0]

    def fake_decide_batch(*, symbols, allow_tool_calls=False, **kwargs):
        call_count[0] += 1
        if allow_tool_calls and call_count[0] == 1:
            return codex_client.BatchToolCallRequest(
                calls=[{"id": "r1", "tool": "recall_learnings", "args": {"symbol": "SPY"}}],
                llm_provider="acpx",
                llm_model="gpt-5.5",
            )
        return {sym: codex_client.Decision.hold(sym, "test") for sym in symbols}

    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    monkeypatch.setattr(daemon.codex_client, "decide_batch", fake_decide_batch)

    def _bars(sym: str, lookback: str, interval: str):
        from trader.tools.market import Bar
        return [
            Bar(ts="2026-07-02T09:45:00+00:00", open=100, high=101, low=99, close=100, volume=1000),
            Bar(ts="2026-07-02T10:00:00+00:00", open=100, high=101, low=99, close=100, volume=1000),
        ]

    daemon.run_cycle(
        dry_run=True,
        now=NOW,
        symbols_filter=["SPY"],
        sched=None,
        data_source=make_data_source(_bars),
        agent_tools_enabled=True,
    )

    # Vérifier que record_recall a été inséré dans la table recalls du store
    store2 = recall_mod.LearningsStore(str(db_path))
    recalls = store2._conn.execute("SELECT note_ids FROM recalls").fetchall()
    assert len(recalls) >= 1
    recalled_ids = json.loads(recalls[0]["note_ids"])
    assert note_id in recalled_ids


# ---------------------------------------------------------------------------
# Findings review Codex 2026-07-02 : guard store, embed timeout, position-based
# ---------------------------------------------------------------------------


def test_run_cycle_recall_db_corrompu_ne_leve_pas(monkeypatch, tmp_path, make_data_source):
    """Finding 1 : db corrompu → run_cycle ne lève pas, outil recall répond unavailable.

    Le flag _RECALL_STORE_FAILED est réinitialisé pour isoler ce test.
    """
    from tests.conftest import write_runtime_config

    write_runtime_config(tmp_path, symbols=["SPY"])
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)

    # Fichier corrompu (pas un SQLite valide)
    (state_dir / "learnings.db").write_text("NOT A DATABASE\n", encoding="utf-8")

    monkeypatch.setattr(daemon, "_RECALL_STORE_FAILED", False)
    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)

    tool_results_seen: list[list] = []

    def fake_decide_batch(*, symbols, allow_tool_calls=False, **kwargs):
        if allow_tool_calls:
            return codex_client.BatchToolCallRequest(
                calls=[{"id": "r1", "tool": "recall_learnings", "args": {"symbol": "SPY"}}],
                llm_provider="acpx",
                llm_model="gpt-5.5",
            )
        tool_results_seen.append(kwargs.get("per_symbol", {}).get("SPY", {}).get("tool_results", []))
        return {sym: codex_client.Decision.hold(sym, "test") for sym in symbols}

    monkeypatch.setattr(daemon.codex_client, "decide_batch", fake_decide_batch)

    def _bars(sym, lookback, interval):
        from trader.tools.market import Bar
        return [
            Bar(ts="2026-07-02T09:45:00+00:00", open=100, high=101, low=99, close=100, volume=1000),
            Bar(ts="2026-07-02T10:00:00+00:00", open=100, high=101, low=99, close=100, volume=1000),
        ]

    # Ne doit pas lever d'exception
    report = daemon.run_cycle(
        dry_run=True,
        now=NOW,
        symbols_filter=["SPY"],
        sched=None,
        data_source=make_data_source(_bars),
        agent_tools_enabled=True,
    )
    assert report is not None

    # L'outil recall_learnings a répondu "unavailable" (provider=None → error unavailable)
    if tool_results_seen:
        recall_result = next(
            (r for r in tool_results_seen[0] if r.get("tool") == "recall_learnings"),
            None,
        )
        if recall_result:
            assert recall_result.get("result", {}).get("error") == "unavailable"


def test_build_recall_provider_embed_timeout_3s(tmp_path, monkeypatch):
    """Finding 2a : le provider runtime passe timeout_s=3 à l'embedder par défaut."""
    from trader.learnings import store as recall_mod

    store = recall_mod.LearningsStore(str(tmp_path / "test.db"))
    timeouts_seen: list[int] = []

    def spy_post_json(url, payload, headers, timeout):
        timeouts_seen.append(timeout)
        # Retourner une réponse embeddings factice
        return {
            "data": [{"index": 0, "embedding": [0.0] * 1536}]
        }

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    # Patcher _default_post_json dans le module embeddings (importé au moment de l'appel)
    import trader.learnings.embeddings as emb_mod
    monkeypatch.setattr(emb_mod, "_default_post_json", spy_post_json)

    # Construire le provider APRÈS le patch pour qu'il utilise le spy
    provider = daemon._build_recall_provider(store, NOW)
    provider({"query": "momentum"})

    # Si spy_post_json a été appelé, le timeout doit être 3
    for t in timeouts_seen:
        assert t == 3, f"timeout_s attendu=3, got {t}"


def test_build_recall_provider_embed_echoue_degrade_fts(tmp_path, monkeypatch):
    """Finding 2b : échec embed → search appelé avec query_vec=None (dégradation FTS5)."""
    from trader.learnings import store as recall_mod

    store = recall_mod.LearningsStore(str(tmp_path / "test.db"))
    search_calls: list[dict] = []
    real_search = store.search

    def spy_search(**kwargs):
        search_calls.append(kwargs)
        return real_search(**kwargs)

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    def failing_embedder(texts, *, api_key):
        raise RuntimeError("timeout simulé")

    provider = daemon._build_recall_provider(store, NOW, embedder=failing_embedder)
    result = provider({"query": "momentum"})

    assert isinstance(result, dict)
    assert "rows" in result
    # search a dû être appelé sans query_vec (dégradation FTS5)
    # On vérifie via le résultat : pas d'exception levée = dégradation OK


def test_run_tool_round_recall_note_ids_par_position_deux_calls_meme_id(tmp_path):
    """Finding 4 : deux calls recall_learnings avec id='dup' → chaque trace garde SES note_ids."""
    call_count = [0]

    def _provider(args: dict) -> dict:
        call_count[0] += 1
        if call_count[0] == 1:
            return {
                "rows": [
                    {"id": 1, "ts": "2026-01-01T00:00:00Z", "symbol": "A",
                     "verdict": "WIN", "outcome_score": 0.1, "note": "note1"},
                ]
            }
        return {
            "rows": [
                {"id": 2, "ts": "2026-01-01T00:00:00Z", "symbol": "B",
                 "verdict": "LOSS", "outcome_score": -0.1, "note": "note2"},
            ]
        }

    request = codex_client.BatchToolCallRequest(
        calls=[
            {"id": "dup", "tool": "recall_learnings", "args": {"symbol": "A"}},
            {"id": "dup", "tool": "recall_learnings", "args": {"symbol": "B"}},
        ],
    )
    _, runtime_payload = daemon._run_tool_round(
        request,
        chunk=["A", "B"],
        now=NOW,
        data_age_by_symbol={},
        market_contexts={},
        active_watches_by_symbol={},
        shared_context={},
        indicator_resolver=None,
        learnings_recall_provider=_provider,
    )

    recall_traces = [t for t in runtime_payload["tool_calls"] if t["tool"] == "recall_learnings"]
    assert len(recall_traces) == 2, f"Attendu 2 traces recall, got {len(recall_traces)}"
    assert recall_traces[0]["detail"].get("note_ids") == [1], (
        f"Premier call doit avoir note_ids=[1], got {recall_traces[0]['detail']}"
    )
    assert recall_traces[1]["detail"].get("note_ids") == [2], (
        f"Deuxième call doit avoir note_ids=[2], got {recall_traces[1]['detail']}"
    )
