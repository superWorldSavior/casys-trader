# tests/queue/test_worker.py
import pytest
from trader.queue.ledger import TaskLedger
from trader.queue.pools import ResourcePools
from trader.queue.worker import Worker, RetryableError


def _setup(tmp_path):
    led = TaskLedger(tmp_path / "q.db")
    pools = ResourcePools({"acpx": 1, "ib": 1})
    return led, pools


def test_worker_runs_handler_and_completes(tmp_path):
    led, pools = _setup(tmp_path)
    led.enqueue(kind="decide", priority=5, scheduled_at_ms=0, now_ms=0,
                dedup_key="d", partition_key="C1", resource="acpx",
                payload='{"chunk": ["AAPL"]}')
    seen = []
    handlers = {"decide": lambda task: seen.append(task["dedup_key"])}
    w = Worker(led, pools, handlers, worker_id="w1")
    assert w.run_once(now_ms=1, token="tok1") is True
    assert seen == ["d"]
    (st,) = led._conn.execute("SELECT status FROM tasks").fetchone()
    assert st == "done"
    assert "acpx" in pools.free_resources()            # ressource relâchée


def test_worker_retryable_error_requeues(tmp_path):
    led, pools = _setup(tmp_path)
    led.enqueue(kind="decide", priority=5, scheduled_at_ms=0, now_ms=0,
                dedup_key="d", partition_key="C1", resource="acpx", max_attempts=3)

    def boom(task):
        raise RetryableError("overload")

    w = Worker(led, pools, {"decide": boom}, worker_id="w1")
    assert w.run_once(now_ms=1, token="tok1") is True
    (st,) = led._conn.execute("SELECT status FROM tasks").fetchone()
    assert st == "pending"                              # requeue, pas dead
    assert "acpx" in pools.free_resources()


def test_worker_returns_false_when_resource_saturated(tmp_path):
    led, pools = _setup(tmp_path)
    led.enqueue(kind="decide", priority=5, scheduled_at_ms=0, now_ms=0,
                dedup_key="d", partition_key="C1", resource="acpx")
    pools.try_acquire("acpx")                            # sature acpx
    w = Worker(led, pools, {"decide": lambda t: None}, worker_id="w1")
    assert w.run_once(now_ms=1, token="tok1") is False  # rien de claimable
