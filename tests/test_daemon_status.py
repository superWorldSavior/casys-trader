import json
import logging
from datetime import datetime, timedelta, timezone

from trader import daemon
from trader.codex_client import Decision
from trader.tools.market import Bar
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


def test_run_cycle_ecrit_un_statut_et_un_rapport_courant(monkeypatch, tmp_path, patch_batch, make_data_source) -> None:
    _write_runtime_config(tmp_path)
    state_dir = tmp_path / "state"
    now = datetime(2026, 6, 5, 12, 0, tzinfo=timezone.utc)

    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    data_source = make_data_source(lambda symbol, lookback, interval: [
        Bar(ts=now.isoformat(), open=100.0, high=101.0, low=99.0, close=100.0, volume=1000.0)
    ])
    patch_batch(lambda **kwargs: Decision.hold(kwargs["symbol"], "attente"))

    daemon.run_cycle(
        dry_run=True,
        now=now,
        symbols_filter=["SPY"],
        sched=Scheduler(state_dir / "scheduler.json"),
        data_source=data_source,
    )

    status = json.loads((state_dir / "daemon_status.json").read_text())
    current_report = json.loads((state_dir / "current_report.json").read_text())
    events = (state_dir / "events.jsonl").read_text().splitlines()

    assert status["phase"] == "cycle_completed"
    assert status["current_symbol"] is None
    assert status["decisions_done"] == 1
    assert status["symbols_total"] == 1
    assert status["last_decision"]["symbol"] == "SPY"
    assert [d["symbol"] for d in current_report["decisions"]] == ["SPY"]
    assert any(json.loads(line)["event"] == "decision_recorded" for line in events)


def test_run_cycle_ecrit_la_decision_dans_le_ledger(monkeypatch, tmp_path, patch_batch, make_data_source) -> None:
    _write_runtime_config(tmp_path)
    state_dir = tmp_path / "state"
    now = datetime(2026, 6, 5, 12, 0, tzinfo=timezone.utc)

    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    data_source = make_data_source(lambda symbol, lookback, interval: [
        Bar(ts=now.isoformat(), open=100.0, high=101.0, low=99.0, close=100.0, volume=1000.0)
    ])
    patch_batch(
        lambda **kwargs: Decision(
            symbol=kwargs["symbol"],
            action="HOLD",
            quantity=0.0,
            confidence=0.7,
            rationale="range sans catalyseur",
            intent="HOLD",
            llm_provider="spark",
            llm_model="gpt-5.3-codex-spark/medium",
        )
    )

    daemon.run_cycle(
        dry_run=True,
        now=now,
        symbols_filter=["SPY"],
        sched=Scheduler(state_dir / "scheduler.json"),
        data_source=data_source,
    )

    rows = [
        json.loads(line)
        for line in (state_dir / "decisions.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert len(rows) == 1
    assert rows[0]["decision_id"] == "2026-06-05T12:00:00+00:00|0|SPY"
    assert rows[0]["action"] == "HOLD"
    assert rows[0]["confidence"] == 0.7
    assert rows[0]["price"] == 100.0
    assert rows[0]["decision"]["rationale"] == "range sans catalyseur"
    assert rows[0]["labels"] == {}


def test_run_cycle_loggue_la_progression_console(monkeypatch, tmp_path, caplog, patch_batch, make_data_source) -> None:
    _write_runtime_config(tmp_path)
    state_dir = tmp_path / "state"
    now = datetime(2026, 6, 5, 12, 0, tzinfo=timezone.utc)
    caplog.set_level(logging.INFO, logger="casys-trader")

    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    data_source = make_data_source(lambda symbol, lookback, interval: [
        Bar(ts=now.isoformat(), open=100.0, high=101.0, low=99.0, close=100.0, volume=1000.0)
    ])
    patch_batch(lambda **kwargs: Decision.hold(kwargs["symbol"], "attente"))

    daemon.run_cycle(
        dry_run=True,
        now=now,
        symbols_filter=["SPY"],
        sched=Scheduler(state_dir / "scheduler.json"),
        data_source=data_source,
    )

    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert "[cycle] start" in messages
    assert "[market] loaded" in messages
    assert "[batch] deciding" in messages
    assert "[decision 1/1] SPY result" in messages
    assert "[cycle] completed" in messages


def test_run_cycle_historise_la_perf_par_modele_sur_fill(monkeypatch, tmp_path, patch_batch, make_data_source) -> None:
    _write_runtime_config(tmp_path)
    state_dir = tmp_path / "state"
    now = datetime(2026, 6, 5, 12, 0, tzinfo=timezone.utc)

    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    data_source = make_data_source(lambda symbol, lookback, interval: [
        Bar(ts=now.isoformat(), open=100.0, high=101.0, low=99.0, close=100.0, volume=1000.0)
    ])
    patch_batch(lambda **kwargs: Decision(
            symbol=kwargs["symbol"],
            action="BUY",
            quantity=10.0,
            confidence=0.72,
            rationale="test model perf",
            intent="OPEN_LONG",
            llm_provider="ollama-cloud",
            llm_model="nemotron-3-nano:30b-cloud",
            llm_fallback_reason="spark:quota_exceeded"),
    )

    daemon.run_cycle(
        dry_run=False,
        now=now,
        symbols_filter=["SPY"],
        sched=Scheduler(state_dir / "scheduler.json"),
        data_source=data_source,
    )

    rows = [
        json.loads(line)
        for line in (state_dir / "model_performance.jsonl").read_text().splitlines()
    ]
    assert len(rows) == 1
    assert rows[0]["symbol"] == "SPY"
    assert rows[0]["action"] == "BUY"
    assert rows[0]["quantity"] == 10.0
    assert rows[0]["llm_provider"] == "ollama-cloud"
    assert rows[0]["llm_model"] == "nemotron-3-nano:30b-cloud"
    assert rows[0]["llm_fallback_reason"] == "spark:quota_exceeded"
    assert rows[0]["equity"] == 100000.0


def test_run_cycle_bloque_decision_sur_donnees_marche_perimees(monkeypatch, tmp_path, patch_batch, make_data_source) -> None:
    _write_runtime_config(tmp_path)
    state_dir = tmp_path / "state"
    now = datetime(2026, 6, 5, 16, 0, tzinfo=timezone.utc)
    stale_ts = (now - timedelta(hours=2)).isoformat()
    codex_calls = 0

    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    data_source = make_data_source(lambda symbol, lookback, interval: [
        Bar(ts=stale_ts, open=100.0, high=101.0, low=99.0, close=100.0, volume=1000.0)
    ])

    def decide(**kwargs) -> Decision:
        nonlocal codex_calls
        codex_calls += 1
        return Decision(
            symbol=kwargs["symbol"],
            action="BUY",
            quantity=10.0,
            confidence=0.8,
            rationale="ne doit pas être appelé",
            intent="OPEN_LONG",
        )

    patch_batch(decide)

    report = daemon.run_cycle(
        dry_run=False,
        now=now,
        symbols_filter=["SPY"],
        sched=Scheduler(state_dir / "scheduler.json"),
        data_source=data_source,
    )

    assert codex_calls == 0
    assert report["decisions"] == [
        {
            "symbol": "SPY",
            "action": "HOLD",
            "qty": 0.0,
            "confidence": 0.0,
            "rationale": "stale_market_data",
            "next_wake_in_minutes": 30.0,
            "intent": "HOLD",
            "trade_plan_created": False,
            "executed": False,
            "reason": "stale_market_data",
            "last_bar_ts": stale_ts,
            "stale_reason": "too_old",
            "data_age_minutes": 120.0,
        }
    ]
    assert report["stale_market_data"]["SPY"] == {
        "last_bar_ts": stale_ts,
        "stale_reason": "too_old",
        "data_age_minutes": 120.0,
    }
    assert json.loads((state_dir / "broker.json").read_text())["fills"] == []
