"""Tests du fusible currency-correct : gate borne la quantité native de l'agent.

Ces tests verrouillent :
  1. le rôle fusible de RiskGate.max_order_quantity_at_price avec fx_rate non-USD ;
  2. la formule du budget natif (utilisée en Task 5B pour construire le contexte agent) ;
  3. l'intégration daemon : run_cycle REJETTE (sans modifier la quantité agent)
     un ordre dont la valeur dépasse max_order_value.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from trader.runtime import daemon
from trader.agent.client import Decision
from trader.execution.risk import RiskGate, RiskLimits
from trader.market import fx
from trader.execution.broker import Order, SimBroker
from trader.tools.market import Bar
from trader.tools.scheduler import Scheduler


# ---------------------------------------------------------------------------
# Tests unitaires (risk gate + formule budget natif)
# ---------------------------------------------------------------------------


def test_gate_order_value_cap_is_currency_correct():
    """Unité (gate seul, PAS le daemon) : max_order_quantity_at_price calcule un
    plafond d'ordre currency-correct. NB : le daemon REJETTE (reject-not-clamp) un
    ordre au-dessus de ce plafond — il ne le réduit pas. Ce plafond sert de conseil
    de sizing (advisory) côté contexte agent (Task 5B), pas de clamp daemon."""
    gate = RiskGate(RiskLimits(
        max_order_value=10_000.0,
        max_risk_per_trade_pct=0.01,
        max_position_value=50_000.0,
        max_gross_exposure=200_000.0,
        max_orders_per_cycle=10,
        min_equity=0.0,
    ))
    assert fx.currency_for("2379.TW") == "TWD"
    advisory_cap = gate.max_order_quantity_at_price(870.0, fx_rate=0.031)
    assert advisory_cap * 870.0 * 0.031 <= 10_000.0 + 1e-6  # exposition USD bornée
    assert advisory_cap > 300.0                             # pas l'ancien ~11 (currency-correct)


def test_native_risk_budget_formula():
    """Le budget natif livré à l'agent = risk_pct * equity_USD / fx_rate."""
    equity_usd, pct, rate = 100_000.0, 0.01, 0.031
    risk_budget_native = pct * equity_usd / rate
    assert risk_budget_native == pytest.approx(1_000.0 / 0.031)  # ~32258 TWD


def test_risk_capacity_context_expose_le_plafond_gross_restant_en_quantite_native(tmp_path) -> None:
    """Le contexte agent doit voir le plafond restant, pas seulement max_order_value.

    Régression du 2026-06-29 : BUY 2892.TW rejeté car max_order_native passait,
    mais le portefeuille était déjà proche de max_gross_exposure.
    """
    broker = SimBroker(tmp_path / "broker.json", starting_cash=100_000.0)
    broker.submit(
        Order("SPY", "BUY", 925.0),
        100.0,
        "2026-06-29T04:00:00+00:00",
        dry_run=False,
        fx_rate=1.0,
    )
    prices = {"SPY": 100.0, "2892.TW": 32.95}
    rates = {"SPY": 1.0, "2892.TW": 0.031}
    def rate_of(sym):
        return rates[sym]
    limits = RiskLimits(
        max_order_value=10_000.0,
        max_risk_per_trade_pct=0.01,
        max_position_value=30_000.0,
        max_gross_exposure=100_000.0,
        max_orders_per_cycle=5,
        min_equity=50_000.0,
    )
    gross = daemon._gross_exposure(broker, prices, rate_of=rate_of)

    context = daemon._risk_capacity_context(
        symbols=["2892.TW"],
        prices=prices,
        broker=broker,
        gross_exposure=gross,
        limits=limits,
        equity=98_700.0,
        rate_of=rate_of,
    )

    expected_remaining_usd = 7_500.0
    expected_qty = expected_remaining_usd / (32.95 * 0.031)
    per_symbol = context["per_symbol"]["2892.TW"]
    assert context["gross_remaining_usd"] == pytest.approx(expected_remaining_usd)
    assert per_symbol["max_buy_qty"] == pytest.approx(expected_qty)
    assert per_symbol["max_buy_notional_native"] == pytest.approx(expected_remaining_usd / 0.031)


# ---------------------------------------------------------------------------
# Test d'intégration : run_cycle clampe une quantité sur-proposée
# ---------------------------------------------------------------------------


def _write_config_with_fx(root, *, symbols=("SPY",)) -> None:
    """Crée la config minimale incluant fx.yaml pour le test d'intégration."""
    (root / "config").mkdir(exist_ok=True)
    (root / "mandate").mkdir(exist_ok=True)
    symbols_yaml = "".join(f"  - {symbol}\n" for symbol in symbols)
    (root / "config" / "universe.yaml").write_text(
        f"starting_cash: 100000\nsymbols:\n{symbols_yaml}"
    )
    (root / "config" / "risk.yaml").write_text(
        "max_position_value: 20000\n"
        "max_gross_exposure: 100000\n"
        "max_order_value: 10000\n"
        "max_risk_per_trade_pct: 0.01\n"
        "max_orders_per_cycle: 5\n"
        "min_equity: 50000\n"
    )
    # fx.yaml avec uniquement USD (SPY est USD — pas de conversion)
    (root / "config" / "fx.yaml").write_text(
        "# fx.yaml minimal pour tests\n"
        "TWD:\n"
        "  yahoo: TWD=X\n"
        "  invert: true\n"
        "  fallback: 0.031\n"
        "EUR:\n"
        "  yahoo: EURUSD=X\n"
        "  invert: false\n"
        "  fallback: 1.08\n"
    )
    (root / "mandate" / "mandate.md").write_text("# Mandat\n")
    (root / "mandate" / "memory.md").write_text("# Memoire\n")


def test_run_cycle_rejette_quantite_native_sur_proposee(monkeypatch, tmp_path, make_data_source) -> None:
    """Le daemon REJETTE (sans clamper) un BUY absurde (1000) qui dépasse max_order_value.

    SPY ≈ 100 USD, max_order_value=10 000 USD → order_value=100 000 > 10 000 → rejeté.
    La quantité de l'agent (1000) n'est pas modifiée ; l'ordre n'est pas exécuté.
    """
    _write_config_with_fx(tmp_path)
    state_dir = tmp_path / "state"
    now = datetime(2026, 6, 5, 14, 30, tzinfo=timezone.utc)

    # Agent propose une quantité absurde
    decision = Decision(
        symbol="SPY",
        action="BUY",
        quantity=1000.0,
        confidence=0.95,
        rationale="test reject",
        intent="OPEN_LONG",
        exit_plan={"hard_stop": {"type": "price", "price": 95.0}},
    )

    def fake_batch_decide(**kwargs):
        return {
            sym: decision if sym == "SPY" else Decision.hold(sym, "hold")
            for sym in kwargs["decidable"]
        }, 1

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

    decisions = report["decisions"]
    assert decisions, "aucune décision dans le rapport"
    spy_dec = next((d for d in decisions if d["symbol"] == "SPY"), None)
    assert spy_dec is not None, "décision SPY absente"

    # L'ordre doit être REJETÉ (executed=False, reason contient 'risk')
    # et la quantité dans le journal doit rester celle de l'agent (1000), non clampée
    assert spy_dec.get("executed") is False, (
        f"ordre non rejeté : executed={spy_dec.get('executed')}"
    )
    assert "risk" in (spy_dec.get("reason") or ""), (
        f"raison de rejet inattendue : {spy_dec.get('reason')}"
    )
    recorded_qty = spy_dec.get("qty", spy_dec.get("quantity"))
    assert recorded_qty == pytest.approx(1000.0), (
        f"quantité agent modifiée par le daemon : {recorded_qty} ≠ 1000"
    )


def test_run_cycle_fx_rates_dans_le_rapport(monkeypatch, tmp_path, make_data_source) -> None:
    """Le rapport de cycle contient la clé 'fx_rates' pour l'audit."""
    _write_config_with_fx(tmp_path)
    state_dir = tmp_path / "state"
    now = datetime(2026, 6, 5, 14, 30, tzinfo=timezone.utc)

    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    monkeypatch.setattr(daemon, "_batch_decide", lambda **kw: (
        {sym: Decision.hold(sym, "hold") for sym in kw["decidable"]}, 0
    ))

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

    assert "fx_rates" in report, "clé 'fx_rates' absente du rapport"
    assert isinstance(report["fx_rates"], dict)
    assert report["fx_rates"].get("USD") == 1.0


def test_run_cycle_persiste_fx_rate_dans_model_performance_non_usd(
    monkeypatch,
    tmp_path,
    make_data_source,
) -> None:
    """Attribution/trades clôturés lisent model_performance.jsonl.

    Si le daemon n'y copie pas le fx_rate du fill, un trade TWD est ensuite
    recompté comme si 1 TWD valait 1 USD dans le cockpit.
    """
    _write_config_with_fx(tmp_path, symbols=("2379.TW",))
    state_dir = tmp_path / "state"
    now = datetime(2026, 6, 29, 1, 30, tzinfo=timezone.utc)
    expected_rate = 1.0 / 32.0

    decision = Decision(
        symbol="2379.TW",
        action="BUY",
        quantity=100.0,
        confidence=0.95,
        rationale="test fx",
        intent="OPEN_LONG",
        exit_plan={"hard_stop": {"type": "price", "price": 860.0}},
    )

    def fake_batch_decide(**kwargs):
        return {sym: decision for sym in kwargs["decidable"]}, 1

    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    monkeypatch.setattr(daemon, "_batch_decide", fake_batch_decide)

    def bars(symbol: str, lookback: str, interval: str):
        close = 32.0 if symbol == "TWD=X" else 870.0
        return [
            Bar(
                ts=now.isoformat(),
                open=close,
                high=close,
                low=close,
                close=close,
                volume=1000.0,
            )
        ]

    report = daemon.run_cycle(
        dry_run=False,
        now=now,
        symbols_filter=["2379.TW"],
        sched=Scheduler(state_dir / "scheduler.json"),
        data_source=make_data_source(bars),
    )

    assert report["decisions"][0]["executed"] is True
    rows = [
        __import__("json").loads(line)
        for line in (state_dir / "model_performance.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert rows[-1]["symbol"] == "2379.TW"
    assert rows[-1]["fx_rate"] == pytest.approx(expected_rate)
