"""Tests du helper batch _batch_decide (budget d'appels modèle honoré)."""

from datetime import datetime, timezone

from trader import daemon
from trader.codex_client import ContextResearchRequest, Decision, IndicatorRequest
from trader.indicator_watch import summarize_watch
from trader.tools.market import Bar
from trader.tools.scheduler import Scheduler

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


def _write_runtime_config(root) -> None:
    (root / "config").mkdir()
    (root / "mandate").mkdir()
    (root / "config" / "universe.yaml").write_text("starting_cash: 100000\nsymbols:\n  - SPY\n")
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
    assert captured["SPY"]["session"] == {"open": True}
    assert "since_open_m" not in captured["SPY"]["session"]
    assert "to_close_m" not in captured["SPY"]["session"]
    assert captured["QQQ"]["data_age_m"] is None  # âge inconnu = inconnu, pas 0


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

    from trader.trade_plan import TradePlanStore, create_trade_plan

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
        def set_symbol_next_wake_in(self, sym: str, *, minutes: float, now: datetime) -> None:
            operations.append(("wake", sym))

        def clear_symbol_next_wake(self, sym: str) -> None:
            operations.append(("clear", sym))

        def remove_indicator_watch(self, watch_id: str) -> None:
            operations.append(("remove", watch_id))

        def set_symbol_indicator_watch(self, sym: str, watch: dict) -> None:
            operations.append(("set", watch["id"]))

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


def test_apply_decision_schedule_rejette_l_annulation_d_une_watch_autre_symbole(monkeypatch) -> None:
    now = datetime(2026, 6, 15, 14, 30, tzinfo=timezone.utc)
    operations: list[tuple[str, str]] = []
    events: list[tuple[str, dict]] = []

    class RecordingScheduler:
        def set_symbol_next_wake_in(self, sym: str, *, minutes: float, now: datetime) -> None:
            operations.append(("wake", sym))

        def clear_symbol_next_wake(self, sym: str) -> None:
            operations.append(("clear", sym))

        def remove_indicator_watch(self, watch_id: str) -> None:
            operations.append(("remove", watch_id))

        def set_symbol_indicator_watch(self, sym: str, watch: dict) -> None:
            operations.append(("set", watch["id"]))

    monkeypatch.setattr(
        daemon,
        "_append_event",
        lambda event, **payload: events.append((event, payload)),
    )

    daemon._apply_decision_schedule(
        sched=RecordingScheduler(),
        sym="SYM",
        now=now,
        next_wake_in_minutes=None,
        cancel_watch_ids=["OTHER:hash"],
        pending_indicator_watch=None,
        entry={},
    )

    assert ("remove", "OTHER:hash") not in operations
    assert events == [
        (
            "watch_cancel_rejected",
            {"symbol": "SYM", "watch_id": "OTHER:hash", "reason": "not_owned_by_symbol"},
        )
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


def test_run_cycle_passe_le_plafond_decisionnel_a_batch_decide(monkeypatch, tmp_path, make_data_source) -> None:
    _write_runtime_config(tmp_path)
    state_dir = tmp_path / "state"
    now = datetime(2026, 6, 5, 12, 0, tzinfo=timezone.utc)
    captured: dict[str, int] = {}

    def fake_batch_decide(**kwargs):
        captured["decision_timeout_s"] = kwargs["decision_timeout_s"]
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
    )

    assert captured["decision_timeout_s"] == 444


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
