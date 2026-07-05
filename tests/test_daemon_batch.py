"""Tests du helper batch _batch_decide (budget d'appels modèle honoré)."""

import threading
import time
import logging
from types import SimpleNamespace
from datetime import datetime, timezone

from trader.runtime import daemon
from trader.application import planner_batch
from trader.agent.client import ContextResearchRequest, Decision, IndicatorRequest
from trader.agent.protocol.parsing import parse_batch
from trader.planning.indicator_watch import summarize_watch
from trader.market.market_data import Bar
from trader.planning.scheduler import Scheduler

_COMMON = dict(
    mandate="",
    memory="",
    shared_context={},
    triggers_by_symbol={},
    tradable_bars_by_symbol={},
    tradable_symbols=["SPY", "QQQ"],
    runtime_interval="15m",
    runtime_lookback="5d",
    max_context_requests_per_symbol=2,
    max_indicators_per_request=4,
    now=datetime(2026, 6, 15, 14, 30, tzinfo=timezone.utc),
    data_age_by_symbol={},
)


def test_planner_batch_module_expose_batch_decide() -> None:
    assert callable(planner_batch.batch_decide)


def _write_runtime_config(root, *, symbols=("SPY",)) -> None:
    (root / "config").mkdir()
    (root / "mandate").mkdir()
    symbols_yaml = "".join(f"  - {symbol}\n" for symbol in symbols)
    (root / "config" / "universe.yaml").write_text(f"starting_cash: 100000\nsymbols:\n{symbols_yaml}")
    (root / "config" / "risk.yaml").write_text(
        "\n".join(
            [
                "max_position_value: 20000",
                "max_gross_exposure: 100000",
                "max_order_value: 10000",
                "max_orders_per_cycle: 5",
                "min_equity: 50000",
            ]
        )
    )
    (root / "mandate" / "mandate.md").write_text("# Mandat\n")
    (root / "mandate" / "memory.md").write_text("# Memoire\n")


def _fresh_data_source(make_data_source, now: datetime):
    return make_data_source(
        lambda symbol, lookback, interval: [
            Bar(
                ts=now.isoformat() if interval != "1d" else now.date().isoformat(),
                open=100.0,
                high=101.0,
                low=99.0,
                close=100.0,
                volume=1000.0,
            )
        ]
    )


def _decision_entry_for_test(decision: Decision, *, price: float, executed: bool = True) -> dict:
    return {
        "symbol": decision.symbol,
        "action": decision.action,
        "qty": abs(decision.quantity),
        "confidence": decision.confidence,
        "rationale": decision.rationale,
        "intent": decision.intent,
        "executed": executed,
        "reason": "ok" if executed else decision.rationale,
        "price": price,
        "trade_plan_created": False,
        "decision_source": "llm",
        "model_called": True,
    }


def _install_queue_decide_stream(monkeypatch, stream_events, trace: list[tuple]):
    import trader.application.queue_dispatch as queue_dispatch

    def fake_iter_decide_results_via_queue(**_kwargs):
        for sym, decision, calls in stream_events:
            trace.append(("decision_ready", sym))
            yield sym, decision, calls

    def fake_dispatch_decide_via_queue(**_kwargs):
        decisions = {}
        undecided = set()
        model_calls = 0
        for sym, decision, calls in stream_events:
            trace.append(("decision_ready", sym))
            if decision is None:
                undecided.add(sym)
            else:
                decisions[sym] = decision
                model_calls += calls
        return decisions, model_calls, undecided

    monkeypatch.setattr(queue_dispatch, "iter_decide_results_via_queue", fake_iter_decide_results_via_queue)
    monkeypatch.setattr(queue_dispatch, "dispatch_decide_via_queue", fake_dispatch_decide_via_queue)


def _install_tracing_decision_executor(monkeypatch, trace: list[tuple]):
    def fake_execute_one_cycle_decision(*, sym, index, total, decision, state, ctx):
        trace.append(("execute", sym, decision.intent, state.gross))
        if decision.intent in {"CLOSE", "REDUCE"}:
            state.gross = 0.0
            trace.append(("refresh_after_exit", sym, state.gross))
        ctx.record_decision(
            _decision_entry_for_test(
                decision,
                price=ctx.prices[sym],
                executed=decision.action != "HOLD",
            )
        )
        return state

    monkeypatch.setattr(daemon, "_execute_one_cycle_decision", fake_execute_one_cycle_decision)


def _seed_long_position(state_dir, symbol: str, *, quantity: float = 10.0, price: float = 100.0) -> None:
    broker = daemon.SimBroker(state_dir / "broker.json", starting_cash=100_000.0)
    broker.submit(
        daemon.Order(symbol=symbol, side="BUY", quantity=quantity),
        price,
        "2026-06-15T13:00:00+00:00",
        dry_run=False,
    )


def test_queue_decide_streams_ready_reducer_before_slow_opening_finishes(
    monkeypatch,
    tmp_path,
    make_data_source,
) -> None:
    _write_runtime_config(tmp_path, symbols=("SPY", "QQQ"))
    state_dir = tmp_path / "state"
    _seed_long_position(state_dir, "SPY", quantity=10.0)
    now = datetime(2026, 6, 15, 14, 30, tzinfo=timezone.utc)
    trace: list[tuple] = []
    reduce_decision = Decision(
        symbol="SPY",
        action="HOLD",
        quantity=0.0,
        confidence=0.8,
        rationale="reduce now",
        intent="REDUCE",
        resolve_from_position=True,
        reduce_fraction=0.5,
    )
    open_decision = Decision(
        symbol="QQQ",
        action="BUY",
        quantity=1.0,
        confidence=0.7,
        rationale="open later",
        intent="OPEN_LONG",
        exit_plan={"hard_stop": {"type": "price", "price": 95.0}},
    )

    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    _install_queue_decide_stream(
        monkeypatch,
        [("SPY", reduce_decision, 1), ("QQQ", open_decision, 1)],
        trace,
    )
    _install_tracing_decision_executor(monkeypatch, trace)

    daemon.run_cycle(
        dry_run=False,
        now=now,
        symbols_filter=["SPY", "QQQ"],
        sched=Scheduler(state_dir / "scheduler.json"),
        data_source=_fresh_data_source(make_data_source, now),
        queue_decide_enabled=True,
        task_ledger=object(),
    )

    assert trace.index(("execute", "SPY", "REDUCE", 1000.0)) < trace.index(("decision_ready", "QQQ"))
    assert trace.index(("refresh_after_exit", "SPY", 0.0)) < trace.index(("execute", "QQQ", "OPEN_LONG", 0.0))


def test_queue_decide_buffers_reverse_until_collection_is_exhausted(
    monkeypatch,
    tmp_path,
    make_data_source,
) -> None:
    _write_runtime_config(tmp_path, symbols=("SPY", "QQQ"))
    state_dir = tmp_path / "state"
    _seed_long_position(state_dir, "SPY", quantity=10.0)
    now = datetime(2026, 6, 15, 14, 30, tzinfo=timezone.utc)
    trace: list[tuple] = []
    reverse_decision = Decision(
        symbol="SPY",
        action="HOLD",
        quantity=5.0,
        confidence=0.9,
        rationale="flip",
        intent="REVERSE",
        resolve_from_position=True,
        exit_plan={"hard_stop": {"type": "price", "price": 105.0}},
    )
    hold_decision = Decision.hold("QQQ", "wait")

    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    _install_queue_decide_stream(
        monkeypatch,
        [("SPY", reverse_decision, 1), ("QQQ", hold_decision, 1)],
        trace,
    )
    _install_tracing_decision_executor(monkeypatch, trace)

    daemon.run_cycle(
        dry_run=False,
        now=now,
        symbols_filter=["SPY", "QQQ"],
        sched=Scheduler(state_dir / "scheduler.json"),
        data_source=_fresh_data_source(make_data_source, now),
        queue_decide_enabled=True,
        task_ledger=object(),
    )

    assert trace.index(("decision_ready", "SPY")) < trace.index(("decision_ready", "QQQ"))
    assert trace.index(("decision_ready", "QQQ")) < trace.index(("execute", "SPY", "REVERSE", 1000.0))


def test_queue_decide_executes_buffered_openings_by_gross_merit_order(
    monkeypatch,
    tmp_path,
    make_data_source,
) -> None:
    _write_runtime_config(tmp_path, symbols=("LOW", "HIGH"))
    state_dir = tmp_path / "state"
    now = datetime(2026, 6, 15, 14, 30, tzinfo=timezone.utc)
    trace: list[tuple] = []
    low_conf = Decision(
        symbol="LOW",
        action="BUY",
        quantity=1.0,
        confidence=0.2,
        rationale="weak open",
        intent="OPEN_LONG",
        exit_plan={"hard_stop": {"type": "price", "price": 95.0}},
    )
    high_conf = Decision(
        symbol="HIGH",
        action="BUY",
        quantity=1.0,
        confidence=0.95,
        rationale="strong open",
        intent="OPEN_LONG",
        exit_plan={"hard_stop": {"type": "price", "price": 95.0}},
    )

    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    _install_queue_decide_stream(
        monkeypatch,
        [("LOW", low_conf, 1), ("HIGH", high_conf, 1)],
        trace,
    )
    _install_tracing_decision_executor(monkeypatch, trace)

    daemon.run_cycle(
        dry_run=False,
        now=now,
        symbols_filter=["LOW", "HIGH"],
        sched=Scheduler(state_dir / "scheduler.json"),
        data_source=_fresh_data_source(make_data_source, now),
        queue_decide_enabled=True,
        task_ledger=object(),
    )

    executed_symbols = [item[1] for item in trace if item[0] == "execute"]
    assert executed_symbols == ["HIGH", "LOW"]


def test_execute_one_cycle_decision_records_hold_without_mutating_state() -> None:
    records: list[dict] = []
    snap = SimpleNamespace(equity=100_000.0)
    state = daemon.DecisionExecutionState(snap=snap, gross=1234.0)
    ctx = daemon.DecisionExecutionContext(
        now=_COMMON["now"],
        min_wake_minutes=None,
        max_wake_minutes=None,
        macro_next=None,
        broker=SimpleNamespace(positions=lambda: {}),
        plan_store=SimpleNamespace(),
        gate=SimpleNamespace(),
        sched=None,
        prices={"SPY": 100.0},
        execution_eligibility={},
        tradable_bars_by_symbol={},
        data_age_by_symbol={},
        runtime_data_source_by_sym={},
        armed_plan_ids={},
        armed_plan_orders={},
        armed_reference_volatilities={},
        held_symbols=set(),
        cockpit={},
        runtime_interval="15m",
        starting_equity=100_000.0,
        require_hard_stop=True,
        dry_run=True,
        queue_execute_enabled=False,
        execute_ledger=None,
        record_decision=records.append,
        rate_for_symbol=lambda _symbol: 1.0,
    )

    new_state = daemon._execute_one_cycle_decision(
        sym="SPY",
        index=1,
        total=1,
        decision=Decision.hold("SPY", "no_decision_in_batch"),
        state=state,
        ctx=ctx,
    )

    assert new_state is state
    assert new_state.snap is snap
    assert new_state.gross == 1234.0
    assert len(records) == 1
    assert records[0]["symbol"] == "SPY"
    assert records[0]["executed"] is False
    assert records[0]["reason"] == "no_decision_in_batch"


def test_execution_blocked_reason_gate_les_ordres_hors_execution() -> None:
    # §13.5 — la garde déterministe : un ordre ne part que si execution.enabled.
    elig = {
        "OK": {"execution": {"enabled": True, "reason": "tradable"}},
        "CLOSED": {"execution": {"enabled": False, "reason": "session_closed"}},
        "STALE": {"execution": {"enabled": False, "reason": "runtime_stale"}},
    }
    assert daemon._execution_blocked_reason(elig, "OK") is None
    assert daemon._execution_blocked_reason(elig, "CLOSED") == "execution:session_closed"
    assert daemon._execution_blocked_reason(elig, "STALE") == "execution:runtime_stale"
    # Symbole hors classification (cas anormal) : fail-open par défaut (sorties de
    # protection), fail-closed pour les ouvertures (invariant §10).
    assert daemon._execution_blocked_reason(elig, "UNKNOWN") is None
    assert daemon._execution_blocked_reason(elig, "UNKNOWN", fail_closed=True) == "execution:unclassified"
    # Un symbole classé exécutable n'est jamais bloqué, même en fail_closed.
    assert daemon._execution_blocked_reason(elig, "OK", fail_closed=True) is None


def test_build_execution_eligibility_separe_planning_et_execution_pour_un_stale() -> None:
    # §13.2 branchée : un symbole runtime-stale mais au daily présent (frais) →
    # execution interdite (runtime_stale), planning autorisé (daily_context_fresh).
    now = datetime(2026, 6, 15, 14, 30, tzinfo=timezone.utc)  # séance US ouverte
    elig = daemon._build_execution_eligibility(
        ["STALE"],
        stale_market_data={
            "STALE": {"last_bar_ts": "2026-06-12T20:00:00+00:00", "data_age_minutes": 4000.0}
        },
        prices={"STALE": 100.0},
        daily_bars_by_symbol={"STALE": [Bar(ts="2026-06-12", open=1.0, high=1.0, low=1.0, close=1.0, volume=1.0)]},
        data_age_by_symbol={"STALE": 4000.0},
        now=now,
        runtime_interval="15m",
    )
    assert elig["STALE"]["execution"]["enabled"] is False
    assert elig["STALE"]["execution"]["reason"] == "runtime_stale"
    assert elig["STALE"]["execution"]["data_age_minutes"] == 4000.0
    assert elig["STALE"]["planning"]["enabled"] is True
    assert elig["STALE"]["planning"]["reason"] == "daily_context_fresh"


def test_batch_decide_injecte_age_data_et_session_par_symbole(monkeypatch) -> None:
    # Code over instructions : l'âge des prix et l'état de la séance de la place
    # du symbole sont des FAITS calculés par le code et injectés dans le payload —
    # le mandat n'a pas à lister les horaires des marchés ni le délai data.
    captured: dict = {}

    def fake_batch(*, per_symbol, **kwargs):
        captured.update(per_symbol)
        return {sym: Decision.hold(sym, "x") for sym in per_symbol}

    monkeypatch.setattr(daemon.codex_client, "decide_batch", fake_batch)
    common = {
        **_COMMON,
        # Lundi 15/06 14h30 UTC = 10h30 EDT : séance US ouverte depuis 60min.
        "now": datetime(2026, 6, 15, 14, 30, tzinfo=timezone.utc),
        "data_age_by_symbol": {"SPY": 9.6},
    }

    decisions, n = daemon._batch_decide(decidable=["SPY", "QQQ"], max_model_calls=1, **common)

    assert captured["SPY"]["data_age_m"] == 10  # arrondi à la minute entière
    # session_context enrichi : ouvert → depuis combien de temps + combien avant cloche.
    assert captured["SPY"]["session"]["open"] is True
    assert captured["SPY"]["session"]["since_open_m"] == 60   # 14h30 - 13h30 EDT open
    assert captured["SPY"]["session"]["to_close_m"] == 330    # 20h00 - 14h30 UTC close
    assert "next_open" not in captured["SPY"]["session"]
    assert captured["QQQ"]["data_age_m"] is None  # âge inconnu = inconnu, pas 0


def test_daemon_batch_decide_wrapper_delegue_au_module_application(monkeypatch) -> None:
    captured: dict = {}

    def fake_batch_decide(**kwargs):
        captured.update(kwargs)
        return {"SPY": Decision.hold("SPY", "module")}, 0

    monkeypatch.setattr(planner_batch, "batch_decide", fake_batch_decide)

    decisions, calls = daemon._batch_decide(
        decidable=["SPY"],
        max_model_calls=1,
        **_COMMON,
    )

    assert calls == 0
    assert decisions["SPY"].rationale == "module"
    assert captured["decidable"] == ["SPY"]


def test_batch_decide_injecte_last_llm_review_du_plan_ouvert(monkeypatch) -> None:
    # Continuité de thèse : le dernier verdict LLM persisté (last_llm_review d'un
    # TradePlan ouvert) est réinjecté dans le contexte par symbole au réveil — le
    # modèle voit son propre verdict intact/fragile/invalidated précédent.
    captured: dict = {}

    def fake_batch(*, per_symbol, **kwargs):
        captured.update(per_symbol)
        return {sym: Decision.hold(sym, "x") for sym in per_symbol}

    monkeypatch.setattr(daemon.codex_client, "decide_batch", fake_batch)
    review = {"ts": "2026-06-15T13:00:00+00:00", "verdict": "fragile", "action": "HOLD"}

    daemon._batch_decide(
        decidable=["SPY", "QQQ"],
        max_model_calls=1,
        last_review_by_symbol={"SPY": review},
        **_COMMON,
    )

    assert captured["SPY"].get("last_llm_review") == review
    assert "last_llm_review" not in captured["QQQ"]  # pas de review => champ absent


def test_batch_decide_injecte_le_market_context_execution_planning(monkeypatch) -> None:
    # §13.2 branchée : le bloc execution/planning arrive dans le per_symbol envoyé au
    # LLM — le modèle voit execution.enabled=false et ne confond pas analyse et ordre.
    captured: dict = {}

    def fake_batch(*, per_symbol, **kwargs):
        captured.update(per_symbol)
        return {sym: Decision.hold(sym, "x") for sym in per_symbol}

    monkeypatch.setattr(daemon.codex_client, "decide_batch", fake_batch)
    mc = {
        "execution": {"enabled": False, "reason": "runtime_stale"},
        "planning": {"enabled": True, "reason": "daily_context_fresh"},
    }

    daemon._batch_decide(
        decidable=["SPY"],
        max_model_calls=1,
        market_context_by_symbol={"SPY": mc},
        **_COMMON,
    )

    assert captured["SPY"].get("execution") == mc["execution"]
    assert captured["SPY"].get("planning") == mc["planning"]


def test_batch_decide_reinjecte_last_llm_review_dans_les_deux_batches(monkeypatch) -> None:
    # La review doit suivre le symbole dans le 1er batch ET dans le 2e batch déclenché
    # par un REQUEST_CONTEXT (sessions jetables : le 2e batch n'a pas l'historique du 1er).
    captured: list[dict] = []
    request = IndicatorRequest(symbol="SPY", indicators=["z_score"], timeframe="1h")

    def fake_batch(*, symbols, per_symbol, allow_context_request, **kwargs):
        captured.append({s: dict(f) for s, f in per_symbol.items()})
        if allow_context_request:
            return {"SPY": ContextResearchRequest(symbol="SPY", rationale="z", requests=[request])}
        return {sym: Decision.hold(sym, "x") for sym in symbols}

    def fake_resolve(requests, *args, **kwargs):
        return {"requests": [{"symbol": "SPY", "indicators": {"z_score": 1.2}}]}

    monkeypatch.setattr(daemon.codex_client, "decide_batch", fake_batch)
    monkeypatch.setattr(daemon, "resolve_indicator_requests", fake_resolve)
    review = {"ts": "2026-06-15T13:00:00+00:00", "verdict": "fragile"}

    daemon._batch_decide(
        decidable=["SPY"],
        max_model_calls=2,
        last_review_by_symbol={"SPY": review},
        **_COMMON,
    )

    assert len(captured) == 2  # round-trip REQUEST_CONTEXT a bien eu lieu
    assert captured[0]["SPY"].get("last_llm_review") == review  # 1er batch
    assert captured[1]["SPY"].get("last_llm_review") == review  # 2e batch (per_symbol2)


def test_last_review_by_symbol_filtre_plans_sans_review_et_hors_perimetre(tmp_path) -> None:
    # Mapping symbole -> dernier verdict LLM, restreint aux plans ouverts du batch
    # courant qui PORTENT une review (narrow contract : on ne passe pas le store entier).
    from dataclasses import replace

    from trader.planning.trade_plan import TradePlanStore, create_trade_plan

    store = TradePlanStore(tmp_path / "plans.json")

    def _plan(sym: str, review: dict | None = None):
        plan = create_trade_plan(
            symbol=sym,
            side="LONG",
            quantity=10.0,
            entry_price=100.0,
            opened_at="2026-06-05T12:00:00+00:00",
            raw_exit_plan={"hard_stop": 95.0},
        )
        return plan if review is None else replace(plan, last_llm_review=review)

    review = {"ts": "2026-06-05T12:15:00+00:00", "verdict": "intact"}
    store.upsert(_plan("SPY", review))
    store.upsert(_plan("QQQ"))  # plan ouvert mais sans review
    store.upsert(_plan("IWM", review))  # review mais hors du batch décidé

    out = daemon._last_review_by_symbol(store, ["SPY", "QQQ"])

    assert out == {"SPY": review}


def test_batch_decide_expose_les_active_watches_du_symbole_seulement(monkeypatch, tmp_path) -> None:
    captured: dict = {}
    now = datetime(2026, 6, 15, 14, 30, tzinfo=timezone.utc)
    sched = Scheduler(tmp_path / "scheduler.json")
    spy_watch = {
        "id": "spy-watch",
        "symbol": "SPY",
        "on_trigger": "WAKE",
        "expires_at": "2026-06-15T15:00:00+00:00",
        "conditions": [{"indicator": "return", "op": ">", "value": 0.01, "timeframe": "1h"}],
    }
    qqq_watch = {
        "id": "qqq-watch",
        "symbol": "QQQ",
        "on_trigger": "WAKE",
        "expires_at": "2026-06-15T15:00:00+00:00",
        "conditions": [{"indicator": "z_score", "op": ">=", "value": 1.5, "timeframe": "15m"}],
    }
    sched.set_symbol_indicator_watch("SPY", spy_watch)
    sched.set_symbol_indicator_watch("QQQ", qqq_watch)

    def fake_batch(*, symbols, per_symbol, **kwargs):
        captured.update(per_symbol)
        return {sym: Decision.hold(sym, "x") for sym in symbols}

    monkeypatch.setattr(daemon.codex_client, "decide_batch", fake_batch)

    daemon._batch_decide(
        decidable=["SPY"],
        max_model_calls=1,
        sched=sched,
        **{**_COMMON, "now": now},
    )

    assert captured["SPY"]["active_watches"] == [summarize_watch(spy_watch)]


def test_apply_decision_schedule_annule_les_watches_avant_de_reposer(monkeypatch) -> None:
    apply_fn = getattr(daemon, "_apply_decision_schedule", None)
    assert apply_fn is not None
    now = datetime(2026, 6, 15, 14, 30, tzinfo=timezone.utc)
    operations: list[tuple[str, str]] = []
    events: list[tuple[str, dict]] = []

    class RecordingScheduler:
        def __init__(self) -> None:
            self._watches: list[dict] = []

        def set_symbol_next_wake_in(self, sym: str, *, minutes: float, now: datetime) -> None:
            operations.append(("wake", sym))

        def set_symbol_next_wake(self, sym: str, when_iso: str) -> None:
            operations.append(("wake_at", when_iso))

        def clear_symbol_next_wake(self, sym: str) -> None:
            operations.append(("clear", sym))

        def remove_indicator_watch(self, watch_id: str) -> None:
            operations.append(("remove", watch_id))

        def set_symbol_indicator_watch(self, sym: str, watch: dict) -> None:
            operations.append(("set", watch["id"]))
            self._watches.append(watch)

        def active_indicator_watches(self, *, now: datetime) -> list[dict]:
            return list(self._watches)

    monkeypatch.setattr(
        daemon,
        "_append_event",
        lambda event, **payload: events.append((event, payload)),
    )
    entry = {"indicator_watch_created": False}
    pending_watch = {
        "id": "new-watch",
        "expires_at": "2026-06-15T15:00:00+00:00",
        "logic": "all",
        "on_trigger": "WAKE",
        "conditions": [{"indicator": "return", "op": ">", "value": 0.01, "timeframe": "1h"}],
    }

    apply_fn(
        sched=RecordingScheduler(),
        sym="SPY",
        now=now,
        next_wake_in_minutes=None,
        cancel_watch_ids=["SPY:old-watch"],
        pending_indicator_watch=pending_watch,
        entry=entry,
    )

    assert ("remove", "SPY:old-watch") in operations
    assert events == [
        ("watch_cancelled_by_agent", {"symbol": "SPY", "watch_id": "SPY:old-watch"}),
    ]
    assert operations.index(("remove", "SPY:old-watch")) < operations.index(("set", "new-watch"))
    assert entry["indicator_watch_created"] is True
    # F4 : le résultat de l'annulation est tracé pour l'audit (dérivé par tool_trace).
    assert entry["cancel_watch_results"] == [
        {"watch_id": "SPY:old-watch", "outcome": "cancelled"}
    ]


def test_apply_decision_schedule_loggue_cancel_et_arm_en_info(monkeypatch, caplog) -> None:
    """Fix observabilité : la gestion de veilles reste visible à l'INFO même
    quand la décision est un HOLD (dont la ligne result est en DEBUG)."""
    now = datetime(2026, 6, 15, 14, 30, tzinfo=timezone.utc)

    class NoopScheduler:
        def set_symbol_next_wake_in(self, *a, **k) -> None: ...
        def set_symbol_next_wake(self, *a, **k) -> None: ...
        def clear_symbol_next_wake(self, *a, **k) -> None: ...
        def remove_indicator_watch(self, *a, **k) -> None: ...
        def set_symbol_indicator_watch(self, *a, **k) -> None: ...
        def active_indicator_watches(self, *a, **k) -> list[dict]:
            return []

    monkeypatch.setattr(daemon, "_append_event", lambda event, **payload: None)
    caplog.set_level(logging.INFO, logger="casys-trader")

    daemon._apply_decision_schedule(
        sched=NoopScheduler(),
        sym="SPY",
        now=now,
        next_wake_in_minutes=None,
        cancel_watch_ids=["SPY:old"],
        pending_indicator_watch={
            "id": "SPY:new",
            "expires_at": "2026-06-15T15:00:00+00:00",
            "logic": "all",
            "on_trigger": "EXECUTE_ORDER",
            "conditions": [],
        },
        entry={"indicator_watch_created": False},
    )

    msgs = "\n".join(r.getMessage() for r in caplog.records)
    assert "[watch] annulée par l'agent SPY SPY:old" in msgs
    assert "[watch] armée SPY SPY:new on_trigger=EXECUTE_ORDER" in msgs


def test_apply_decision_schedule_rejette_l_annulation_d_une_watch_autre_symbole(monkeypatch) -> None:
    now = datetime(2026, 6, 15, 14, 30, tzinfo=timezone.utc)
    operations: list[tuple[str, str]] = []
    events: list[tuple[str, dict]] = []

    class RecordingScheduler:
        def __init__(self) -> None:
            self._watches: list[dict] = []

        def set_symbol_next_wake_in(self, sym: str, *, minutes: float, now: datetime) -> None:
            operations.append(("wake", sym))

        def set_symbol_next_wake(self, sym: str, when_iso: str) -> None:
            operations.append(("wake_at", when_iso))

        def clear_symbol_next_wake(self, sym: str) -> None:
            operations.append(("clear", sym))

        def remove_indicator_watch(self, watch_id: str) -> None:
            operations.append(("remove", watch_id))

        def set_symbol_indicator_watch(self, sym: str, watch: dict) -> None:
            operations.append(("set", watch["id"]))
            self._watches.append(watch)

        def active_indicator_watches(self, *, now: datetime) -> list[dict]:
            return list(self._watches)

    monkeypatch.setattr(
        daemon,
        "_append_event",
        lambda event, **payload: events.append((event, payload)),
    )

    entry: dict = {}
    daemon._apply_decision_schedule(
        sched=RecordingScheduler(),
        sym="SYM",
        now=now,
        next_wake_in_minutes=None,
        cancel_watch_ids=["OTHER:hash"],
        pending_indicator_watch=None,
        entry=entry,
    )

    assert ("remove", "OTHER:hash") not in operations
    assert events == [
        (
            "watch_cancel_rejected",
            {"symbol": "SYM", "watch_id": "OTHER:hash", "reason": "not_owned_by_symbol"},
        )
    ]
    # F4 : le refus d'annulation (ownership) est tracé pour l'audit, pas seulement en event.
    assert entry["cancel_watch_results"] == [
        {"watch_id": "OTHER:hash", "outcome": "not_owned"}
    ]


def test_run_cycle_applique_cancel_watch_ids_avant_nouvelle_veille(
    monkeypatch, tmp_path, make_data_source
) -> None:
    _write_runtime_config(tmp_path)
    state_dir = tmp_path / "state"
    now = datetime(2026, 6, 15, 14, 30, tzinfo=timezone.utc)

    class RecordingScheduler(Scheduler):
        def __init__(self, path):
            super().__init__(path)
            self.operations: list[tuple[str, str]] = []

        def remove_indicator_watch(self, watch_id: str) -> None:
            self.operations.append(("remove", watch_id))
            super().remove_indicator_watch(watch_id)

        def set_symbol_indicator_watch(self, symbol: str, watch: dict) -> None:
            self.operations.append(("set", watch["id"]))
            super().set_symbol_indicator_watch(symbol, watch)

    sched = RecordingScheduler(state_dir / "scheduler.json")
    sched.set_symbol_indicator_watch(
        "SPY",
        {
            "id": "SPY:old-watch",
            "symbol": "SPY",
            "on_trigger": "WAKE",
            "expires_at": "2026-06-15T15:00:00+00:00",
            "conditions": [{"indicator": "return", "op": ">", "value": 0.01, "timeframe": "1h"}],
        },
    )
    sched.set_symbol_next_wake("SPY", now.isoformat())
    sched.operations.clear()
    events: list[tuple[str, dict]] = []

    def fake_batch_decide(**kwargs):
        return {
            "SPY": Decision(
                symbol="SPY",
                action="HOLD",
                quantity=0.0,
                confidence=0.0,
                rationale="correction",
                intent="HOLD",
                cancel_watch_ids=["SPY:old-watch"],
                indicator_watch={
                    "ttl_minutes": 30,
                    "conditions": [{"indicator": "return", "op": "<", "value": -0.01, "timeframe": "1h"}],
                },
            )
        }, 1

    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    monkeypatch.setattr(daemon, "_batch_decide", fake_batch_decide)
    monkeypatch.setattr(
        daemon,
        "_append_event",
        lambda event, **payload: events.append((event, payload)),
    )
    data_source = make_data_source(
        lambda symbol, lookback, interval: [
            Bar(ts=now.isoformat(), open=100.0, high=101.0, low=99.0, close=100.0, volume=1000.0)
        ]
    )

    daemon.run_cycle(
        dry_run=True,
        now=now,
        symbols_filter=["SPY"],
        sched=sched,
        data_source=data_source,
    )

    remove_index = sched.operations.index(("remove", "SPY:old-watch"))
    first_set_index = next(index for index, operation in enumerate(sched.operations) if operation[0] == "set")
    assert remove_index < first_set_index
    assert (
        "watch_cancelled_by_agent",
        {"symbol": "SPY", "watch_id": "SPY:old-watch"},
    ) in events


def test_budget_zero_ne_fait_aucun_appel_et_tout_hold(monkeypatch) -> None:
    calls = {"n": 0}

    def fake_batch(**kwargs):
        calls["n"] += 1
        return {}

    monkeypatch.setattr(daemon.codex_client, "decide_batch", fake_batch)
    decisions, n = daemon._batch_decide(decidable=["SPY", "QQQ"], max_model_calls=0, **_COMMON)

    assert calls["n"] == 0
    assert n == 0
    assert decisions["SPY"].action == "HOLD"
    assert decisions["SPY"].rationale == "model_call_budget_exhausted"
    assert decisions["QQQ"].action == "HOLD"


def test_batch_decide_passe_le_plafond_decisionnel_900s_par_defaut(monkeypatch) -> None:
    timeouts: list[int] = []

    def fake_batch(*, symbols, timeout_s, **kwargs):
        timeouts.append(timeout_s)
        return {sym: Decision.hold(sym, "attente") for sym in symbols}

    monkeypatch.setattr(daemon.codex_client, "decide_batch", fake_batch)

    decisions, n = daemon._batch_decide(decidable=["SPY", "QQQ"], max_model_calls=1, **_COMMON)

    assert n == 1
    assert decisions["SPY"].action == "HOLD"
    assert timeouts == [900]


def test_batch_decide_decoupe_les_decisions_en_chunks_paralleles_bornes(monkeypatch) -> None:
    symbols = [f"SYM{i}" for i in range(12)]
    chunks: list[tuple[str, ...]] = []
    active = 0
    max_active = 0
    lock = threading.Lock()

    def fake_batch(*, symbols, **kwargs):
        nonlocal active, max_active
        with lock:
            chunks.append(tuple(symbols))
            active += 1
            max_active = max(max_active, active)
        time.sleep(0.03)
        with lock:
            active -= 1
        return {sym: Decision.hold(sym, "attente") for sym in symbols}

    monkeypatch.setattr(daemon.codex_client, "decide_batch", fake_batch)

    decisions, n = daemon._batch_decide(
        decidable=symbols,
        max_model_calls=10,
        decision_batch_size=5,
        decision_batch_parallelism=2,
        **{**_COMMON, "tradable_symbols": symbols},
    )

    assert n == 3
    assert sorted(len(chunk) for chunk in chunks) == [2, 5, 5]
    assert max(len(chunk) for chunk in chunks) == 5
    assert max_active == 2
    assert set(decisions) == set(symbols)


def test_batch_decide_loggue_le_decoupage_des_chunks(monkeypatch, caplog) -> None:
    caplog.set_level("DEBUG", logger="trader.application.planner_batch")

    def fake_batch(*, symbols, **kwargs):
        return {sym: Decision.hold(sym, "x") for sym in symbols}

    monkeypatch.setattr(planner_batch.codex_client, "decide_batch", fake_batch)

    planner_batch.batch_decide(
        decidable=["A", "B", "C"],
        max_model_calls=2,
        decision_batch_size=2,
        decision_batch_parallelism=1,
        **_COMMON,
    )

    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert "planner_batch chunks=2 allowed=2 skipped=0" in messages


def test_batch_decide_ne_depasse_pas_le_budget_appels_en_chunks(monkeypatch) -> None:
    symbols = [f"SYM{i}" for i in range(12)]
    chunks: list[tuple[str, ...]] = []

    def fake_batch(*, symbols, **kwargs):
        chunks.append(tuple(symbols))
        return {sym: Decision.hold(sym, "attente") for sym in symbols}

    monkeypatch.setattr(daemon.codex_client, "decide_batch", fake_batch)

    decisions, n = daemon._batch_decide(
        decidable=symbols,
        max_model_calls=2,
        decision_batch_size=5,
        decision_batch_parallelism=3,
        **{**_COMMON, "tradable_symbols": symbols},
    )

    assert n == 2
    assert sorted(len(chunk) for chunk in chunks) == [5, 5]
    assert sum(1 for decision in decisions.values() if decision.rationale == "model_call_budget_exhausted") == 2
    assert set(decisions) == set(symbols)


def test_batch_decide_passe_le_plafond_custom_aux_deux_appels(monkeypatch) -> None:
    timeouts: list[tuple[bool, int]] = []

    def fake_batch(*, symbols, allow_context_request, timeout_s, **kwargs):
        timeouts.append((allow_context_request, timeout_s))
        if allow_context_request:
            return {
                "SPY": ContextResearchRequest(
                    symbol="SPY",
                    rationale="besoin z",
                    requests=[IndicatorRequest(symbol="SPY", indicators=["z_score"])],
                )
            }
        return {sym: Decision.hold(sym, "attente") for sym in symbols}

    monkeypatch.setattr(daemon.codex_client, "decide_batch", fake_batch)

    decisions, n = daemon._batch_decide(
        decidable=["SPY"],
        max_model_calls=2,
        decision_timeout_s=333,
        **_COMMON,
    )

    assert n == 2
    assert decisions["SPY"].action == "HOLD"
    assert timeouts == [(True, 333), (False, 333)]


def test_batch_decide_attache_context_request_apres_round_trip(monkeypatch) -> None:
    request = IndicatorRequest(symbol="SPY", indicators=["z_score"], timeframe="1h")
    calls: list[bool] = []

    def fake_batch(*, symbols, allow_context_request, **kwargs):
        calls.append(allow_context_request)
        if allow_context_request:
            return {
                "SPY": ContextResearchRequest(
                    symbol="SPY",
                    rationale="besoin z",
                    requests=[request],
                ),
                "QQQ": Decision.hold("QQQ", "range"),
            }
        return {sym: Decision.hold(sym, "attente") for sym in symbols}

    def fake_resolve_indicator_requests(requests, *args, **kwargs):
        assert list(requests) == [request]
        return {"requests": [{"symbol": "SPY", "indicators": {"z_score": 1.2}}]}

    monkeypatch.setattr(daemon.codex_client, "decide_batch", fake_batch)
    monkeypatch.setattr(daemon, "resolve_indicator_requests", fake_resolve_indicator_requests)

    decisions, n = daemon._batch_decide(decidable=["SPY", "QQQ"], max_model_calls=2, **_COMMON)

    assert n == 2
    assert calls == [True, False]
    assert decisions["SPY"].context_request == {
        "rounds": 1,
        "requested": [{"symbol": "SPY", "indicators": ["z_score"], "timeframe": "1h"}],
        "resolved": 1,
    }
    assert decisions["QQQ"].context_request is None


def test_batch_decide_reinjecte_active_watches_apres_request_context(monkeypatch, tmp_path) -> None:
    now = datetime(2026, 6, 15, 14, 30, tzinfo=timezone.utc)
    sched = Scheduler(tmp_path / "scheduler.json")
    spy_watch = {
        "id": "SPY:watch",
        "symbol": "SPY",
        "on_trigger": "WAKE",
        "expires_at": "2026-06-15T15:00:00+00:00",
        "logic": "any",
        "conditions": [{"indicator": "return", "op": ">", "value": 0.01, "timeframe": "1h"}],
    }
    sched.set_symbol_indicator_watch("SPY", spy_watch)
    request = IndicatorRequest(symbol="SPY", indicators=["return"], timeframe="1h")
    captured_per_symbol: list[dict] = []

    def fake_batch(*, symbols, allow_context_request, per_symbol, **kwargs):
        captured_per_symbol.append(per_symbol)
        if allow_context_request:
            return {
                "SPY": ContextResearchRequest(
                    symbol="SPY",
                    rationale="besoin return",
                    requests=[request],
                )
            }
        return {sym: Decision.hold(sym, "attente") for sym in symbols}

    def fake_resolve_indicator_requests(requests, *args, **kwargs):
        return {"requests": [{"symbol": "SPY", "indicators": {"return": 0.02}}]}

    monkeypatch.setattr(daemon.codex_client, "decide_batch", fake_batch)
    monkeypatch.setattr(daemon, "resolve_indicator_requests", fake_resolve_indicator_requests)

    daemon._batch_decide(
        decidable=["SPY"],
        max_model_calls=2,
        sched=sched,
        **{**_COMMON, "now": now},
    )

    assert captured_per_symbol[1]["SPY"]["active_watches"] == [summarize_watch(spy_watch)]


def test_run_cycle_passe_les_parametres_decisionnels_a_batch_decide(monkeypatch, tmp_path, make_data_source) -> None:
    _write_runtime_config(tmp_path)
    state_dir = tmp_path / "state"
    now = datetime(2026, 6, 5, 12, 0, tzinfo=timezone.utc)
    captured: dict[str, int] = {}

    def fake_batch_decide(**kwargs):
        captured["decision_timeout_s"] = kwargs["decision_timeout_s"]
        captured["decision_batch_size"] = kwargs["decision_batch_size"]
        captured["decision_batch_parallelism"] = kwargs["decision_batch_parallelism"]
        return {sym: Decision.hold(sym, "attente") for sym in kwargs["decidable"]}, 1

    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    monkeypatch.setattr(daemon, "_batch_decide", fake_batch_decide)
    data_source = make_data_source(lambda symbol, lookback, interval: [
        Bar(ts=now.isoformat(), open=100.0, high=101.0, low=99.0, close=100.0, volume=1000.0)
    ])

    daemon.run_cycle(
        dry_run=True,
        now=now,
        symbols_filter=["SPY"],
        sched=Scheduler(state_dir / "scheduler.json"),
        data_source=data_source,
        decision_timeout_s=444,
        decision_batch_size=7,
        decision_batch_parallelism=2,
    )

    assert captured["decision_timeout_s"] == 444
    assert captured["decision_batch_size"] == 7
    assert captured["decision_batch_parallelism"] == 2


def test_budget_un_fait_un_seul_batch_et_request_context_devient_hold(monkeypatch) -> None:
    calls = {"n": 0}

    def fake_batch(*, symbols, allow_context_request, **kwargs):
        calls["n"] += 1
        return {
            "SPY": ContextResearchRequest(
                symbol="SPY",
                rationale="besoin z",
                requests=[IndicatorRequest(symbol="SPY", indicators=["z_score"])],
            ),
            "QQQ": Decision.hold("QQQ", "range"),
        }

    monkeypatch.setattr(daemon.codex_client, "decide_batch", fake_batch)
    decisions, n = daemon._batch_decide(decidable=["SPY", "QQQ"], max_model_calls=1, **_COMMON)

    assert calls["n"] == 1  # pas de 2e batch
    assert n == 1
    assert decisions["SPY"].action == "HOLD"
    assert "budget" in decisions["SPY"].rationale
    assert decisions["SPY"].context_request == {
        "rounds": 1,
        "requested": [{"symbol": "SPY", "indicators": ["z_score"], "timeframe": "1h"}],
        "resolved": 0,
    }
    assert decisions["QQQ"].action == "HOLD"


def test_veille_armee_sans_next_wake_dort_jusqu_a_expiration(tmp_path) -> None:
    """Finding 2026-07-02 (confirmé Codex) : une veille armée sans next_wake ne
    doit PLUS effacer le timer (→ défaut 30 min → re-décision aveugle), mais
    poser le réveil à l'expiration de la veille."""
    from trader.planning.scheduler import Scheduler

    sched = Scheduler(tmp_path / "scheduler.json")
    now = datetime(2026, 7, 2, 10, 0, tzinfo=timezone.utc)
    watch = {
        "id": "AIR.PA:w1",
        "symbol": "AIR.PA",
        "expires_at": "2026-07-02T13:00:00+00:00",  # +3h
        "logic": "all",
        "on_trigger": "EXECUTE_ORDER",
        "conditions": [{"indicator": "return", "op": ">", "value": 0.01, "timeframe": "1h"}],
    }
    daemon._apply_decision_schedule(
        sched=sched,
        sym="AIR.PA",
        now=now,
        next_wake_in_minutes=None,          # l'agent n'a rien programmé
        cancel_watch_ids=[],
        pending_indicator_watch=watch,
        entry={"indicator_watch_created": False},
    )

    # Le symbole dort jusqu'à l'expiration de la veille, PAS le défaut global.
    assert sched.next_wake("AIR.PA") == datetime(2026, 7, 2, 13, 0, tzinfo=timezone.utc)
    # Donc il n'est PAS dû avant (exclu du batch périodique de re-décision).
    assert "AIR.PA" not in sched.due_symbols(["AIR.PA"], now=datetime(2026, 7, 2, 10, 30, tzinfo=timezone.utc))
    # ...mais redevient dû après expiration (réarmement/revue possible).
    assert "AIR.PA" in sched.due_symbols(["AIR.PA"], now=datetime(2026, 7, 2, 13, 1, tzinfo=timezone.utc))


def test_hold_sans_veille_ni_wake_reste_sur_le_defaut(tmp_path) -> None:
    """Sans veille armée ni next_wake, le comportement historique tient :
    le timer symbole est effacé (→ cadence par défaut)."""
    from trader.planning.scheduler import Scheduler

    sched = Scheduler(tmp_path / "scheduler.json")
    now = datetime(2026, 7, 2, 10, 0, tzinfo=timezone.utc)
    sched.set_symbol_next_wake_in("SPY", minutes=99, now=now)  # un timer préexistant
    daemon._apply_decision_schedule(
        sched=sched,
        sym="SPY",
        now=now,
        next_wake_in_minutes=None,
        cancel_watch_ids=[],
        pending_indicator_watch=None,
        entry={},
    )
    assert sched.next_wake("SPY") is None  # effacé → retombe sur le défaut global


# ---------------------------------------------------------------------------
# L2 — _resolve_position_aware_decision (tests unitaires purs)
# ---------------------------------------------------------------------------

def _make_pending_close(symbol: str = "SPY", qty: float = 0.0) -> Decision:
    """Decision CLOSE sans side (resolve_from_position=True, action=HOLD provisoire)."""
    return Decision(
        symbol=symbol,
        action="HOLD",
        quantity=qty,
        confidence=0.8,
        rationale="close position",
        intent="CLOSE",
        resolve_from_position=True,
    )


def _make_pending_reduce(
    symbol: str = "SPY",
    qty: float = 0.0,
    fraction: float | None = None,
) -> Decision:
    """Decision REDUCE sans side (resolve_from_position=True)."""
    return Decision(
        symbol=symbol,
        action="HOLD",
        quantity=qty,
        confidence=0.8,
        rationale="reduce position",
        intent="REDUCE",
        resolve_from_position=True,
        reduce_fraction=fraction,
    )


def _make_pending_reverse(symbol: str = "SPY", qty: float = 20.0) -> Decision:
    """Decision REVERSE sans side (resolve_from_position=True, qty = nouvelle jambe)."""
    return Decision(
        symbol=symbol,
        action="HOLD",
        quantity=qty,
        confidence=0.8,
        rationale="flip position",
        intent="REVERSE",
        resolve_from_position=True,
    )


def test_resolve_close_long_position_produit_sell() -> None:
    """L2 : CLOSE sur position longue (qty=10) → action=SELL, qty=10."""
    dec = _make_pending_close()
    resolved = daemon._resolve_position_aware_decision(dec, position_quantity=10.0)

    assert resolved.action == "SELL"
    assert resolved.quantity == 10.0
    assert resolved.resolve_from_position is False


def test_resolve_close_short_position_produit_buy() -> None:
    """L2 : CLOSE sur position courte (qty=-8) → action=BUY, qty=8."""
    dec = _make_pending_close()
    resolved = daemon._resolve_position_aware_decision(dec, position_quantity=-8.0)

    assert resolved.action == "BUY"
    assert resolved.quantity == 8.0
    assert resolved.resolve_from_position is False


def test_resolve_reduce_fraction_sur_long_position() -> None:
    """L2 : REDUCE fraction=0.5 sur position=10 → SELL 5."""
    dec = _make_pending_reduce(fraction=0.5)
    resolved = daemon._resolve_position_aware_decision(dec, position_quantity=10.0)

    assert resolved.action == "SELL"
    assert resolved.quantity == 5.0


def test_resolve_reduce_qty_abs_sur_long_position() -> None:
    """L2 : REDUCE qty=3 (abs) sans fraction → SELL 3, clampé à |pos|."""
    dec = _make_pending_reduce(qty=3.0)
    resolved = daemon._resolve_position_aware_decision(dec, position_quantity=10.0)

    assert resolved.action == "SELL"
    assert resolved.quantity == 3.0


def test_resolve_reduce_qty_abs_depasse_position_est_clampee() -> None:
    """L2 fail-safe : qty > |pos| → clampé à |pos|, pas d'ordre oversized."""
    dec = _make_pending_reduce(qty=15.0)
    resolved = daemon._resolve_position_aware_decision(dec, position_quantity=10.0)

    assert resolved.action == "SELL"
    assert resolved.quantity == 10.0  # clampé à |pos|


def test_resolve_reverse_sur_long_position_derive_side_sell() -> None:
    """L2 : REVERSE sur position longue → SELL fermeture + nouvelle jambe."""
    dec = _make_pending_reverse(qty=20.0)
    resolved = daemon._resolve_position_aware_decision(dec, position_quantity=10.0)

    assert resolved.action == "SELL"
    assert resolved.quantity == 30.0


def test_resolve_reverse_deja_resolu_est_idempotent() -> None:
    """L2/R3 : un REVERSE déjà résolu ne double pas la jambe de fermeture."""
    dec = _make_pending_reverse(qty=20.0)
    once = daemon._resolve_position_aware_decision(dec, position_quantity=10.0)
    twice = daemon._resolve_position_aware_decision(once, position_quantity=10.0)

    assert twice == once
    assert twice.action == "SELL"
    assert twice.quantity == 30.0


def test_resolve_reverse_sur_short_position_derive_side_buy() -> None:
    """L2 : REVERSE sur position courte → BUY fermeture + nouvelle jambe."""
    dec = _make_pending_reverse(qty=20.0)
    resolved = daemon._resolve_position_aware_decision(dec, position_quantity=-5.0)

    assert resolved.action == "BUY"
    assert resolved.quantity == 25.0


def test_resolve_no_position_produit_nothing_to_close() -> None:
    """L2 fail-safe ABSOLU : pas de position → HOLD 'nothing_to_close', jamais d'ordre."""
    dec = _make_pending_close()
    resolved = daemon._resolve_position_aware_decision(dec, position_quantity=0.0)

    assert resolved.action == "HOLD"
    assert resolved.rationale == "nothing_to_close"


def test_resolve_relative_add_force_la_resolution_meme_sans_flag() -> None:
    """Défense aval : un Decision relatif construit à la main repasse par la dérivation."""
    dec = Decision(
        symbol="SPY",
        action="SELL",
        quantity=3.0,
        confidence=0.9,
        rationale="add explicite incoherent",
        intent="ADD",
        resolve_from_position=False,
    )
    resolved = daemon._resolve_position_aware_decision(dec, position_quantity=10.0)

    assert resolved.action == "BUY"
    assert resolved.quantity == 3.0
    assert resolved.resolve_from_position is False


def test_resolve_relative_close_sans_position_force_le_fusible_meme_sans_flag() -> None:
    """Défense aval : CLOSE manuel sans position → HOLD, jamais ordre explicite aveugle."""
    dec = Decision(
        symbol="SPY",
        action="SELL",
        quantity=10.0,
        confidence=0.9,
        rationale="close manuel",
        intent="CLOSE",
        resolve_from_position=False,
    )
    resolved = daemon._resolve_position_aware_decision(dec, position_quantity=0.0)

    assert resolved.action == "HOLD"
    assert resolved.quantity == 0.0
    assert resolved.intent == "HOLD"
    assert resolved.rationale == "nothing_to_close"


# ---------------------------------------------------------------------------
# L4 — ADD intent : _resolve_position_aware_decision (tests unitaires purs)
# ---------------------------------------------------------------------------

def _make_pending_add(symbol: str = "SPY", qty: float = 5.0) -> Decision:
    """Decision ADD sans side (resolve_from_position=True, qty = renforcement)."""
    return Decision(
        symbol=symbol,
        action="HOLD",
        quantity=qty,
        confidence=0.8,
        rationale="scale-in conviction",
        intent="ADD",  # type: ignore[arg-type]
        resolve_from_position=True,
    )


def test_resolve_add_long_position_produit_buy() -> None:
    """L4 : ADD sur position longue → action=BUY (même sens), qty conservée."""
    dec = _make_pending_add(qty=5.0)
    resolved = daemon._resolve_position_aware_decision(dec, position_quantity=10.0)

    assert resolved.action == "BUY"
    assert resolved.quantity == 5.0
    assert resolved.resolve_from_position is False


def test_resolve_add_short_position_produit_sell() -> None:
    """L4 : ADD sur position courte → action=SELL (même sens), qty conservée."""
    dec = _make_pending_add(qty=3.0)
    resolved = daemon._resolve_position_aware_decision(dec, position_quantity=-8.0)

    assert resolved.action == "SELL"
    assert resolved.quantity == 3.0
    assert resolved.resolve_from_position is False


def test_parse_side_explicite_relative_est_ignoree_puis_derivee_depuis_position() -> None:
    raw = """
    {"decisions": [
      {"symbol": "SPY", "confidence": 0.8, "rationale": "renforce long",
       "decision_reason_code": "ENTRY_SIGNAL",
       "calls": [{"tool": "propose_order", "args": {"intent": "ADD", "side": "SELL", "qty": 3}}]},
      {"symbol": "QQQ", "confidence": 0.8, "rationale": "flip short",
       "decision_reason_code": "REVERSAL",
       "calls": [{"tool": "propose_order", "args": {"intent": "REVERSE", "side": "SELL", "qty": 4}}]}
    ]}
    """
    parsed = parse_batch(raw, ["SPY", "QQQ"], allow_context_request=False)

    add = daemon._resolve_position_aware_decision(parsed["SPY"], position_quantity=10.0)
    reverse = daemon._resolve_position_aware_decision(parsed["QQQ"], position_quantity=-6.0)

    assert add.action == "BUY"
    assert add.quantity == 3.0
    assert reverse.action == "BUY"
    assert reverse.quantity == 10.0


def test_resolve_add_sans_position_produit_add_without_position() -> None:
    """L4 fail-safe : ADD sans position ouverte → HOLD 'add_without_position'."""
    dec = _make_pending_add(qty=5.0)
    resolved = daemon._resolve_position_aware_decision(dec, position_quantity=0.0)

    assert resolved.action == "HOLD"
    assert resolved.rationale == "add_without_position"


def test_resolve_add_ne_casse_pas_resolve_close_l2() -> None:
    """L4 invariant : l'ajout de ADD ne doit pas altérer la résolution CLOSE (L2).
    CLOSE sur position longue → SELL qty=|pos| (comportement L2 inchangé)."""
    dec = _make_pending_close()
    resolved = daemon._resolve_position_aware_decision(dec, position_quantity=10.0)

    assert resolved.action == "SELL"
    assert resolved.quantity == 10.0
