# tests/queue/test_worker.py
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
    handlers = {"decide": lambda task, *, heartbeat=None: seen.append(task["dedup_key"])}
    w = Worker(led, pools, handlers, worker_id="w1")
    assert w.run_once(now_ms=1, token="tok1") is True
    assert seen == ["d"]
    (st,) = led._conn.execute("SELECT status FROM tasks").fetchone()
    assert st == "done"
    assert "acpx" in pools.free_resources()            # ressource relâchée


def test_worker_passe_heartbeat_au_handler_et_renouvelle_le_lease(tmp_path):
    led, pools = _setup(tmp_path)
    led.enqueue(kind="decide", priority=5, scheduled_at_ms=0, now_ms=0,
                dedup_key="d", partition_key="C1", resource="acpx")
    heartbeat_calls = []
    original_heartbeat = led.heartbeat

    def spy_heartbeat(**kwargs):
        heartbeat_calls.append(kwargs)
        return original_heartbeat(**kwargs)

    led.heartbeat = spy_heartbeat  # type: ignore[method-assign]

    ticks = iter([2.0, 3.0])

    def now_fn():
        return next(ticks)

    def handler(task, *, heartbeat=None):
        assert heartbeat is not None
        assert heartbeat() is True
        return "ok"

    w = Worker(led, pools, {"decide": handler}, worker_id="w1", lease_ms=1_000, now_fn=now_fn)

    assert w.run_once(now_ms=1, token="tok1") is True
    assert heartbeat_calls == [
        {"task_id": 1, "token": "tok1", "now_ms": 2000, "lease_ms": 1000}
    ]


def test_worker_retryable_error_requeues(tmp_path):
    led, pools = _setup(tmp_path)
    led.enqueue(kind="decide", priority=5, scheduled_at_ms=0, now_ms=0,
                dedup_key="d", partition_key="C1", resource="acpx", max_attempts=3)

    def boom(task, *, heartbeat=None):
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
    w = Worker(led, pools, {"decide": lambda t, *, heartbeat=None: None}, worker_id="w1")
    assert w.run_once(now_ms=1, token="tok1") is False  # rien de claimable


# ── FIX 1 : is_overload conditionnel ───────────────────────────────────────

def test_worker_retryable_no_overload_flag_keeps_limit(tmp_path):
    """RetryableError sans is_overload → effective_limit inchangé (M non baissé)."""
    led, pools = _setup(tmp_path)
    led.enqueue(kind="decide", priority=5, scheduled_at_ms=0, now_ms=0,
                dedup_key="d", partition_key="C1", resource="acpx", max_attempts=3)
    initial_limit = pools.effective_limit("acpx")

    def boom(task, *, heartbeat=None):
        raise RetryableError("timeout réseau")  # is_overload=False par défaut

    w = Worker(led, pools, {"decide": boom}, worker_id="w1")
    w.run_once(now_ms=1, token="tok1")
    assert pools.effective_limit("acpx") == initial_limit  # M intact


def test_worker_retryable_with_overload_flag_lowers_limit(tmp_path):
    """RetryableError avec is_overload=True → effective_limit baisse (AIMD).
    On utilise limit=4 pour que le halving (→2) soit visible au-dessus du plancher 1.
    """
    led = TaskLedger(tmp_path / "q.db")
    pools = ResourcePools({"acpx": 4, "ib": 1})  # limit 4 pour que halving soit mesurable
    led.enqueue(kind="decide", priority=5, scheduled_at_ms=0, now_ms=0,
                dedup_key="d", partition_key="C1", resource="acpx", max_attempts=3)
    initial_limit = pools.effective_limit("acpx")  # 4

    def boom(task, *, heartbeat=None):
        raise RetryableError("rate limit externe", is_overload=True)

    w = Worker(led, pools, {"decide": boom}, worker_id="w1")
    w.run_once(now_ms=1, token="tok1")
    assert pools.effective_limit("acpx") < initial_limit  # 2 < 4 → M a baissé


def test_worker_resource_miss_unclaims_without_burning_attempt(tmp_path):
    """Course rare : free_resources signale acpx libre mais try_acquire échoue.
    La tâche doit revenir pending avec attempts=0 (aucune tentative brûlée)."""
    led, _ = _setup(tmp_path)
    led.enqueue(kind="decide", priority=5, scheduled_at_ms=0, now_ms=0,
                dedup_key="d", partition_key="C1", resource="acpx")

    class RacingPools:
        """Simule la course : free_resources dit oui, try_acquire dit non."""
        def free_resources(self):
            return ["acpx"]

        def try_acquire(self, resource):
            return False

        def release(self, resource):
            pass

    w = Worker(led, RacingPools(), {"decide": lambda t: None}, worker_id="w1")
    result = w.run_once(now_ms=1, token="tok1")
    assert result is True  # du travail a été tenté (claim + unclaim)

    row = led._conn.execute("SELECT status, attempts FROM tasks").fetchone()
    assert row["status"] == "pending"
    assert row["attempts"] == 0  # tentative non brûlée
