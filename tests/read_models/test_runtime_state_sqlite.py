from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from trader.execution.broker import Order
from trader.infrastructure.state_db.broker_store import SqliteBroker
from trader.infrastructure.state_db.connection import open_state_db
from trader.infrastructure.state_db.migrations import import_broker_from_json, import_scheduler_from_json
from trader.infrastructure.state_db.scheduler_store import SqliteScheduler
from trader.infrastructure.queue.ledger import TaskLedger
from trader.reporting.read_models.runtime_state import (
    _load_fills_safe,
    _load_queue_worker_activity_safe,
    _load_scheduler_data_safe,
    _load_scheduler_wakes_safe,
)


def test_load_fills_safe_reads_sqlite_when_db_exists_without_json(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    db = open_state_db(state_dir / "casys.db")
    import_broker_from_json(db, state_dir / "_absent_broker.json", starting_cash=100_000.0)
    broker = SqliteBroker(db)
    broker.submit(Order("SPY", "BUY", 2.0), 101.0, "2026-06-10T10:00:00+00:00", dry_run=False)

    fills = _load_fills_safe(state_dir / "broker.json")

    assert not (state_dir / "broker.json").exists()
    assert fills == [
        {
            "symbol": "SPY",
            "side": "BUY",
            "quantity": 2.0,
            "price": 101.0,
            "ts": "2026-06-10T10:00:00+00:00",
            "commission": 0.0,
            "commission_currency": "USD",
            "commission_model": "none",
            "fx_rate": 1.0,
        }
    ]


def test_scheduler_read_models_read_sqlite_when_db_exists_without_json(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    db = open_state_db(state_dir / "casys.db")
    import_scheduler_from_json(db, state_dir / "_absent_scheduler.json")
    scheduler = SqliteScheduler(db)
    scheduler.set_default_next_wake("2026-06-10T12:00:00+00:00")
    scheduler.set_symbol_next_wake("SPY", "2026-06-10T11:30:00+00:00")
    scheduler.set_stale_streak("SPY", 2)
    scheduler.set_symbol_indicator_watch(
        "SPY",
        {
            "id": "SPY:active",
            "symbol": "SPY",
            "created_at": "2026-06-10T10:00:00+00:00",
            "expires_at": "2026-06-10T13:00:00+00:00",
        },
    )
    scheduler.set_symbol_indicator_watch(
        "QQQ",
        {
            "id": "QQQ:expired",
            "symbol": "QQQ",
            "created_at": "2026-06-10T10:00:00+00:00",
            "expires_at": "2026-06-10T10:30:00+00:00",
        },
    )

    watches, streaks = _load_scheduler_data_safe(
        state_dir / "scheduler.json",
        now=datetime(2026, 6, 10, 12, 0, tzinfo=UTC),
    )
    default_next_wake, symbol_wakes = _load_scheduler_wakes_safe(state_dir / "scheduler.json")

    assert not (state_dir / "scheduler.json").exists()
    assert [watch["id"] for watch in watches] == ["SPY:active"]
    assert streaks == {"SPY": 2}
    assert default_next_wake == "2026-06-10T12:00:00+00:00"
    assert symbol_wakes == {"SPY": "2026-06-10T11:30:00+00:00"}


def test_queue_worker_activity_reads_running_decide_workers(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    ledger = TaskLedger(state_dir / "task_ledger.db")
    now_ms = 1_000
    first = ledger.enqueue(
        kind="decide",
        priority=0,
        scheduled_at_ms=now_ms,
        now_ms=now_ms,
        partition_key="SPY",
        resource="acpx",
    )
    second = ledger.enqueue(
        kind="decide",
        priority=0,
        scheduled_at_ms=now_ms,
        now_ms=now_ms,
        partition_key="QQQ",
        resource="acpx",
    )
    ledger.enqueue(
        kind="decide",
        priority=0,
        scheduled_at_ms=now_ms,
        now_ms=now_ms,
        partition_key="IWM",
        resource="acpx",
    )

    assert first is not None
    assert second is not None
    ledger.claim(worker_id="w1", token="t1", now_ms=now_ms, lease_ms=60_000, free_resources=["acpx"])
    ledger.claim(worker_id="w2", token="t2", now_ms=now_ms, lease_ms=60_000, free_resources=["acpx"])

    activity = _load_queue_worker_activity_safe(state_dir / "task_ledger.db")

    assert activity == {
        "active_workers": 2,
        "running_tasks": 2,
        "pending_tasks": 1,
    }
