"""TDD L1 — sizing en risque (risk_pct).

Couvre :
1. Parsing : risk_pct extrait → Decision.risk_pct_target, qty placeholder = 0.0
2. Parsing : qty explicite → comportement inchangé, risk_pct_target = None
3. Parsing : pas de qty ni risk_pct → order_qty_required
4. qty_from_risk_pct : unités, fx, inputs invalides
5. Intégration daemon : risk_pct sans hard_stop → risk_sizing_needs_stop
6. Intégration daemon : risk_pct + hard_stop → qty dérivée correctement, ordre exécuté
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from trader.agent.protocol.parsing import _decision_from_symbol_calls
from trader.agent.protocol.types import Decision
from trader.application.decide import planner_batch
from trader.application.execute.order_admission import qty_from_risk_pct
from trader.runtime import daemon
from trader.market.market_data import Bar
from trader.planning.scheduler import Scheduler


# ---------------------------------------------------------------------------
# 1-3 : tests de parsing
# ---------------------------------------------------------------------------


def _symbol_call_data(*, direction: str = "long", exit_plan: dict | None = None, **extra_args) -> dict:
    """Construit un élément 'decisions[n]' avec strategy_entry dans calls."""
    args: dict = {"direction": direction, **extra_args}
    if exit_plan is not None:
        args["exit"] = exit_plan
    return {
        "symbol": "SPY",
        "confidence": 0.85,
        "rationale": "test",
        "decision_reason_code": "ENTRY_SIGNAL",
        "calls": [{"tool": "strategy_entry", "args": args}],
    }


def _strategy_close_data(**extra_args) -> dict:
    return {
        "symbol": "SPY",
        "confidence": 0.85,
        "rationale": "test",
        "decision_reason_code": "EXIT_SIGNAL",
        "calls": [{"tool": "strategy_close", "args": extra_args}],
    }


def test_parse_risk_pct_sets_target_no_qty() -> None:
    """risk_pct fourni sans qty → risk_pct_target set, qty=0.0 placeholder."""
    data = _symbol_call_data(
        risk_pct=0.005,
        exit_plan={"stop": {"type": "price", "price": 95.0}},
    )
    dec = _decision_from_symbol_calls(data, "SPY")
    assert isinstance(dec, Decision)
    assert dec.risk_pct_target == pytest.approx(0.005)
    assert dec.quantity == 0.0  # placeholder
    assert dec.action == "BUY"
    assert dec.intent == "OPEN_LONG"


def test_parse_qty_explicit_risk_pct_absent() -> None:
    """qty explicite → comportement inchangé, risk_pct_target = None."""
    data = _symbol_call_data(
        qty=20,
        exit_plan={"stop": {"type": "price", "price": 95.0}},
    )
    dec = _decision_from_symbol_calls(data, "SPY")
    assert dec.quantity == pytest.approx(20.0)
    assert dec.risk_pct_target is None


def test_parse_qty_explicit_wins_over_risk_pct() -> None:
    """qty explicite + risk_pct : qty gagne (Explicit Over Implicit)."""
    data = _symbol_call_data(
        qty=15,
        risk_pct=0.01,
        exit_plan={"stop": {"type": "price", "price": 95.0}},
    )
    dec = _decision_from_symbol_calls(data, "SPY")
    assert dec.quantity == pytest.approx(15.0)
    # risk_pct_target est None quand qty est fourni
    assert dec.risk_pct_target is None


def test_parse_no_qty_no_risk_pct_raises() -> None:
    """qty absent ET risk_pct absent → order_qty_required."""
    data = _symbol_call_data(exit_plan={"stop": {"type": "price", "price": 95.0}})
    with pytest.raises(ValueError, match="order_qty_required"):
        _decision_from_symbol_calls(data, "SPY")


def test_parse_risk_pct_hors_open_est_ignore_si_qty_presente() -> None:
    """REDUCE + qty + risk_pct : risk_pct est superflu, pas bloquant."""
    data = _strategy_close_data(qty=12, risk_pct=0.005)
    dec = _decision_from_symbol_calls(data, "SPY")

    assert dec.intent == "REDUCE"
    assert dec.quantity == pytest.approx(12.0)
    assert dec.resolve_from_position is True
    assert dec.risk_pct_target is None


def test_parse_close_side_risk_pct_sans_qty_ignore_champs_superflus() -> None:
    """CLOSE dérive side+qty depuis la position et ignore side/risk_pct."""
    data = _strategy_close_data(side="SELL", risk_pct=0.005)
    dec = _decision_from_symbol_calls(data, "SPY")

    assert dec.action == "HOLD"
    assert dec.intent == "CLOSE"
    assert dec.quantity == 0.0
    assert dec.resolve_from_position is True
    assert dec.risk_pct_target is None


# ---------------------------------------------------------------------------
# 4 : tests unitaires qty_from_risk_pct
# ---------------------------------------------------------------------------


def test_qty_from_risk_pct_basic() -> None:
    """Formule de base : qty = risk_pct * equity / stop_distance_usd (USD natif)."""
    # 0.5% de 100 000$ sur une distance de 5$ → 100 unités
    result = qty_from_risk_pct(0.005, 100_000.0, 5.0, fx_rate=1.0)
    assert result == pytest.approx(100.0)


def test_qty_from_risk_pct_with_fx() -> None:
    """Symbole non-USD : stop_distance_native × fx_rate → distance USD."""
    # equity 100 000 USD, risk_pct 1%, stop 10 TWD, fx 0.031
    # qty = 0.01 * 100_000 / (10 * 0.031) = 1000 / 0.31 ≈ 3225.8
    result = qty_from_risk_pct(0.01, 100_000.0, 10.0, fx_rate=0.031)
    assert result == pytest.approx(1000.0 / 0.31)


def test_qty_from_risk_pct_zero_stop_distance_returns_zero() -> None:
    assert qty_from_risk_pct(0.01, 100_000.0, 0.0) == 0.0


def test_qty_from_risk_pct_zero_equity_returns_zero() -> None:
    assert qty_from_risk_pct(0.01, 0.0, 5.0) == 0.0


def test_qty_from_risk_pct_zero_risk_pct_returns_zero() -> None:
    assert qty_from_risk_pct(0.0, 100_000.0, 5.0) == 0.0


def test_qty_from_risk_pct_negative_inputs_return_zero() -> None:
    assert qty_from_risk_pct(-0.01, 100_000.0, 5.0) == 0.0
    assert qty_from_risk_pct(0.01, -100_000.0, 5.0) == 0.0
    assert qty_from_risk_pct(0.01, 100_000.0, -5.0) == 0.0


def test_qty_from_risk_pct_nan_inputs_return_zero() -> None:
    assert qty_from_risk_pct(float("nan"), 100_000.0, 5.0) == 0.0
    assert qty_from_risk_pct(0.01, float("nan"), 5.0) == 0.0
    assert qty_from_risk_pct(0.01, 100_000.0, float("nan")) == 0.0


# ---------------------------------------------------------------------------
# Helpers d'intégration (pattern extrait de test_daemon_sizing.py)
# ---------------------------------------------------------------------------


def _write_runtime_config(
    root,
    *,
    max_risk_per_trade_pct: float = 0.01,
    max_position_value: float = 20_000,
    max_order_value: float = 10_000,
) -> None:
    (root / "config").mkdir(exist_ok=True)
    (root / "mandate").mkdir(exist_ok=True)
    (root / "config" / "universe.yaml").write_text(
        "starting_cash: 100000\nsymbols:\n  - SPY\n"
    )
    (root / "config" / "risk.yaml").write_text(
        "\n".join([
            f"max_position_value: {max_position_value}",
            "max_gross_exposure: 100000",
            f"max_order_value: {max_order_value}",
            f"max_risk_per_trade_pct: {max_risk_per_trade_pct}",
            "min_equity: 50000",
        ])
    )
    (root / "config" / "fx.yaml").write_text(
        "TWD:\n  yahoo: TWD=X\n  invert: true\n  fallback: 0.031\n"
        "EUR:\n  yahoo: EURUSD=X\n  invert: false\n  fallback: 1.08\n"
    )
    (root / "mandate" / "mandate.md").write_text("# Mandat\n")
    (root / "mandate" / "memory.md").write_text("# Memoire\n")


def _spy_bars(now: datetime):
    return [Bar(ts=now.isoformat(), open=100.0, high=101.0, low=99.0, close=100.0, volume=1000.0)]


# ---------------------------------------------------------------------------
# 5 : risk_pct sans hard_stop → rejeté risk_sizing_needs_stop
# ---------------------------------------------------------------------------


def test_daemon_risk_pct_no_stop_rejected(monkeypatch, tmp_path, make_data_source) -> None:
    """risk_pct fourni mais exit sans hard_stop → rejected risk_sizing_needs_stop."""
    _write_runtime_config(tmp_path)
    state_dir = tmp_path / "state"
    now = datetime(2026, 6, 5, 14, 30, tzinfo=timezone.utc)

    # Décision avec risk_pct_target mais PAS de hard_stop dans exit_plan
    decision = Decision(
        symbol="SPY",
        action="BUY",
        quantity=0.0,          # placeholder
        confidence=0.90,
        rationale="test risk_pct sans stop",
        intent="OPEN_LONG",
        exit_plan=None,        # pas de stop
        risk_pct_target=0.005,
    )

    def fake_batch_decide(**kwargs):
        return {sym: decision if sym == "SPY" else Decision.hold(sym, "hold") for sym in kwargs["decidable"]}, 1

    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    monkeypatch.setattr(planner_batch, "batch_decide", fake_batch_decide)

    report = daemon.run_cycle(
        dry_run=True,
        now=now,
        symbols_filter=["SPY"],
        sched=Scheduler(state_dir / "scheduler.json"),
        data_source=make_data_source(lambda sym, lb, iv: _spy_bars(now)),
    )

    decisions = report["decisions"]
    spy_dec = next((d for d in decisions if d["symbol"] == "SPY"), None)
    assert spy_dec is not None
    assert spy_dec.get("executed") is False
    assert "risk_sizing_needs_stop" in (spy_dec.get("reason") or "")
    assert spy_dec.get("risk_pct_target") == pytest.approx(0.005)


# ---------------------------------------------------------------------------
# 6 : risk_pct + hard_stop → qty dérivée, ordre exécuté
# ---------------------------------------------------------------------------


def test_daemon_risk_pct_derives_qty_and_executes(monkeypatch, tmp_path, make_data_source) -> None:
    """risk_pct=0.005 + stop=95 → qty=10 (0.5% de 100k / 5$ stop) → exécuté.

    SPY price=100, stop=95, distance=5, equity≈100k, risk_pct=0.5%
    qty = 0.005 * 100_000 / 5 = 100 unités
    max_order_value=10_000 → 100*100=10_000 (juste dans la borne)
    max_risk_per_trade_pct=0.01 → autorise jusqu'à 200 unités → 100 passe
    """
    _write_runtime_config(tmp_path, max_risk_per_trade_pct=0.01)
    state_dir = tmp_path / "state"
    now = datetime(2026, 6, 5, 14, 30, tzinfo=timezone.utc)

    # risk_pct=0.005, stop=95 : qty attendue = 0.005 * equity / 5
    decision = Decision(
        symbol="SPY",
        action="BUY",
        quantity=0.0,       # placeholder
        confidence=0.90,
        rationale="test risk sizing",
        intent="OPEN_LONG",
        exit_plan={"hard_stop": {"type": "price", "price": 95.0}},
        risk_pct_target=0.005,
    )

    def fake_batch_decide(**kwargs):
        return {sym: decision if sym == "SPY" else Decision.hold(sym, "hold") for sym in kwargs["decidable"]}, 1

    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    monkeypatch.setattr(planner_batch, "batch_decide", fake_batch_decide)

    report = daemon.run_cycle(
        dry_run=True,
        now=now,
        symbols_filter=["SPY"],
        sched=Scheduler(state_dir / "scheduler.json"),
        data_source=make_data_source(lambda sym, lb, iv: _spy_bars(now)),
    )

    decisions = report["decisions"]
    spy_dec = next((d for d in decisions if d["symbol"] == "SPY"), None)
    assert spy_dec is not None, f"pas de décision SPY : {decisions}"

    # Avec dry_run=True, executed=False même si tous les gates passent.
    # La preuve de succès = reason=="ok" (aucun gate n'a rejeté).
    reason = spy_dec.get("reason")
    assert reason == "ok", f"gates non passés : reason={reason!r}, entry={spy_dec}"

    # La qty dérivée est ~100 (0.005 * 100k / 5)
    recorded_qty = spy_dec.get("qty")
    assert recorded_qty is not None
    assert recorded_qty == pytest.approx(100.0, rel=0.05), (
        f"qty attendue ≈ 100, obtenu {recorded_qty}"
    )

    # Le marker risk_qty_derived doit être présent
    assert spy_dec.get("risk_qty_derived") is True


def test_daemon_risk_pct_exceeds_max_risk_traced_without_blocking(monkeypatch, tmp_path, make_data_source) -> None:
    """risk_pct > max_risk_per_trade_pct → qty dérivée + warning, pas rejet risk.

    risk_pct=0.02 (2%), max_risk_per_trade_pct=0.01 (1%).
    qty dérivée = 0.02 * 100k / 5 = 400 > max_risk_qty = 0.01 * 100k / 5 = 200.
    → warning risk_per_trade_exceeded, les autres fusibles restent responsables.
    """
    _write_runtime_config(
        tmp_path,
        max_risk_per_trade_pct=0.01,
        max_position_value=100_000,
        max_order_value=100_000,
    )
    state_dir = tmp_path / "state"
    now = datetime(2026, 6, 5, 14, 30, tzinfo=timezone.utc)

    decision = Decision(
        symbol="SPY",
        action="BUY",
        quantity=0.0,
        confidence=0.90,
        rationale="test risk trop grand",
        intent="OPEN_LONG",
        exit_plan={"hard_stop": {"type": "price", "price": 95.0}},
        risk_pct_target=0.02,  # 2% > max 1%
    )

    def fake_batch_decide(**kwargs):
        return {sym: decision if sym == "SPY" else Decision.hold(sym, "hold") for sym in kwargs["decidable"]}, 1

    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    monkeypatch.setattr(planner_batch, "batch_decide", fake_batch_decide)

    report = daemon.run_cycle(
        dry_run=True,
        now=now,
        symbols_filter=["SPY"],
        sched=Scheduler(state_dir / "scheduler.json"),
        data_source=make_data_source(lambda sym, lb, iv: _spy_bars(now)),
    )

    decisions = report["decisions"]
    spy_dec = next((d for d in decisions if d["symbol"] == "SPY"), None)
    assert spy_dec is not None
    # Avec dry_run=True, executed=False même si tous les gates passent.
    assert spy_dec.get("executed") is False
    assert spy_dec.get("reason") == "ok"
    assert spy_dec.get("qty") == pytest.approx(400.0)
    assert spy_dec["risk_warnings"][0]["code"] == "risk_per_trade_exceeded"
