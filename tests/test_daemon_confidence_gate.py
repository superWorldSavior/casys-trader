"""Tests d'intégration du gate de confiance adapté au risque dans run_cycle.

Vérifie que le daemon rejette correctement les ouvertures avec une confiance
insuffisante au regard du risque planifié, et laisse passer les ouvertures
ayant une confiance suffisante.
"""

from datetime import datetime, timezone

import pytest

from trader import daemon
from trader.codex_client import Decision
from trader.tools.market import Bar
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
    now = datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)

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


def test_run_cycle_approuve_ouverture_confidence_suffisante(
    monkeypatch, tmp_path, make_data_source
) -> None:
    """Confidence 0.95 avec le même risque → approuvé (dry_run, donc executed=False mais reason=ok)."""
    _write_runtime_config(tmp_path)
    state_dir = tmp_path / "state"
    now = datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)

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
    now = datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)

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
