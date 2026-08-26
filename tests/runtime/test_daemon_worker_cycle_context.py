from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from trader.agent.client import Decision
from trader.market.market_data import Bar
from trader.planning.scheduler import Scheduler
from trader.planning.trade_plan import create_trade_plan
from trader.runtime import daemon
from trader.runtime.worker_cycle_context import WorkerCycleContextHandle
from tests.conftest import write_runtime_config
from tests.plan_store_fakes import MemoryTradePlanStore


@pytest.mark.parametrize(
    ("closed_exit_plan", "bar_low", "expected_reason"),
    [
        ({"hard_stop": 95.0}, 94.0, "hard_stop"),
        ({"hard_stop": 90.0, "max_hold_minutes": 1.0}, 99.0, "max_hold"),
    ],
    ids=("hard_stop", "max_hold"),
)
def test_run_cycle_publishes_only_post_exit_open_plans_to_workers(
    monkeypatch,
    tmp_path,
    make_data_source,
    closed_exit_plan,
    bar_low,
    expected_reason,
) -> None:
    """A worker sees the settled post-protection lifecycle, never a closed plan."""

    now = datetime(2026, 6, 5, 14, 30, tzinfo=timezone.utc)
    cycle_id = now.isoformat()
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    write_runtime_config(tmp_path, symbols=("SPY", "QQQ"))

    closed_plan = create_trade_plan(
        symbol="SPY",
        side="LONG",
        quantity=10.0,
        entry_price=100.0,
        opened_at=(now - timedelta(minutes=2)).isoformat(),
        raw_exit_plan=closed_exit_plan,
    )
    survivor = create_trade_plan(
        symbol="QQQ",
        side="LONG",
        quantity=10.0,
        entry_price=100.0,
        opened_at=(now - timedelta(minutes=10)).isoformat(),
        raw_exit_plan={
            "hard_stop": 95.0,
            "take_profits": [{"name": "tp1", "price": 110.0, "fraction": 0.5}],
            "max_hold_minutes": 120.0,
            "exit_watch": {
                "ttl_minutes": 90.0,
                "cooldown_minutes": 15.0,
                "logic": "all",
                "conditions": [
                    {
                        "type": "close",
                        "op": ">",
                        "value": 102.0,
                        "interval": "15m",
                        "lookback": "5d",
                        "window": 3,
                    }
                ],
            },
        },
    )
    plan_store = MemoryTradePlanStore([closed_plan, survivor])
    broker = daemon.SimBroker(state_dir / "broker.json", starting_cash=100_000.0)
    for symbol in ("SPY", "QQQ"):
        broker.submit(
            daemon.Order(symbol=symbol, side="BUY", quantity=10.0),
            100.0,
            (now - timedelta(minutes=20)).isoformat(),
            dry_run=False,
        )

    handle = WorkerCycleContextHandle()
    scanner_calls: list[str] = []

    def persist_exit_watch_cooldown(*, plan_store, now, **_kwargs) -> list[dict]:
        scanner_calls.append("scan")
        assert [plan.id for plan in plan_store.open_plans()] == [survivor.id]
        open_plan = plan_store.open_plans()[0]
        watch = {**(open_plan.exit_watch or {}), "last_triggered_at": now.isoformat()}
        plan_store.upsert(open_plan.model_copy(update={"exit_watch": watch}))
        return [{"symbol": "QQQ", "plan_id": survivor.id, "watch_id": watch["id"]}]

    def bars(symbol: str, _lookback: str, _interval: str) -> list[Bar]:
        low = bar_low if symbol == "SPY" else 99.0
        return [
            Bar(
                ts=now.isoformat(),
                open=100.0,
                high=101.0,
                low=low,
                close=100.0,
                volume=1000.0,
            )
        ]

    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    monkeypatch.setattr(daemon, "make_broker", lambda **_kwargs: broker)
    monkeypatch.setattr(daemon, "make_trade_plan_store", lambda **_kwargs: plan_store)
    monkeypatch.setattr(daemon.cycle_scheduling, "scan_exit_watches", persist_exit_watch_cooldown)
    monkeypatch.setattr(
        daemon.codex_client,
        "decide_batch",
        lambda *, symbols, **_kwargs: {
            symbol: Decision.hold(symbol, "snapshot lifecycle test") for symbol in symbols
        },
    )

    report = daemon.run_cycle(
        dry_run=False,
        now=now,
        symbols_filter=["SPY", "QQQ"],
        sched=Scheduler(state_dir / "scheduler.json"),
        data_source=make_data_source(bars),
        worker_cycle_context=handle,
    )

    context = handle.current_for_cycle(cycle_id)
    assert report["planned_exits"][0]["reason"] == expected_reason
    assert scanner_calls == ["scan"]
    assert [plan.id for plan in context.open_plans.raw_plans] == [survivor.id]
    assert [row["id"] for row in context.open_plans.rows] == [survivor.id]

    raw_survivor = context.open_plans.raw_plans[0]
    row = context.open_plans.rows[0]
    assert raw_survivor.hard_stop_price == 95.0
    assert raw_survivor.max_hold_minutes == 120.0
    assert raw_survivor.exit_watch["last_triggered_at"] == cycle_id
    assert row["hard_stop_price"] == 95.0
    assert row["max_hold_minutes"] == 120.0
    assert row["take_profits"] == [
        {
            "name": "tp1",
            "price": 110.0,
            "fraction": 0.5,
            "quantity": 5.0,
            "after_fill": "",
        }
    ]
    assert row["exit_watch"]["last_triggered_at"] == cycle_id
    assert row["exit_watch"]["conditions"][0]["type"] == "close"
    assert row["exit_watch"]["conditions"][0]["value"] == 102.0
