from __future__ import annotations

import json
from dataclasses import asdict

from trader.application.execute_queue_dispatch import dispatch_execute_order_via_queue
from trader.execution.broker import Fill


class _FakeLedger:
    def __init__(self, *, enqueue_result: int | None = 7, task_results: list[dict | None] | None = None) -> None:
        self.enqueue_result = enqueue_result
        self.task_results = list(task_results or [])
        self.enqueued: list[dict] = []
        self.abandoned: list[dict] = []

    def enqueue(self, **kwargs):
        self.enqueued.append(kwargs)
        return self.enqueue_result

    def get(self, task_id: int):
        if self.task_results:
            return self.task_results.pop(0)
        return {"id": task_id, "status": "pending", "result": None}

    def abandon(self, **kwargs):
        self.abandoned.append(kwargs)
        return True


def _dispatch(ledger: _FakeLedger, **overrides):
    params = {
        "ledger": ledger,
        "symbol": "AAPL",
        "side": "BUY",
        "quantity": 5.0,
        "rationale": "test",
        "price": 100.0,
        "ts": "2026-07-05T08:00:00+00:00",
        "fx_rate": 1.0,
        "dry_run": False,
        "plan_to_upsert": None,
        "symbol_to_close": None,
        "cycle_id": "2026-07-05T08:00:00+00:00",
        "intent": "OPEN_LONG",
        "budget_s": 1.0,
        "now_fn": lambda: 10.0,
        "sleep_fn": lambda _seconds: None,
    }
    params.update(overrides)
    return dispatch_execute_order_via_queue(**params)


def test_execute_queue_dispatch_done_returns_fill_and_enqueues_stable_payload() -> None:
    fill = Fill(symbol="AAPL", side="BUY", quantity=5.0, price=100.0, ts="2026-07-05T08:00:00+00:00")
    ledger = _FakeLedger(task_results=[{"id": 7, "status": "done", "result": json.dumps(asdict(fill))}])

    outcome = _dispatch(ledger)

    assert outcome.task_id == 7
    assert outcome.terminal == "done"
    assert outcome.reason is None
    assert outcome.fill == fill
    assert ledger.enqueued[0]["kind"] == "execute_order"
    assert ledger.enqueued[0]["resource"] == "portfolio"
    assert ledger.enqueued[0]["partition_key"] == "portfolio"
    assert ledger.enqueued[0]["dedup_key"] == "exec:2026-07-05T08:00:00+00:00:AAPL:OPEN_LONG"
    payload = json.loads(ledger.enqueued[0]["payload"])
    assert payload["order"] == {"symbol": "AAPL", "side": "BUY", "quantity": 5.0, "rationale": "test"}
    assert payload["price"] == 100.0
    assert payload["dry_run"] is False


def test_execute_queue_dispatch_dead_maps_fail_closed_reason() -> None:
    ledger = _FakeLedger(task_results=[{"id": 7, "status": "dead", "result": None}])

    outcome = _dispatch(ledger)

    assert outcome.terminal == "dead"
    assert outcome.reason == "queue_execute_dead"
    assert outcome.fill is None


def test_execute_queue_dispatch_not_found_maps_fail_closed_reason() -> None:
    ledger = _FakeLedger(task_results=[None])

    outcome = _dispatch(ledger)

    assert outcome.terminal == "not_found"
    assert outcome.reason == "queue_execute_not_found"
    assert outcome.fill is None


def test_execute_queue_dispatch_timeout_maps_fail_closed_reason() -> None:
    clock_values = iter([10.0, 10.0, 10.2])
    ledger = _FakeLedger(task_results=[{"id": 7, "status": "pending", "result": None}])

    outcome = _dispatch(ledger, budget_s=0.1, now_fn=lambda: next(clock_values, 10.2))

    assert outcome.terminal == "timeout"
    assert outcome.reason == "queue_execute_timeout"
    assert outcome.late_execution_risk is False
    assert outcome.fill is None
    assert outcome.abandoned is True
    assert ledger.abandoned == [
        {"task_id": 7, "now_ms": 10200, "error": "queue_execute_timeout"}
    ]


def test_execute_queue_dispatch_done_without_fill_is_fail_closed_for_live_order() -> None:
    ledger = _FakeLedger(task_results=[{"id": 7, "status": "done", "result": None}])

    outcome = _dispatch(ledger, dry_run=False)

    assert outcome.terminal == "done"
    assert outcome.reason == "queue_execute_no_fill"
    assert outcome.fill is None


def test_execute_queue_dispatch_done_without_fill_is_ok_for_dry_run() -> None:
    ledger = _FakeLedger(task_results=[{"id": 7, "status": "done", "result": None}])

    outcome = _dispatch(ledger, dry_run=True)

    assert outcome.terminal == "done"
    assert outcome.reason is None
    assert outcome.fill is None


def test_execute_queue_dispatch_enqueue_none_maps_fail_closed_reason() -> None:
    ledger = _FakeLedger(enqueue_result=None)

    outcome = _dispatch(ledger)

    assert outcome.task_id is None
    assert outcome.terminal == "enqueue_failed"
    assert outcome.reason == "queue_execute_enqueue_failed"
