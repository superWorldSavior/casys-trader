import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from trader.runtime import daemon
from trader.agent.client import Decision
from trader.agent.protocol.parsing import parse_batch
from trader.execution.broker import Order, SimBroker
from trader.market.market_data import Bar
from trader.planning.exit_engine import ExitEvaluation, ExitSignal
from trader.planning.scheduler import Scheduler
from trader.planning.trade_plan import InvalidExitPlanError, TradePlanStore, create_trade_plan, resolve_exit_plan


def test_llm_exit_reason_for_model_performance_tague_uniquement_les_sorties() -> None:
    assert daemon._llm_exit_reason_for_intent("CLOSE") == "llm_exit"
    assert daemon._llm_exit_reason_for_intent("REDUCE") == "llm_exit"
    assert daemon._llm_exit_reason_for_intent("REVERSE") == "llm_exit"
    assert daemon._llm_exit_reason_for_intent("OPEN_LONG") is None
    assert daemon._llm_exit_reason_for_intent("OPEN_SHORT") is None


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
                "min_equity: 50000",
            ]
        )
    )
    (root / "mandate" / "mandate.md").write_text("# Mandat\n")
    (root / "mandate" / "memory.md").write_text("# Memoire\n")


def test_run_cycle_execute_les_sorties_planifiees_avant_codex(monkeypatch, tmp_path, patch_batch, make_data_source) -> None:
    _write_runtime_config(tmp_path)
    state_dir = tmp_path / "state"
    now = datetime(2026, 6, 5, 14, 30, tzinfo=timezone.utc)
    broker = SimBroker(state_dir / "broker.json", starting_cash=100_000)
    broker.submit(Order("SPY", "BUY", 10.0), 100.0, "2026-06-05T14:00:00+00:00", dry_run=False)
    TradePlanStore(state_dir / "trade_plans.json").upsert(
        create_trade_plan(
            symbol="SPY",
            side="LONG",
            quantity=10.0,
            entry_price=100.0,
            opened_at="2026-06-05T14:00:00+00:00",
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


def test_apply_planned_exits_preserve_daemon_monkeypatch_hooks(monkeypatch, tmp_path) -> None:
    state_dir = tmp_path / "state"
    now = datetime(2026, 6, 5, 14, 30, tzinfo=timezone.utc)
    broker = SimBroker(state_dir / "broker.json", starting_cash=100_000)
    broker.submit(Order("SPY", "BUY", 10.0), 100.0, "2026-06-05T14:00:00+00:00", dry_run=False)
    plan_store = TradePlanStore(state_dir / "trade_plans.json")
    plan_store.upsert(
        create_trade_plan(
            symbol="SPY",
            side="LONG",
            quantity=10.0,
            entry_price=100.0,
            opened_at="2026-06-05T14:00:00+00:00",
            raw_exit_plan={"hard_stop": 50.0},
        )
    )

    def fake_evaluate_plan(plan, **kwargs) -> ExitEvaluation:
        return ExitEvaluation(
            updated_plan=plan,
            signal=ExitSignal(
                symbol=plan.symbol,
                side="SELL",
                quantity=10.0,
                reason="patched_exit",
                fill_price=103.0,
            ),
            close_plan=True,
        )

    monkeypatch.setattr(daemon, "evaluate_plan", fake_evaluate_plan)
    monkeypatch.setattr(
        daemon,
        "_clamp_exit_quantity",
        lambda **kwargs: (4.0, None),
    )
    monkeypatch.setattr(
        daemon,
        "_execution_blocked_reason",
        lambda execution_eligibility, symbol, *, fail_closed=False: "execution:patched_block",
    )

    entries = daemon._apply_planned_exits(
        broker=broker,
        plan_store=plan_store,
        prices={"SPY": 100.0},
        now=now,
        dry_run=False,
        starting_equity=100_000.0,
        execution_eligibility={"SPY": {"execution": {"enabled": True}}},
    )

    assert entries == [
        {
            "symbol": "SPY",
            "side": "SELL",
            "quantity": 4.0,
            "reason": "execution:patched_block",
            "price": 100.0,
            "executed": False,
            "dry_run": False,
        }
    ]
    assert broker.positions()["SPY"].quantity == pytest.approx(10.0)


def test_run_cycle_persiste_fx_rate_sur_sortie_planifiee_non_usd(
    monkeypatch,
    tmp_path,
    patch_batch,
    make_data_source,
) -> None:
    _write_runtime_config(tmp_path, symbols=["2379.TW"])
    (tmp_path / "config" / "fx.yaml").write_text(
        "TWD:\n"
        "  yahoo: TWD=X\n"
        "  invert: true\n"
        "  fallback: 0.031\n",
        encoding="utf-8",
    )
    state_dir = tmp_path / "state"
    now = datetime(2026, 6, 29, 1, 30, tzinfo=timezone.utc)
    expected_rate = 1.0 / 32.0
    broker = SimBroker(state_dir / "broker.json", starting_cash=100_000)
    broker.submit(
        Order("2379.TW", "BUY", 100.0),
        870.0,
        "2026-06-29T01:00:00+00:00",
        dry_run=False,
        fx_rate=expected_rate,
    )
    TradePlanStore(state_dir / "trade_plans.json").upsert(
        create_trade_plan(
            symbol="2379.TW",
            side="LONG",
            quantity=100.0,
            entry_price=870.0,
            opened_at="2026-06-29T01:00:00+00:00",
            raw_exit_plan={"take_profits": [{"name": "tp1", "price": 880.0, "fraction": 1.0}]},
        )
    )

    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    data_source = make_data_source(lambda symbol, lookback, interval: [
        Bar(
            ts=now.isoformat(),
            open=32.0 if symbol == "TWD=X" else 880.0,
            high=32.0 if symbol == "TWD=X" else 881.0,
            low=32.0 if symbol == "TWD=X" else 879.0,
            close=32.0 if symbol == "TWD=X" else 880.0,
            volume=1000.0,
        )
    ])
    patch_batch(lambda **kwargs: Decision.hold(kwargs["symbol"], "attente"))

    report = daemon.run_cycle(
        dry_run=False,
        now=now,
        symbols_filter=["2379.TW"],
        sched=Scheduler(state_dir / "scheduler.json"),
        data_source=data_source,
    )

    assert report["planned_exits"][0]["executed"] is True
    rows = [
        json.loads(line)
        for line in (state_dir / "model_performance.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert rows[-1]["symbol"] == "2379.TW"
    assert rows[-1]["intent"] == "PLANNED_EXIT"
    assert rows[-1]["fx_rate"] == pytest.approx(expected_rate)


def test_run_cycle_ne_sort_pas_hors_session_meme_si_tp_atteint(monkeypatch, tmp_path, patch_batch, make_data_source) -> None:
    """§13.5 — la garde déterministe couvre AUSSI les sorties mécaniques : le TP est
    atteint (calcul fait) mais la session est fermée (samedi) → l'ordre ne part pas
    (executed=False, execution:session_closed), la position reste. Réessai au prochain
    cycle exécutable."""
    _write_runtime_config(tmp_path)
    state_dir = tmp_path / "state"
    now = datetime(2026, 6, 13, 12, 0, tzinfo=timezone.utc)  # samedi → session US fermée
    broker = SimBroker(state_dir / "broker.json", starting_cash=100_000)
    broker.submit(Order("SPY", "BUY", 10.0), 100.0, "2026-06-12T20:00:00+00:00", dry_run=False)
    TradePlanStore(state_dir / "trade_plans.json").upsert(
        create_trade_plan(
            symbol="SPY",
            side="LONG",
            quantity=10.0,
            entry_price=100.0,
            opened_at="2026-06-12T20:00:00+00:00",
            raw_exit_plan={"take_profits": [{"name": "tp1", "price": 105.0, "fraction": 0.5}]},
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

    pe = report["planned_exits"][0]
    assert pe["executed"] is False
    assert pe["reason"] == "execution:session_closed"
    # La position n'a PAS été réduite : la sortie repartira au prochain cycle exécutable.
    assert SimBroker(state_dir / "broker.json").positions()["SPY"].quantity == 10.0


def test_run_cycle_clamp_les_sorties_planifiees_sur_position_broker(monkeypatch, tmp_path, patch_batch, make_data_source) -> None:
    _write_runtime_config(tmp_path)
    state_dir = tmp_path / "state"
    now = datetime(2026, 6, 5, 14, 30, tzinfo=timezone.utc)
    broker = SimBroker(state_dir / "broker.json", starting_cash=100_000)
    broker.submit(Order("SPY", "BUY", 5.0), 100.0, "2026-06-05T14:00:00+00:00", dry_run=False)
    TradePlanStore(state_dir / "trade_plans.json").upsert(
        create_trade_plan(
            symbol="SPY",
            side="LONG",
            quantity=10.0,
            entry_price=100.0,
            opened_at="2026-06-05T14:00:00+00:00",
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
    now = datetime(2026, 6, 5, 14, 30, tzinfo=timezone.utc)

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


def test_run_cycle_planifie_une_revue_post_entry_apres_ouverture(
    monkeypatch, tmp_path, patch_batch, make_data_source
) -> None:
    _write_runtime_config(tmp_path)
    state_dir = tmp_path / "state"
    now = datetime(2026, 6, 5, 14, 30, tzinfo=timezone.utc)
    sched = Scheduler(state_dir / "scheduler.json")

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

    daemon.run_cycle(
        dry_run=False,
        now=now,
        symbols_filter=["SPY"],
        sched=sched,
        data_source=data_source,
    )

    assert sched.next_wake("SPY") == now + timedelta(minutes=15)


def test_run_cycle_persiste_last_llm_review_sur_position_ouverte(
    monkeypatch, tmp_path, patch_batch, make_data_source
) -> None:
    _write_runtime_config(tmp_path)
    state_dir = tmp_path / "state"
    now = datetime(2026, 6, 5, 12, 15, tzinfo=timezone.utc)
    broker = SimBroker(state_dir / "broker.json", starting_cash=100_000)
    broker.submit(Order("SPY", "BUY", 10.0), 100.0, "2026-06-05T12:00:00+00:00", dry_run=False)
    store = TradePlanStore(state_dir / "trade_plans.json")
    store.upsert(
        create_trade_plan(
            symbol="SPY",
            side="LONG",
            quantity=10.0,
            entry_price=100.0,
            opened_at="2026-06-05T12:00:00+00:00",
            raw_exit_plan={"hard_stop": {"type": "price", "price": 95.0}},
        )
    )

    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    data_source = make_data_source(lambda symbol, lookback, interval: [
        Bar(ts=now.isoformat(), open=101.0, high=102.0, low=100.0, close=101.0, volume=1000.0)
    ])
    patch_batch(lambda **kwargs: Decision(
            symbol="SPY",
            action="HOLD",
            quantity=0.0,
            confidence=0.8,
            rationale="thèse intacte",
            intent="HOLD",
            llm_provider="acpx",
            llm_model="gpt-5.5/medium"),
    )

    daemon.run_cycle(
        dry_run=False,
        now=now,
        symbols_filter=["SPY"],
        sched=Scheduler(state_dir / "scheduler.json"),
        data_source=data_source,
    )

    review = TradePlanStore(state_dir / "trade_plans.json").open_plans()[0].last_llm_review
    assert review == {
        "ts": now.isoformat(),
        "verdict": "intact",
        "action": "HOLD",
        "intent": "HOLD",
        "llm_provider": "acpx",
        "llm_model": "gpt-5.5/medium",
    }


def test_run_cycle_persiste_reference_volatility_pour_trailing_multiple(
    monkeypatch,
    tmp_path,
    patch_batch,
    make_data_source,
) -> None:
    _write_runtime_config(tmp_path)
    state_dir = tmp_path / "state"
    now = datetime(2026, 6, 5, 14, 30, tzinfo=timezone.utc)

    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    bars = [
        Bar(ts="2026-06-05T11:30:00+00:00", open=100.0, high=101.0, low=99.0, close=100.0, volume=1000.0),
        Bar(ts="2026-06-05T11:45:00+00:00", open=100.0, high=102.0, low=99.5, close=101.0, volume=1000.0),
        Bar(ts=now.isoformat(), open=101.0, high=104.0, low=100.5, close=103.0, volume=1000.0),
    ]
    data_source = make_data_source(lambda symbol, lookback, interval: bars)
    patch_batch(
        lambda **kwargs: Decision(
            symbol="SPY",
            action="BUY",
            quantity=10.0,
            confidence=0.8,
            rationale="setup vol",
            intent="OPEN_LONG",
            exit_plan={
                "hard_stop": {"type": "price", "price": 95.0},
                "trailing_stop": {"trail_type": "volatility_multiple", "trail_value": 2.0},
            },
        ),
    )

    report = daemon.run_cycle(
        dry_run=False,
        now=now,
        symbols_filter=["SPY"],
        sched=Scheduler(state_dir / "scheduler.json"),
        data_source=data_source,
    )

    assert report["decisions"][0]["trade_plan_created"] is True
    plans = TradePlanStore(state_dir / "trade_plans.json").open_plans()
    assert len(plans) == 1
    assert plans[0].reference_volatility is not None
    assert plans[0].reference_volatility > 0


def test_reference_volatility_prefere_vol_daily_et_refuse_stop_hors_borne() -> None:
    cockpit = {
        "cols": ["s", "vol", "vol_d"],
        "rows": [["SPY", 0.01, 0.08]],
    }

    reference_volatility = daemon._reference_volatility_for_symbol(
        "SPY",
        entry_price=100.0,
        cockpit=cockpit,
        tradable_bars_by_symbol={},
    )

    assert reference_volatility == 8.0
    with pytest.raises(InvalidExitPlanError, match="hard_stop_above_max_pct"):
        resolve_exit_plan(
            {"hard_stop": {"type": "volatility_multiple", "multiple": 1.0, "max_pct": 0.03}},
            entry_price=100.0,
            side="LONG",
            reference_volatility=reference_volatility,
        )


def test_run_cycle_cloture_le_plan_quand_codex_ferme_la_position(monkeypatch, tmp_path, patch_batch, make_data_source) -> None:
    _write_runtime_config(tmp_path)
    state_dir = tmp_path / "state"
    now = datetime(2026, 6, 5, 14, 30, tzinfo=timezone.utc)
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
    rows = [
        json.loads(line)
        for line in (state_dir / "model_performance.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert rows[0]["intent"] == "CLOSE"
    assert rows[0]["exit_reason"] == "llm_exit"


def test_run_cycle_autorise_close_qui_reduit_le_risque_meme_si_ordre_depasse_max_order(
    monkeypatch,
    tmp_path,
    patch_batch,
    make_data_source,
) -> None:
    _write_runtime_config(tmp_path)
    state_dir = tmp_path / "state"
    now = datetime(2026, 6, 5, 14, 30, tzinfo=timezone.utc)
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


def test_run_cycle_rejette_order_value_sans_modifier_quantite_agent(
    monkeypatch,
    tmp_path,
    patch_batch,
    make_data_source,
) -> None:
    _write_runtime_config(tmp_path)
    state_dir = tmp_path / "state"
    now = datetime(2026, 6, 5, 14, 30, tzinfo=timezone.utc)
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
            confidence=0.95,
            rationale="ordre legerement trop gros",
            intent="OPEN_LONG",
            # stop serré : risque borné loin du clamp, l'order-value reste la borne testée
            exit_plan={"hard_stop": {"type": "price", "price": 99.9}},
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
    assert decision["executed"] is False
    assert decision["reason"] == "risk:order_value_exceeded"
    assert decision["qty"] == 101.0
    assert "requested_qty" not in decision
    assert "SPY" not in SimBroker(state_dir / "broker.json").positions()


def test_run_cycle_rejette_order_value_prix_non_binaire_sans_modifier_quantite_agent(
    monkeypatch,
    tmp_path,
    patch_batch,
    make_data_source,
) -> None:
    _write_runtime_config(tmp_path)
    state_dir = tmp_path / "state"
    now = datetime(2026, 6, 5, 14, 30, tzinfo=timezone.utc)

    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    data_source = make_data_source(lambda symbol, lookback, interval: [
        Bar(ts=now.isoformat(), open=2.39, high=2.40, low=2.38, close=2.39, volume=1000.0)
    ])
    patch_batch(lambda **kwargs: Decision(
            symbol="SPY",
            action="BUY",
            quantity=5_000.0,
            confidence=0.95,
            rationale="prix non binaire",
            intent="OPEN_LONG",
            exit_plan={"hard_stop": {"type": "price", "price": 2.29}}),
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
    assert decision["qty"] == 5_000.0
    assert "requested_qty" not in decision
    assert "SPY" not in SimBroker(state_dir / "broker.json").positions()


def test_run_cycle_rejette_open_long_quand_risque_depasse_un_pourcent_sans_clamp(
    monkeypatch,
    tmp_path,
    patch_batch,
    make_data_source,
) -> None:
    _write_runtime_config(tmp_path, max_order_value=100_000)
    state_dir = tmp_path / "state"
    now = datetime(2026, 6, 5, 14, 30, tzinfo=timezone.utc)

    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    data_source = make_data_source(lambda symbol, lookback, interval: [
        Bar(ts=now.isoformat(), open=100.0, high=101.0, low=99.0, close=100.0, volume=1000.0)
    ])
    patch_batch(lambda **kwargs: Decision(
            symbol="SPY",
            action="BUY",
            quantity=300.0,
            confidence=0.95,
            rationale="risque trop eleve",
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

    decision = report["decisions"][0]
    assert decision["executed"] is False
    assert decision["reason"] == "risk:risk_per_trade_exceeded"
    assert decision["qty"] == pytest.approx(300.0)
    assert "requested_qty" not in decision
    assert decision["risk_clamped"] is False
    assert decision["stop_distance"] == pytest.approx(5.0)
    assert decision["risk_pct"] > 0.01
    assert "SPY" not in SimBroker(state_dir / "broker.json").positions()


def test_run_cycle_rejette_open_long_si_hard_stop_est_du_mauvais_cote(
    monkeypatch,
    tmp_path,
    patch_batch,
    make_data_source,
) -> None:
    _write_runtime_config(tmp_path)
    state_dir = tmp_path / "state"
    now = datetime(2026, 6, 5, 14, 30, tzinfo=timezone.utc)

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
            rationale="stop du mauvais cote",
            intent="OPEN_LONG",
            exit_plan={"hard_stop": {"type": "price", "price": 105.0}}),
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
    assert decision["reason"] == "invalid_exit_plan:hard_stop_wrong_side"
    assert SimBroker(state_dir / "broker.json").positions() == {}


def test_run_cycle_accepte_open_long_si_hard_stop_est_du_bon_cote(
    monkeypatch,
    tmp_path,
    patch_batch,
    make_data_source,
) -> None:
    _write_runtime_config(tmp_path)
    state_dir = tmp_path / "state"
    now = datetime(2026, 6, 5, 14, 30, tzinfo=timezone.utc)

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
            rationale="stop protecteur",
            intent="OPEN_LONG",
            exit_plan={"hard_stop": {"type": "price", "price": 98.0}}),
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
    assert decision["risk_clamped"] is False
    assert decision["stop_distance"] == pytest.approx(2.0)
    assert SimBroker(state_dir / "broker.json").positions()["SPY"].quantity == 10.0


def test_run_cycle_resout_hard_stop_volatilite_direct_avant_risque(
    monkeypatch,
    tmp_path,
    patch_batch,
    make_data_source,
) -> None:
    _write_runtime_config(tmp_path, max_order_value=100_000)
    state_dir = tmp_path / "state"
    now = datetime(2026, 6, 5, 14, 30, tzinfo=timezone.utc)
    fresh_vol_calls: list[dict] = []

    def fresh_volatility(symbol, *, entry_price, cockpit, tradable_bars_by_symbol):
        fresh_vol_calls.append(
            {
                "symbol": symbol,
                "entry_price": entry_price,
                "has_cockpit": bool(cockpit),
                "has_bars": symbol in tradable_bars_by_symbol,
            }
        )
        return 2.0

    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    monkeypatch.setattr(daemon, "_reference_volatility_for_symbol", fresh_volatility)
    data_source = make_data_source(lambda symbol, lookback, interval: [
        Bar(ts=now.isoformat(), open=100.0, high=101.0, low=99.0, close=100.0, volume=1000.0)
    ])
    patch_batch(lambda **kwargs: Decision(
            symbol="SPY",
            action="BUY",
            quantity=10.0,
            confidence=0.8,
            rationale="stop volatilite direct",
            intent="OPEN_LONG",
            exit_plan={"hard_stop": {"type": "volatility_multiple", "multiple": 1.5}}),
    )

    report = daemon.run_cycle(
        dry_run=False,
        now=now,
        symbols_filter=["SPY"],
        sched=Scheduler(state_dir / "scheduler.json"),
        data_source=data_source,
    )

    decision = report["decisions"][0]
    plans = TradePlanStore(state_dir / "trade_plans.json").open_plans()
    assert decision["executed"] is True
    assert decision["reason"] == "ok"
    assert decision["risk_unbounded_no_stop"] is False
    assert decision["stop_distance"] == pytest.approx(3.0)
    assert plans[0].hard_stop_price == pytest.approx(97.0)
    assert fresh_vol_calls == [
        {"symbol": "SPY", "entry_price": 100.0, "has_cockpit": True, "has_bars": True}
    ]


def test_run_cycle_rejette_stop_direct_volatilite_si_volatilite_indisponible(
    monkeypatch,
    tmp_path,
    patch_batch,
    make_data_source,
) -> None:
    _write_runtime_config(tmp_path, max_order_value=100_000)
    state_dir = tmp_path / "state"
    now = datetime(2026, 6, 5, 14, 30, tzinfo=timezone.utc)

    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    monkeypatch.setattr(
        daemon,
        "_reference_volatility_for_symbol",
        lambda symbol, *, entry_price, cockpit, tradable_bars_by_symbol: None,
    )
    data_source = make_data_source(lambda symbol, lookback, interval: [
        Bar(ts=now.isoformat(), open=100.0, high=101.0, low=99.0, close=100.0, volume=1000.0)
    ])
    patch_batch(lambda **kwargs: Decision(
            symbol="SPY",
            action="BUY",
            quantity=10.0,
            confidence=0.8,
            rationale="stop volatilite sans vol",
            intent="OPEN_LONG",
            exit_plan={"hard_stop": {"type": "volatility_multiple", "multiple": 1.5}}),
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
    assert decision["reason"] == "invalid_exit_plan:hard_stop_volatility_unavailable"
    assert decision["reason"] != "risk:missing_hard_stop"
    assert "SPY" not in SimBroker(state_dir / "broker.json").positions()


def test_run_cycle_direct_persiste_take_profit_risk_multiple_resolu(
    monkeypatch,
    tmp_path,
    patch_batch,
    make_data_source,
) -> None:
    _write_runtime_config(tmp_path, max_order_value=100_000)
    state_dir = tmp_path / "state"
    now = datetime(2026, 6, 5, 14, 30, tzinfo=timezone.utc)

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
            rationale="tp en R direct",
            intent="OPEN_LONG",
            exit_plan={
                "hard_stop": {"type": "percent", "percent": 0.03},
                "take_profits": [{"type": "risk_multiple", "r": 2.0, "fraction": 0.5}],
            }),
    )

    report = daemon.run_cycle(
        dry_run=False,
        now=now,
        symbols_filter=["SPY"],
        sched=Scheduler(state_dir / "scheduler.json"),
        data_source=data_source,
    )

    decision = report["decisions"][0]
    plans = TradePlanStore(state_dir / "trade_plans.json").open_plans()
    assert decision["executed"] is True
    assert plans[0].hard_stop_price == pytest.approx(97.0)
    assert len(plans[0].take_profits) == 1
    assert plans[0].take_profits[0].price == pytest.approx(106.0)
    assert plans[0].take_profits[0].fraction == pytest.approx(0.5)


def test_run_cycle_resout_hard_stop_structural_direct_depuis_barres_fraiches(
    monkeypatch,
    tmp_path,
    patch_batch,
    make_data_source,
) -> None:
    _write_runtime_config(tmp_path, max_order_value=100_000)
    state_dir = tmp_path / "state"
    now = datetime(2026, 6, 5, 14, 30, tzinfo=timezone.utc)

    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    data_source = make_data_source(lambda symbol, lookback, interval: [
        Bar(ts=now.isoformat(), open=100.0, high=102.0, low=low, close=100.0, volume=1000.0)
        for low in [90.0, 96.0, 94.0, 97.0]
    ])
    patch_batch(lambda **kwargs: Decision(
            symbol="SPY",
            action="BUY",
            quantity=10.0,
            confidence=0.8,
            rationale="stop structural direct",
            intent="OPEN_LONG",
            exit_plan={"hard_stop": {"type": "structural", "anchor": "swing_low", "window": 3}}),
    )

    report = daemon.run_cycle(
        dry_run=False,
        now=now,
        symbols_filter=["SPY"],
        sched=Scheduler(state_dir / "scheduler.json"),
        data_source=data_source,
    )

    decision = report["decisions"][0]
    plans = TradePlanStore(state_dir / "trade_plans.json").open_plans()
    assert decision["executed"] is True
    assert decision["risk_unbounded_no_stop"] is False
    assert plans[0].hard_stop_price == pytest.approx(94.0)


def test_run_cycle_rejette_open_sans_hard_stop_meme_confiant(
    monkeypatch,
    tmp_path,
    patch_batch,
    make_data_source,
) -> None:
    """Guardrail D6 déterministe : une ouverture sans hard_stop est rejetée,
    le risque non borné reste tracé pour l'audit."""
    _write_runtime_config(tmp_path, max_order_value=100_000)
    state_dir = tmp_path / "state"
    now = datetime(2026, 6, 5, 14, 30, tzinfo=timezone.utc)

    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    data_source = make_data_source(lambda symbol, lookback, interval: [
        Bar(ts=now.isoformat(), open=100.0, high=101.0, low=99.0, close=100.0, volume=1000.0)
    ])
    patch_batch(lambda **kwargs: Decision(
            symbol="SPY",
            action="BUY",
            quantity=120.0,
            confidence=0.95,
            rationale="ouverture sans stop dur",
            intent="OPEN_LONG",
            exit_plan={"take_profits": [{"name": "tp1", "price": 105.0, "fraction": 1.0}]}),
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
    assert decision["reason"] == "risk:missing_hard_stop"
    assert decision["risk_unbounded_no_stop"] is True
    assert decision["risk_pct"] is None
    assert decision["stop_distance"] is None
    assert "SPY" not in SimBroker(state_dir / "broker.json").positions()


def test_run_cycle_rejette_risque_avant_order_value_sans_modifier_quantite_agent(
    monkeypatch,
    tmp_path,
    patch_batch,
    make_data_source,
) -> None:
    _write_runtime_config(tmp_path, max_order_value=15_000)
    state_dir = tmp_path / "state"
    now = datetime(2026, 6, 5, 14, 30, tzinfo=timezone.utc)

    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    data_source = make_data_source(lambda symbol, lookback, interval: [
        Bar(ts=now.isoformat(), open=100.0, high=101.0, low=99.0, close=100.0, volume=1000.0)
    ])
    patch_batch(lambda **kwargs: Decision(
            symbol="SPY",
            action="BUY",
            quantity=300.0,
            confidence=0.95,
            rationale="risque puis notionnel trop eleves",
            intent="OPEN_LONG",
            exit_plan={"hard_stop": 95.0}),
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
    assert decision["reason"] == "risk:risk_per_trade_exceeded"
    assert decision["qty"] == pytest.approx(300.0)
    assert "requested_qty" not in decision
    assert decision["risk_clamped"] is False
    assert decision["risk_pct"] > 0.01
    assert "SPY" not in SimBroker(state_dir / "broker.json").positions()


def test_run_cycle_rejette_si_position_value_depasse_sans_clamp_order_value(
    monkeypatch,
    tmp_path,
    patch_batch,
    make_data_source,
) -> None:
    _write_runtime_config(tmp_path, max_position_value=14_000, max_order_value=100_000)
    state_dir = tmp_path / "state"
    now = datetime(2026, 6, 5, 14, 30, tzinfo=timezone.utc)
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
            confidence=0.95,
            rationale="ajout trop gros pour position",
            intent="OPEN_LONG",
            exit_plan={"hard_stop": {"type": "price", "price": 99.9}}),
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
    assert "requested_qty" not in decision
    assert decision["qty"] == 120.0
    assert SimBroker(state_dir / "broker.json").positions()["SPY"].quantity == 50.0


def test_run_cycle_rejette_si_gross_exposure_depasse_sans_clamp_order_value(
    monkeypatch,
    tmp_path,
    patch_batch,
    make_data_source,
) -> None:
    _write_runtime_config(tmp_path, symbols=["SPY", "QQQ"], max_gross_exposure=14_000, max_order_value=100_000)
    state_dir = tmp_path / "state"
    now = datetime(2026, 6, 5, 14, 30, tzinfo=timezone.utc)
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
            confidence=0.95,
            rationale="ajout trop gros pour gross",
            intent="OPEN_LONG",
            exit_plan={"hard_stop": {"type": "price", "price": 99.9}}),
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
    assert "requested_qty" not in decision
    assert decision["qty"] == 120.0
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
    now = datetime(2026, 6, 5, 14, 30, tzinfo=timezone.utc)
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
            quantity=50.0,
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


def test_run_cycle_reverse_position_aware_long_vers_short_utilise_qty_totale(
    monkeypatch,
    tmp_path,
    patch_batch,
    make_data_source,
) -> None:
    _write_runtime_config(tmp_path)
    state_dir = tmp_path / "state"
    now = datetime(2026, 6, 5, 14, 30, tzinfo=timezone.utc)
    broker = SimBroker(state_dir / "broker.json", starting_cash=100_000)
    broker.submit(Order("SPY", "BUY", 10.0), 100.0, "2026-06-05T11:00:00+00:00", dry_run=False)

    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    data_source = make_data_source(lambda symbol, lookback, interval: [
        Bar(ts=now.isoformat(), open=100.0, high=101.0, low=99.0, close=100.0, volume=1000.0)
    ])
    patch_batch(lambda **kwargs: Decision(
            symbol="SPY",
            action="HOLD",
            quantity=20.0,
            confidence=0.8,
            rationale="reverse target short 20",
            intent="REVERSE",
            resolve_from_position=True,
            exit_plan={"hard_stop": {"type": "price", "price": 105.0}}),
    )

    report = daemon.run_cycle(
        dry_run=False,
        now=now,
        symbols_filter=["SPY"],
        sched=Scheduler(state_dir / "scheduler.json"),
        data_source=data_source,
    )

    decision = report["decisions"][0]
    assert decision["action"] == "SELL"
    assert decision["qty"] == 30.0
    assert decision["reason"] == "ok"
    assert SimBroker(state_dir / "broker.json").positions()["SPY"].quantity == -20.0


def test_run_cycle_reverse_position_aware_short_vers_long_utilise_qty_totale(
    monkeypatch,
    tmp_path,
    patch_batch,
    make_data_source,
) -> None:
    _write_runtime_config(tmp_path)
    state_dir = tmp_path / "state"
    now = datetime(2026, 6, 5, 14, 30, tzinfo=timezone.utc)
    broker = SimBroker(state_dir / "broker.json", starting_cash=100_000)
    broker.submit(Order("SPY", "SELL", 8.0), 100.0, "2026-06-05T11:00:00+00:00", dry_run=False)

    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    data_source = make_data_source(lambda symbol, lookback, interval: [
        Bar(ts=now.isoformat(), open=100.0, high=101.0, low=99.0, close=100.0, volume=1000.0)
    ])
    patch_batch(lambda **kwargs: Decision(
            symbol="SPY",
            action="HOLD",
            quantity=12.0,
            confidence=0.8,
            rationale="reverse target long 12",
            intent="REVERSE",
            resolve_from_position=True,
            exit_plan={"hard_stop": {"type": "price", "price": 95.0}}),
    )

    report = daemon.run_cycle(
        dry_run=False,
        now=now,
        symbols_filter=["SPY"],
        sched=Scheduler(state_dir / "scheduler.json"),
        data_source=data_source,
    )

    decision = report["decisions"][0]
    assert decision["action"] == "BUY"
    assert decision["qty"] == 20.0
    assert decision["reason"] == "ok"
    assert SimBroker(state_dir / "broker.json").positions()["SPY"].quantity == 12.0


def test_run_cycle_add_sans_hard_stop_est_rejete(
    monkeypatch,
    tmp_path,
    patch_batch,
    make_data_source,
) -> None:
    _write_runtime_config(tmp_path)
    state_dir = tmp_path / "state"
    now = datetime(2026, 6, 5, 14, 30, tzinfo=timezone.utc)
    broker = SimBroker(state_dir / "broker.json", starting_cash=100_000)
    broker.submit(Order("SPY", "BUY", 10.0), 100.0, "2026-06-05T11:00:00+00:00", dry_run=False)

    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    data_source = make_data_source(lambda symbol, lookback, interval: [
        Bar(ts=now.isoformat(), open=100.0, high=101.0, low=99.0, close=100.0, volume=1000.0)
    ])
    patch_batch(lambda **kwargs: Decision(
            symbol="SPY",
            action="HOLD",
            quantity=5.0,
            confidence=0.95,
            rationale="add sans stop",
            intent="ADD",
            resolve_from_position=True),
    )

    report = daemon.run_cycle(
        dry_run=False,
        now=now,
        symbols_filter=["SPY"],
        sched=Scheduler(state_dir / "scheduler.json"),
        data_source=data_source,
    )

    decision = report["decisions"][0]
    assert decision["reason"] == "risk:missing_hard_stop"
    assert decision["executed"] is False
    assert SimBroker(state_dir / "broker.json").positions()["SPY"].quantity == 10.0


def test_run_cycle_add_depassement_risque_est_rejete(
    monkeypatch,
    tmp_path,
    patch_batch,
    make_data_source,
) -> None:
    _write_runtime_config(tmp_path)
    state_dir = tmp_path / "state"
    now = datetime(2026, 6, 5, 14, 30, tzinfo=timezone.utc)
    broker = SimBroker(state_dir / "broker.json", starting_cash=100_000)
    broker.submit(Order("SPY", "BUY", 10.0), 100.0, "2026-06-05T11:00:00+00:00", dry_run=False)

    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    data_source = make_data_source(lambda symbol, lookback, interval: [
        Bar(ts=now.isoformat(), open=100.0, high=101.0, low=99.0, close=100.0, volume=1000.0)
    ])
    patch_batch(lambda **kwargs: Decision(
            symbol="SPY",
            action="HOLD",
            quantity=200.0,
            confidence=0.95,
            rationale="add trop risque",
            intent="ADD",
            resolve_from_position=True,
            exit_plan={"hard_stop": {"type": "price", "price": 90.0}}),
    )

    report = daemon.run_cycle(
        dry_run=False,
        now=now,
        symbols_filter=["SPY"],
        sched=Scheduler(state_dir / "scheduler.json"),
        data_source=data_source,
    )

    decision = report["decisions"][0]
    assert decision["reason"] == "risk:risk_per_trade_exceeded"
    assert decision["executed"] is False
    assert decision["risk_pct"] > 0.01
    assert SimBroker(state_dir / "broker.json").positions()["SPY"].quantity == 10.0


def test_run_cycle_add_long_rejette_hard_stop_du_mauvais_cote(
    monkeypatch,
    tmp_path,
    patch_batch,
    make_data_source,
) -> None:
    _write_runtime_config(tmp_path)
    state_dir = tmp_path / "state"
    now = datetime(2026, 6, 5, 14, 30, tzinfo=timezone.utc)
    broker = SimBroker(state_dir / "broker.json", starting_cash=100_000)
    broker.submit(Order("SPY", "BUY", 10.0), 100.0, "2026-06-05T11:00:00+00:00", dry_run=False)

    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    data_source = make_data_source(lambda symbol, lookback, interval: [
        Bar(ts=now.isoformat(), open=100.0, high=101.0, low=99.0, close=100.0, volume=1000.0)
    ])
    patch_batch(lambda **kwargs: Decision(
            symbol="SPY",
            action="HOLD",
            quantity=5.0,
            confidence=0.95,
            rationale="add avec stop du mauvais cote",
            intent="ADD",
            resolve_from_position=True,
            exit_plan={"hard_stop": {"type": "price", "price": 120.0}}),
    )

    report = daemon.run_cycle(
        dry_run=False,
        now=now,
        symbols_filter=["SPY"],
        sched=Scheduler(state_dir / "scheduler.json"),
        data_source=data_source,
    )

    decision = report["decisions"][0]
    assert decision["reason"] == "invalid_exit_plan:hard_stop_wrong_side"
    assert decision["executed"] is False
    assert SimBroker(state_dir / "broker.json").positions()["SPY"].quantity == 10.0


def test_run_cycle_add_long_accepte_hard_stop_du_bon_cote(
    monkeypatch,
    tmp_path,
    patch_batch,
    make_data_source,
) -> None:
    _write_runtime_config(tmp_path)
    state_dir = tmp_path / "state"
    now = datetime(2026, 6, 5, 14, 30, tzinfo=timezone.utc)
    broker = SimBroker(state_dir / "broker.json", starting_cash=100_000)
    broker.submit(Order("SPY", "BUY", 10.0), 100.0, "2026-06-05T11:00:00+00:00", dry_run=False)

    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    data_source = make_data_source(lambda symbol, lookback, interval: [
        Bar(ts=now.isoformat(), open=100.0, high=101.0, low=99.0, close=100.0, volume=1000.0)
    ])
    patch_batch(lambda **kwargs: Decision(
            symbol="SPY",
            action="HOLD",
            quantity=5.0,
            confidence=0.95,
            rationale="add avec stop protecteur",
            intent="ADD",
            resolve_from_position=True,
            exit_plan={"hard_stop": {"type": "price", "price": 98.0}}),
    )

    report = daemon.run_cycle(
        dry_run=False,
        now=now,
        symbols_filter=["SPY"],
        sched=Scheduler(state_dir / "scheduler.json"),
        data_source=data_source,
    )

    decision = report["decisions"][0]
    assert decision["reason"] == "ok"
    assert decision["executed"] is True
    assert decision["stop_distance"] == pytest.approx(2.0)
    assert decision["risk_pct"] == pytest.approx(0.0003)
    assert SimBroker(state_dir / "broker.json").positions()["SPY"].quantity == 15.0


def test_run_cycle_add_rejette_petit_ajout_si_risque_position_totale_depasse(
    monkeypatch,
    tmp_path,
    patch_batch,
    make_data_source,
) -> None:
    _write_runtime_config(tmp_path, max_position_value=200_000, max_order_value=100_000)
    state_dir = tmp_path / "state"
    now = datetime(2026, 6, 5, 14, 30, tzinfo=timezone.utc)
    broker = SimBroker(state_dir / "broker.json", starting_cash=100_000)
    broker.submit(Order("SPY", "BUY", 100.0), 100.0, "2026-06-05T11:00:00+00:00", dry_run=False)

    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    data_source = make_data_source(lambda symbol, lookback, interval: [
        Bar(ts=now.isoformat(), open=50.0, high=51.0, low=49.0, close=50.0, volume=1000.0)
    ])
    patch_batch(lambda **kwargs: Decision(
            symbol="SPY",
            action="HOLD",
            quantity=1.0,
            confidence=0.95,
            rationale="petit add mais risque total au prix moyen",
            intent="ADD",
            resolve_from_position=True,
            exit_plan={"hard_stop": {"type": "price", "price": 41.0}}),
    )

    report = daemon.run_cycle(
        dry_run=False,
        now=now,
        symbols_filter=["SPY"],
        sched=Scheduler(state_dir / "scheduler.json"),
        data_source=data_source,
    )

    decision = report["decisions"][0]
    assert decision["reason"] == "risk:risk_per_trade_exceeded"
    assert decision["executed"] is False
    assert decision["qty"] == pytest.approx(1.0)
    assert decision["risk_pct"] > 0.01
    assert SimBroker(state_dir / "broker.json").positions()["SPY"].quantity == 100.0


def test_run_cycle_legacy_relative_ignored_fields_arrivent_dans_runtime_ledger(
    monkeypatch,
    tmp_path,
    make_data_source,
) -> None:
    _write_runtime_config(tmp_path)
    state_dir = tmp_path / "state"
    now = datetime(2026, 6, 5, 14, 30, tzinfo=timezone.utc)
    broker = SimBroker(state_dir / "broker.json", starting_cash=100_000)
    broker.submit(Order("SPY", "BUY", 10.0), 100.0, "2026-06-05T11:00:00+00:00", dry_run=False)
    raw = """
    {"decisions": [
      {"symbol": "SPY", "action": "SELL", "quantity": 10,
       "confidence": 0.8, "rationale": "close legacy",
       "intent": "CLOSE", "decision_reason_code": "EXIT_SIGNAL"}
    ]}
    """
    parsed = parse_batch(raw, ["SPY"], allow_context_request=False)

    def fake_batch_decide(**kwargs):
        return parsed, 1

    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    monkeypatch.setattr(daemon, "_batch_decide", fake_batch_decide)

    report = daemon.run_cycle(
        dry_run=False,
        now=now,
        symbols_filter=["SPY"],
        sched=Scheduler(state_dir / "scheduler.json"),
        data_source=make_data_source(lambda symbol, lookback, interval: [
            Bar(ts=now.isoformat(), open=100.0, high=101.0, low=99.0, close=100.0, volume=1000.0)
        ]),
    )

    normalizations = report["decisions"][0]["tool_normalizations"]
    assert normalizations[0]["code"] == "relative_intent_position_resolved"
    assert normalizations[0]["ignored_fields"] == ["action"]

    rows = [
        json.loads(line)
        for line in (state_dir / "decisions.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    runtime = rows[-1]["runtime"]
    assert runtime["tool_normalizations"][0]["ignored_fields"] == ["action"]


def test_run_cycle_close_direct_sans_position_passe_par_fusible_position_aware(
    monkeypatch,
    tmp_path,
    patch_batch,
    make_data_source,
) -> None:
    _write_runtime_config(tmp_path)
    state_dir = tmp_path / "state"
    now = datetime(2026, 6, 5, 14, 30, tzinfo=timezone.utc)

    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    data_source = make_data_source(lambda symbol, lookback, interval: [
        Bar(ts=now.isoformat(), open=100.0, high=101.0, low=99.0, close=100.0, volume=1000.0)
    ])
    patch_batch(lambda **kwargs: Decision(
            symbol="SPY",
            action="SELL",
            quantity=0.0,
            confidence=0.8,
            rationale="close risk_pct mal resolu",
            intent="CLOSE"),
    )

    report = daemon.run_cycle(
        dry_run=False,
        now=now,
        symbols_filter=["SPY"],
        sched=Scheduler(state_dir / "scheduler.json"),
        data_source=data_source,
    )

    decision = report["decisions"][0]
    assert decision["reason"] == "nothing_to_close"
    assert decision["executed"] is False
    assert SimBroker(state_dir / "broker.json").positions() == {}


@pytest.mark.parametrize(
    ("intent", "reason"),
    [("CLOSE", "nothing_to_close"), ("ADD", "add_without_position")],
)
def test_resolve_position_aware_hold_preserve_les_metadonnees_llm(intent: str, reason: str) -> None:
    decision = Decision(
        symbol="SPY",
        action="HOLD",
        quantity=5.0,
        confidence=0.73,
        rationale="decision llm",
        intent=intent,
        resolve_from_position=True,
        llm_provider="acpx",
        llm_model="gpt-5.5/medium",
        thesis={"setup": "breakout", "horizon": "swing", "invalidation": "under support"},
        domain_tools={"tool_rounds": 1, "tool_calls": [{"tool": "fetch"}]},
        cancel_watch_ids=["watch-1"],
        next_wake_event="session_open",
    )

    resolved = daemon._resolve_position_aware_decision(decision, 0.0)

    assert resolved.action == "HOLD"
    assert resolved.quantity == 0.0
    assert resolved.intent == "HOLD"
    assert resolved.rationale == reason
    assert resolved.llm_provider == "acpx"
    assert resolved.llm_model == "gpt-5.5/medium"
    assert resolved.thesis == decision.thesis
    assert resolved.domain_tools == decision.domain_tools
    assert resolved.cancel_watch_ids == ["watch-1"]
    assert resolved.next_wake_event == "session_open"


def test_run_cycle_clamp_close_trop_grand_pour_ne_pas_reverser(monkeypatch, tmp_path, patch_batch, make_data_source) -> None:
    _write_runtime_config(tmp_path)
    state_dir = tmp_path / "state"
    now = datetime(2026, 6, 5, 14, 30, tzinfo=timezone.utc)
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
    first_now = datetime(2026, 6, 5, 14, 30, tzinfo=timezone.utc)
    second_now = datetime(2026, 6, 5, 14, 40, tzinfo=timezone.utc)
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
            confidence=0.95,
            rationale="setup tp",
            intent="OPEN_LONG",
            exit_plan={
                "hard_stop": {"type": "price", "price": 95.0},
                "take_profits": [{"name": "tp1", "price": 105.0, "fraction": 0.5}],
            },
            llm_provider="acpx",
            llm_model="gpt-5.5/medium",
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
    assert rows[1]["llm_provider"] == "acpx"
    assert rows[1]["llm_model"] == "gpt-5.5/medium"
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
    _mocked_now = datetime(2026, 6, 17, 15, 0, tzinfo=timezone.utc)
    fresh_ts = _mocked_now.isoformat()
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
        daemon.main(
            ["--live", "--poll", "0.01"],
            now_fn=lambda: _mocked_now,
        )

    history_rows = [
        json.loads(line)
        for line in (state_dir / "history.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert history_rows[-1]["cash"] == 99_529.65
    assert history_rows[-1]["equity"] == 100_059.65
    assert history_rows[-1]["n_executed"] == 1


def test_run_cycle_rejette_un_exit_plan_invalide_avant_fill(monkeypatch, tmp_path, patch_batch, make_data_source) -> None:
    _write_runtime_config(tmp_path)
    state_dir = tmp_path / "state"
    now = datetime(2026, 6, 5, 14, 30, tzinfo=timezone.utc)

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
    now = datetime(2026, 6, 5, 14, 30, tzinfo=timezone.utc)
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
    now = datetime(2026, 6, 5, 14, 30, tzinfo=timezone.utc)
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
            quantity=10.0,
            confidence=0.8,
            rationale="reverse",
            intent="REVERSE",
            exit_plan={"hard_stop": {"type": "price", "price": 105.0}}),
    )

    report = daemon.run_cycle(
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
    # §13.7 — le chemin REVERSE enrichit aussi le contexte d'entrée.
    assert plans[0].entry_thesis == "reverse"
    assert plans[0].entry_context is not None
    assert plans[0].entry_context["price"] == 102.0
    decision = report["decisions"][0]
    assert decision["risk_clamped"] is False
    assert decision["risk_unbounded_no_stop"] is False
    assert decision["stop_distance"] == pytest.approx(3.0)
    assert decision["risk_pct"] == pytest.approx(10.0 * 3.0 / report["portfolio"]["equity"])


def test_run_cycle_reduce_resynchronise_le_plan_sur_position_restante(monkeypatch, tmp_path, patch_batch, make_data_source) -> None:
    _write_runtime_config(tmp_path)
    state_dir = tmp_path / "state"
    now = datetime(2026, 6, 5, 14, 30, tzinfo=timezone.utc)
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


def test_run_cycle_add_resynchronise_le_plan_sur_position_totale(monkeypatch, tmp_path, patch_batch, make_data_source) -> None:
    _write_runtime_config(tmp_path)
    state_dir = tmp_path / "state"
    now = datetime(2026, 6, 5, 14, 30, tzinfo=timezone.utc)
    broker = SimBroker(state_dir / "broker.json", starting_cash=100_000)
    broker.submit(Order("SPY", "BUY", 10.0), 100.0, "2026-06-05T11:00:00+00:00", dry_run=False)
    last_review = {
        "reviewed_at": "2026-06-05T14:00:00+00:00",
        "action": "HOLD",
        "rationale": "thesis intacte",
    }
    TradePlanStore(state_dir / "trade_plans.json").upsert(
        replace(
            create_trade_plan(
                symbol="SPY",
                side="LONG",
                quantity=10.0,
                entry_price=100.0,
                opened_at="2026-06-05T11:00:00+00:00",
                raw_exit_plan={"hard_stop": {"type": "price", "price": 94.0}},
            ),
            last_llm_review=last_review,
        )
    )

    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    data_source = make_data_source(lambda symbol, lookback, interval: [
        Bar(ts=now.isoformat(), open=102.0, high=103.0, low=101.0, close=102.0, volume=1000.0)
    ])
    patch_batch(lambda **kwargs: Decision(
            symbol="SPY",
            action="HOLD",
            quantity=5.0,
            confidence=0.9,
            rationale="add with tighter plan",
            intent="ADD",
            resolve_from_position=True,
            exit_plan={"hard_stop": {"type": "price", "price": 97.0}}),
    )

    report = daemon.run_cycle(
        dry_run=False,
        now=now,
        symbols_filter=["SPY"],
        sched=Scheduler(state_dir / "scheduler.json"),
        data_source=data_source,
    )

    decision = report["decisions"][0]
    pos = SimBroker(state_dir / "broker.json").positions()["SPY"]
    plans = TradePlanStore(state_dir / "trade_plans.json").open_plans()
    assert decision["reason"] == "ok"
    assert decision["trade_plan_created"] is True
    assert decision["post_entry_review_scheduled"] is True
    assert pos.quantity == 15.0
    assert len(plans) == 1
    assert plans[0].side == "LONG"
    assert plans[0].remaining_quantity == 15.0
    assert plans[0].entry_price == pytest.approx((10.0 * 100.0 + 5.0 * 102.0) / 15.0)
    assert plans[0].hard_stop_price == pytest.approx(97.0)
    assert plans[0].last_llm_review == last_review


def test_run_cycle_rejette_un_ordre_non_hold_sans_intent(monkeypatch, tmp_path, patch_batch, make_data_source) -> None:
    _write_runtime_config(tmp_path)
    state_dir = tmp_path / "state"
    now = datetime(2026, 6, 5, 14, 30, tzinfo=timezone.utc)

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


# ── Chantier B : persistance des trade plans ─────────────────────────────────


def test_run_cycle_decision_contient_plan_complet_apres_ouverture(
    monkeypatch,
    tmp_path,
    patch_batch,
    make_data_source,
) -> None:
    """À la création d'un plan, l'entrée decisions.jsonl contient le plan complet."""
    _write_runtime_config(tmp_path)
    state_dir = tmp_path / "state"
    now = datetime(2026, 6, 5, 14, 30, tzinfo=timezone.utc)

    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    data_source = make_data_source(lambda symbol, lookback, interval: [
        Bar(ts=now.isoformat(), open=100.0, high=101.0, low=99.0, close=100.0, volume=1000.0)
    ])
    patch_batch(lambda **kwargs: Decision(
        symbol="SPY",
        action="BUY",
        quantity=10.0,
        confidence=0.85,
        rationale="setup",
        intent="OPEN_LONG",
        exit_plan={
            "hard_stop": {"type": "price", "price": 95.0},
            "take_profits": [{"name": "tp1", "price": 105.0, "fraction": 0.5}],
        },
    ))

    report = daemon.run_cycle(
        dry_run=False,
        now=now,
        symbols_filter=["SPY"],
        sched=Scheduler(state_dir / "scheduler.json"),
        data_source=data_source,
    )

    decision = report["decisions"][0]
    assert decision["executed"] is True
    assert decision["trade_plan_created"] is True
    # Le plan complet doit être présent dans la décision
    assert "trade_plan" in decision
    trade_plan = decision["trade_plan"]
    assert trade_plan["hard_stop_price"] == pytest.approx(95.0)
    assert trade_plan["entry_price"] == pytest.approx(100.0)
    assert trade_plan["side"] == "LONG"
    assert trade_plan["quantity"] == pytest.approx(10.0)
    assert len(trade_plan["take_profits"]) == 1
    assert trade_plan["take_profits"][0]["price"] == pytest.approx(105.0)


def test_run_cycle_planned_exit_hard_stop_contient_niveaux_et_fill(
    monkeypatch,
    tmp_path,
    patch_batch,
    make_data_source,
) -> None:
    """À l'exécution d'un exit hard_stop, planned_exits contient niveaux + fill_price."""
    _write_runtime_config(tmp_path)
    state_dir = tmp_path / "state"
    now = datetime(2026, 6, 9, 16, 48, tzinfo=timezone.utc)
    broker = SimBroker(state_dir / "broker.json", starting_cash=100_000)
    # Simulation du short CL=F du post-mortem
    broker.submit(Order("SPY", "SELL", 10.0), 86.71, "2026-06-09T16:09:45+00:00", dry_run=False)
    TradePlanStore(state_dir / "trade_plans.json").upsert(
        create_trade_plan(
            symbol="SPY",
            side="SHORT",
            quantity=10.0,
            entry_price=86.71,
            opened_at="2026-06-09T16:09:45+00:00",
            raw_exit_plan={
                "hard_stop": 87.30,
                "take_profits": [{"name": "tp1", "price": 83.0, "fraction": 0.5}],
            },
        )
    )

    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    # bar_high = 89.49 > stop 87.30, price close = 87.97 > stop → fill = 87.97
    data_source = make_data_source(lambda symbol, lookback, interval: [
        Bar(ts=now.isoformat(), open=86.71, high=89.49, low=86.50, close=87.97, volume=5000.0)
    ])
    patch_batch(lambda **kwargs: Decision.hold(kwargs["symbol"], "hold"))

    report = daemon.run_cycle(
        dry_run=False,
        now=now,
        symbols_filter=[],
        sched=Scheduler(state_dir / "scheduler.json"),
        data_source=data_source,
    )

    assert len(report["planned_exits"]) == 1
    exit_entry = report["planned_exits"][0]
    assert exit_entry["reason"] == "hard_stop"
    assert exit_entry["executed"] is True
    # Le fill_price doit être présent dans le rapport
    assert "fill_price" in exit_entry
    assert exit_entry["fill_price"] == pytest.approx(87.97)  # price > stop → fill = price
    # Les niveaux du plan doivent être présents
    assert "plan_snapshot" in exit_entry
    snap = exit_entry["plan_snapshot"]
    assert snap["hard_stop_price"] == pytest.approx(87.30)
    assert snap["entry_price"] == pytest.approx(86.71)
    assert snap["side"] == "SHORT"


def test_run_cycle_planned_exit_fill_price_est_stop_quand_spike_revenu(
    monkeypatch,
    tmp_path,
    patch_batch,
    make_data_source,
) -> None:
    """Cas spike-revenu : bar_high traverse le stop mais price close est revenu sous le stop.
    Fill doit être au stop (conservateur), pas au prix close."""
    _write_runtime_config(tmp_path)
    state_dir = tmp_path / "state"
    now = datetime(2026, 6, 9, 16, 48, tzinfo=timezone.utc)
    broker = SimBroker(state_dir / "broker.json", starting_cash=100_000)
    broker.submit(Order("SPY", "SELL", 10.0), 86.71, "2026-06-09T16:09:45+00:00", dry_run=False)
    TradePlanStore(state_dir / "trade_plans.json").upsert(
        create_trade_plan(
            symbol="SPY",
            side="SHORT",
            quantity=10.0,
            entry_price=86.71,
            opened_at="2026-06-09T16:09:45+00:00",
            raw_exit_plan={"hard_stop": 87.30},
        )
    )

    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    # Spike : bar_high 89.49 traverse le stop, mais price close 86.50 revenu sous le stop
    data_source = make_data_source(lambda symbol, lookback, interval: [
        Bar(ts=now.isoformat(), open=86.71, high=89.49, low=85.50, close=86.50, volume=5000.0)
    ])
    patch_batch(lambda **kwargs: Decision.hold(kwargs["symbol"], "hold"))

    report = daemon.run_cycle(
        dry_run=False,
        now=now,
        symbols_filter=[],
        sched=Scheduler(state_dir / "scheduler.json"),
        data_source=data_source,
    )

    assert len(report["planned_exits"]) == 1
    exit_entry = report["planned_exits"][0]
    assert exit_entry["reason"] == "hard_stop"
    # fill_price = stop (car price < stop → conservateur)
    assert exit_entry["fill_price"] == pytest.approx(87.30)


# ── Review Codex — corrections 2 et 5 ────────────────────────────────────────


class TestGardeTemporelleBarre:
    """MAJOR 2 — les extrêmes d'une barre ne doivent s'appliquer que si la barre
    a démarré APRÈS l'ouverture du plan (bar.ts >= plan.opened_at).
    Sinon : un plan ouvert juste après un spike serait stoppé à tort."""

    def test_barre_anterieure_a_ouverture_nexclut_pas_le_stop_au_prix(
        self, monkeypatch, tmp_path, patch_batch, make_data_source
    ) -> None:
        """La barre est antérieure au plan → les extrêmes ne doivent PAS déclencher
        le stop, mais price seul doit encore fonctionner."""
        _write_runtime_config(tmp_path)
        state_dir = tmp_path / "state"
        now = datetime(2026, 6, 9, 17, 10, tzinfo=timezone.utc)
        # Plan ouvert à 17:05 ; barre ts 17:00 = antérieure
        plan_opened_at = "2026-06-09T17:05:00+00:00"
        broker = SimBroker(state_dir / "broker.json", starting_cash=100_000)
        broker.submit(Order("SPY", "SELL", 10.0), 86.71, plan_opened_at, dry_run=False)
        TradePlanStore(state_dir / "trade_plans.json").upsert(
            create_trade_plan(
                symbol="SPY",
                side="SHORT",
                quantity=10.0,
                entry_price=86.71,
                opened_at=plan_opened_at,
                raw_exit_plan={"hard_stop": 87.30},
            )
        )

        monkeypatch.setattr(daemon, "ROOT", tmp_path)
        monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
        # Barre ts 17:00 (fraîche, 10 min avant now) mais antérieure au plan (17:05).
        # bar_high 89.49 > stop 87.30 ; price close 86.50 < stop.
        # Garde temporelle doit empêcher l'application des extrêmes → aucun exit.
        data_source = make_data_source(lambda symbol, lookback, interval: [
            Bar(
                ts="2026-06-09T17:00:00+00:00",  # antérieure à plan_opened_at 17:05
                open=86.71, high=89.49, low=85.50, close=86.50, volume=5000.0,
            )
        ])
        patch_batch(lambda **kwargs: Decision.hold(kwargs["symbol"], "hold"))

        report = daemon.run_cycle(
            dry_run=False,
            now=now,
            symbols_filter=[],
            sched=Scheduler(state_dir / "scheduler.json"),
            data_source=data_source,
        )

        # Aucun exit : price < stop, et barre antérieure donc extrêmes ignorés
        assert report["planned_exits"] == []

    def test_barre_anterieure_nexempte_pas_stop_au_prix(
        self, monkeypatch, tmp_path, patch_batch, make_data_source
    ) -> None:
        """Même barre antérieure, mais price seul dépasse le stop → déclenchement normal."""
        _write_runtime_config(tmp_path)
        state_dir = tmp_path / "state"
        now = datetime(2026, 6, 9, 17, 10, tzinfo=timezone.utc)
        plan_opened_at = "2026-06-09T17:05:00+00:00"
        broker = SimBroker(state_dir / "broker.json", starting_cash=100_000)
        broker.submit(Order("SPY", "SELL", 10.0), 86.71, plan_opened_at, dry_run=False)
        TradePlanStore(state_dir / "trade_plans.json").upsert(
            create_trade_plan(
                symbol="SPY",
                side="SHORT",
                quantity=10.0,
                entry_price=86.71,
                opened_at=plan_opened_at,
                raw_exit_plan={"hard_stop": 87.30},
            )
        )

        monkeypatch.setattr(daemon, "ROOT", tmp_path)
        monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
        # Même barre antérieure (17:00 < 17:05), mais price close 87.60 > stop 87.30
        # → déclenchement au prix, fill = price (pas de barre extrêmes)
        data_source = make_data_source(lambda symbol, lookback, interval: [
            Bar(
                ts="2026-06-09T17:00:00+00:00",
                open=86.71, high=89.49, low=85.50, close=87.60, volume=5000.0,
            )
        ])
        patch_batch(lambda **kwargs: Decision.hold(kwargs["symbol"], "hold"))

        report = daemon.run_cycle(
            dry_run=False,
            now=now,
            symbols_filter=[],
            sched=Scheduler(state_dir / "scheduler.json"),
            data_source=data_source,
        )

        assert len(report["planned_exits"]) == 1
        assert report["planned_exits"][0]["reason"] == "hard_stop"

    def test_barre_posterieure_declenche_normalement(
        self, monkeypatch, tmp_path, patch_batch, make_data_source
    ) -> None:
        """Barre postérieure au plan → les extrêmes s'appliquent normalement."""
        _write_runtime_config(tmp_path)
        state_dir = tmp_path / "state"
        now = datetime(2026, 6, 9, 17, 10, tzinfo=timezone.utc)
        plan_opened_at = "2026-06-09T17:00:00+00:00"
        broker = SimBroker(state_dir / "broker.json", starting_cash=100_000)
        broker.submit(Order("SPY", "SELL", 10.0), 86.71, plan_opened_at, dry_run=False)
        TradePlanStore(state_dir / "trade_plans.json").upsert(
            create_trade_plan(
                symbol="SPY",
                side="SHORT",
                quantity=10.0,
                entry_price=86.71,
                opened_at=plan_opened_at,
                raw_exit_plan={"hard_stop": 87.30},
            )
        )

        monkeypatch.setattr(daemon, "ROOT", tmp_path)
        monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
        # Barre ts 17:05 postérieure au plan (17:00) → extrêmes appliqués
        # bar_high 89.49 > stop 87.30, price close 86.50 < stop → spike détecté, fill = stop
        data_source = make_data_source(lambda symbol, lookback, interval: [
            Bar(
                ts="2026-06-09T17:05:00+00:00",
                open=86.71, high=89.49, low=85.50, close=86.50, volume=5000.0,
            )
        ])
        patch_batch(lambda **kwargs: Decision.hold(kwargs["symbol"], "hold"))

        report = daemon.run_cycle(
            dry_run=False,
            now=now,
            symbols_filter=[],
            sched=Scheduler(state_dir / "scheduler.json"),
            data_source=data_source,
        )

        assert len(report["planned_exits"]) == 1
        assert report["planned_exits"][0]["reason"] == "hard_stop"
        assert report["planned_exits"][0]["fill_price"] == pytest.approx(87.30)


class TestCheminBloqueObservabilite:
    """MINOR 5 — chemins bloqués (clamp/position manquante) doivent inclure
    fill_price et plan_snapshot pour l'uniformité de l'observabilité."""

    def test_chemin_bloque_inclut_fill_price_et_plan_snapshot(
        self, monkeypatch, tmp_path, patch_batch, make_data_source
    ) -> None:
        """Exit bloqué (position broker insuffisante) doit tout de même exposer
        fill_price et plan_snapshot dans l'entrée du rapport."""
        _write_runtime_config(tmp_path)
        state_dir = tmp_path / "state"
        now = datetime(2026, 6, 5, 12, 10, tzinfo=timezone.utc)
        SimBroker(state_dir / "broker.json", starting_cash=100_000)
        # Position broker = 0 → _clamp_exit_quantity retourne "no_position_to_reduce"
        # (pas de submit de position initiale volontairement : SimBroker initialise broker.json)
        TradePlanStore(state_dir / "trade_plans.json").upsert(
            create_trade_plan(
                symbol="SPY",
                side="LONG",
                quantity=10.0,
                entry_price=100.0,
                opened_at="2026-06-05T12:00:00+00:00",
                raw_exit_plan={
                    "hard_stop": 95.0,
                    "take_profits": [{"name": "tp1", "price": 105.0, "fraction": 0.5}],
                },
            )
        )

        monkeypatch.setattr(daemon, "ROOT", tmp_path)
        monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
        data_source = make_data_source(lambda symbol, lookback, interval: [
            Bar(ts=now.isoformat(), open=106.0, high=107.0, low=105.0, close=106.0, volume=1000.0)
        ])
        patch_batch(lambda **kwargs: Decision.hold(kwargs["symbol"], "hold"))

        report = daemon.run_cycle(
            dry_run=False,
            now=now,
            symbols_filter=[],
            sched=Scheduler(state_dir / "scheduler.json"),
            data_source=data_source,
        )

        assert len(report["planned_exits"]) == 1
        exit_entry = report["planned_exits"][0]
        assert exit_entry["executed"] is False
        # fill_price et plan_snapshot doivent être présents même sur chemin bloqué
        assert "fill_price" in exit_entry
        assert "plan_snapshot" in exit_entry
        snap = exit_entry["plan_snapshot"]
        assert snap["hard_stop_price"] == pytest.approx(95.0)
        assert snap["side"] == "LONG"


class TestExitChecks5mBars:
    """Checks de sortie affinés sur barres 5m pour les symboles avec plan ouvert."""

    def _setup_state(self, tmp_path, state_dir, *, opened_at: str, symbol: str = "SPY") -> None:
        from trader.execution.broker import Order, SimBroker
        from trader.planning.trade_plan import TradePlanStore, create_trade_plan
        broker = SimBroker(state_dir / "broker.json", starting_cash=100_000)
        broker.submit(Order(symbol, "BUY", 10.0), 100.0, opened_at, dry_run=False)
        TradePlanStore(state_dir / "trade_plans.json").upsert(
            create_trade_plan(
                symbol=symbol,
                side="LONG",
                quantity=10.0,
                entry_price=100.0,
                opened_at=opened_at,
                raw_exit_plan={"hard_stop": 95.0},
            )
        )

    def test_5m_fetch_uniquement_sur_symbole_avec_plan(
        self, monkeypatch, tmp_path, patch_batch, make_data_source
    ) -> None:
        """get_bars doit être appelé avec interval='5m' pour le symbole avec plan,
        et PAS avec interval='5m' pour un symbole sans plan."""
        _write_runtime_config(tmp_path, symbols=["SPY", "QQQ"])
        state_dir = tmp_path / "state"
        now = datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)
        opened_at = "2026-06-10T11:00:00+00:00"
        self._setup_state(tmp_path, state_dir, opened_at=opened_at, symbol="SPY")
        # QQQ n'a pas de plan → pas de fetch 5m attendu pour QQQ

        monkeypatch.setattr(daemon, "ROOT", tmp_path)
        monkeypatch.setattr(daemon, "STATE_DIR", state_dir)

        calls: list[tuple[str, str, str]] = []

        def get_bars_spy(symbol, lookback, interval):
            calls.append((symbol, lookback, interval))
            return [Bar(ts=now.isoformat(), open=98.0, high=99.0, low=97.5, close=98.0, volume=500.0)]

        data_source = make_data_source(get_bars_spy)
        patch_batch(lambda **kwargs: Decision.hold(kwargs["symbol"], "hold"))

        daemon.run_cycle(
            dry_run=True,
            now=now,
            symbols_filter=["SPY", "QQQ"],
            sched=Scheduler(state_dir / "scheduler.json"),
            data_source=data_source,
        )

        five_m_calls = [(sym, iv) for sym, _lk, iv in calls if iv == "5m"]
        assert ("SPY", "5m") in five_m_calls, "SPY (avec plan) doit être fetché en 5m"
        assert ("QQQ", "5m") not in five_m_calls, "QQQ (sans plan) ne doit PAS être fetché en 5m"

    def test_spike_5m_declenche_le_stop(
        self, monkeypatch, tmp_path, patch_batch, make_data_source
    ) -> None:
        """Un spike visible sur la barre 5m (bar_low <= stop) mais invisible en 15m
        doit déclencher le stop grâce aux barres 5m."""
        _write_runtime_config(tmp_path)
        state_dir = tmp_path / "state"
        # Plan LONG SPY, stop à 95.0
        # Barre 15m low=96 → pas de stop
        # Barre 5m low=94.5 ≤ 95 → stop déclenché
        opened_at = "2026-06-10T11:00:00+00:00"
        now = datetime(2026, 6, 10, 14, 30, tzinfo=timezone.utc)
        self._setup_state(tmp_path, state_dir, opened_at=opened_at)

        monkeypatch.setattr(daemon, "ROOT", tmp_path)
        monkeypatch.setattr(daemon, "STATE_DIR", state_dir)

        bar_15m = Bar(
            ts="2026-06-10T14:00:00+00:00",
            open=97.0, high=98.0, low=96.0, close=97.0, volume=1000.0,
        )
        bar_5m = Bar(
            ts="2026-06-10T14:20:00+00:00",
            open=96.0, high=96.5, low=94.5, close=95.5, volume=300.0,
        )

        def get_bars(symbol, lookback, interval):
            if interval == "5m":
                return [bar_5m]
            return [bar_15m]

        data_source = make_data_source(get_bars)
        patch_batch(lambda **kwargs: Decision.hold(kwargs["symbol"], "hold"))

        report = daemon.run_cycle(
            dry_run=False,
            now=now,
            symbols_filter=[],
            sched=Scheduler(state_dir / "scheduler.json"),
            data_source=data_source,
        )

        assert len(report["planned_exits"]) == 1, "Le stop doit être déclenché via la barre 5m"
        assert report["planned_exits"][0]["reason"] == "hard_stop"

    def test_fallback_15m_si_fetch_5m_echoue(
        self, monkeypatch, tmp_path, patch_batch, make_data_source
    ) -> None:
        """Si le fetch 5m lève une exception, le daemon doit tomber en fallback
        sur les barres 15m et ne pas bloquer l'évaluation des exits.

        Le 15m fournit low=96 > stop=95 → PAS de stop.
        Le 5m aurait fourni low=94.5 ≤ 95 → stop déclenché.
        Mais ici le 5m échoue, donc fallback 15m → PAS de stop (planned_exits vide).

        Ce test vérifie que :
        1. L'exception 5m est absorbée (pas de crash du daemon).
        2. Les barres 15m sont utilisées en fallback.
        3. Le comportement est cohérent avec les barres 15m (pas de stop ici).
        """
        _write_runtime_config(tmp_path)
        state_dir = tmp_path / "state"
        opened_at = "2026-06-10T11:00:00+00:00"
        now = datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)
        self._setup_state(tmp_path, state_dir, opened_at=opened_at)

        monkeypatch.setattr(daemon, "ROOT", tmp_path)
        monkeypatch.setattr(daemon, "STATE_DIR", state_dir)

        # 15m : low=96 > stop=95 → pas de stop déclenché
        bar_15m = Bar(
            ts="2026-06-10T11:45:00+00:00",
            open=97.0, high=98.0, low=96.0, close=97.0, volume=1000.0,
        )

        fetch_5m_attempted = []

        def get_bars(symbol, lookback, interval):
            if interval == "5m":
                fetch_5m_attempted.append(symbol)
                raise RuntimeError("5m indispo simulé — erreur arbitraire")
            return [bar_15m]

        data_source = make_data_source(get_bars)
        patch_batch(lambda **kwargs: Decision.hold(kwargs["symbol"], "hold"))

        # Ne doit pas lever d'exception
        report = daemon.run_cycle(
            dry_run=False,
            now=now,
            symbols_filter=[],
            sched=Scheduler(state_dir / "scheduler.json"),
            data_source=data_source,
        )

        # Le fetch 5m a été tenté pour SPY (symbole avec plan)
        assert "SPY" in fetch_5m_attempted, "Le fetch 5m doit avoir été tenté"
        # Fallback 15m : low=96 > stop=95 → pas de stop (comportement 15m)
        assert report["planned_exits"] == [], "Fallback 15m : low=96 > stop=95, pas de stop"

    def test_bars_interval_dans_rapport_selon_chemin(
        self, monkeypatch, tmp_path, patch_batch, make_data_source
    ) -> None:
        """planned_exits[*].bars_interval = '5m' quand les barres 5m ont servi,
        '15m' quand le fallback a été pris."""
        _write_runtime_config(tmp_path, symbols=["SPY", "QQQ"])
        state_dir = tmp_path / "state"
        opened_at = "2026-06-10T11:00:00+00:00"
        now = datetime(2026, 6, 10, 14, 30, tzinfo=timezone.utc)

        # SPY : plan LONG, stop 95, barre 5m low=94.5 → déclenché, interval='5m'
        from trader.execution.broker import Order, SimBroker
        from trader.planning.trade_plan import TradePlanStore, create_trade_plan
        broker = SimBroker(state_dir / "broker.json", starting_cash=100_000)
        broker.submit(Order("SPY", "BUY", 10.0), 100.0, opened_at, dry_run=False)
        broker.submit(Order("QQQ", "BUY", 5.0), 200.0, opened_at, dry_run=False)
        store = TradePlanStore(state_dir / "trade_plans.json")
        store.upsert(create_trade_plan(
            symbol="SPY", side="LONG", quantity=10.0, entry_price=100.0,
            opened_at=opened_at, raw_exit_plan={"hard_stop": 95.0},
        ))
        # QQQ : plan LONG, stop 180, fetch 5m échoue → fallback 15m, barre 15m low=178 → déclenché
        store.upsert(create_trade_plan(
            symbol="QQQ", side="LONG", quantity=5.0, entry_price=200.0,
            opened_at=opened_at, raw_exit_plan={"hard_stop": 180.0},
        ))

        monkeypatch.setattr(daemon, "ROOT", tmp_path)
        monkeypatch.setattr(daemon, "STATE_DIR", state_dir)

        def get_bars(symbol, lookback, interval):
            if interval == "5m":
                if symbol == "SPY":
                    return [Bar(
                        ts="2026-06-10T14:20:00+00:00",
                        open=96.0, high=96.5, low=94.5, close=95.5, volume=300.0,
                    )]
                # QQQ : échec 5m
                raise RuntimeError("QQQ 5m indispo")
            # 15m
            if symbol == "SPY":
                return [Bar(
                    ts="2026-06-10T14:00:00+00:00",
                    open=97.0, high=98.0, low=96.0, close=97.0, volume=1000.0,
                )]
            if symbol == "QQQ":
                return [Bar(
                    ts="2026-06-10T14:00:00+00:00",
                    open=185.0, high=186.0, low=178.0, close=182.0, volume=800.0,
                )]
            return []

        data_source = make_data_source(get_bars)
        patch_batch(lambda **kwargs: Decision.hold(kwargs["symbol"], "hold"))

        report = daemon.run_cycle(
            dry_run=False,
            now=now,
            symbols_filter=[],
            sched=Scheduler(state_dir / "scheduler.json"),
            data_source=data_source,
        )

        exits_by_symbol = {e["symbol"]: e for e in report["planned_exits"]}
        assert exits_by_symbol["SPY"]["bars_interval"] == "5m", "SPY : 5m utilisé"
        assert exits_by_symbol["QQQ"]["bars_interval"] == "15m", "QQQ : fallback 15m"


class TestExitChecks5mValidation:
    """MAJOR 1 — Validation des barres 5m avant substitution."""

    def _setup_long_spy(self, state_dir, *, opened_at: str) -> None:
        from trader.execution.broker import Order, SimBroker
        from trader.planning.trade_plan import TradePlanStore, create_trade_plan
        broker = SimBroker(state_dir / "broker.json", starting_cash=100_000)
        broker.submit(Order("SPY", "BUY", 10.0), 100.0, opened_at, dry_run=False)
        TradePlanStore(state_dir / "trade_plans.json").upsert(
            create_trade_plan(
                symbol="SPY", side="LONG", quantity=10.0, entry_price=100.0,
                opened_at=opened_at, raw_exit_plan={"hard_stop": 95.0},
            )
        )

    def test_ts_imparsable_5m_utilise_fallback_15m(
        self, monkeypatch, tmp_path, patch_batch, make_data_source
    ) -> None:
        """Une barre 5m avec ts imparsable → fallback sur les barres 15m (bars_interval='15m')."""
        _write_runtime_config(tmp_path)
        state_dir = tmp_path / "state"
        opened_at = "2026-06-10T11:00:00+00:00"
        now = datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)
        self._setup_long_spy(state_dir, opened_at=opened_at)

        monkeypatch.setattr(daemon, "ROOT", tmp_path)
        monkeypatch.setattr(daemon, "STATE_DIR", state_dir)

        # Barre 5m avec ts invalide ; high/low = valeurs qui déclenche le stop si utilisées
        bar_5m_invalid_ts = Bar(ts="NOT_A_DATE", open=94.0, high=96.0, low=94.0, close=94.5, volume=100.0)
        # Barre 15m saine avec low=96 > stop=95 → pas de stop
        bar_15m = Bar(ts="2026-06-10T11:45:00+00:00", open=97.0, high=98.0, low=96.0, close=97.0, volume=1000.0)

        def get_bars(symbol, lookback, interval):
            if interval == "5m":
                return [bar_5m_invalid_ts]
            return [bar_15m]

        data_source = make_data_source(get_bars)
        patch_batch(lambda **kwargs: Decision.hold(kwargs["symbol"], "hold"))

        report = daemon.run_cycle(
            dry_run=False,
            now=now,
            symbols_filter=[],
            sched=Scheduler(state_dir / "scheduler.json"),
            data_source=data_source,
        )

        # Pas de stop déclenché (fallback 15m, low=96 > 95)
        assert report["planned_exits"] == [], "ts imparsable → fallback 15m, pas de stop"

    def test_high_manquant_5m_pas_de_crash_fallback_15m(
        self, monkeypatch, tmp_path, patch_batch, make_data_source
    ) -> None:
        """Une barre 5m avec high non-fini (float('nan')) → fallback 15m, pas de crash."""
        _write_runtime_config(tmp_path)
        state_dir = tmp_path / "state"
        opened_at = "2026-06-10T11:00:00+00:00"
        now = datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)
        self._setup_long_spy(state_dir, opened_at=opened_at)

        monkeypatch.setattr(daemon, "ROOT", tmp_path)
        monkeypatch.setattr(daemon, "STATE_DIR", state_dir)

        # Barre 5m avec high=NaN ; low=94 déclencherait le stop si utilisée
        bar_5m_nan_high = Bar(
            ts="2026-06-10T11:55:00+00:00",
            open=94.0, high=float("nan"), low=94.0, close=94.5, volume=100.0,
        )
        # Barre 15m saine low=96 > stop=95 → pas de stop
        bar_15m = Bar(ts="2026-06-10T11:45:00+00:00", open=97.0, high=98.0, low=96.0, close=97.0, volume=1000.0)

        def get_bars(symbol, lookback, interval):
            if interval == "5m":
                return [bar_5m_nan_high]
            return [bar_15m]

        data_source = make_data_source(get_bars)
        patch_batch(lambda **kwargs: Decision.hold(kwargs["symbol"], "hold"))

        # Ne doit pas crasher
        report = daemon.run_cycle(
            dry_run=False,
            now=now,
            symbols_filter=[],
            sched=Scheduler(state_dir / "scheduler.json"),
            data_source=data_source,
        )

        # Fallback 15m : pas de stop
        assert report["planned_exits"] == [], "high NaN → fallback 15m, pas de stop"


class TestExitChecks5mFreshness:
    """MAJOR 2 — Fraîcheur des barres 5m avant substitution."""

    def _setup_long_spy(self, state_dir, *, opened_at: str) -> None:
        from trader.execution.broker import Order, SimBroker
        from trader.planning.trade_plan import TradePlanStore, create_trade_plan
        broker = SimBroker(state_dir / "broker.json", starting_cash=100_000)
        broker.submit(Order("SPY", "BUY", 10.0), 100.0, opened_at, dry_run=False)
        TradePlanStore(state_dir / "trade_plans.json").upsert(
            create_trade_plan(
                symbol="SPY", side="LONG", quantity=10.0, entry_price=100.0,
                opened_at=opened_at, raw_exit_plan={"hard_stop": 95.0},
            )
        )

    def test_barres_5m_stale_fallback_vers_15m(
        self, monkeypatch, tmp_path, patch_batch, make_data_source
    ) -> None:
        """Des barres 5m périmées (trop vieilles) doivent déclencher le fallback 15m.
        bars_interval='15m' dans le rapport ; pas de stop si 15m ne le déclenche pas."""
        _write_runtime_config(tmp_path)
        state_dir = tmp_path / "state"
        opened_at = "2026-06-10T11:00:00+00:00"
        now = datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)
        self._setup_long_spy(state_dir, opened_at=opened_at)

        monkeypatch.setattr(daemon, "ROOT", tmp_path)
        monkeypatch.setattr(daemon, "STATE_DIR", state_dir)

        # Barre 5m avec ts 35 min avant now (> budget 5m = 5+15=20 min) mais APRÈS l'ouverture
        # du plan (11:00) → passe la garde temporelle MAIS doit être rejetée par fraîcheur
        bar_5m_stale = Bar(
            ts="2026-06-10T11:25:00+00:00",  # 35 min avant now=12:00 → stale pour 5m (budget=20 min)
            open=94.0, high=96.0, low=94.0, close=94.5, volume=100.0,
        )
        # Barre 15m fraîche avec low=96 > stop=95 → pas de stop
        bar_15m = Bar(ts="2026-06-10T11:45:00+00:00", open=97.0, high=98.0, low=96.0, close=97.0, volume=1000.0)

        def get_bars(symbol, lookback, interval):
            if interval == "5m":
                return [bar_5m_stale]
            return [bar_15m]

        data_source = make_data_source(get_bars)
        patch_batch(lambda **kwargs: Decision.hold(kwargs["symbol"], "hold"))

        report = daemon.run_cycle(
            dry_run=False,
            now=now,
            symbols_filter=[],
            sched=Scheduler(state_dir / "scheduler.json"),
            data_source=data_source,
        )

        # Fallback 15m : low=96 > stop=95, pas de stop déclenché
        assert report["planned_exits"] == [], "barres 5m stale → fallback 15m, pas de stop"

    def test_freshness_budget_5m_dans_market(self) -> None:
        """freshness_budget_minutes('5m') doit retourner 5+grace, pas 60+grace.
        Vérifie que '5m' est bien dans _INTERVAL_MINUTES de market.py."""
        from trader.market.market_data import freshness_budget_minutes, _FRESHNESS_GRACE_MINUTES
        budget = freshness_budget_minutes("5m")
        # 5m + 15 grace = 20 min — si 5m absent du dict, on obtiendrait 75 min (défaut 60+15)
        assert budget == 5.0 + _FRESHNESS_GRACE_MINUTES, (
            f"freshness_budget_minutes('5m') = {budget}, attendu {5.0 + _FRESHNESS_GRACE_MINUTES} ; "
            "'5m' doit être dans _INTERVAL_MINUTES"
        )


class TestExitChecks5mAggregation:
    """MAJOR 3 — Agrégation high/low sur fenêtre de barres 5m (pas seulement la dernière)."""

    def _setup_long_spy(self, state_dir, *, opened_at: str, stop: float = 95.0) -> None:
        from trader.execution.broker import Order, SimBroker
        from trader.planning.trade_plan import TradePlanStore, create_trade_plan
        broker = SimBroker(state_dir / "broker.json", starting_cash=100_000)
        broker.submit(Order("SPY", "BUY", 10.0), 100.0, opened_at, dry_run=False)
        TradePlanStore(state_dir / "trade_plans.json").upsert(
            create_trade_plan(
                symbol="SPY", side="LONG", quantity=10.0, entry_price=100.0,
                opened_at=opened_at, raw_exit_plan={"hard_stop": stop},
            )
        )

    def test_spike_dans_avant_derniere_5m_declenche_stop(
        self, monkeypatch, tmp_path, patch_batch, make_data_source
    ) -> None:
        """Un spike (low ≤ stop) dans une barre 5m antérieure à la dernière doit déclencher
        le stop — couvrir la fenêtre, pas seulement la dernière barre."""
        _write_runtime_config(tmp_path)
        state_dir = tmp_path / "state"
        opened_at = "2026-06-10T11:00:00+00:00"
        now = datetime(2026, 6, 10, 14, 30, tzinfo=timezone.utc)
        self._setup_long_spy(state_dir, opened_at=opened_at, stop=95.0)

        monkeypatch.setattr(daemon, "ROOT", tmp_path)
        monkeypatch.setattr(daemon, "STATE_DIR", state_dir)

        # Fenêtre de 3 barres 5m :
        # - barre 2 (avant-dernière) : low=94.5 ≤ stop=95 → spike DANS la fenêtre
        # - barre 3 (dernière) : low=96 > stop=95 → spike revenu
        bar_5m_1 = Bar(ts="2026-06-10T14:10:00+00:00", open=97.0, high=98.0, low=96.5, close=97.0, volume=300.0)
        bar_5m_2 = Bar(ts="2026-06-10T14:15:00+00:00", open=96.5, high=97.0, low=94.5, close=96.0, volume=300.0)
        bar_5m_3 = Bar(ts="2026-06-10T14:20:00+00:00", open=96.0, high=97.0, low=96.0, close=96.5, volume=300.0)

        def get_bars(symbol, lookback, interval):
            if interval == "5m":
                return [bar_5m_1, bar_5m_2, bar_5m_3]
            # 15m : low=96 > stop=95 → pas de stop sans agrégation
            return [Bar(ts="2026-06-10T14:00:00+00:00", open=97.0, high=98.0, low=96.0, close=96.5, volume=900.0)]

        data_source = make_data_source(get_bars)
        patch_batch(lambda **kwargs: Decision.hold(kwargs["symbol"], "hold"))

        report = daemon.run_cycle(
            dry_run=False,
            now=now,
            symbols_filter=[],
            sched=Scheduler(state_dir / "scheduler.json"),
            data_source=data_source,
        )

        assert len(report["planned_exits"]) == 1, (
            "spike dans l'avant-dernière barre 5m doit déclencher le stop via agrégation"
        )
        assert report["planned_exits"][0]["reason"] == "hard_stop"

    def test_spike_5m_anterieur_ouverture_plan_non_declenche(
        self, monkeypatch, tmp_path, patch_batch, make_data_source
    ) -> None:
        """Un spike (low ≤ stop) dans une barre 5m antérieure à l'ouverture du plan
        ne doit PAS déclencher le stop — la garde temporelle doit s'appliquer au filtre."""
        _write_runtime_config(tmp_path)
        state_dir = tmp_path / "state"
        # Plan ouvert à 11:52 ; barres 5m à 11:45 et 11:50 sont antérieures
        opened_at = "2026-06-10T11:52:00+00:00"
        now = datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)
        self._setup_long_spy(state_dir, opened_at=opened_at, stop=95.0)

        monkeypatch.setattr(daemon, "ROOT", tmp_path)
        monkeypatch.setattr(daemon, "STATE_DIR", state_dir)

        # Barre antérieure au plan avec spike low=94 (ne doit PAS être agrégée)
        bar_before_plan = Bar(ts="2026-06-10T11:45:00+00:00", open=94.5, high=96.0, low=94.0, close=95.5, volume=300.0)
        # Barre postérieure au plan sans spike
        bar_after_plan = Bar(ts="2026-06-10T11:55:00+00:00", open=96.0, high=97.0, low=96.0, close=96.5, volume=300.0)

        def get_bars(symbol, lookback, interval):
            if interval == "5m":
                return [bar_before_plan, bar_after_plan]
            return [Bar(ts="2026-06-10T11:45:00+00:00", open=97.0, high=98.0, low=96.0, close=96.5, volume=900.0)]

        data_source = make_data_source(get_bars)
        patch_batch(lambda **kwargs: Decision.hold(kwargs["symbol"], "hold"))

        report = daemon.run_cycle(
            dry_run=False,
            now=now,
            symbols_filter=[],
            sched=Scheduler(state_dir / "scheduler.json"),
            data_source=data_source,
        )

        # La barre antérieure est filtrée → pas de stop
        assert report["planned_exits"] == [], (
            "barre 5m antérieure à l'ouverture du plan doit être ignorée dans l'agrégation"
        )


class TestExitChecks5mMinor:
    """MINOR 4 — Tests complémentaires de robustesse des checks 5m."""

    def _setup_long_spy(self, state_dir, *, opened_at: str, stop: float = 95.0) -> None:
        from trader.execution.broker import Order, SimBroker
        from trader.planning.trade_plan import TradePlanStore, create_trade_plan
        broker = SimBroker(state_dir / "broker.json", starting_cash=100_000)
        broker.submit(Order("SPY", "BUY", 10.0), 100.0, opened_at, dry_run=False)
        TradePlanStore(state_dir / "trade_plans.json").upsert(
            create_trade_plan(
                symbol="SPY", side="LONG", quantity=10.0, entry_price=100.0,
                opened_at=opened_at, raw_exit_plan={"hard_stop": stop},
            )
        )

    def test_liste_5m_vide_sortie_15m_fonctionne(
        self, monkeypatch, tmp_path, patch_batch, make_data_source
    ) -> None:
        """Liste 5m vide (get_bars retourne []) → fallback 15m, sortie basée sur 15m."""
        _write_runtime_config(tmp_path)
        state_dir = tmp_path / "state"
        opened_at = "2026-06-10T11:00:00+00:00"
        now = datetime(2026, 6, 10, 14, 30, tzinfo=timezone.utc)
        self._setup_long_spy(state_dir, opened_at=opened_at, stop=95.0)

        monkeypatch.setattr(daemon, "ROOT", tmp_path)
        monkeypatch.setattr(daemon, "STATE_DIR", state_dir)

        # Barre 15m fraîche avec low=94 → stop déclenché
        bar_15m = Bar(ts="2026-06-10T14:00:00+00:00", open=97.0, high=98.0, low=94.0, close=96.0, volume=1000.0)

        def get_bars(symbol, lookback, interval):
            if interval == "5m":
                return []  # vide
            return [bar_15m]

        data_source = make_data_source(get_bars)
        patch_batch(lambda **kwargs: Decision.hold(kwargs["symbol"], "hold"))

        report = daemon.run_cycle(
            dry_run=False,
            now=now,
            symbols_filter=[],
            sched=Scheduler(state_dir / "scheduler.json"),
            data_source=data_source,
        )

        # Fallback 15m : low=94 ≤ stop=95 → stop déclenché
        assert len(report["planned_exits"]) == 1, "fallback 15m doit fonctionner avec liste 5m vide"
        assert report["planned_exits"][0]["reason"] == "hard_stop"
        assert report["planned_exits"][0]["bars_interval"] == "15m"

    def test_plan_ouvert_hors_tradable_prices_pas_de_fetch_ni_crash(
        self, monkeypatch, tmp_path, patch_batch, make_data_source
    ) -> None:
        """Un plan ouvert sur un symbole absent de tradable_prices (hors univers/stale)
        ne doit pas déclencher de fetch 5m ni provoquer de crash."""
        _write_runtime_config(tmp_path, symbols=["QQQ"])  # univers = QQQ seulement
        state_dir = tmp_path / "state"
        opened_at = "2026-06-10T11:00:00+00:00"
        now = datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)
        # Plan ouvert sur SPY, mais SPY n'est pas dans l'univers QQQ
        self._setup_long_spy(state_dir, opened_at=opened_at)  # SPY

        monkeypatch.setattr(daemon, "ROOT", tmp_path)
        monkeypatch.setattr(daemon, "STATE_DIR", state_dir)

        calls_5m: list[str] = []

        def get_bars(symbol, lookback, interval):
            if interval == "5m":
                calls_5m.append(symbol)
            return [Bar(ts=now.isoformat(), open=200.0, high=201.0, low=199.0, close=200.0, volume=1000.0)]

        data_source = make_data_source(get_bars)
        patch_batch(lambda **kwargs: Decision.hold(kwargs["symbol"], "hold"))

        # Ne doit pas crasher
        report = daemon.run_cycle(
            dry_run=False,
            now=now,
            symbols_filter=[],
            sched=Scheduler(state_dir / "scheduler.json"),
            data_source=data_source,
        )

        # SPY absent de tradable_prices → pas de fetch 5m pour SPY
        assert "SPY" not in calls_5m, "SPY hors tradable_prices → pas de fetch 5m"
        # Pas de crash ; le plan SPY n'est pas évalué (pas de prix)
        assert report["planned_exits"] == []


# ---------------------------------------------------------------------------
# Tests FX : _gross_exposure avec rate_of
# ---------------------------------------------------------------------------

def test_gross_exposure_fx_blind_sans_rate_of_retourne_natif(tmp_path) -> None:
    """Sans rate_of, _gross_exposure renvoie la somme native (comportement historique)."""
    state_dir = tmp_path / "state"
    broker = SimBroker(state_dir / "broker.json", starting_cash=100_000)
    broker.submit(Order("2379.TW", "BUY", 100.0), 800.0, "2026-06-24T09:00:00+00:00", dry_run=False)

    prices = {"2379.TW": 820.0}
    gross = daemon._gross_exposure(broker, prices)

    assert gross == pytest.approx(100.0 * 820.0, rel=1e-9)


def test_gross_exposure_fx_aware_twd_convergi_en_usd(tmp_path) -> None:
    """rate_of=0.031 pour TWD : _gross_exposure multiplie par le taux FX."""
    state_dir = tmp_path / "state"
    broker = SimBroker(state_dir / "broker.json", starting_cash=100_000)
    broker.submit(Order("2379.TW", "BUY", 100.0), 800.0, "2026-06-24T09:00:00+00:00", dry_run=False)

    prices = {"2379.TW": 820.0}
    twd_rate = 0.031

    gross_native = daemon._gross_exposure(broker, prices)
    gross_usd = daemon._gross_exposure(broker, prices, rate_of=lambda sym: twd_rate)

    assert gross_usd == pytest.approx(gross_native * twd_rate, rel=1e-9)
    # Sanity : la valeur USD doit être ~32× plus petite que la valeur native
    assert gross_usd < gross_native / 10


def test_gross_exposure_fx_multi_position_somme_en_usd(tmp_path) -> None:
    """Deux positions dans des devises différentes : chacune est converti séparément."""
    state_dir = tmp_path / "state"
    broker = SimBroker(state_dir / "broker.json", starting_cash=100_000)
    broker.submit(Order("AAPL", "BUY", 10.0), 200.0, "2026-06-24T09:00:00+00:00", dry_run=False)
    broker.submit(Order("2379.TW", "BUY", 50.0), 800.0, "2026-06-24T09:00:00+00:00", dry_run=False)

    prices = {"AAPL": 210.0, "2379.TW": 820.0}
    rates = {"AAPL": 1.0, "2379.TW": 0.031}

    gross = daemon._gross_exposure(broker, prices, rate_of=lambda sym: rates.get(sym, 1.0))

    expected = abs(10.0 * 210.0 * 1.0) + abs(50.0 * 820.0 * 0.031)
    assert gross == pytest.approx(expected, rel=1e-9)
