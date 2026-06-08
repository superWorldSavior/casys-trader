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
        "starting_cash: 100000\nsymbols:\n  - SPY\n  - QQQ\n"
    )
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


def _bars(symbol: str, lookback: str, interval: str) -> list[Bar]:
    base = 100.0 if symbol == "SPY" else 200.0
    return [
        Bar(ts="2026-06-05T11:30:00+00:00", open=base, high=base + 1, low=base - 1, close=base, volume=1000.0),
        Bar(ts="2026-06-05T11:45:00+00:00", open=base + 1, high=base + 2, low=base, close=base + 2, volume=1000.0),
        Bar(ts="2026-06-05T11:55:00+00:00", open=base + 2, high=base + 3, low=base + 1, close=base + 4, volume=1000.0),
    ]


def test_run_cycle_ecrit_le_learning_emis_par_lagent(monkeypatch, tmp_path, patch_batch, make_data_source) -> None:
    _write_runtime_config(tmp_path)
    state_dir = tmp_path / "state"
    sched = Scheduler(state_dir / "scheduler.json")
    now = datetime(2026, 6, 5, 12, 0, tzinfo=timezone.utc)

    def decide(**kwargs):
        return Decision(
            symbol=kwargs["symbol"],
            action="HOLD",
            quantity=0.0,
            confidence=0.5,
            rationale="range",
            intent="HOLD",
            learning="le range SPY tient depuis 3 reveils",
        )

    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    data_source = make_data_source(_bars)
    patch_batch(decide)

    daemon.run_cycle(dry_run=True, now=now, symbols_filter=["SPY"], sched=sched, data_source=data_source)

    recent = LearningsStore(state_dir / "learnings.jsonl").recent()
    assert [item["note"] for item in recent] == ["le range SPY tient depuis 3 reveils"]
    assert recent[0]["symbol"] == "SPY"
    # Le learning porte le résultat de la décision (pour juger les bons choix).
    assert recent[0]["reason"] == "hold"
    assert recent[0]["executed"] is False


def test_run_cycle_injecte_lattribution_dans_le_contexte(monkeypatch, tmp_path, patch_batch, make_data_source) -> None:
    import json as _json

    _write_runtime_config(tmp_path)
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    sched = Scheduler(state_dir / "scheduler.json")
    now = datetime(2026, 6, 5, 12, 0, tzinfo=timezone.utc)

    # Un round-trip clôturé déjà dans l'historique de performance.
    with (state_dir / "model_performance.jsonl").open("w", encoding="utf-8") as f:
        f.write(_json.dumps({"ts": "2026-06-05T10:00:00+00:00", "symbol": "SPY", "action": "BUY",
                             "quantity": 10, "price": 100.0, "confidence": 0.9, "intent": "OPEN_LONG"}) + "\n")
        f.write(_json.dumps({"ts": "2026-06-05T11:00:00+00:00", "symbol": "SPY", "action": "SELL",
                             "quantity": 10, "price": 110.0, "confidence": None, "intent": "PLANNED_EXIT",
                             "exit_reason": "take_profit"}) + "\n")

    contexts: list[dict] = []

    def decide(**kwargs):
        contexts.append(kwargs["context"])
        return Decision.hold(kwargs["symbol"], "attente")

    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    data_source = make_data_source(_bars)
    patch_batch(decide)

    daemon.run_cycle(dry_run=True, now=now, symbols_filter=["SPY"], sched=sched, data_source=data_source)

    assert contexts
    attribution = contexts[0]["attribution"]
    assert attribution["n_closed_trades"] == 1
    assert attribution["realized_pnl"] == 100.0


def test_run_cycle_reinjecte_les_learnings_recents_dans_le_contexte(
    monkeypatch,
    tmp_path,
    patch_batch,
    make_data_source,
) -> None:
    _write_runtime_config(tmp_path)
    state_dir = tmp_path / "state"
    sched = Scheduler(state_dir / "scheduler.json")
    now = datetime(2026, 6, 5, 12, 0, tzinfo=timezone.utc)

    LearningsStore(state_dir / "learnings.jsonl").append(
        symbol="SPY", note="cassure ratee au dernier reveil", now=now
    )

    contexts: list[dict] = []

    def decide(**kwargs):
        contexts.append(kwargs["context"])
        return Decision.hold(kwargs["symbol"], "attente")

    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    data_source = make_data_source(_bars)
    patch_batch(decide)

    daemon.run_cycle(dry_run=True, now=now, symbols_filter=["SPY"], sched=sched, data_source=data_source)

    assert contexts
    learnings = contexts[0]["learnings"]
    assert [item["note"] for item in learnings] == ["cassure ratee au dernier reveil"]
