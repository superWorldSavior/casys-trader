import json
import logging
from datetime import datetime, timedelta, timezone

from trader.runtime import daemon
from trader.agent.client import Decision
from trader.execution.broker import IbkrCommissionModel
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
            llm_provider="acpx",
            llm_model="gpt-5.5/medium",
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
    caplog.set_level(logging.DEBUG, logger="casys-trader")

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

    info_messages = "\n".join(
        record.getMessage() for record in caplog.records if record.levelno >= logging.INFO
    )
    all_messages = "\n".join(record.getMessage() for record in caplog.records)
    # Progression visible à l'INFO en prod
    assert "[cycle] start" in info_messages
    assert "[market] loaded" in info_messages
    assert "[batch] deciding" in info_messages
    assert "[cycle] completed" in info_messages
    # Trades-only à l'INFO : le détail d'une décision HOLD est en DEBUG, pas à
    # l'INFO (sinon 31 HOLD/cycle noient la console). Toujours loggé, mais DEBUG.
    assert "[decision 1/1] SPY result" not in info_messages
    assert "[decision 1/1] SPY result" in all_messages


def test_run_cycle_historise_la_perf_par_modele_sur_fill(monkeypatch, tmp_path, patch_batch, make_data_source) -> None:
    _write_runtime_config(tmp_path)
    state_dir = tmp_path / "state"
    # 14:30 UTC = 10:30 ET — session US régulière ouverte (13:30–20:00 UTC en EDT)
    now = datetime(2026, 6, 5, 14, 30, tzinfo=timezone.utc)

    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    data_source = make_data_source(lambda symbol, lookback, interval: [
        Bar(ts=now.isoformat(), open=100.0, high=101.0, low=99.0, close=100.0, volume=1000.0)
    ])
    patch_batch(lambda **kwargs: Decision(
            symbol=kwargs["symbol"],
            action="BUY",
            quantity=10.0,
            confidence=0.95,
            rationale="test model perf",
            intent="OPEN_LONG",
            exit_plan={"hard_stop": {"type": "price", "price": 95.0}},
            llm_provider="ollama-cloud",
            llm_model="nemotron-3-nano:30b-cloud",
            llm_fallback_reason="acpx:quota_exceeded"),
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
    assert rows[0]["llm_fallback_reason"] == "acpx:quota_exceeded"
    assert rows[0]["equity"] == 100000.0


def test_run_cycle_loggue_les_commissions_du_fill(monkeypatch, tmp_path, patch_batch, make_data_source) -> None:
    _write_runtime_config(tmp_path)
    state_dir = tmp_path / "state"
    # 14:30 UTC = 10:30 ET — session US régulière ouverte (13:30–20:00 UTC en EDT)
    now = datetime(2026, 6, 5, 14, 30, tzinfo=timezone.utc)

    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    data_source = make_data_source(lambda symbol, lookback, interval: [
        Bar(ts=now.isoformat(), open=100.0, high=101.0, low=99.0, close=100.0, volume=1000.0)
    ])
    patch_batch(lambda **kwargs: Decision(
            symbol=kwargs["symbol"],
            action="BUY",
            quantity=10.0,
            confidence=0.95,
            rationale="test frais",
            intent="OPEN_LONG",
            exit_plan={"hard_stop": {"type": "price", "price": 95.0}},
            llm_provider="acpx",
            llm_model="gpt-5.5/medium"),
    )

    daemon.run_cycle(
        dry_run=False,
        now=now,
        symbols_filter=["SPY"],
        sched=Scheduler(state_dir / "scheduler.json"),
        data_source=data_source,
        commission_model=IbkrCommissionModel(),
    )

    rows = [
        json.loads(line)
        for line in (state_dir / "model_performance.jsonl").read_text().splitlines()
    ]
    assert rows[0]["commission"] == 0.35
    assert rows[0]["commission_currency"] == "USD"
    assert rows[0]["commission_model"] == "ibkr_us_stock_tiered"
    assert rows[0]["equity"] == 99_999.65


def test_run_cycle_report_portefeuille_expose_le_pnl_latent_net_avec_commissions(
    monkeypatch,
    tmp_path,
    patch_batch,
    make_data_source,
) -> None:
    _write_runtime_config(tmp_path)
    state_dir = tmp_path / "state"
    # 14:30 UTC = 10:30 ET — session US régulière ouverte (13:30–20:00 UTC en EDT)
    now = datetime(2026, 6, 5, 14, 30, tzinfo=timezone.utc)

    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    data_source = make_data_source(lambda symbol, lookback, interval: [
        Bar(ts=now.isoformat(), open=100.0, high=101.0, low=99.0, close=100.0, volume=1000.0)
    ])
    patch_batch(lambda **kwargs: Decision(
            symbol=kwargs["symbol"],
            action="BUY",
            quantity=10.0,
            confidence=0.95,
            rationale="test latent net",
            intent="OPEN_LONG",
            exit_plan={"hard_stop": {"type": "price", "price": 95.0}},
            llm_provider="acpx",
            llm_model="gpt-5.5/medium"),
    )
    sched = Scheduler(state_dir / "scheduler.json")
    sched.set_symbol_next_wake("SPY", now.isoformat())

    report = daemon.run_cycle(
        dry_run=False,
        now=now,
        symbols_filter=["SPY"],
        sched=sched,
        data_source=data_source,
        commission_model=IbkrCommissionModel(),
    )

    holding = report["portfolio"]["holdings"][0]
    current_report = json.loads((state_dir / "current_report.json").read_text())
    persisted_holding = current_report["portfolio"]["holdings"][0]
    assert holding["unrealized_pnl"] == 0.0
    assert holding["round_trip_fee"] == 0.7
    assert holding["unrealized_pnl_net"] == -0.7
    assert persisted_holding == holding


def test_run_cycle_bloque_decision_sur_donnees_marche_perimees(monkeypatch, tmp_path, patch_batch, make_data_source) -> None:
    _write_runtime_config(tmp_path)
    state_dir = tmp_path / "state"
    now = datetime(2026, 6, 5, 16, 0, tzinfo=timezone.utc)
    stale_ts = (now - timedelta(hours=2)).isoformat()
    # Daily AUSSI périmé (séance ancienne) : sinon un daily du jour serait jugé frais
    # par séance (§13.4) et le symbole deviendrait analysable au lieu de HOLD stale.
    old_daily_ts = (now - timedelta(days=10)).date().isoformat()
    codex_calls = 0

    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    data_source = make_data_source(lambda symbol, lookback, interval: [
        Bar(
            ts=(old_daily_ts if interval == "1d" else stale_ts),
            open=100.0, high=101.0, low=99.0, close=100.0, volume=1000.0,
        )
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
    assert len(report["decisions"]) == 1
    d = report["decisions"][0]
    # Vérifie les champs métier essentiels (la clé 'news' est ajoutée par Fix B
    # sur TOUS les chemins via record_decision — ne pas l'exclure de l'assertion).
    assert d["symbol"] == "SPY"
    assert d["action"] == "HOLD"
    assert d["qty"] == 0.0
    assert d["confidence"] == 0.0
    assert d["rationale"] == "stale_market_data"
    assert d["next_wake_in_minutes"] == 30.0
    assert d["intent"] == "HOLD"
    assert d["trade_plan_created"] is False
    assert d["executed"] is False
    assert d["reason"] == "stale_market_data"
    assert d["decision_reason_code"] == "DATA_STALE"
    assert d["decision_source"] == "infra"
    assert d["model_called"] is False
    assert d["stale_streak"] == 1
    assert d["data_source"] is None
    assert d["last_bar_ts"] == stale_ts
    assert d["stale_reason"] == "too_old"
    assert d["data_age_minutes"] == 120.0
    assert "news" in d  # Fix B : news présent sur tous les chemins
    assert report["stale_market_data"]["SPY"] == {
        "last_bar_ts": stale_ts,
        "stale_reason": "too_old",
        "data_age_minutes": 120.0,
    }
    assert json.loads((state_dir / "broker.json").read_text())["fills"] == []


def test_run_cycle_garde_tradable_une_barre_horaire_de_59_minutes(
    monkeypatch,
    tmp_path,
    patch_batch,
    make_data_source,
) -> None:
    _write_runtime_config(tmp_path)
    state_dir = tmp_path / "state"
    now = datetime(2026, 6, 5, 8, 59, tzinfo=timezone.utc)
    codex_calls = 0

    def bars(symbol: str, lookback: str, interval: str) -> list[Bar]:
        return [
            Bar(
                ts=(now - timedelta(minutes=179)).isoformat(),
                open=100.0,
                high=101.0,
                low=99.0,
                close=100.0,
                volume=1000.0,
            ),
            Bar(
                ts=(now - timedelta(minutes=119)).isoformat(),
                open=101.0,
                high=102.0,
                low=100.0,
                close=101.0,
                volume=1000.0,
            ),
            Bar(
                ts=(now - timedelta(minutes=59)).isoformat(),
                open=102.0,
                high=103.0,
                low=101.0,
                close=102.0,
                volume=1000.0,
            ),
        ]

    def decide(**kwargs) -> Decision:
        nonlocal codex_calls
        codex_calls += 1
        return Decision.hold(kwargs["symbol"], "attente")

    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    patch_batch(decide)
    data_source = make_data_source(bars)

    report = daemon.run_cycle(
        dry_run=True,
        now=now,
        symbols_filter=["SPY"],
        sched=Scheduler(state_dir / "scheduler.json"),
        data_source=data_source,
        runtime_interval="1h",
    )

    assert codex_calls == 1
    assert report["stale_market_data"] == {}
    assert [decision["symbol"] for decision in report["decisions"]] == ["SPY"]
    assert report["decisions"][0]["rationale"] == "attente"


# ---------------------------------------------------------------------------
# Identité daemon : pid + started_at dans daemon_status.json
# ---------------------------------------------------------------------------


def test_run_cycle_status_contient_pid(monkeypatch, tmp_path, patch_batch, make_data_source) -> None:
    """_write_status inclut le champ 'pid' (os.getpid()) dans chaque battement."""
    import os
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
    assert "pid" in status
    assert status["pid"] == os.getpid()


def _fast_main_patches(monkeypatch, tmp_path, state_dir):
    """Patches communs pour exécuter main() < 1 s.

    - connect_ib → MarketError immédiate (pas de retries réseau IB)
    - time.sleep → no-op (pas d'attente entre cycles)
    - ROOT/STATE_DIR → tmp_path

    Avec --once et MarketError sur connect_ib, la boucle fait 1 itération :
    try → connect_ib → MarketError → except → _write_status → break → finally.
    """
    from trader.tools import market

    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    monkeypatch.setattr(daemon, "connect_ib", lambda *a, **kw: (_ for _ in ()).throw(
        market.MarketError("ib_unavailable", "stub test")
    ))
    monkeypatch.setattr("trader.runtime.daemon.time.sleep", lambda _: None)


def test_main_ecrit_pid_file_au_demarrage(monkeypatch, tmp_path) -> None:
    """main() écrit state/daemon.pid avec son os.getpid() avant le premier _write_status.

    Rapide : connect_ib lève MarketError immédiatement, time.sleep=no-op.
    """
    import os
    _write_runtime_config(tmp_path)
    state_dir = tmp_path / "state"
    _fast_main_patches(monkeypatch, tmp_path, state_dir)

    pid_written_before_first_status: list[str] = []
    original_write_status = daemon._write_status

    def patched_write_status(phase, **kwargs):
        pid_path = state_dir / "daemon.pid"
        if pid_path.exists():
            pid_written_before_first_status.append(
                pid_path.read_text(encoding="utf-8").strip()
            )
        # Laisser _write_status s'exécuter normalement (pas de KeyboardInterrupt ici),
        # puis stopper la boucle avec KeyboardInterrupt après la première écriture.
        original_write_status(phase, **kwargs)
        raise KeyboardInterrupt("stop after first write_status")

    monkeypatch.setattr(daemon, "_write_status", patched_write_status)

    import pytest
    with pytest.raises((KeyboardInterrupt, SystemExit)):
        daemon.main(["--once"])

    assert len(pid_written_before_first_status) >= 1, "daemon.pid doit exister avant le premier _write_status"
    assert pid_written_before_first_status[0] == str(os.getpid())


def test_main_supprime_pid_file_au_shutdown_propre(monkeypatch, tmp_path) -> None:
    """main() supprime state/daemon.pid dans le finally (shutdown propre).

    Rapide : connect_ib lève MarketError → --once → break → finally → pid supprimé.
    """
    _write_runtime_config(tmp_path)
    state_dir = tmp_path / "state"
    _fast_main_patches(monkeypatch, tmp_path, state_dir)

    # Pas de patch de _write_status — on laisse la boucle --once se terminer
    # naturellement via MarketError + stop_after_iteration = True.
    daemon.main(["--once"])

    pid_path = state_dir / "daemon.pid"
    assert not pid_path.exists(), "daemon.pid doit être supprimé au shutdown"

    pid_path = state_dir / "daemon.pid"
    assert not pid_path.exists(), "daemon.pid doit être supprimé au shutdown"
