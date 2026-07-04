"""tests/queue_ledger/test_shared_statedb.py — TaskLedger sur StateDb partagé.

Vérifie :
1. TaskLedger(StateDb) : enqueue/claim/complete sur connexion partagée.
2. Deux TaskLedger construits avec le même StateDb partagent la connexion.
3. SqliteBroker + TaskLedger cohabitent dans la même casys.db (tables distinctes,
   opérations indépendantes sans collision).
"""
from __future__ import annotations

import pytest

from trader.state_db.connection import StateDb
from trader.queue.ledger import TaskLedger


# ---------------------------------------------------------------------------
# Test 1 — opérations de base via StateDb partagé
# ---------------------------------------------------------------------------

def test_shared_statedb_enqueue_claim_complete(tmp_path):
    """TaskLedger(StateDb) : enqueue/claim/complete fonctionnent."""
    db = StateDb(tmp_path / "casys.db")
    led = TaskLedger(db)

    tid = led.enqueue(
        kind="decide", priority=5, scheduled_at_ms=0, now_ms=0,
        dedup_key="t1", partition_key="AAPL",
    )
    assert isinstance(tid, int)

    task = led.claim(
        worker_id="w", token="tok", now_ms=1, lease_ms=1000, free_resources=[],
    )
    assert task is not None
    assert task["kind"] == "decide"
    assert task["status"] == "running"
    assert task["claim_token"] == "tok"

    ok = led.complete(task_id=task["id"], token="tok", now_ms=2)
    assert ok is True

    row = db.query_one("SELECT status FROM tasks WHERE id=?", (task["id"],))
    assert row["status"] == "done"


def test_shared_statedb_idempotent_schema(tmp_path):
    """Deux TaskLedger(StateDb) successifs ne lèvent pas (CREATE IF NOT EXISTS)."""
    db = StateDb(tmp_path / "casys.db")
    TaskLedger(db)
    TaskLedger(db)  # idempotent — ne doit pas lever


# ---------------------------------------------------------------------------
# Test 2 — deux ledgers sur le même StateDb partagent la connexion
# ---------------------------------------------------------------------------

def test_two_ledgers_share_same_statedb(tmp_path):
    """Deux TaskLedger(db) partagent la même instance StateDb."""
    db = StateDb(tmp_path / "casys.db")
    led1 = TaskLedger(db)
    led2 = TaskLedger(db)

    assert led1._db is led2._db, "les deux ledgers doivent référencer le même StateDb"

    # led1 enfile, led2 claime
    led1.enqueue(
        kind="decide", priority=5, scheduled_at_ms=0, now_ms=0,
        dedup_key="shared", partition_key="X",
    )
    task = led2.claim(
        worker_id="w", token="tok", now_ms=1, lease_ms=1000, free_resources=[],
    )
    assert task is not None
    assert task["kind"] == "decide"

    # led2 complete, led1 voit le résultat
    led2.complete(task_id=task["id"], token="tok", now_ms=2)
    row = db.query_one("SELECT status FROM tasks WHERE id=?", (task["id"],))
    assert row["status"] == "done"


# ---------------------------------------------------------------------------
# Test 3 — coexistence SqliteBroker + TaskLedger dans casys.db
# ---------------------------------------------------------------------------

def test_broker_and_ledger_coexist(tmp_path):
    """SqliteBroker et TaskLedger cohabitent dans la même DB sans collision."""
    from trader.state_db.migrations import import_broker_from_json
    from trader.state_db.broker_store import SqliteBroker

    db = StateDb(tmp_path / "casys.db")

    # Amorce broker (schéma + starting_cash) sans JSON préexistant
    import_broker_from_json(db, tmp_path / "broker.json", starting_cash=50_000.0)
    broker = SqliteBroker(db)

    # TaskLedger sur la même connexion — schéma additionnel créé proprement
    led = TaskLedger(db)

    # Broker opérationnel
    assert broker.cash() == pytest.approx(50_000.0)

    # Ledger opérationnel
    tid = led.enqueue(
        kind="decide", priority=5, scheduled_at_ms=0, now_ms=0,
        dedup_key="coex-1", partition_key="AAPL",
    )
    assert isinstance(tid, int)

    task = led.claim(
        worker_id="w", token="tok", now_ms=1, lease_ms=1000, free_resources=[],
    )
    assert task is not None
    led.complete(task_id=task["id"], token="tok", now_ms=2)

    # Les deux jeux de tables sont dans la même DB
    tables = {
        r[0]
        for r in db._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert "broker_state" in tables, "table broker_state absente"
    assert "tasks" in tables, "table tasks absente"

    # Les deux sont encore lisibles après les opérations croisées
    assert broker.cash() == pytest.approx(50_000.0)
    row = db.query_one("SELECT status FROM tasks WHERE id=?", (task["id"],))
    assert row["status"] == "done"
