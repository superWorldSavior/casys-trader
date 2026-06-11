"""D7 étage B — plans armés : le daemon exécute au déclenchement sans re-appel LLM."""

from datetime import datetime, timezone

from trader import daemon
from trader.codex_client import Decision
from trader.tools.market import Bar
from trader.tools.execution import SimBroker
from trader.tools.scheduler import Scheduler


def _runtime_config(root) -> None:
    (root / "config").mkdir()
    (root / "mandate").mkdir()
    (root / "config" / "universe.yaml").write_text(
        "starting_cash: 100000\nsymbols:\n  - SPY\n"
    )
    (root / "config" / "risk.yaml").write_text(
        "max_position_value: 20000\nmax_gross_exposure: 100000\n"
        "max_order_value: 10000\nmax_orders_per_cycle: 5\nmin_equity: 50000\n"
    )
    (root / "mandate" / "mandate.md").write_text("# Mandat\n")
    (root / "mandate" / "memory.md").write_text("# Memoire\n")


def _bars_at(now_iso: str):
    def factory(symbol, lookback, interval):
        return [
            Bar(ts=now_iso, open=100.0, high=100.5, low=99.5, close=100.0, volume=1000.0)
            for _ in range(4)
        ]

    return factory


def _armed_trigger(*, stop_price: float, intent: str = "OPEN_LONG") -> dict:
    action = "BUY" if intent == "OPEN_LONG" else "SELL"
    return {
        "watch_id": "SPY:abc123",
        "symbol": "SPY",
        "on_trigger": "EXECUTE_ORDER",
        "order": {
            "intent": intent,
            "action": action,
            "qty": 10.0,
            "confidence": 0.9,
            "exit_plan": {"hard_stop": {"type": "price", "price": stop_price}},
            "rationale": "scénario breakout",
        },
    }


def _run(monkeypatch, tmp_path, patch_batch, make_data_source, trigger) -> tuple[dict, list]:
    _runtime_config(tmp_path)
    state_dir = tmp_path / "state"
    now = datetime(2026, 6, 11, 12, 0, tzinfo=timezone.utc)

    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)

    llm_calls: list[str] = []

    def decide(**kwargs):
        llm_calls.append(kwargs["symbol"])
        return Decision.hold(kwargs["symbol"], "attente")

    patch_batch(decide)
    report = daemon.run_cycle(
        dry_run=False,
        now=now,
        symbols_filter=["SPY"],
        sched=Scheduler(state_dir / "scheduler.json"),
        data_source=make_data_source(_bars_at(now.isoformat())),
        indicator_triggers=[trigger],
    )
    return report, llm_calls


def test_plan_arme_execute_sans_appel_llm(monkeypatch, tmp_path, patch_batch, make_data_source) -> None:
    # prix 100, stop 95 (LONG) : cohérent -> exécution directe
    report, llm_calls = _run(
        monkeypatch, tmp_path, patch_batch, make_data_source, _armed_trigger(stop_price=95.0)
    )

    assert llm_calls == []  # zéro appel modèle
    entry = report["decisions"][0]
    assert entry["executed"] is True
    assert entry["reason"] == "ok"
    assert entry["armed_plan_id"] == "SPY:abc123"
    assert entry["llm_provider"] is None
    assert SimBroker(tmp_path / "state" / "broker.json").positions()["SPY"].quantity == 10.0


def test_plan_arme_incoherent_avec_le_stop_reveille_le_planificateur(
    monkeypatch, tmp_path, patch_batch, make_data_source
) -> None:
    # prix 100, stop 105 (LONG) : le prix a DÉJÀ franchi le stop -> pas d'exécution
    # aveugle ; le scénario est invalidé, donc on réveille le LLM AVEC le contexte
    # d'annulation pour qu'il re-décide (re-armer autrement, ou laisser).
    report, llm_calls = _run(
        monkeypatch, tmp_path, patch_batch, make_data_source, _armed_trigger(stop_price=105.0)
    )

    assert llm_calls == ["SPY"]  # un appel : le planificateur est informé
    entry = report["decisions"][0]
    assert entry["executed"] is False  # le faux LLM répond HOLD
    assert "SPY" not in SimBroker(tmp_path / "state" / "broker.json").positions()
    # télémétrie : l'annulation est tracée en événement
    events = (tmp_path / "state" / "events.jsonl").read_text(encoding="utf-8")
    assert "armed_plan_cancelled" in events
    assert "stop_incoherent" in events


def test_plan_arme_passe_par_le_gate_de_risque(
    monkeypatch, tmp_path, patch_batch, make_data_source
) -> None:
    # qty énorme : le clamp risque 1% s'applique comme pour toute décision
    trigger = _armed_trigger(stop_price=95.0)
    trigger["order"]["qty"] = 5_000.0  # 5000 × 5 de stop_distance = 25000 >> 1% equity
    report, llm_calls = _run(monkeypatch, tmp_path, patch_batch, make_data_source, trigger)

    assert llm_calls == []
    entry = report["decisions"][0]
    assert entry["qty"] < 5_000.0  # clampé (risque et/ou order_value)
    assert entry.get("risk_clamped") is True


def test_plan_arme_sur_position_existante_reveille_le_planificateur(
    monkeypatch, tmp_path, patch_batch, make_data_source
) -> None:
    # une position existe déjà : pas d'exécution mécanique d'un plan d'OUVERTURE
    # potentiellement périmé — le planificateur re-décide (review Codex).
    from trader.tools.execution import Order

    _runtime_config(tmp_path)
    state_dir = tmp_path / "state"
    now = datetime(2026, 6, 11, 12, 0, tzinfo=timezone.utc)
    broker = SimBroker(state_dir / "broker.json", starting_cash=100_000)
    broker.submit(Order("SPY", "BUY", 5.0), 100.0, "2026-06-11T11:00:00+00:00", dry_run=False)

    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)

    llm_calls: list[str] = []

    def decide(**kwargs):
        llm_calls.append(kwargs["symbol"])
        return Decision.hold(kwargs["symbol"], "attente")

    patch_batch(decide)
    daemon.run_cycle(
        dry_run=False,
        now=now,
        symbols_filter=["SPY"],
        sched=Scheduler(state_dir / "scheduler.json"),
        data_source=make_data_source(_bars_at(now.isoformat())),
        indicator_triggers=[_armed_trigger(stop_price=95.0)],
    )

    assert llm_calls == ["SPY"]  # réveil planificateur, pas d'exécution aveugle
    assert SimBroker(state_dir / "broker.json").positions()["SPY"].quantity == 5.0  # inchangée
    events = (state_dir / "events.jsonl").read_text(encoding="utf-8")
    assert "position_exists" in events


def test_plan_arme_trace_sa_provenance_dans_le_ledger(
    monkeypatch, tmp_path, patch_batch, make_data_source
) -> None:
    import json as _json

    _run(monkeypatch, tmp_path, patch_batch, make_data_source, _armed_trigger(stop_price=95.0))

    rows = [
        _json.loads(line)
        for line in (tmp_path / "state" / "decisions.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    executed = [r for r in rows if r.get("executed")]
    assert executed and executed[0]["source"] == "armed_plan"


def test_deux_plans_du_meme_symbole_au_meme_cycle_reveillent_le_planificateur(
    monkeypatch, tmp_path, patch_batch, make_data_source
) -> None:
    # scénarios alternatifs qui déclenchent ENSEMBLE = ambiguïté : on n'exécute
    # pas arbitrairement le dernier, on réveille le planificateur (D7, Erwan).
    _runtime_config(tmp_path)
    state_dir = tmp_path / "state"
    now = datetime(2026, 6, 11, 12, 0, tzinfo=timezone.utc)

    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)

    llm_calls: list[str] = []

    def decide(**kwargs):
        llm_calls.append(kwargs["symbol"])
        return Decision.hold(kwargs["symbol"], "attente")

    patch_batch(decide)
    plan_long = _armed_trigger(stop_price=95.0, intent="OPEN_LONG")
    plan_short = _armed_trigger(stop_price=105.0, intent="OPEN_SHORT")
    plan_short["watch_id"] = "SPY:def456"

    daemon.run_cycle(
        dry_run=False,
        now=now,
        symbols_filter=["SPY"],
        sched=Scheduler(state_dir / "scheduler.json"),
        data_source=make_data_source(_bars_at(now.isoformat())),
        indicator_triggers=[plan_long, plan_short],
    )

    assert llm_calls == ["SPY"]  # le planificateur arbitre, pas le hasard
    assert "SPY" not in SimBroker(state_dir / "broker.json").positions()
    events = (state_dir / "events.jsonl").read_text(encoding="utf-8")
    assert "armed_plan_conflict" in events
