"""Tests TDD — DecidePool + propagation du result dans le Worker.

Cas couverts :
  1. Worker result : handler → str → task.result non-NULL dans le ledger.
  2. Rétro-compat : handler no-op (retourne None) → task.result = NULL.
  3. Pool : 3 tâches decide enfilées → toutes 'done' avec result non-null.
  4. RetryableError non-overload → requeue (status='pending', attempts >= 1).
  5. RetryableError overload → on_overload() → effective_limit baisse (AIMD).
"""
from __future__ import annotations

import json
import time
from dataclasses import replace

import pytest

from trader.agent_protocol.types import Decision
from trader.application.decide_handler import make_decide_handler
from trader.queue.decide_pool import DecidePool
from trader.queue.ledger import TaskLedger
from trader.queue.pools import ResourcePools
from trader.queue.worker import Worker, RetryableError

SYMBOL = "AAPL"

_BASE_PAYLOAD = {
    "symbol": SYMBOL,
    "mandate": "test mandate",
    "memory": "test memory",
    "shared_context": {},
    "per_symbol_facts": {},
    "decision_timeout_s": 60,
    "agent_tools_enabled": False,
}


def _ok_decision(symbol: str = SYMBOL, action: str = "BUY") -> Decision:
    return Decision(
        symbol=symbol, action=action,  # type: ignore[arg-type]
        quantity=10.0, confidence=0.8, rationale="signal",
    )


class _FakeClient:
    """Client mock déterministe pour les tests du pool."""

    def __init__(self, responses: dict | None = None, *, raise_exc=None):
        self._responses = responses or {}
        self._raise_exc = raise_exc

    def decide_batch(self, *, symbols, **kwargs):
        if self._raise_exc is not None:
            raise self._raise_exc
        return {sym: self._responses.get(sym, _ok_decision(sym)) for sym in symbols}


def _enqueue_decide(led: TaskLedger, symbol: str, now_ms: int, *,
                    resource: str = "acpx", max_attempts: int = 3,
                    payload_override: dict | None = None) -> int:
    """Helper : enfile une tâche decide pour symbol."""
    payload = {**_BASE_PAYLOAD, "symbol": symbol, **(payload_override or {})}
    return led.enqueue(
        kind="decide",
        priority=0,
        scheduled_at_ms=now_ms,
        now_ms=now_ms,
        dedup_key=f"decide:{symbol}",
        partition_key=symbol,
        resource=resource,
        max_attempts=max_attempts,
        payload=json.dumps(payload),
    )


# ---------------------------------------------------------------------------
# 1 + 2 : propagation du result dans Worker.run_once
# ---------------------------------------------------------------------------

def test_worker_handler_result_ecrit_dans_ledger(tmp_path):
    """Handler retournant str → task.result = cette str dans le ledger."""
    led = TaskLedger(tmp_path / "q.db")
    pools = ResourcePools({})
    led.enqueue(kind="decide", priority=0, scheduled_at_ms=0, now_ms=0, dedup_key="d1")

    w = Worker(led, pools, {"decide": lambda task: "resultat_test"}, worker_id="w1")
    assert w.run_once(now_ms=1, token="t1") is True

    row = led._conn.execute("SELECT status, result FROM tasks WHERE id=1").fetchone()
    assert row["status"] == "done"
    assert row["result"] == "resultat_test"


def test_worker_noop_handler_result_none(tmp_path):
    """Handler no-op (None) → task.result = NULL (rétro-compat shadow)."""
    led = TaskLedger(tmp_path / "q.db")
    pools = ResourcePools({})
    led.enqueue(kind="decide", priority=0, scheduled_at_ms=0, now_ms=0, dedup_key="d1")

    w = Worker(led, pools, {"decide": lambda task: None}, worker_id="w1")
    assert w.run_once(now_ms=1, token="t1") is True

    row = led._conn.execute("SELECT status, result FROM tasks WHERE id=1").fetchone()
    assert row["status"] == "done"
    assert row["result"] is None


# ---------------------------------------------------------------------------
# 3 : pool — 3 tâches → done avec result non-null
# ---------------------------------------------------------------------------

def test_pool_3_taches_decide_done_avec_result(tmp_path):
    """3 tâches decide → toutes 'done' avec result JSON non-null après start/stop."""
    symbols = ["AAPL", "MSFT", "GOOG"]
    client = _FakeClient({sym: _ok_decision(sym) for sym in symbols})

    led = TaskLedger(tmp_path / "q.db")
    pools = ResourcePools({"acpx": 2})

    now_ms = int(time.time() * 1000)
    for sym in symbols:
        _enqueue_decide(led, sym, now_ms, resource="acpx")

    handlers = {"decide": make_decide_handler(codex_client=client)}
    pool = DecidePool(
        ledger=led,
        pools=pools,
        handlers=handlers,
        num_workers=2,
        now_fn=time.time,
    )
    pool.start()

    # Attend que les 3 tâches soient done (max 5 s)
    deadline = time.time() + 5.0
    while time.time() < deadline:
        if led.count_by_status("done", kind="decide") == 3:
            break
        time.sleep(0.05)

    pool.stop()

    assert led.count_by_status("done", kind="decide") == 3

    rows = led._conn.execute(
        "SELECT result FROM tasks WHERE kind='decide'"
    ).fetchall()
    assert len(rows) == 3
    for row in rows:
        assert row["result"] is not None, "result ne doit pas être NULL"
        data = json.loads(row["result"])
        assert data["action"] in ("BUY", "SELL", "HOLD")
        assert "symbol" in data


# ---------------------------------------------------------------------------
# 4 : RetryableError non-overload → requeue
# ---------------------------------------------------------------------------

def test_pool_retryable_error_requeue(tmp_path):
    """Handler lève RetryableError(is_overload=False) → tâche requeue ('pending')."""
    synthetic_hold = replace(
        Decision.hold(SYMBOL, "timeout LLM"),
        llm_error="timeout",
    )
    client = _FakeClient({SYMBOL: synthetic_hold})

    led = TaskLedger(tmp_path / "q.db")
    pools = ResourcePools({"acpx": 1})

    now_ms = int(time.time() * 1000)
    _enqueue_decide(led, SYMBOL, now_ms, resource="acpx", max_attempts=3)

    handlers = {"decide": make_decide_handler(codex_client=client)}
    pool = DecidePool(
        ledger=led, pools=pools, handlers=handlers,
        num_workers=1, now_fn=time.time,
        # Backoff long pour que la tâche ne soit pas reclaimed dans la fenêtre du test
        backoff_base_ms=60_000,
    )
    pool.start()

    # Attend qu'au moins 1 tentative soit enregistrée
    deadline = time.time() + 5.0
    while time.time() < deadline:
        row = led._conn.execute("SELECT attempts FROM tasks").fetchone()
        if row and row["attempts"] >= 1:
            break
        time.sleep(0.05)

    pool.stop()

    row = led._conn.execute("SELECT status, attempts FROM tasks").fetchone()
    assert row["status"] == "pending", f"attendu pending, got {row['status']}"
    assert row["attempts"] >= 1


# ---------------------------------------------------------------------------
# 5 : RetryableError overload → AIMD on_overload
# ---------------------------------------------------------------------------

def test_pool_overload_baisse_effective_limit(tmp_path):
    """RetryableError(is_overload=True) → effective_limit d'acpx baisse (AIMD)."""
    synthetic_hold = replace(
        Decision.hold(SYMBOL, "rate limit"),
        llm_error="rate_limited",
    )
    client = _FakeClient({SYMBOL: synthetic_hold})

    led = TaskLedger(tmp_path / "q.db")
    pools = ResourcePools({"acpx": 4})  # limit 4 → halving vers 2 visible
    initial_limit = pools.effective_limit("acpx")

    now_ms = int(time.time() * 1000)
    _enqueue_decide(led, SYMBOL, now_ms, resource="acpx", max_attempts=3)

    handlers = {"decide": make_decide_handler(codex_client=client)}
    pool = DecidePool(
        ledger=led, pools=pools, handlers=handlers,
        num_workers=1, now_fn=time.time,
        backoff_base_ms=60_000,
    )
    pool.start()

    # Attend qu'au moins 1 tentative soit enregistrée (on_overload appelé dedans)
    deadline = time.time() + 5.0
    while time.time() < deadline:
        row = led._conn.execute("SELECT attempts FROM tasks").fetchone()
        if row and row["attempts"] >= 1:
            break
        time.sleep(0.05)

    pool.stop()

    assert pools.effective_limit("acpx") < initial_limit, (
        f"effective_limit devrait avoir baissé après overload, "
        f"était {initial_limit}, est {pools.effective_limit('acpx')}"
    )
