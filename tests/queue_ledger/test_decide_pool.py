"""Tests TDD — DecidePool + propagation du result dans le Worker.

Cas couverts :
  1. Worker result : handler → str → task.result non-NULL dans le ledger.
  2. Rétro-compat : handler no-op (retourne None) → task.result = NULL.
  3. Pool : 3 tâches decide enfilées → toutes 'done' avec result non-null.
  4. RetryableError non-overload → requeue (status='pending', attempts >= 1).
  5. RetryableError overload → on_overload() → effective_limit baisse (AIMD).
  6. [FIX 1] finish_now_ms capturé après handler → scheduled_at ancré sur fin.
  7. [FIX 2] stop(timeout_s court) avec thread bloquant → thread vivant conservé.
  8. [FIX 3] start() lève RuntimeError si threads encore vivants.
  9. Worker idle : ne dépend pas du time.sleep global patché par les tests daemon.
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


# ---------------------------------------------------------------------------
# 6 : [FIX 1] finish_now_ms ancré sur la fin du handler, pas le début
# ---------------------------------------------------------------------------

def test_backoff_ancre_sur_finish_now_ms(tmp_path):
    """Handler lent (avance l'horloge de 900s) + RetryableError →
    scheduled_at reflète l'heure de FIN du handler, pas de début du claim.

    Sans fix : scheduled_at = now_ms_claim + backoff → déjà écoulé après 900s.
    Avec fix  : scheduled_at = finish_now_ms + backoff → correctement dans le futur.
    """
    T0_S = 1_000_000  # epoch fictive (secondes)
    ADVANCE_S = 900   # simule 15 min de LLM

    clock = [float(T0_S)]

    def fake_now() -> float:
        return clock[0]

    def slow_handler(task):
        clock[0] += ADVANCE_S  # avance l'horloge pendant le "traitement"
        raise RetryableError("timeout LLM simulé")

    led = TaskLedger(tmp_path / "q.db")
    pools = ResourcePools({})
    led.enqueue(
        kind="decide", priority=0,
        scheduled_at_ms=0, now_ms=0,
        dedup_key="d1", max_attempts=3,
    )

    pool = DecidePool(
        ledger=led,
        pools=pools,
        handlers={"decide": slow_handler},
        num_workers=1,
        now_fn=fake_now,
        backoff_base_ms=1000,
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

    row = led._conn.execute("SELECT status, scheduled_at FROM tasks").fetchone()
    assert row["status"] == "pending", f"attendu pending, got {row['status']}"

    # scheduled_at doit être >= T_finish * 1000 (pas claimable immédiatement
    # à l'heure de fin du handler)
    T_finish_ms = int((T0_S + ADVANCE_S) * 1000)
    assert row["scheduled_at"] >= T_finish_ms, (
        f"scheduled_at={row['scheduled_at']} devrait être >= T_finish_ms={T_finish_ms} — "
        f"le backoff doit être ancré sur finish_now_ms, pas now_ms_claim "
        f"(now_ms_claim * 1000 ≈ {T0_S * 1000})"
    )


# ---------------------------------------------------------------------------
# 7 : [FIX 2] stop() avec thread bloquant → thread vivant conservé dans _threads
# ---------------------------------------------------------------------------

def test_stop_thread_bloque_reste_reference(tmp_path):
    """stop(timeout_s court) avec un handler bloquant → _threads garde le thread
    encore vivant (pas de clear aveugle) et logge un warning."""
    import threading

    entered = threading.Event()
    unblock = threading.Event()

    led = TaskLedger(tmp_path / "q.db")
    pools = ResourcePools({})
    led.enqueue(kind="decide", priority=0, scheduled_at_ms=0, now_ms=0, dedup_key="d1")

    def blocking_handler(task):
        entered.set()   # signale que le handler est entré
        unblock.wait()  # bloque jusqu'au nettoyage du test
        return "done"

    pool = DecidePool(
        ledger=led,
        pools=pools,
        handlers={"decide": blocking_handler},
        num_workers=1,
        now_fn=time.time,
    )
    pool.start()

    # Attend que le handler soit entré
    assert entered.wait(timeout=5.0), "le handler n'a pas démarré dans les temps"

    # stop avec timeout court → le thread est encore vivant à l'expiration
    pool.stop(timeout_s=0.2)

    try:
        # Le thread vivant ne doit PAS avoir été clearé de _threads
        assert len(pool._threads) >= 1, (
            "_threads ne doit pas être vidé quand un thread est encore vivant"
        )
        assert any(t.is_alive() for t in pool._threads), (
            "le thread encore vivant doit rester référencé dans _threads"
        )
    finally:
        # Nettoyage : débloque le handler pour permettre une fin propre
        unblock.set()
        for t in pool._threads:
            t.join(timeout=2.0)


# ---------------------------------------------------------------------------
# 8 : [FIX 3] start() lève RuntimeError si threads encore vivants
# ---------------------------------------------------------------------------

def test_start_leve_si_threads_encore_vivants(tmp_path):
    """start() après un stop incomplet (threads encore vivants) → RuntimeError."""
    import threading

    entered = threading.Event()
    unblock = threading.Event()

    led = TaskLedger(tmp_path / "q.db")
    pools = ResourcePools({})
    led.enqueue(kind="decide", priority=0, scheduled_at_ms=0, now_ms=0, dedup_key="d1")

    def blocking_handler(task):
        entered.set()
        unblock.wait()
        return "done"

    pool = DecidePool(
        ledger=led,
        pools=pools,
        handlers={"decide": blocking_handler},
        num_workers=1,
        now_fn=time.time,
    )
    pool.start()

    assert entered.wait(timeout=5.0), "le handler n'a pas démarré dans les temps"

    # stop partiel → thread encore vivant dans _threads
    pool.stop(timeout_s=0.2)

    try:
        # Un second start() doit être refusé
        with pytest.raises(RuntimeError, match="threads encore actifs"):
            pool.start()
    finally:
        # Nettoyage
        unblock.set()
        for t in pool._threads:
            t.join(timeout=2.0)


# ---------------------------------------------------------------------------
# 9 : worker idle indépendant du time.sleep global
# ---------------------------------------------------------------------------

def test_idle_worker_ne_depend_pas_du_time_sleep_global(monkeypatch, tmp_path):
    """Les tests daemon patchent parfois time.sleep pour piloter la boucle main.

    Les workers queue tournent en arrière-plan et ne doivent pas hériter de ce
    patch, sinon CASYS_QUEUE_DECIDE_ENABLED=1 rend les tests main instables.
    """
    real_sleep = time.sleep
    calls: list[float] = []

    def forbidden_sleep(seconds: float) -> None:
        calls.append(seconds)
        raise KeyboardInterrupt("sleep global patché par un test daemon")

    monkeypatch.setattr(time, "sleep", forbidden_sleep)

    led = TaskLedger(tmp_path / "q.db")
    pools = ResourcePools({})
    pool = DecidePool(
        ledger=led,
        pools=pools,
        handlers={"decide": lambda task: "done"},
        num_workers=1,
        now_fn=time.time,
    )
    pool.start()

    try:
        real_sleep(0.12)
        assert calls == []
        assert pool._threads and all(t.is_alive() for t in pool._threads)
    finally:
        pool.stop(timeout_s=1.0)
