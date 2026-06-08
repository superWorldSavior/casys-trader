import json
from datetime import datetime, timedelta, timezone

import pytest

from trader import daemon
from trader.codex_client import Decision
from trader.tools.execution import Order, SimBroker
from trader.tools.market import Bar
from trader.tools.scheduler import Scheduler
from trader.trade_plan import TradePlanStore, create_trade_plan


def _write_runtime_config(
    root,
    *,
    symbols: list[str] | None = None,
    max_position_value: float = 20_000,
    max_gross_exposure: float = 100_000,
    max_order_value: float = 10_000,
) -> None:
    (root / "config").mkdir()
    (root / "mandate").mkdir()
    symbols = symbols or ["SPY"]
    (root / "config" / "universe.yaml").write_text(
        "starting_cash: 100000\nsymbols:\n" + "".join(f"  - {symbol}\n" for symbol in symbols)
    )
    (root / "config" / "risk.yaml").write_text(
        "\n".join(
            [
                f"max_position_value: {max_position_value}",
                f"max_gross_exposure: {max_gross_exposure}",
                f"max_order_value: {max_order_value}",
                "max_orders_per_cycle: 5",
                "min_equity: 50000",
            ]
        )
    )
    (root / "mandate" / "mandate.md").write_text("# Mandat\n")
    (root / "mandate" / "memory.md").write_text("# Memoire\n")


def test_run_cycle_execute_les_sorties_planifiees_avant_codex(monkeypatch, tmp_path, patch_batch, make_data_source) -> None:
    _write_runtime_config(tmp_path)
    state_dir = tmp_path / "state"
    now = datetime(2026, 6, 5, 12, 10, tzinfo=timezone.utc)
    broker = SimBroker(state_dir / "broker.json", starting_cash=100_000)
    broker.submit(Order("SPY", "BUY", 10.0), 100.0, "2026-06-05T12:00:00+00:00", dry_run=False)
    TradePlanStore(state_dir / "trade_plans.json").upsert(
        create_trade_plan(
            symbol="SPY",
            side="LONG",
            quantity=10.0,
            entry_price=100.0,
            opened_at="2026-06-05T12:00:00+00:00",
            raw_exit_plan={"take_profits": [{"name": "tp1", "price": 105.0, "fraction": 0.5}]},
        )
    )
    codex_calls = 0

    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    data_source = make_data_source(lambda symbol, lookback, interval: [
        Bar(ts=now.isoformat(), open=106.0, high=107.0, low=105.0, close=106.0, volume=1000.0)
    ])

    def decide(**kwargs) -> Decision:
        nonlocal codex_calls
        codex_calls += 1
        return Decision.hold(kwargs["symbol"], "attente")

    patch_batch(decide)

    report = daemon.run_cycle(
        dry_run=False,
        now=now,
        symbols_filter=["SPY"],
        sched=Scheduler(state_dir / "scheduler.json"),
        data_source=data_source,
    )

    assert report["planned_exits"][0]["reason"] == "take_profit:tp1"
    assert report["planned_exits"][0]["executed"] is True
    assert codex_calls == 1
    assert SimBroker(state_dir / "broker.json").positions()["SPY"].quantity == 5.0


def test_run_cycle_clamp_les_sorties_planifiees_sur_position_broker(monkeypatch, tmp_path, patch_batch, make_data_source) -> None:
    _write_runtime_config(tmp_path)
    state_dir = tmp_path / "state"
    now = datetime(2026, 6, 5, 12, 10, tzinfo=timezone.utc)
    broker = SimBroker(state_dir / "broker.json", starting_cash=100_000)
    broker.submit(Order("SPY", "BUY", 5.0), 100.0, "2026-06-05T12:00:00+00:00", dry_run=False)
    TradePlanStore(state_dir / "trade_plans.json").upsert(
        create_trade_plan(
            symbol="SPY",
            side="LONG",
            quantity=10.0,
            entry_price=100.0,
            opened_at="2026-06-05T12:00:00+00:00",
            raw_exit_plan={"take_profits": [{"name": "tp1", "price": 105.0, "fraction": 1.0}]},
        )
    )

    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    data_source = make_data_source(lambda symbol, lookback, interval: [
        Bar(ts=now.isoformat(), open=106.0, high=107.0, low=105.0, close=106.0, volume=1000.0)
    ])
    patch_batch(lambda **kwargs: Decision.hold(kwargs["symbol"], "attente"))

    report = daemon.run_cycle(
        dry_run=False,
        now=now,
        symbols_filter=["SPY"],
        sched=Scheduler(state_dir / "scheduler.json"),
        data_source=data_source,
    )

    assert report["planned_exits"][0]["reason"] == "take_profit:tp1"
    assert report["planned_exits"][0]["requested_quantity"] == 10.0
    assert report["planned_exits"][0]["quantity"] == 5.0
    assert SimBroker(state_dir / "broker.json").positions() == {}
    assert TradePlanStore(state_dir / "trade_plans.json").open_plans() == []


def test_run_cycle_exit_watch_reveille_agent_sans_sortie_auto(monkeypatch, tmp_path, patch_batch, make_data_source) -> None:
    _write_runtime_config(tmp_path)
    state_dir = tmp_path / "state"
    now = datetime(2026, 6, 5, 12, 20, tzinfo=timezone.utc)
    broker = SimBroker(state_dir / "broker.json", starting_cash=100_000)
    broker.submit(Order("SPY", "BUY", 10.0), 100.0, "2026-06-05T12:00:00+00:00", dry_run=False)
    TradePlanStore(state_dir / "trade_plans.json").upsert(
        create_trade_plan(
            symbol="SPY",
            side="LONG",
            quantity=10.0,
            entry_price=100.0,
            opened_at="2026-06-05T12:00:00+00:00",
            raw_exit_plan={
                "hard_stop": 95.0,
                "exit_watch": {
                    "ttl_minutes": 90,
                    "cooldown_minutes": 15,
                    "logic": "any",
                    "conditions": [
                        {"indicator": "return", "op": "<", "value": -0.01, "timeframe": "1h", "window": 2},
                    ],
                },
            },
        )
    )
    contexts: list[dict] = []

    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    data_source = make_data_source(lambda symbol, lookback, interval: [
        Bar(ts="2026-06-05T11:00:00+00:00", open=100.0, high=101.0, low=99.0, close=100.0, volume=1000.0),
        Bar(ts=now.isoformat(), open=98.0, high=99.0, low=97.0, close=98.0, volume=1000.0),
    ])

    def decide(**kwargs) -> Decision:
        contexts.append(kwargs["context"])
        return Decision.hold(kwargs["symbol"], "réveil sortie seulement")

    patch_batch(decide)

    report = daemon.run_cycle(
        dry_run=False,
        now=now,
        symbols_filter=[],
        sched=Scheduler(state_dir / "scheduler.json"),
        data_source=data_source,
    )

    assert [decision["symbol"] for decision in report["decisions"]] == ["SPY"]
    assert report["exit_watch_triggers"][0]["symbol"] == "SPY"
    assert report["exit_watch_triggers"][0]["source"] == "exit_watch"
    assert contexts[0]["indicator_triggers"][0]["source"] == "exit_watch"
    assert SimBroker(state_dir / "broker.json").positions()["SPY"].quantity == 10.0


def test_run_cycle_persiste_un_plan_apres_ouverture(monkeypatch, tmp_path, patch_batch, make_data_source) -> None:
    _write_runtime_config(tmp_path)
    state_dir = tmp_path / "state"
    now = datetime(2026, 6, 5, 12, 0, tzinfo=timezone.utc)

    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    data_source = make_data_source(lambda symbol, lookback, interval: [
        Bar(ts=now.isoformat(), open=100.0, high=101.0, low=99.0, close=100.0, volume=1000.0)
    ])
    patch_batch(lambda **kwargs: Decision(
            symbol="SPY",
            action="BUY",
            quantity=10.0,
            confidence=0.8,
            rationale="setup",
            intent="OPEN_LONG",
            exit_plan={"hard_stop": {"type": "price", "price": 95.0}}),
    )

    report = daemon.run_cycle(
        dry_run=False,
        now=now,
        symbols_filter=["SPY"],
        sched=Scheduler(state_dir / "scheduler.json"),
        data_source=data_source,
    )

    assert report["decisions"][0]["trade_plan_created"] is True
    assert report["portfolio"]["cash"] == 99_000.0
    assert report["portfolio"]["holdings"][0]["symbol"] == "SPY"
    assert report["portfolio"]["holdings"][0]["quantity"] == 10.0
    current_report = json.loads((state_dir / "current_report.json").read_text(encoding="utf-8"))
    assert current_report["portfolio"]["holdings"][0]["symbol"] == "SPY"
    plans = TradePlanStore(state_dir / "trade_plans.json").open_plans()
    assert len(plans) == 1
    assert plans[0].hard_stop_price == 95.0


def test_run_cycle_cloture_le_plan_quand_codex_ferme_la_position(monkeypatch, tmp_path, patch_batch, make_data_source) -> None:
    _write_runtime_config(tmp_path)
    state_dir = tmp_path / "state"
    now = datetime(2026, 6, 5, 12, 0, tzinfo=timezone.utc)
    broker = SimBroker(state_dir / "broker.json", starting_cash=100_000)
    broker.submit(Order("SPY", "BUY", 10.0), 100.0, "2026-06-05T11:00:00+00:00", dry_run=False)
    store = TradePlanStore(state_dir / "trade_plans.json")
    store.upsert(
        create_trade_plan(
            symbol="SPY",
            side="LONG",
            quantity=10.0,
            entry_price=100.0,
            opened_at="2026-06-05T11:00:00+00:00",
            raw_exit_plan={"hard_stop": {"type": "price", "price": 95.0}},
        )
    )

    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    data_source = make_data_source(lambda symbol, lookback, interval: [
        Bar(ts=now.isoformat(), open=102.0, high=103.0, low=101.0, close=102.0, volume=1000.0)
    ])
    patch_batch(lambda **kwargs: Decision(
            symbol="SPY",
            action="SELL",
            quantity=10.0,
            confidence=0.8,
            rationale="thèse invalidée",
            intent="CLOSE"),
    )

    report = daemon.run_cycle(
        dry_run=False,
        now=now,
        symbols_filter=["SPY"],
        sched=Scheduler(state_dir / "scheduler.json"),
        data_source=data_source,
    )

    assert report["decisions"][0]["executed"] is True
    assert TradePlanStore(state_dir / "trade_plans.json").open_plans() == []
    assert SimBroker(state_dir / "broker.json").positions() == {}


def test_run_cycle_autorise_close_qui_reduit_le_risque_meme_si_ordre_depasse_max_order(
    monkeypatch,
    tmp_path,
    patch_batch,
    make_data_source,
) -> None:
    _write_runtime_config(tmp_path)
    state_dir = tmp_path / "state"
    now = datetime(2026, 6, 5, 12, 0, tzinfo=timezone.utc)
    broker = SimBroker(state_dir / "broker.json", starting_cash=100_000)
    broker.submit(Order("SPY", "BUY", 150.0), 100.0, "2026-06-05T11:00:00+00:00", dry_run=False)

    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    data_source = make_data_source(lambda symbol, lookback, interval: [
        Bar(ts=now.isoformat(), open=100.0, high=101.0, low=99.0, close=100.0, volume=1000.0)
    ])
    patch_batch(lambda **kwargs: Decision(
            symbol="SPY",
            action="SELL",
            quantity=150.0,
            confidence=0.8,
            rationale="sortie risque",
            intent="CLOSE"),
    )

    report = daemon.run_cycle(
        dry_run=False,
        now=now,
        symbols_filter=["SPY"],
        sched=Scheduler(state_dir / "scheduler.json"),
        data_source=data_source,
    )

    assert report["decisions"][0]["executed"] is True
    assert report["decisions"][0]["reason"] == "ok"
    assert SimBroker(state_dir / "broker.json").positions() == {}


def test_run_cycle_clamp_order_value_et_execute_sans_repasser_par_le_modele(
    monkeypatch,
    tmp_path,
    patch_batch,
    make_data_source,
) -> None:
    _write_runtime_config(tmp_path)
    state_dir = tmp_path / "state"
    now = datetime(2026, 6, 5, 12, 0, tzinfo=timezone.utc)
    codex_calls = 0

    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    data_source = make_data_source(lambda symbol, lookback, interval: [
        Bar(ts=now.isoformat(), open=100.0, high=101.0, low=99.0, close=100.0, volume=1000.0)
    ])

    def decide(**kwargs) -> Decision:
        nonlocal codex_calls
        codex_calls += 1
        return Decision(
            symbol=kwargs["symbol"],
            action="BUY",
            quantity=101.0,
            confidence=0.8,
            rationale="ordre legerement trop gros",
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

    decision = report["decisions"][0]
    assert codex_calls == 1
    assert decision["executed"] is True
    assert decision["reason"] == "ok"
    assert decision["requested_qty"] == 101.0
    assert decision["qty"] == 100.0
    assert decision["qty"] * decision["price"] <= 10_000
    assert SimBroker(state_dir / "broker.json").positions()["SPY"].quantity == 100.0


def test_run_cycle_clamp_order_value_reste_sous_plafond_avec_prix_non_binaire(
    monkeypatch,
    tmp_path,
    patch_batch,
    make_data_source,
) -> None:
    _write_runtime_config(tmp_path)
    state_dir = tmp_path / "state"
    now = datetime(2026, 6, 5, 12, 0, tzinfo=timezone.utc)

    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    data_source = make_data_source(lambda symbol, lookback, interval: [
        Bar(ts=now.isoformat(), open=2.39, high=2.40, low=2.38, close=2.39, volume=1000.0)
    ])
    patch_batch(lambda **kwargs: Decision(
            symbol="SPY",
            action="BUY",
            quantity=5_000.0,
            confidence=0.8,
            rationale="prix non binaire",
            intent="OPEN_LONG"),
    )

    report = daemon.run_cycle(
        dry_run=False,
        now=now,
        symbols_filter=["SPY"],
        sched=Scheduler(state_dir / "scheduler.json"),
        data_source=data_source,
    )

    decision = report["decisions"][0]
    assert decision["executed"] is True
    assert decision["reason"] == "ok"
    assert decision["requested_qty"] == 5_000.0
    assert decision["qty"] * decision["price"] <= 10_000
    assert SimBroker(state_dir / "broker.json").positions()["SPY"].quantity == decision["qty"]


def test_run_cycle_rejette_si_position_value_depasse_apres_clamp_order_value(
    monkeypatch,
    tmp_path,
    patch_batch,
    make_data_source,
) -> None:
    _write_runtime_config(tmp_path, max_position_value=14_000)
    state_dir = tmp_path / "state"
    now = datetime(2026, 6, 5, 12, 0, tzinfo=timezone.utc)
    broker = SimBroker(state_dir / "broker.json", starting_cash=100_000)
    broker.submit(Order("SPY", "BUY", 50.0), 100.0, "2026-06-05T11:00:00+00:00", dry_run=False)

    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    data_source = make_data_source(lambda symbol, lookback, interval: [
        Bar(ts=now.isoformat(), open=100.0, high=101.0, low=99.0, close=100.0, volume=1000.0)
    ])
    patch_batch(lambda **kwargs: Decision(
            symbol="SPY",
            action="BUY",
            quantity=120.0,
            confidence=0.8,
            rationale="ajout trop gros pour position",
            intent="OPEN_LONG"),
    )

    report = daemon.run_cycle(
        dry_run=False,
        now=now,
        symbols_filter=["SPY"],
        sched=Scheduler(state_dir / "scheduler.json"),
        data_source=data_source,
    )

    decision = report["decisions"][0]
    assert decision["executed"] is False
    assert decision["reason"] == "risk:position_value_exceeded"
    assert decision["requested_qty"] == 120.0
    assert decision["qty"] == 100.0
    assert SimBroker(state_dir / "broker.json").positions()["SPY"].quantity == 50.0


def test_run_cycle_rejette_si_gross_exposure_depasse_apres_clamp_order_value(
    monkeypatch,
    tmp_path,
    patch_batch,
    make_data_source,
) -> None:
    _write_runtime_config(tmp_path, symbols=["SPY", "QQQ"], max_gross_exposure=14_000)
    state_dir = tmp_path / "state"
    now = datetime(2026, 6, 5, 12, 0, tzinfo=timezone.utc)
    broker = SimBroker(state_dir / "broker.json", starting_cash=100_000)
    broker.submit(Order("QQQ", "BUY", 50.0), 100.0, "2026-06-05T11:00:00+00:00", dry_run=False)

    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    data_source = make_data_source(lambda symbol, lookback, interval: [
        Bar(ts=now.isoformat(), open=100.0, high=101.0, low=99.0, close=100.0, volume=1000.0)
    ])
    patch_batch(lambda **kwargs: Decision(
            symbol="SPY",
            action="BUY",
            quantity=120.0,
            confidence=0.8,
            rationale="ajout trop gros pour gross",
            intent="OPEN_LONG"),
    )

    report = daemon.run_cycle(
        dry_run=False,
        now=now,
        symbols_filter=["SPY"],
        sched=Scheduler(state_dir / "scheduler.json"),
        data_source=data_source,
    )

    decision = report["decisions"][0]
    assert decision["executed"] is False
    assert decision["reason"] == "risk:gross_exposure_exceeded"
    assert decision["requested_qty"] == 120.0
    assert decision["qty"] == 100.0
    assert "SPY" not in SimBroker(state_dir / "broker.json").positions()
    assert SimBroker(state_dir / "broker.json").positions()["QQQ"].quantity == 50.0


def test_run_cycle_ne_clamp_pas_reverse_trop_gros(
    monkeypatch,
    tmp_path,
    patch_batch,
    make_data_source,
) -> None:
    _write_runtime_config(tmp_path)
    state_dir = tmp_path / "state"
    now = datetime(2026, 6, 5, 12, 0, tzinfo=timezone.utc)
    broker = SimBroker(state_dir / "broker.json", starting_cash=100_000)
    broker.submit(Order("SPY", "BUY", 150.0), 100.0, "2026-06-05T11:00:00+00:00", dry_run=False)

    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    data_source = make_data_source(lambda symbol, lookback, interval: [
        Bar(ts=now.isoformat(), open=100.0, high=101.0, low=99.0, close=100.0, volume=1000.0)
    ])
    patch_batch(lambda **kwargs: Decision(
            symbol="SPY",
            action="SELL",
            quantity=200.0,
            confidence=0.8,
            rationale="reverse trop gros",
            intent="REVERSE"),
    )

    report = daemon.run_cycle(
        dry_run=False,
        now=now,
        symbols_filter=["SPY"],
        sched=Scheduler(state_dir / "scheduler.json"),
        data_source=data_source,
    )

    decision = report["decisions"][0]
    assert decision["executed"] is False
    assert decision["reason"] == "risk:order_value_exceeded"
    assert "requested_qty" not in decision
    assert decision["qty"] == 200.0
    assert SimBroker(state_dir / "broker.json").positions()["SPY"].quantity == 150.0


def test_run_cycle_clamp_close_trop_grand_pour_ne_pas_reverser(monkeypatch, tmp_path, patch_batch, make_data_source) -> None:
    _write_runtime_config(tmp_path)
    state_dir = tmp_path / "state"
    now = datetime(2026, 6, 5, 12, 0, tzinfo=timezone.utc)
    broker = SimBroker(state_dir / "broker.json", starting_cash=100_000)
    broker.submit(Order("SPY", "BUY", 100.0), 100.0, "2026-06-05T11:00:00+00:00", dry_run=False)

    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    data_source = make_data_source(lambda symbol, lookback, interval: [
        Bar(ts=now.isoformat(), open=100.0, high=101.0, low=99.0, close=100.0, volume=1000.0)
    ])
    patch_batch(lambda **kwargs: Decision(
            symbol="SPY",
            action="SELL",
            quantity=120.0,
            confidence=0.8,
            rationale="close trop grand",
            intent="CLOSE"),
    )

    report = daemon.run_cycle(
        dry_run=False,
        now=now,
        symbols_filter=["SPY"],
        sched=Scheduler(state_dir / "scheduler.json"),
        data_source=data_source,
    )

    assert report["decisions"][0]["executed"] is True
    assert report["decisions"][0]["qty"] == 100.0
    assert SimBroker(state_dir / "broker.json").positions() == {}


def test_run_cycle_attribue_les_sorties_planifiees_au_modele_createur(monkeypatch, tmp_path, patch_batch, make_data_source) -> None:
    _write_runtime_config(tmp_path)
    state_dir = tmp_path / "state"
    first_now = datetime(2026, 6, 5, 12, 0, tzinfo=timezone.utc)
    second_now = datetime(2026, 6, 5, 12, 10, tzinfo=timezone.utc)
    calls = 0

    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)

    def get_bars(symbol, lookback, interval):
        price = 100.0 if calls == 0 else 106.0
        return [Bar(ts=first_now.isoformat(), open=price, high=price + 1, low=price - 1, close=price, volume=1000.0)]

    data_source = make_data_source(get_bars)

    def decide(**kwargs) -> Decision:
        nonlocal calls
        calls += 1
        return Decision(
            symbol="SPY",
            action="BUY",
            quantity=10.0,
            confidence=0.72,
            rationale="setup tp",
            intent="OPEN_LONG",
            exit_plan={"take_profits": [{"name": "tp1", "price": 105.0, "fraction": 0.5}]},
            llm_provider="spark",
            llm_model="gpt-5.3-codex-spark/medium",
        )

    patch_batch(decide)

    daemon.run_cycle(
        dry_run=False,
        now=first_now,
        symbols_filter=["SPY"],
        sched=Scheduler(state_dir / "scheduler.json"),
        data_source=data_source,
    )
    daemon.run_cycle(
        dry_run=False,
        now=second_now,
        symbols_filter=[],
        sched=Scheduler(state_dir / "scheduler.json"),
        data_source=data_source,
    )

    rows = [
        json.loads(line)
        for line in (state_dir / "model_performance.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert len(rows) == 2
    assert rows[1]["action"] == "SELL"
    assert rows[1]["intent"] == "PLANNED_EXIT"
    assert rows[1]["llm_provider"] == "spark"
    assert rows[1]["llm_model"] == "gpt-5.3-codex-spark/medium"
    assert rows[1]["exit_reason"] == "take_profit:tp1"


def test_main_historise_les_sorties_planifiees_meme_sans_symbole_du(monkeypatch, tmp_path, patch_batch, make_data_source) -> None:
    _write_runtime_config(tmp_path)
    state_dir = tmp_path / "state"
    sched = Scheduler(state_dir / "scheduler.json")
    sched.set_next_wake("2099-01-01T00:00:00+00:00")
    broker = SimBroker(state_dir / "broker.json", starting_cash=100_000)
    broker.submit(Order("SPY", "BUY", 10.0), 100.0, "2026-06-05T11:00:00+00:00", dry_run=False)
    TradePlanStore(state_dir / "trade_plans.json").upsert(
        create_trade_plan(
            symbol="SPY",
            side="LONG",
            quantity=10.0,
            entry_price=100.0,
            opened_at="2026-06-05T11:00:00+00:00",
            raw_exit_plan={"take_profits": [{"name": "tp1", "price": 105.0, "fraction": 0.5}]},
        )
    )

    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    fresh_ts = datetime.now(timezone.utc).isoformat()
    data_source = make_data_source(lambda symbol, lookback, interval: [
        Bar(ts=fresh_ts, open=106.0, high=107.0, low=105.0, close=106.0, volume=1000.0)
    ])
    monkeypatch.setattr(daemon, "connect_ib", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(daemon, "IBDataSource", lambda _ib, reconnect_factory=None: data_source)
    patch_batch(lambda **kwargs: (_ for _ in ()).throw(AssertionError("Codex ne doit pas être appelé")))
    monkeypatch.setattr(
        daemon.time,
        "sleep",
        lambda seconds: (_ for _ in ()).throw(KeyboardInterrupt),
    )

    with pytest.raises(KeyboardInterrupt):
        daemon.main(["--live", "--poll", "0.01"])

    history_rows = [
        json.loads(line)
        for line in (state_dir / "history.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert history_rows[-1]["cash"] == 99_530.0
    assert history_rows[-1]["equity"] == 100_060.0
    assert history_rows[-1]["n_executed"] == 1


def test_run_cycle_rejette_un_exit_plan_invalide_avant_fill(monkeypatch, tmp_path, patch_batch, make_data_source) -> None:
    _write_runtime_config(tmp_path)
    state_dir = tmp_path / "state"
    now = datetime(2026, 6, 5, 12, 0, tzinfo=timezone.utc)

    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    data_source = make_data_source(lambda symbol, lookback, interval: [
        Bar(ts=now.isoformat(), open=100.0, high=101.0, low=99.0, close=100.0, volume=1000.0)
    ])
    patch_batch(lambda **kwargs: Decision(
            symbol="SPY",
            action="BUY",
            quantity=10.0,
            confidence=0.8,
            rationale="setup",
            intent="OPEN_LONG",
            exit_plan={"take_profits": [{"name": "tp1", "fraction": 0.5}]}),
    )

    report = daemon.run_cycle(
        dry_run=False,
        now=now,
        symbols_filter=["SPY"],
        sched=Scheduler(state_dir / "scheduler.json"),
        data_source=data_source,
    )

    assert report["decisions"][0]["executed"] is False
    assert report["decisions"][0]["reason"].startswith("invalid_exit_plan")
    assert SimBroker(state_dir / "broker.json").positions() == {}


def test_run_cycle_ne_replanifie_pas_un_ordre_bloque(monkeypatch, tmp_path, patch_batch, make_data_source) -> None:
    _write_runtime_config(tmp_path)
    state_dir = tmp_path / "state"
    sched = Scheduler(state_dir / "scheduler.json")
    now = datetime(2026, 6, 5, 12, 0, tzinfo=timezone.utc)
    sched.set_symbol_next_wake("SPY", now.isoformat())

    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    data_source = make_data_source(lambda symbol, lookback, interval: [
        Bar(ts=now.isoformat(), open=100.0, high=101.0, low=99.0, close=100.0, volume=1000.0)
    ])
    patch_batch(lambda **kwargs: Decision(
            symbol="SPY",
            action="BUY",
            quantity=10.0,
            confidence=0.8,
            rationale="setup invalide",
            intent="OPEN_LONG",
            next_wake_in_minutes=240.0,
            indicator_watch={
                "ttl_minutes": 90,
                "conditions": [
                    {"indicator": "return", "op": ">", "value": 0.01, "interval": "1h", "window": 3},
                ],
            },
            exit_plan={"take_profits": [{"name": "tp1", "fraction": 0.5}]}),
    )

    report = daemon.run_cycle(
        dry_run=False,
        now=now,
        symbols_filter=["SPY"],
        sched=sched,
        data_source=data_source,
        default_wake_minutes=30.0,
    )

    assert report["decisions"][0]["executed"] is False
    assert report["decisions"][0]["reason"].startswith("invalid_exit_plan")
    assert report["decisions"][0]["indicator_watch_created"] is False
    assert sched.next_wake("SPY") == now + timedelta(minutes=30)
    assert sched.active_indicator_watches(now=now) == []


def test_run_cycle_reverse_cree_un_plan_sur_la_position_nette_finale(monkeypatch, tmp_path, patch_batch, make_data_source) -> None:
    _write_runtime_config(tmp_path)
    state_dir = tmp_path / "state"
    now = datetime(2026, 6, 5, 12, 0, tzinfo=timezone.utc)
    broker = SimBroker(state_dir / "broker.json", starting_cash=100_000)
    broker.submit(Order("SPY", "BUY", 10.0), 100.0, "2026-06-05T11:00:00+00:00", dry_run=False)
    TradePlanStore(state_dir / "trade_plans.json").upsert(
        create_trade_plan(
            symbol="SPY",
            side="LONG",
            quantity=10.0,
            entry_price=100.0,
            opened_at="2026-06-05T11:00:00+00:00",
            raw_exit_plan={"hard_stop": {"type": "price", "price": 95.0}},
        )
    )

    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    data_source = make_data_source(lambda symbol, lookback, interval: [
        Bar(ts=now.isoformat(), open=102.0, high=103.0, low=101.0, close=102.0, volume=1000.0)
    ])
    patch_batch(lambda **kwargs: Decision(
            symbol="SPY",
            action="SELL",
            quantity=20.0,
            confidence=0.8,
            rationale="reverse",
            intent="REVERSE",
            exit_plan={"hard_stop": {"type": "price", "price": 105.0}}),
    )

    daemon.run_cycle(
        dry_run=False,
        now=now,
        symbols_filter=["SPY"],
        sched=Scheduler(state_dir / "scheduler.json"),
        data_source=data_source,
    )

    pos = SimBroker(state_dir / "broker.json").positions()["SPY"]
    plans = TradePlanStore(state_dir / "trade_plans.json").open_plans()
    assert pos.quantity == -10.0
    assert len(plans) == 1
    assert plans[0].side == "SHORT"
    assert plans[0].quantity == 10.0


def test_run_cycle_reduce_resynchronise_le_plan_sur_position_restante(monkeypatch, tmp_path, patch_batch, make_data_source) -> None:
    _write_runtime_config(tmp_path)
    state_dir = tmp_path / "state"
    now = datetime(2026, 6, 5, 12, 0, tzinfo=timezone.utc)
    broker = SimBroker(state_dir / "broker.json", starting_cash=100_000)
    broker.submit(Order("SPY", "BUY", 10.0), 100.0, "2026-06-05T11:00:00+00:00", dry_run=False)
    TradePlanStore(state_dir / "trade_plans.json").upsert(
        create_trade_plan(
            symbol="SPY",
            side="LONG",
            quantity=10.0,
            entry_price=100.0,
            opened_at="2026-06-05T11:00:00+00:00",
            raw_exit_plan={"take_profits": [{"name": "tp1", "price": 105.0, "fraction": 1.0}]},
        )
    )

    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    data_source = make_data_source(lambda symbol, lookback, interval: [
        Bar(ts=now.isoformat(), open=102.0, high=103.0, low=101.0, close=102.0, volume=1000.0)
    ])
    patch_batch(lambda **kwargs: Decision(
            symbol="SPY",
            action="SELL",
            quantity=4.0,
            confidence=0.8,
            rationale="reduce",
            intent="REDUCE"),
    )

    daemon.run_cycle(
        dry_run=False,
        now=now,
        symbols_filter=["SPY"],
        sched=Scheduler(state_dir / "scheduler.json"),
        data_source=data_source,
    )

    plans = TradePlanStore(state_dir / "trade_plans.json").open_plans()
    assert plans[0].remaining_quantity == 6.0
    assert plans[0].take_profits[0].quantity == 6.0


def test_run_cycle_rejette_un_ordre_non_hold_sans_intent(monkeypatch, tmp_path, patch_batch, make_data_source) -> None:
    _write_runtime_config(tmp_path)
    state_dir = tmp_path / "state"
    now = datetime(2026, 6, 5, 12, 0, tzinfo=timezone.utc)

    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    data_source = make_data_source(lambda symbol, lookback, interval: [
        Bar(ts=now.isoformat(), open=100.0, high=101.0, low=99.0, close=100.0, volume=1000.0)
    ])
    patch_batch(lambda **kwargs: Decision(symbol="SPY", action="BUY", quantity=10.0, confidence=0.8, rationale="setup"))

    report = daemon.run_cycle(
        dry_run=False,
        now=now,
        symbols_filter=["SPY"],
        sched=Scheduler(state_dir / "scheduler.json"),
        data_source=data_source,
    )

    assert report["decisions"][0]["executed"] is False
    assert report["decisions"][0]["reason"] == "invalid_intent"
