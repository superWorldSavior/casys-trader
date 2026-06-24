"""Tests d'intégration du gate de confiance adapté au risque dans run_cycle.

Vérifie que le daemon rejette correctement les ouvertures avec une confiance
insuffisante au regard du risque planifié, et laisse passer les ouvertures
ayant une confiance suffisante.
"""

from datetime import datetime, timezone


from trader import daemon
from trader.codex_client import Decision
from trader.tools.market import Bar
from trader.tools.memory import LearningsStore
from trader.tools.scheduler import Scheduler


def _write_runtime_config(root) -> None:
    (root / "config").mkdir()
    (root / "mandate").mkdir()
    (root / "config" / "universe.yaml").write_text(
        "starting_cash: 100000\nsymbols:\n  - SPY\n"
    )
    # risk.yaml sans les clés confidence → défauts 0.7/0.9 appliqués
    (root / "config" / "risk.yaml").write_text(
        "\n".join(
            [
                "max_position_value: 20000",
                "max_gross_exposure: 100000",
                "max_order_value: 10000",
                "max_risk_per_trade_pct: 0.01",
                "max_orders_per_cycle: 5",
                "min_equity: 50000",
            ]
        )
    )
    (root / "mandate" / "mandate.md").write_text("# Mandat\n")
    (root / "mandate" / "memory.md").write_text("# Memoire\n")


def _open_long_decision(confidence: float) -> Decision:
    """Décision OPEN_LONG avec un hard stop qui génère un risk_pct non nul."""
    return Decision(
        symbol="SPY",
        action="BUY",
        quantity=10.0,
        confidence=confidence,
        rationale="test gate confiance",
        intent="OPEN_LONG",
        exit_plan={"hard_stop": {"type": "price", "price": 95.0}},
    )


def test_run_cycle_rejette_ouverture_confidence_insuffisante(
    monkeypatch, tmp_path, make_data_source
) -> None:
    """Confidence 0.58 avec un risque calculé → rejeté confidence_below_required."""
    _write_runtime_config(tmp_path)
    state_dir = tmp_path / "state"
    now = datetime(2026, 6, 5, 14, 30, tzinfo=timezone.utc)  # vendredi, 10:30 ET, session US ouverte

    decision = _open_long_decision(confidence=0.58)

    def fake_batch_decide(**kwargs):
        return {sym: decision if sym == "SPY" else Decision.hold(sym, "hold") for sym in kwargs["decidable"]}, 1

    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    monkeypatch.setattr(daemon, "_batch_decide", fake_batch_decide)
    data_source = make_data_source(
        lambda symbol, lookback, interval: [
            Bar(ts=now.isoformat(), open=100.0, high=101.0, low=99.0, close=100.0, volume=1000.0)
        ]
    )

    report = daemon.run_cycle(
        dry_run=True,
        now=now,
        symbols_filter=["SPY"],
        sched=Scheduler(state_dir / "scheduler.json"),
        data_source=data_source,
    )

    decision_entry = report["decisions"][0]
    assert decision_entry["executed"] is False
    assert decision_entry["reason"] == "risk:confidence_below_required"


def test_merge_gate_feedback_fusionne_avec_le_learning_de_lagent() -> None:
    """Sur rejet de confiance, le seuil raté est appendé au learning de l'agent."""
    out = daemon._merge_gate_feedback(
        "risk:confidence_below_required",
        "confidence=0.58 required=0.7000 planned_risk_pct=0.005",
        "je tente un long sur cassure",
    )
    assert out is not None
    assert "je tente un long sur cassure" in out  # learning de l'agent préservé
    assert "required=0.7000" in out               # seuil exact raté


def test_merge_gate_feedback_trace_le_rejet_meme_sans_learning() -> None:
    """Rejet sans learning agent → on enregistre quand même le feedback du gate."""
    out = daemon._merge_gate_feedback(
        "risk:confidence_below_required",
        "confidence=0.58 required=0.7000",
        None,
    )
    assert out is not None and "required=0.7000" in out


def test_merge_gate_feedback_laisse_les_autres_cas_intacts() -> None:
    """Hors rejet de confiance (ou sans contexte), la note de l'agent passe telle quelle."""
    assert daemon._merge_gate_feedback("ok", "ctx", "garde") == "garde"
    assert daemon._merge_gate_feedback("ok", None, None) is None
    # fail-safe : rejet de confiance mais contexte manquant → note inchangée
    assert daemon._merge_gate_feedback("risk:confidence_below_required", None, "x") == "x"


def test_run_cycle_rejet_confiance_injecte_le_feedback_dans_les_learnings(
    monkeypatch, tmp_path, make_data_source
) -> None:
    """Intégration : un rejet de confiance laisse à l'agent le seuil exact raté
    dans ses learnings relus au prochain réveil (pas juste un échec silencieux)."""
    _write_runtime_config(tmp_path)
    state_dir = tmp_path / "state"
    now = datetime(2026, 6, 5, 14, 30, tzinfo=timezone.utc)  # vendredi, séance US ouverte

    decision = Decision(
        symbol="SPY",
        action="BUY",
        quantity=10.0,
        confidence=0.58,
        rationale="test gate confiance",
        intent="OPEN_LONG",
        exit_plan={"hard_stop": {"type": "price", "price": 95.0}},
        learning="je tente un long sur cassure",
    )

    def fake_batch_decide(**kwargs):
        return {sym: decision if sym == "SPY" else Decision.hold(sym, "hold") for sym in kwargs["decidable"]}, 1

    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    monkeypatch.setattr(daemon, "_batch_decide", fake_batch_decide)
    data_source = make_data_source(
        lambda symbol, lookback, interval: [
            Bar(ts=now.isoformat(), open=100.0, high=101.0, low=99.0, close=100.0, volume=1000.0)
        ]
    )

    daemon.run_cycle(
        dry_run=True,
        now=now,
        symbols_filter=["SPY"],
        sched=Scheduler(state_dir / "scheduler.json"),
        data_source=data_source,
    )

    recent = LearningsStore(state_dir / "learnings.jsonl").recent()
    assert recent, "le rejet de confiance doit laisser une trace dans les learnings"
    note = recent[0]["note"]
    assert "je tente un long sur cassure" in note   # learning de l'agent préservé
    assert "confidence=0.58" in note and "required=" in note  # feedback du gate
    assert recent[0]["reason"] == "risk:confidence_below_required"


def test_run_cycle_approuve_ouverture_confidence_suffisante(
    monkeypatch, tmp_path, make_data_source
) -> None:
    """Confidence 0.95 avec le même risque → approuvé (dry_run, donc executed=False mais reason=ok)."""
    _write_runtime_config(tmp_path)
    state_dir = tmp_path / "state"
    now = datetime(2026, 6, 5, 14, 30, tzinfo=timezone.utc)  # vendredi, 10:30 ET, session US ouverte

    decision = _open_long_decision(confidence=0.95)

    def fake_batch_decide(**kwargs):
        return {sym: decision if sym == "SPY" else Decision.hold(sym, "hold") for sym in kwargs["decidable"]}, 1

    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    monkeypatch.setattr(daemon, "_batch_decide", fake_batch_decide)
    data_source = make_data_source(
        lambda symbol, lookback, interval: [
            Bar(ts=now.isoformat(), open=100.0, high=101.0, low=99.0, close=100.0, volume=1000.0)
        ]
    )

    report = daemon.run_cycle(
        dry_run=True,
        now=now,
        symbols_filter=["SPY"],
        sched=Scheduler(state_dir / "scheduler.json"),
        data_source=data_source,
    )

    decision_entry = report["decisions"][0]
    # dry_run=True → executed=False mais reason=ok (gate confiance passé)
    assert decision_entry["reason"] == "ok"


def test_run_cycle_rejette_ouverture_sans_hard_stop(
    monkeypatch, tmp_path, make_data_source
) -> None:
    """Guardrail D6 déterministe : ouverture sans hard_stop → rejet, même à confiance max."""
    _write_runtime_config(tmp_path)
    state_dir = tmp_path / "state"
    now = datetime(2026, 6, 5, 14, 30, tzinfo=timezone.utc)  # vendredi, 10:30 ET, session US ouverte

    decision = Decision(
        symbol="SPY",
        action="BUY",
        quantity=10.0,
        confidence=0.97,  # même très confiant, pas de stop = pas d'ouverture
        rationale="setup",
        intent="OPEN_LONG",
        exit_plan=None,
    )

    def fake_batch_decide(**kwargs):
        return {sym: decision if sym == "SPY" else Decision.hold(sym, "hold") for sym in kwargs["decidable"]}, 1

    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    monkeypatch.setattr(daemon, "_batch_decide", fake_batch_decide)
    data_source = make_data_source(
        lambda symbol, lookback, interval: [
            Bar(ts=now.isoformat(), open=100.0, high=101.0, low=99.0, close=100.0, volume=1000.0)
        ]
    )

    report = daemon.run_cycle(
        dry_run=True,
        now=now,
        symbols_filter=["SPY"],
        sched=Scheduler(state_dir / "scheduler.json"),
        data_source=data_source,
    )

    decision_entry = report["decisions"][0]
    assert decision_entry["executed"] is False
    assert decision_entry["reason"] == "risk:missing_hard_stop"


def _enable_exploration(root) -> None:
    """Mode exploration paper : gate confiance off, stop optionnel, position 30k."""
    (root / "config" / "risk.yaml").write_text(
        "\n".join(
            [
                "max_position_value: 30000",
                "max_gross_exposure: 100000",
                "max_order_value: 10000",
                "max_risk_per_trade_pct: 0.01",
                "max_orders_per_cycle: 5",
                "min_equity: 50000",
                "confidence_gate_enabled: false",
                "require_hard_stop: false",
            ]
        )
    )


def test_run_cycle_gate_confiance_off_laisse_passer_confiance_basse(
    monkeypatch, tmp_path, make_data_source
) -> None:
    """confidence_gate_enabled=false → ouverture (avec stop) à confiance ridicule
    n'est PLUS rejetée pour la confiance."""
    _write_runtime_config(tmp_path)
    _enable_exploration(tmp_path)
    state_dir = tmp_path / "state"
    now = datetime(2026, 6, 5, 14, 30, tzinfo=timezone.utc)
    decision = _open_long_decision(confidence=0.05)

    def fake_batch_decide(**kwargs):
        return {sym: decision if sym == "SPY" else Decision.hold(sym, "hold") for sym in kwargs["decidable"]}, 1

    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    monkeypatch.setattr(daemon, "_batch_decide", fake_batch_decide)
    data_source = make_data_source(
        lambda symbol, lookback, interval: [
            Bar(ts=now.isoformat(), open=100.0, high=101.0, low=99.0, close=100.0, volume=1000.0)
        ]
    )

    report = daemon.run_cycle(
        dry_run=True, now=now, symbols_filter=["SPY"],
        sched=Scheduler(state_dir / "scheduler.json"), data_source=data_source,
    )

    entry = report["decisions"][0]
    # dry_run → executed=False mais reason=ok (gate confiance off, ordre passé)
    assert entry["reason"] == "ok"


def test_run_cycle_require_hard_stop_false_laisse_passer_sans_stop(
    monkeypatch, tmp_path, make_data_source
) -> None:
    """require_hard_stop=false → ouverture SANS hard_stop n'est plus rejetée
    missing_hard_stop ; bornée par les seuls fusibles notionnels."""
    _write_runtime_config(tmp_path)
    _enable_exploration(tmp_path)
    state_dir = tmp_path / "state"
    now = datetime(2026, 6, 5, 14, 30, tzinfo=timezone.utc)
    decision = Decision(
        symbol="SPY", action="BUY", quantity=10.0, confidence=0.55,
        rationale="explore sans stop", intent="OPEN_LONG", exit_plan=None,
    )

    def fake_batch_decide(**kwargs):
        return {sym: decision if sym == "SPY" else Decision.hold(sym, "hold") for sym in kwargs["decidable"]}, 1

    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    monkeypatch.setattr(daemon, "_batch_decide", fake_batch_decide)
    data_source = make_data_source(
        lambda symbol, lookback, interval: [
            Bar(ts=now.isoformat(), open=100.0, high=101.0, low=99.0, close=100.0, volume=1000.0)
        ]
    )

    report = daemon.run_cycle(
        dry_run=True, now=now, symbols_filter=["SPY"],
        sched=Scheduler(state_dir / "scheduler.json"), data_source=data_source,
    )

    entry = report["decisions"][0]
    # dry_run → executed=False mais reason=ok (stop optionnel, borné par notionnel)
    assert entry["reason"] == "ok"


def test_attribution_min_entry_confidence_none_quand_gate_off() -> None:
    """Gate confiance off → attribution non censurée (None) : les trips basse-confiance
    entrent dans les buckets by_confidence. Gate on → seuil min_trade_confidence."""
    assert (
        daemon._attribution_min_entry_confidence(
            {"min_trade_confidence": 0.7}, confidence_gate_enabled=False
        )
        is None
    )
    assert (
        daemon._attribution_min_entry_confidence(
            {"min_trade_confidence": 0.65}, confidence_gate_enabled=True
        )
        == 0.65
    )
    assert (
        daemon._attribution_min_entry_confidence({}, confidence_gate_enabled=True) == 0.7
    )


def test_run_cycle_ouverture_enrichit_le_tradeplan_avec_le_contexte_d_entree(
    monkeypatch, tmp_path, make_data_source
) -> None:
    """§13.7 — au fill d'ouverture, le TradePlan capture la thèse (rationale) et le
    contexte d'entrée (prix, runtime interval, data age, session, daily as-of)."""
    from trader.trade_plan import TradePlanStore

    _write_runtime_config(tmp_path)
    state_dir = tmp_path / "state"
    now = datetime(2026, 6, 5, 14, 30, tzinfo=timezone.utc)  # vendredi, séance US ouverte
    decision = _open_long_decision(confidence=0.95)

    def fake_batch_decide(**kwargs):
        return {sym: decision if sym == "SPY" else Decision.hold(sym, "hold") for sym in kwargs["decidable"]}, 1

    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    monkeypatch.setattr(daemon, "_batch_decide", fake_batch_decide)
    data_source = make_data_source(
        lambda symbol, lookback, interval: [
            Bar(ts=now.isoformat(), open=100.0, high=101.0, low=99.0, close=100.0, volume=1000.0)
        ]
    )

    daemon.run_cycle(
        dry_run=False, now=now, symbols_filter=["SPY"],
        sched=Scheduler(state_dir / "scheduler.json"), data_source=data_source,
    )

    plans = TradePlanStore(state_dir / "trade_plans.json").open_plans()
    assert len(plans) == 1
    plan = plans[0]
    assert plan.entry_thesis == "test gate confiance"
    assert plan.entry_context is not None
    assert plan.entry_context["price"] == 100.0
    assert plan.entry_context["runtime_interval"] == "15m"
