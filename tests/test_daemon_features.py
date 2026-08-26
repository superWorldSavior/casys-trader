import logging
import json
from datetime import datetime, timedelta, timezone

import pytest

from trader.runtime import daemon
from trader.runtime.worker_cycle_context import WorkerCycleContextHandle
from trader.agent.client import Decision
from trader.market.market_data import Bar, MarketError
from trader.planning.trade_plan import ProfitProtection, TakeProfit, TradePlan, TrailingStop
from trader.planning.scheduler import Scheduler
from tests.conftest import write_runtime_config as _write_runtime_config


def _seed_trade_plan(state_dir, plan: TradePlan) -> None:
    path = state_dir / "trade_plans.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"plans": [plan.model_dump()]}, indent=2), encoding="utf-8")


def test_global_plans_summary_rend_un_resume_compact_global(tmp_path) -> None:
    now = datetime(2026, 6, 5, 12, 0, tzinfo=timezone.utc)
    sched = Scheduler(tmp_path / "scheduler.json")
    sched.set_symbol_indicator_watch(
        "SPY",
        {
            "id": "armed-spy",
            "symbol": "SPY",
            "on_trigger": "EXECUTE_ORDER",
            "order": {"intent": "OPEN_LONG"},
            "conditions": [{"indicator": "rsi", "op": "<", "value": 30}],
            "logic": "all",
            "expires_at": (now + timedelta(hours=1)).isoformat(),
        },
    )
    sched.set_symbol_indicator_watch(
        "QQQ",
        {
            "id": "wake-qqq",
            "symbol": "QQQ",
            "on_trigger": "WAKE",
            "conditions": [{"indicator": "macd", "op": ">", "value": 0}],
            "logic": "all",
            "expires_at": (now + timedelta(hours=1)).isoformat(),
        },
    )

    summary = daemon._global_plans_summary(sched, now)

    assert summary == [
        {"symbol": "SPY", "id": "armed-spy", "kind": "armed", "intent": "OPEN_LONG"},
        {"symbol": "QQQ", "id": "wake-qqq", "kind": "wake"},
    ]
    assert all("conditions" not in item for item in summary)
    assert daemon._global_plans_summary(None, now) == []


def test_global_plans_summary_loggue_les_erreurs_sans_lever(caplog) -> None:
    now = datetime(2026, 6, 5, 12, 0, tzinfo=timezone.utc)

    class BrokenScheduler:
        def active_indicator_watches(self, *, now):
            raise RuntimeError("scheduler broken")

    with caplog.at_level(logging.WARNING, logger="casys-trader"):
        summary = daemon._global_plans_summary(BrokenScheduler(), now)

    assert summary == []
    assert "[plans_summary] échec construction résumé global" in caplog.text
    assert "conscience d'état dégradée" in caplog.text


def test_run_cycle_injecte_toujours_active_plans_summary_sched_none(
    monkeypatch,
    tmp_path,
    patch_batch,
    make_data_source,
) -> None:
    _write_runtime_config(tmp_path, symbols=("SPY", "QQQ"))
    state_dir = tmp_path / "state"
    now = datetime(2026, 6, 5, 12, 0, tzinfo=timezone.utc)
    contexts: list[dict] = []

    def bars(symbol: str, lookback: str, interval: str) -> list[Bar]:
        return [
            Bar(
                ts=(now - timedelta(minutes=10)).isoformat(),
                open=100.0,
                high=101.0,
                low=99.0,
                close=100.0,
                volume=1000.0,
            )
            for _ in range(32)
        ]

    def decide(**kwargs) -> Decision:
        contexts.append(kwargs["context"])
        return Decision.hold(kwargs["symbol"], "attente")

    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    patch_batch(decide)
    data_source = make_data_source(bars)

    daemon.run_cycle(dry_run=True, now=now, symbols_filter=["SPY"], sched=None, data_source=data_source)

    assert contexts
    assert contexts[0]["active_plans_summary"] == []


def test_run_cycle_met_a_jour_le_snapshot_des_plans_ouverts(
    monkeypatch,
    tmp_path,
    patch_batch,
    make_data_source,
) -> None:
    _write_runtime_config(tmp_path, symbols=("SPY", "QQQ"))
    state_dir = tmp_path / "state"
    now = datetime(2026, 6, 5, 12, 0, tzinfo=timezone.utc)
    plan = TradePlan(
        id="plan-spy",
        symbol="SPY",
        side="LONG",
        quantity=10.0,
        remaining_quantity=6.0,
        entry_price=100.0,
        opened_at=now.isoformat(),
        hard_stop_price=95.0,
        take_profits=[TakeProfit(name="tp1", price=110.0, fraction=0.5, quantity=3.0)],
        last_llm_review={
            "ts": now.isoformat(),
            "verdict": "intact",
            "action": "HOLD",
            "intent": "HOLD",
            "llm_provider": "acpx",
            "llm_model": "gpt-5",
            "rationale": "tenir",
        },
        entry_thesis="breakout propre",
        entry_context={"large": "noise"},
    )
    _seed_trade_plan(state_dir, plan)

    def bars(symbol: str, lookback: str, interval: str) -> list[Bar]:
        return [
            Bar(
                ts=(now - timedelta(minutes=10)).isoformat(),
                open=100.0,
                high=101.0,
                low=99.0,
                close=100.0,
                volume=1000.0,
            )
            for _ in range(32)
        ]

    def decide(**kwargs) -> Decision:
        return Decision.hold(kwargs["symbol"], "attente")

    worker_context = WorkerCycleContextHandle()
    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    patch_batch(decide)
    data_source = make_data_source(bars)

    daemon.run_cycle(
        dry_run=True,
        now=now,
        symbols_filter=["SPY"],
        sched=Scheduler(state_dir / "scheduler.json"),
        data_source=data_source,
        worker_cycle_context=worker_context,
    )

    published = worker_context.current_for_cycle(now.isoformat())
    assert published.as_of == now.isoformat()
    assert list(published.open_plans.raw_plans) == [plan]
    assert list(published.open_plans.rows) == [
        {
            "id": "plan-spy",
            "symbol": "SPY",
            "side": "LONG",
            "quantity": 10.0,
            "remaining_quantity": 6.0,
            "opened_at": now.isoformat(),
            "entry_price": 100.0,
            "reference_volatility": None,
            "hard_stop_price": 95.0,
            "take_profits": [
                {
                    "name": "tp1",
                    "price": 110.0,
                    "fraction": 0.5,
                    "quantity": 3.0,
                    "after_fill": "",
                }
            ],
            "trailing_stop": None,
            "max_hold_minutes": None,
            "high_watermark": None,
            "low_watermark": None,
            "filled_take_profits": [],
            "profit_protection": None,
            "exit_watch": None,
            "last_llm_review": {
                "ts": now.isoformat(),
                "verdict": "intact",
                "action": "HOLD",
                "intent": "HOLD",
                "llm_provider": "acpx",
                "llm_model": "gpt-5",
            },
            "entry_thesis": "breakout propre",
        }
    ]
    assert published.exit_validation.prices_by_symbol["SPY"] == 100.0
    assert [bar.close for bar in published.exit_validation.bars_by_symbol["SPY"]] == [100.0] * 32


def test_plan_to_context_dict_expose_regles_de_sortie_et_suivi_sans_dump_brut() -> None:
    long_text = "x" * 700
    plan = TradePlan(
        id="plan-exit-rules",
        symbol="SPY",
        side="LONG",
        quantity=10.0,
        remaining_quantity=4.0,
        entry_price=100.0,
        opened_at="2026-06-05T12:00:00+00:00",
        reference_volatility=1.25,
        hard_stop_price=97.0,
        take_profits=[
            TakeProfit(
                name="tp1",
                price=105.0,
                fraction=0.5,
                quantity=5.0,
                after_fill="move_stop_to_breakeven",
            )
        ],
        trailing_stop=TrailingStop(
            enabled_after="tp1",
            trail_type="volatility_multiple",
            trail_value=1.5,
            trail_floored=True,
        ),
        max_hold_minutes=240.0,
        high_watermark=108.0,
        low_watermark=99.0,
        filled_take_profits=["tp1"],
        profit_protection=ProfitProtection(
            arm_at_r=1.0,
            trigger_on_giveback_pct=0.35,
            close_fraction=0.5,
            move_stop_to="breakeven",
            min_hold_minutes=30.0,
            lock_r=0.25,
            triggered=True,
        ),
        exit_watch={
            "id": "SPY:exit-watch",
            "created_at": "2026-06-05T12:00:00+00:00",
            "expires_at": "2026-06-05T16:00:00+00:00",
            "logic": "all",
            "on_trigger": "WAKE",
            "source": "exit_watch",
            "cooldown_minutes": 15.0,
            "last_triggered_at": "2026-06-05T12:30:00+00:00",
            "conditions": [
                {
                    "symbol": "SPY",
                    "indicator": "relative_strength",
                    "op": "<=",
                    "value": -0.2,
                    "interval": "1h",
                    "source_interval": "1h",
                    "lookback": "5d",
                    "window": 48,
                    "as_of": "2026-06-05T12:00:00+00:00",
                    "opaque": {"large": long_text},
                }
            ],
            "rationale": long_text,
            "order": {"secret": long_text},
        },
        entry_context={"large": long_text},
        entry_decision_id="decision-private",
        llm_fallback_reason="provider_private",
        llm_confidence=0.73,
    )

    payload = daemon._plan_to_context_dict(plan)

    assert payload["quantity"] == 10.0
    assert payload["remaining_quantity"] == 4.0
    assert payload["opened_at"] == "2026-06-05T12:00:00+00:00"
    assert payload["reference_volatility"] == 1.25
    assert payload["trailing_stop"] == {
        "enabled_after": "tp1",
        "trail_type": "volatility_multiple",
        "trail_value": 1.5,
        "trail_floored": True,
    }
    assert payload["max_hold_minutes"] == 240.0
    assert payload["high_watermark"] == 108.0
    assert payload["low_watermark"] == 99.0
    assert payload["filled_take_profits"] == ["tp1"]
    assert payload["profit_protection"] == {
        "enabled": True,
        "arm_at_r": 1.0,
        "trigger_on_giveback_pct": 0.35,
        "close_fraction": 0.5,
        "move_stop_to": "breakeven",
        "min_hold_minutes": 30.0,
        "lock_r": 0.25,
        "triggered": True,
    }
    assert payload["exit_watch"] == {
        "id": "SPY:exit-watch",
        "created_at": "2026-06-05T12:00:00+00:00",
        "expires_at": "2026-06-05T16:00:00+00:00",
        "logic": "all",
        "on_trigger": "WAKE",
        "source": "exit_watch",
        "cooldown_minutes": 15.0,
        "last_triggered_at": "2026-06-05T12:30:00+00:00",
        "conditions": [
            {
                "symbol": "SPY",
                "indicator": "relative_strength",
                "op": "<=",
                "value": -0.2,
                "timeframe": "1h",
                "source_interval": "1h",
                "lookback": "5d",
                "window": 48,
                "as_of": "2026-06-05T12:00:00+00:00",
            }
        ],
    }
    assert "entry_context" not in payload
    assert "entry_decision_id" not in payload
    assert "llm_fallback_reason" not in payload
    assert "llm_confidence" not in payload
    assert json.dumps(payload, sort_keys=True, allow_nan=False)


def test_plan_to_context_dict_borne_les_champs_textes_du_plan() -> None:
    long_text = "x" * 700
    plan = TradePlan(
        id="plan-long",
        symbol="SPY",
        side="LONG",
        quantity=10.0,
        remaining_quantity=10.0,
        entry_price=100.0,
        opened_at="2026-06-05T12:00:00+00:00",
        last_llm_review={
            "ts": "2026-06-05T12:10:00+00:00",
            "verdict": long_text,
            "action": "HOLD",
            "intent": "HOLD",
            "llm_provider": "acpx",
            "llm_model": "gpt-5",
            "blob": {"huge": long_text},
        },
        entry_thesis=long_text,
    )

    payload = daemon._plan_to_context_dict(plan)

    assert payload["entry_thesis"] == "x" * 500
    assert payload["last_llm_review"] == {
        "ts": "2026-06-05T12:10:00+00:00",
        "verdict": "x" * 240,
        "action": "HOLD",
        "intent": "HOLD",
        "llm_provider": "acpx",
        "llm_model": "gpt-5",
    }


def test_plan_to_context_dict_signale_les_regles_exceptionnellement_tronquees() -> None:
    take_profits = [
        TakeProfit(
            name=f"tp{index}",
            price=101.0 + index,
            fraction=1.0 / 13,
            quantity=1.0,
        )
        for index in range(13)
    ]
    conditions = [
        {
            "symbol": "SPY",
            "indicator": "relative_strength",
            "op": "<=",
            "value": -0.2,
            "timeframe": "1h",
        }
        for _ in range(13)
    ]
    plan = TradePlan(
        id="plan-bounded",
        symbol="SPY",
        side="LONG",
        quantity=13.0,
        remaining_quantity=13.0,
        entry_price=100.0,
        opened_at="2026-06-05T12:00:00+00:00",
        take_profits=take_profits,
        filled_take_profits=[take_profit.name for take_profit in take_profits],
        exit_watch={"conditions": conditions},
    )

    payload = daemon._plan_to_context_dict(plan)

    assert len(payload["take_profits"]) == 12
    assert payload["take_profits_truncated"] is True
    assert len(payload["filled_take_profits"]) == 12
    assert payload["filled_take_profits_truncated"] is True
    assert len(payload["exit_watch"]["conditions"]) == 12
    assert payload["exit_watch"]["conditions_truncated"] is True
    assert json.dumps(payload, sort_keys=True, allow_nan=False)


def test_run_cycle_utilise_la_source_injectee_sans_appeler_market_get_bars(
    monkeypatch,
    tmp_path,
    patch_batch,
    make_data_source,
) -> None:
    _write_runtime_config(tmp_path, symbols=("SPY", "QQQ"))
    state_dir = tmp_path / "state"
    sched = Scheduler(state_dir / "scheduler.json")
    now = datetime(2026, 6, 5, 12, 0, tzinfo=timezone.utc)

    def injected_bars(symbol: str, lookback: str, interval: str) -> list[Bar]:
        return [
            Bar(ts=now.isoformat(), open=100.0, high=101.0, low=99.0, close=100.0, volume=1000.0)
        ]

    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    monkeypatch.setattr(
        daemon.market,
        "get_bars",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("market.get_bars ne doit plus être appelé")),
    )
    patch_batch(lambda **kwargs: Decision.hold(kwargs["symbol"], "attente"))
    data_source = make_data_source(injected_bars)

    report = daemon.run_cycle(
        dry_run=True,
        now=now,
        symbols_filter=["SPY"],
        sched=sched,
        data_source=data_source,
    )

    assert [decision["symbol"] for decision in report["decisions"]] == ["SPY"]
    assert ("SPY", daemon.DEFAULT_RUNTIME_LOOKBACK, daemon.DEFAULT_RUNTIME_INTERVAL) in data_source.calls


def test_run_cycle_injecte_un_cockpit_compact_sans_barres(monkeypatch, tmp_path, patch_batch, make_data_source) -> None:
    _write_runtime_config(tmp_path, symbols=("SPY", "QQQ"))
    state_dir = tmp_path / "state"
    sched = Scheduler(state_dir / "scheduler.json")
    now = datetime(2026, 6, 5, 12, 0, tzinfo=timezone.utc)
    contexts: list[dict] = []

    def bars(symbol: str, lookback: str, interval: str) -> list[Bar]:
        base = 100.0 if symbol == "SPY" else 200.0
        return [
            Bar(ts="2026-06-05T11:30:00+00:00", open=base, high=base + 1, low=base - 1, close=base, volume=1000.0),
            Bar(ts="2026-06-05T11:45:00+00:00", open=base + 1, high=base + 2, low=base, close=base + 2, volume=1000.0),
            Bar(ts="2026-06-05T11:55:00+00:00", open=base + 2, high=base + 3, low=base + 1, close=base + 4, volume=1000.0),
        ]

    def decide(**kwargs) -> Decision:
        contexts.append(kwargs["context"])
        return Decision.hold(kwargs["symbol"], "attente")

    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    patch_batch(decide)
    data_source = make_data_source(bars)

    daemon.run_cycle(dry_run=True, now=now, symbols_filter=["SPY"], sched=sched, data_source=data_source)

    assert contexts
    semantic = contexts[0]["semantic"]
    assert "requestable_indicator_ids" in semantic
    assert "features" not in semantic
    assert "bars" not in contexts[0]
    cockpit = contexts[0]["cockpit"]
    return_index = cockpit["cols"].index("r")
    spy_row = next(row for row in cockpit["rows"] if row[0] == "SPY")
    assert spy_row[return_index] == 0.04
    assert contexts[0]["risk_limits"]["max_order_value"] == 10000
    risk_capacity = contexts[0]["risk_capacity"]
    assert risk_capacity["gross_remaining_usd"] == 100000
    assert risk_capacity["per_symbol"]["SPY"]["max_buy_qty"] == pytest.approx(10000 / 104)


def test_run_cycle_resout_une_requete_indicateurs_bornee_avant_decision_finale(
    monkeypatch,
    tmp_path,
    patch_batch,
    make_data_source,
) -> None:
    _write_runtime_config(tmp_path, symbols=("SPY", "QQQ"))
    state_dir = tmp_path / "state"
    sched = Scheduler(state_dir / "scheduler.json")
    now = datetime(2026, 6, 5, 12, 0, tzinfo=timezone.utc)
    contexts: list[dict] = []

    def bars(symbol: str, lookback: str, interval: str) -> list[Bar]:
        base = 100.0 if symbol == "SPY" else 200.0
        return [
            Bar(ts="2026-06-05T11:30:00+00:00", open=base, high=base + 1, low=base - 1, close=base, volume=1000.0),
            Bar(ts="2026-06-05T11:45:00+00:00", open=base + 1, high=base + 2, low=base, close=base + 2, volume=1000.0),
            Bar(ts="2026-06-05T11:55:00+00:00", open=base + 2, high=base + 3, low=base + 1, close=base + 4, volume=1000.0),
        ]

    calls = {"n": 0}

    def decide(**kwargs) -> Decision:
        calls["n"] += 1
        contexts.append(kwargs["context"])
        if calls["n"] == 1:
            return daemon.codex_client.ContextResearchRequest(
                symbol=kwargs["symbol"],
                rationale="je veux confirmer le spread",
                requests=[
                    daemon.codex_client.IndicatorRequest(
                        symbol="QQQ",
                        indicators=["z_score", "spread_zscore", "return", "volatility"],
                        window=48,
                    )
                ],
            )
        return Decision.hold(kwargs["symbol"], "attente")

    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    patch_batch(decide)
    data_source = make_data_source(bars)

    daemon.run_cycle(
        dry_run=True,
        now=now,
        symbols_filter=["SPY"],
        sched=sched,
        data_source=data_source,
        max_context_requests_per_symbol=1,
        max_indicators_per_request=2,
    )

    assert len(contexts) == 2
    assert "bars" not in contexts[0]
    assert "research" not in contexts[0]
    assert "prior_rationale" not in contexts[0]
    assert "bars" not in contexts[1]
    assert contexts[1]["research"]["requests"][0]["symbol"] == "QQQ"
    assert list(contexts[1]["research"]["requests"][0]["indicators"]) == ["z_score", "spread_zscore"]
    # Le 2e exec (stateless) reçoit la rationale du 1er pour ne pas re-raisonner à zéro.
    assert contexts[1]["prior_rationale"] == "je veux confirmer le spread"


def test_run_cycle_daily_bars_echouees_ne_bloquent_pas_le_cockpit(monkeypatch, tmp_path, patch_batch, make_data_source) -> None:
    _write_runtime_config(tmp_path, symbols=("SPY", "QQQ"))
    state_dir = tmp_path / "state"
    sched = Scheduler(state_dir / "scheduler.json")
    now = datetime(2026, 6, 5, 12, 0, tzinfo=timezone.utc)
    contexts: list[dict] = []
    requests: list[tuple[str, str, str]] = []

    def runtime_bars(symbol: str) -> list[Bar]:
        base = 100.0 if symbol == "SPY" else 200.0
        return [
            Bar(
                ts=f"2026-06-05T{4 + index // 4:02d}:{(index % 4) * 15:02d}:00+00:00",
                open=base + index * 0.5 - 0.25,
                high=1000.0,
                low=0.0,
                close=base + index * 0.5,
                volume=1000.0,
            )
            for index in range(32)
        ]

    def bars(symbol: str, lookback: str, interval: str) -> list[Bar]:
        requests.append((symbol, lookback, interval))
        if interval == "1d":
            raise MarketError("fetch_failed", f"{symbol}: daily unavailable")
        return runtime_bars(symbol)

    def decide(**kwargs) -> Decision:
        contexts.append(kwargs["context"])
        return Decision.hold(kwargs["symbol"], "attente")

    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    patch_batch(decide)
    data_source = make_data_source(bars)

    daemon.run_cycle(dry_run=True, now=now, symbols_filter=["SPY"], sched=sched, data_source=data_source)

    assert ("SPY", "1y", "1d") in requests
    cockpit = contexts[0]["cockpit"]
    cols = cockpit["cols"]
    spy_row = next(row for row in cockpit["rows"] if row[0] == "SPY")
    assert spy_row[cols.index("htf")] == "trending_up"
    assert spy_row[cols.index("aligned")] is True


def test_run_cycle_daily_bars_stale_sont_ignorees_pour_le_cockpit(monkeypatch, tmp_path, patch_batch, make_data_source) -> None:
    _write_runtime_config(tmp_path, symbols=("SPY", "QQQ"))
    state_dir = tmp_path / "state"
    sched = Scheduler(state_dir / "scheduler.json")
    now = datetime(2026, 6, 5, 12, 0, tzinfo=timezone.utc)
    contexts: list[dict] = []
    requests: list[tuple[str, str, str]] = []

    def runtime_bars(symbol: str) -> list[Bar]:
        base = 100.0 if symbol == "SPY" else 200.0
        return [
            Bar(
                ts=f"2026-06-05T{4 + index // 4:02d}:{(index % 4) * 15:02d}:00+00:00",
                open=base + index * 0.5 - 0.25,
                high=1000.0,
                low=0.0,
                close=base + index * 0.5,
                volume=1000.0,
            )
            for index in range(32)
        ]

    def stale_daily_bars(symbol: str) -> list[Bar]:
        base = 100.0 if symbol == "SPY" else 200.0
        return [
            Bar(ts="2026-06-01T00:00:00+00:00", open=base - 0.5, high=base + 1.0, low=base - 1.0, close=base, volume=1000.0),
            Bar(ts="2026-06-02T00:00:00+00:00", open=base, high=base + 1.5, low=base - 0.5, close=base + 0.5, volume=1000.0),
            Bar(ts="2026-06-03T00:00:00+00:00", open=base + 4.5, high=base + 5.2, low=base + 4.0, close=base + 5.0, volume=1000.0),
        ]

    def bars(symbol: str, lookback: str, interval: str) -> list[Bar]:
        requests.append((symbol, lookback, interval))
        if interval == "1d":
            return stale_daily_bars(symbol)
        return runtime_bars(symbol)

    def decide(**kwargs) -> Decision:
        contexts.append(kwargs["context"])
        return Decision.hold(kwargs["symbol"], "attente")

    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    patch_batch(decide)
    data_source = make_data_source(bars)

    daemon.run_cycle(dry_run=True, now=now, symbols_filter=["SPY"], sched=sched, data_source=data_source)

    assert ("SPY", "1y", "1d") in requests
    cockpit = contexts[0]["cockpit"]
    cols = cockpit["cols"]
    spy_row = next(row for row in cockpit["rows"] if row[0] == "SPY")
    assert spy_row[cols.index("htf")] == "trending_up"
    assert spy_row[cols.index("aligned")] is True


def test_run_cycle_marque_les_decisions_llm_comme_model_called(
    monkeypatch,
    tmp_path,
    patch_batch,
    make_data_source,
) -> None:
    _write_runtime_config(tmp_path, symbols=("SPY", "QQQ"))
    state_dir = tmp_path / "state"
    now = datetime(2026, 6, 5, 12, 0, tzinfo=timezone.utc)

    def bars(symbol: str, lookback: str, interval: str) -> list[Bar]:
        return [
            Bar(
                ts=(now - timedelta(minutes=10)).isoformat(),
                open=100.0,
                high=101.0,
                low=99.0,
                close=100.0,
                volume=1000.0,
            )
            for _ in range(32)
        ]

    def decide(**kwargs) -> Decision:
        return Decision(
            symbol=kwargs["symbol"],
            action="HOLD",
            quantity=0.0,
            confidence=0.5,
            rationale="attente",
            intent="HOLD",
            llm_provider="acpx",
            llm_model="gpt-5.5/medium",
        )

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
    )

    assert report["decisions"][0]["decision_source"] == "llm"
    assert report["decisions"][0]["model_called"] is True


def test_run_cycle_marque_no_decision_in_batch_comme_hold_infra_specifique(
    monkeypatch,
    tmp_path,
    make_data_source,
) -> None:
    _write_runtime_config(tmp_path, symbols=("SPY", "QQQ"))
    state_dir = tmp_path / "state"
    now = datetime(2026, 6, 5, 12, 0, tzinfo=timezone.utc)

    def bars(symbol: str, lookback: str, interval: str) -> list[Bar]:
        return [
            Bar(
                ts=(now - timedelta(minutes=10)).isoformat(),
                open=100.0,
                high=101.0,
                low=99.0,
                close=100.0,
                volume=1000.0,
            )
            for _ in range(32)
        ]

    def empty_batch(**_kwargs) -> dict:
        return {}

    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    monkeypatch.setattr(daemon.codex_client, "decide_batch", empty_batch)
    sched = Scheduler(state_dir / "scheduler.json")
    sched.set_symbol_next_wake_in("SPY", minutes=-5, now=now)
    data_source = make_data_source(bars)

    report = daemon.run_cycle(
        dry_run=True,
        now=now,
        symbols_filter=["SPY"],
        sched=sched,
        data_source=data_source,
    )

    assert report["decisions"][0]["reason"] == "no_decision_in_batch"
    assert report["decisions"][0]["decision_source"] == "infra"
    assert report["decisions"][0]["model_called"] is False
