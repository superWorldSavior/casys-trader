# Task-Ledger — Lot A / Phase 0 (cœur de la file) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Construire le cœur autonome et testé du `task_ledger.db` (file de tâches durable SQLite : enqueue idempotent, claim atomique resource-aware + fencing, retry/backoff, reprise au boot, pools de ressources AIMD, worker), sans le brancher au daemon.

**Architecture:** Un module `trader/queue/` in-process, threads. `TaskLedger` encapsule une connexion SQLite unique (WAL, `busy_timeout`, `check_same_thread=False`) protégée par un `threading.Lock`, exactement comme `trader/learnings/store.py`. Le claim est une transaction `BEGIN IMMEDIATE` avec sous-requête + `RETURNING`, sérialisée par `partition_key` (au claim ET par index unique partiel), filtrée par ressources libres. Le temps est **injecté** (`now_ms`) pour un déterminisme total.

**Tech Stack:** Python stdlib (`sqlite3`, `threading`, `uuid`), `pytest`. Zéro dépendance nouvelle.

## Global Constraints

- **Zéro dépendance nouvelle** : `sqlite3`/`threading`/`uuid` stdlib uniquement.
- **SQLite** : `PRAGMA journal_mode=WAL`, `PRAGMA busy_timeout=5000`, `sqlite3.connect(..., check_same_thread=False)`, une connexion partagée + `threading.Lock` (pattern `trader/learnings/store.py:26,31,107`).
- **Timestamps = epoch millisecondes (int)**. Jamais de TEXT comparé lexicographiquement.
- **Temps injecté** : toute méthode dépendant de l'heure prend `now_ms: int` en paramètre (AX / Deterministic Outputs — comme `now` dans `trader/tools/scheduler.py`).
- **Threads in-process, pas d'asyncio.**
- **AX** : erreurs machine-readable, narrow contracts (une fonction = une chose), test-first, edge-cases prioritaires.
- **Rien branché au daemon en Phase 0** ; flag maître `CASYS_QUEUE_ENABLED` reste à `0` (défini plus tard).
- **Vérifier le vrai exit code pytest** : lancer `pytest ...; echo "EXIT=$?"`, jamais conclure « vert » sur un pipe (`| tail` renvoie le code de `tail`).

## File Structure

- Create `trader/queue/__init__.py` — package marker.
- Create `trader/queue/ledger.py` — `TaskLedger` : schéma/migrations, `enqueue`, `claim`, `complete`, `fail`, `heartbeat`, `recover_on_boot`.
- Create `trader/queue/pools.py` — `ResourcePools` : sémaphores nommés + backpressure AIMD + `free_resources()`.
- Create `trader/queue/worker.py` — `Worker` : `run_once()` (claim → acquire ressource → handler → complete/fail → release) + thread heartbeat.
- Create `tests/queue_ledger/__init__.py`, `tests/queue_ledger/test_ledger.py`, `tests/queue_ledger/test_pools.py`, `tests/queue_ledger/test_worker.py`.

---

### Task 1: `TaskLedger` — connexion + schéma idempotent

**Files:**
- Create: `trader/queue/__init__.py` (vide)
- Create: `trader/queue/ledger.py`
- Test: `tests/queue_ledger/__init__.py` (vide), `tests/queue_ledger/test_ledger.py`

**Interfaces:**
- Produces: `TaskLedger(db_path: str | Path)` ; attribut `path` ; connexion en WAL. Table `tasks` (schéma §4.2 de la spec) + index `idx_claim`, `uniq_running_partition`, `uniq_active_kind_partition`. Migration idempotente (ré-instanciation = no-op).

- [ ] **Step 1: Write the failing test**

```python
# tests/queue_ledger/test_ledger.py
import sqlite3
from trader.queue.ledger import TaskLedger


def _cols(conn, table):
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def test_schema_created_and_wal(tmp_path):
    db = tmp_path / "task_ledger.db"
    TaskLedger(db)  # crée le schéma
    conn = sqlite3.connect(db)
    assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    cols = _cols(conn, "tasks")
    assert {"id", "kind", "dedup_key", "partition_key", "resource",
            "priority", "status", "attempts", "max_attempts",
            "scheduled_at", "lease_expires_at", "claim_token"} <= cols
    idx = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index'")}
    assert {"uniq_running_partition", "uniq_active_kind_partition"} <= idx


def test_reinstantiation_is_idempotent(tmp_path):
    db = tmp_path / "task_ledger.db"
    TaskLedger(db)
    TaskLedger(db)  # ne doit pas lever
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/erwanpesle/Documents/GitHub/casys-trader && python -m pytest tests/queue_ledger/test_ledger.py -q; echo "EXIT=$?"`
Expected: FAIL — `ModuleNotFoundError: trader.queue.ledger`.

- [ ] **Step 3: Write minimal implementation**

```python
# trader/queue/ledger.py
"""task_ledger — file de tâches durable SQLite (Lot A).

Une connexion partagée (WAL) protégée par un Lock, comme learnings/store.py.
Temps injecté (now_ms) pour déterminisme. Timestamps = epoch ms (int).
"""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  kind           TEXT NOT NULL,
  dedup_key      TEXT UNIQUE,
  partition_key  TEXT,
  resource       TEXT,
  priority       INTEGER NOT NULL,
  payload        TEXT,
  status         TEXT NOT NULL,
  attempts       INTEGER DEFAULT 0,
  max_attempts   INTEGER DEFAULT 3,
  scheduled_at   INTEGER NOT NULL,
  lease_expires_at INTEGER,
  claim_token    TEXT,
  claimed_by     TEXT,
  enqueued_seq   INTEGER,
  parent_id      INTEGER,
  result         TEXT,
  error          TEXT,
  created_at     INTEGER,
  updated_at     INTEGER
);
CREATE INDEX IF NOT EXISTS idx_claim ON tasks(status, priority, scheduled_at, id);
CREATE UNIQUE INDEX IF NOT EXISTS uniq_running_partition
  ON tasks(partition_key) WHERE status='running' AND partition_key IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uniq_active_kind_partition
  ON tasks(kind, partition_key)
  WHERE status IN ('pending','running') AND partition_key IS NOT NULL;
"""


class TaskLedger:
    def __init__(self, db_path: str | Path):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/erwanpesle/Documents/GitHub/casys-trader && python -m pytest tests/queue_ledger/test_ledger.py -q; echo "EXIT=$?"`
Expected: PASS, `EXIT=0`.

- [ ] **Step 5: Commit**

```bash
git add trader/queue/__init__.py trader/queue/ledger.py tests/queue_ledger/__init__.py tests/queue_ledger/test_ledger.py
git commit -m "feat(queue): TaskLedger schéma SQLite idempotent (WAL + index partiels)"
```

---

### Task 2: `enqueue` idempotent (dedup_key)

**Files:**
- Modify: `trader/queue/ledger.py`
- Test: `tests/queue_ledger/test_ledger.py`

**Interfaces:**
- Produces: `TaskLedger.enqueue(*, kind, priority, scheduled_at_ms, now_ms, dedup_key=None, partition_key=None, resource=None, payload=None, max_attempts=3, parent_id=None) -> int | None`. Retourne l'`id` créé, ou `None` si `dedup_key` déjà présent (`ON CONFLICT DO NOTHING`). Statut initial `'pending'`, `attempts=0`.

- [ ] **Step 1: Write the failing test**

```python
# tests/queue_ledger/test_ledger.py (append)
def test_enqueue_returns_id_and_is_idempotent(tmp_path):
    led = TaskLedger(tmp_path / "q.db")
    tid = led.enqueue(kind="decide", priority=5, scheduled_at_ms=1000,
                      now_ms=1000, dedup_key="decide|C1|1000", partition_key="C1",
                      resource="acpx", payload='{"chunk": ["AAPL"]}')
    assert isinstance(tid, int)
    dup = led.enqueue(kind="decide", priority=5, scheduled_at_ms=1000,
                      now_ms=1000, dedup_key="decide|C1|1000", partition_key="C1",
                      resource="acpx")
    assert dup is None  # conflit dedup → pas de doublon
    (n,) = led._conn.execute("SELECT COUNT(*) FROM tasks").fetchone()
    assert n == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/erwanpesle/Documents/GitHub/casys-trader && python -m pytest tests/queue_ledger/test_ledger.py::test_enqueue_returns_id_and_is_idempotent -q; echo "EXIT=$?"`
Expected: FAIL — `AttributeError: 'TaskLedger' object has no attribute 'enqueue'`.

- [ ] **Step 3: Write minimal implementation**

```python
# trader/queue/ledger.py (méthode de TaskLedger)
    def enqueue(self, *, kind, priority, scheduled_at_ms, now_ms,
                dedup_key=None, partition_key=None, resource=None,
                payload=None, max_attempts=3, parent_id=None):
        with self._lock:
            cur = self._conn.execute(
                """INSERT INTO tasks(kind, dedup_key, partition_key, resource,
                        priority, payload, status, attempts, max_attempts,
                        scheduled_at, enqueued_seq, parent_id, created_at, updated_at)
                   VALUES(?,?,?,?,?,?, 'pending', 0, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(dedup_key) DO NOTHING
                   RETURNING id""",
                (kind, dedup_key, partition_key, resource, priority, payload,
                 max_attempts, scheduled_at_ms, now_ms, parent_id, now_ms, now_ms),
            )
            row = cur.fetchone()
            self._conn.commit()
            return int(row["id"]) if row else None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/erwanpesle/Documents/GitHub/casys-trader && python -m pytest tests/queue_ledger/test_ledger.py -q; echo "EXIT=$?"`
Expected: PASS, `EXIT=0`.

- [ ] **Step 5: Commit**

```bash
git add trader/queue/ledger.py tests/queue_ledger/test_ledger.py
git commit -m "feat(queue): enqueue idempotent (ON CONFLICT dedup_key)"
```

---

### Task 3: `claim` — priorité, échéance, tie-break, fencing token

**Files:**
- Modify: `trader/queue/ledger.py`
- Test: `tests/queue_ledger/test_ledger.py`

**Interfaces:**
- Produces: `TaskLedger.claim(*, worker_id, token, now_ms, lease_ms, free_resources) -> dict | None`. Sélectionne la tâche `pending` **due** (`scheduled_at <= now_ms`) avec `attempts < max_attempts`, `ORDER BY priority ASC, scheduled_at ASC, id ASC`, en `BEGIN IMMEDIATE`. Passe `status='running'`, pose `claim_token=token`, `claimed_by=worker_id`, `lease_expires_at=now_ms+lease_ms`, `attempts+=1`. Retourne la ligne (dict) ou `None`. (La sérialisation par clé + resource-aware arrivent Tasks 4-5 ; ici `free_resources` est accepté mais le filtre ressource est ajouté Task 5.)

- [ ] **Step 1: Write the failing test**

```python
# tests/queue_ledger/test_ledger.py (append)
def test_claim_orders_by_priority_then_id(tmp_path):
    led = TaskLedger(tmp_path / "q.db")
    led.enqueue(kind="decide", priority=5, scheduled_at_ms=0, now_ms=0,
                dedup_key="d1", partition_key="C1")
    led.enqueue(kind="apply_exits", priority=0, scheduled_at_ms=0, now_ms=0,
                dedup_key="e1", partition_key="p")
    claimed = led.claim(worker_id="w1", token="t1", now_ms=10,
                        lease_ms=1000, free_resources=[])
    assert claimed["kind"] == "apply_exits"   # priorité 0 avant 5
    assert claimed["status"] == "running"
    assert claimed["claim_token"] == "t1"
    assert claimed["attempts"] == 1
    assert claimed["lease_expires_at"] == 1010


def test_claim_skips_future_and_exhausted(tmp_path):
    led = TaskLedger(tmp_path / "q.db")
    led.enqueue(kind="decide", priority=5, scheduled_at_ms=5000, now_ms=0,
                dedup_key="future")  # pas encore due
    assert led.claim(worker_id="w1", token="t1", now_ms=10,
                     lease_ms=1000, free_resources=[]) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/erwanpesle/Documents/GitHub/casys-trader && python -m pytest tests/queue_ledger/test_ledger.py::test_claim_orders_by_priority_then_id tests/queue_ledger/test_ledger.py::test_claim_skips_future_and_exhausted -q; echo "EXIT=$?"`
Expected: FAIL — `AttributeError: ... 'claim'`.

- [ ] **Step 3: Write minimal implementation**

```python
# trader/queue/ledger.py (méthode de TaskLedger)
    def claim(self, *, worker_id, token, now_ms, lease_ms, free_resources):
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("BEGIN IMMEDIATE")
            try:
                row = cur.execute(
                    """UPDATE tasks
                       SET status='running', claimed_by=?, claim_token=?,
                           lease_expires_at=?, attempts=attempts+1, updated_at=?
                       WHERE id = (
                         SELECT id FROM tasks
                         WHERE status='pending' AND scheduled_at <= ?
                           AND attempts < max_attempts
                         ORDER BY priority ASC, scheduled_at ASC, id ASC
                         LIMIT 1)
                       RETURNING *""",
                    (worker_id, token, now_ms + lease_ms, now_ms, now_ms),
                ).fetchone()
                self._conn.commit()
                return dict(row) if row else None
            except Exception:
                self._conn.rollback()
                raise
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/erwanpesle/Documents/GitHub/casys-trader && python -m pytest tests/queue_ledger/test_ledger.py -q; echo "EXIT=$?"`
Expected: PASS, `EXIT=0`.

- [ ] **Step 5: Commit**

```bash
git add trader/queue/ledger.py tests/queue_ledger/test_ledger.py
git commit -m "feat(queue): claim atomique BEGIN IMMEDIATE (priorité + fencing token)"
```

---

### Task 4: `claim` — sérialisation par `partition_key` + invariant DB

**Files:**
- Modify: `trader/queue/ledger.py`
- Test: `tests/queue_ledger/test_ledger.py`

**Interfaces:**
- Modifies `claim` : la sous-requête exclut toute `partition_key` déjà `running`. Invariant DB `uniq_running_partition` déjà en place (Task 1) = filet.

- [ ] **Step 1: Write the failing test**

```python
# tests/queue_ledger/test_ledger.py (append)
def test_claim_serializes_same_partition(tmp_path):
    led = TaskLedger(tmp_path / "q.db")
    led.enqueue(kind="refresh_symbol", priority=5, scheduled_at_ms=0, now_ms=0,
                dedup_key="r-aapl-1", partition_key="AAPL")
    led.enqueue(kind="refresh_symbol", priority=5, scheduled_at_ms=0, now_ms=0,
                dedup_key="r-msft-1", partition_key="MSFT")
    # Note: 2 tâches actives même partition interdites par uniq_active_kind_partition,
    # donc on teste la sérialisation running via 2 kinds différents, même partition :
    led.enqueue(kind="arm_watch", priority=5, scheduled_at_ms=0, now_ms=0,
                dedup_key="w-aapl-1", partition_key="AAPL")
    first = led.claim(worker_id="w1", token="t1", now_ms=1, lease_ms=1000,
                      free_resources=[])
    assert first["partition_key"] == "AAPL"
    # AAPL est maintenant running → la prochaine claim doit sauter tout AAPL
    second = led.claim(worker_id="w2", token="t2", now_ms=1, lease_ms=1000,
                       free_resources=[])
    assert second["partition_key"] == "MSFT"
    third = led.claim(worker_id="w3", token="t3", now_ms=1, lease_ms=1000,
                      free_resources=[])
    assert third is None  # le 2e AAPL reste bloqué tant que le 1er est running
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/erwanpesle/Documents/GitHub/casys-trader && python -m pytest tests/queue_ledger/test_ledger.py::test_claim_serializes_same_partition -q; echo "EXIT=$?"`
Expected: FAIL — `third` n'est pas `None` (AAPL est reclaimé).

- [ ] **Step 3: Write minimal implementation**

```python
# trader/queue/ledger.py — dans claim(), remplacer la sous-requête SELECT par :
                         SELECT id FROM tasks
                         WHERE status='pending' AND scheduled_at <= ?
                           AND attempts < max_attempts
                           AND (partition_key IS NULL OR partition_key NOT IN (
                                 SELECT partition_key FROM tasks
                                 WHERE status='running'
                                   AND partition_key IS NOT NULL))
                         ORDER BY priority ASC, scheduled_at ASC, id ASC
                         LIMIT 1)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/erwanpesle/Documents/GitHub/casys-trader && python -m pytest tests/queue_ledger/test_ledger.py -q; echo "EXIT=$?"`
Expected: PASS, `EXIT=0`.

- [ ] **Step 5: Commit**

```bash
git add trader/queue/ledger.py tests/queue_ledger/test_ledger.py
git commit -m "feat(queue): claim sérialisé par partition_key (au plus 1 running/clé)"
```

---

### Task 5: `claim` — resource-aware (permits libres)

**Files:**
- Modify: `trader/queue/ledger.py`
- Test: `tests/queue_ledger/test_ledger.py`

**Interfaces:**
- Modifies `claim` : ne sélectionne une tâche avec `resource IS NOT NULL` que si sa `resource ∈ free_resources`. Une tâche `resource IS NULL` est toujours éligible. Empêche les workers de claim des tâches dont la ressource est saturée (anti-piégeage §4.2).

- [ ] **Step 1: Write the failing test**

```python
# tests/queue_ledger/test_ledger.py (append)
def test_claim_respects_free_resources(tmp_path):
    led = TaskLedger(tmp_path / "q.db")
    led.enqueue(kind="decide", priority=5, scheduled_at_ms=0, now_ms=0,
                dedup_key="d-acpx", partition_key="C1", resource="acpx")
    led.enqueue(kind="apply_exits", priority=0, scheduled_at_ms=0, now_ms=0,
                dedup_key="e-ib", partition_key="portfolio", resource="ib")
    # acpx saturé (pas dans free), ib libre → seule la sortie ib est claimable
    claimed = led.claim(worker_id="w1", token="t1", now_ms=1, lease_ms=1000,
                        free_resources=["ib"])
    assert claimed["resource"] == "ib"
    # plus aucune ressource libre → decide(acpx) reste non claimable
    assert led.claim(worker_id="w2", token="t2", now_ms=1, lease_ms=1000,
                     free_resources=[]) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/erwanpesle/Documents/GitHub/casys-trader && python -m pytest tests/queue_ledger/test_ledger.py::test_claim_respects_free_resources -q; echo "EXIT=$?"`
Expected: FAIL — la tâche `acpx` est claimée alors que `acpx` n'est pas libre.

- [ ] **Step 3: Write minimal implementation**

```python
# trader/queue/ledger.py — dans claim(), construire dynamiquement la clause ressource :
    def claim(self, *, worker_id, token, now_ms, lease_ms, free_resources):
        if free_resources:
            ph = ",".join("?" for _ in free_resources)
            res_clause = f"(resource IS NULL OR resource IN ({ph}))"
            res_params = list(free_resources)
        else:
            res_clause = "resource IS NULL"
            res_params = []
        sql = f"""UPDATE tasks
                  SET status='running', claimed_by=?, claim_token=?,
                      lease_expires_at=?, attempts=attempts+1, updated_at=?
                  WHERE id = (
                    SELECT id FROM tasks
                    WHERE status='pending' AND scheduled_at <= ?
                      AND attempts < max_attempts
                      AND {res_clause}
                      AND (partition_key IS NULL OR partition_key NOT IN (
                            SELECT partition_key FROM tasks
                            WHERE status='running' AND partition_key IS NOT NULL))
                    ORDER BY priority ASC, scheduled_at ASC, id ASC
                    LIMIT 1)
                  RETURNING *"""
        params = (worker_id, token, now_ms + lease_ms, now_ms, now_ms, *res_params)
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("BEGIN IMMEDIATE")
            try:
                row = cur.execute(sql, params).fetchone()
                self._conn.commit()
                return dict(row) if row else None
            except Exception:
                self._conn.rollback()
                raise
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/erwanpesle/Documents/GitHub/casys-trader && python -m pytest tests/queue_ledger/test_ledger.py -q; echo "EXIT=$?"`
Expected: PASS, `EXIT=0`.

- [ ] **Step 5: Commit**

```bash
git add trader/queue/ledger.py tests/queue_ledger/test_ledger.py
git commit -m "feat(queue): claim resource-aware (ne claim que si permit libre)"
```

---

### Task 6: `complete` / `fail` gardés par fencing token + backoff

**Files:**
- Modify: `trader/queue/ledger.py`
- Test: `tests/queue_ledger/test_ledger.py`

**Interfaces:**
- Produces: `TaskLedger.complete(*, task_id, token, now_ms, result=None) -> bool` (True si `claim_token` matche, sinon False sans effet). `TaskLedger.fail(*, task_id, token, now_ms, error, retryable, backoff_base_ms) -> str` : si `retryable` et `attempts < max_attempts` → `status='pending'`, `scheduled_at = now_ms + backoff_base_ms * 2**(attempts-1)`, retourne `"pending"` ; sinon → `status='dead'`, retourne `"dead"`. Les deux exigent le bon `token` (fencing) ; token périmé → no-op, retourne `False`/`"stale"`.

- [ ] **Step 1: Write the failing test**

```python
# tests/queue_ledger/test_ledger.py (append)
def _claim_one(led, now_ms=1):
    return led.claim(worker_id="w", token="tok", now_ms=now_ms,
                     lease_ms=1000, free_resources=["acpx", "ib", "yahoo"])


def test_complete_requires_token(tmp_path):
    led = TaskLedger(tmp_path / "q.db")
    led.enqueue(kind="decide", priority=5, scheduled_at_ms=0, now_ms=0,
                dedup_key="d", partition_key="C1", resource="acpx")
    t = _claim_one(led)
    assert led.complete(task_id=t["id"], token="WRONG", now_ms=2) is False
    assert led.complete(task_id=t["id"], token="tok", now_ms=2) is True
    (st,) = led._conn.execute("SELECT status FROM tasks WHERE id=?",
                              (t["id"],)).fetchone()
    assert st == "done"


def test_fail_retryable_backoff_then_dead(tmp_path):
    led = TaskLedger(tmp_path / "q.db")
    led.enqueue(kind="decide", priority=5, scheduled_at_ms=0, now_ms=0,
                dedup_key="d", partition_key="C1", resource="acpx", max_attempts=2)
    t = _claim_one(led, now_ms=100)            # attempts -> 1
    status = led.fail(task_id=t["id"], token="tok", now_ms=100,
                      error="overload", retryable=True, backoff_base_ms=1000)
    assert status == "pending"
    (sched,) = led._conn.execute("SELECT scheduled_at FROM tasks WHERE id=?",
                                 (t["id"],)).fetchone()
    assert sched == 100 + 1000 * 2 ** 0        # backoff = base * 2^(attempts-1)
    t2 = _claim_one(led, now_ms=2000)          # attempts -> 2 (== max)
    status2 = led.fail(task_id=t2["id"], token="tok", now_ms=2000,
                       error="overload", retryable=True, backoff_base_ms=1000)
    assert status2 == "dead"                    # plus de tentative disponible
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/erwanpesle/Documents/GitHub/casys-trader && python -m pytest tests/queue_ledger/test_ledger.py::test_complete_requires_token tests/queue_ledger/test_ledger.py::test_fail_retryable_backoff_then_dead -q; echo "EXIT=$?"`
Expected: FAIL — `AttributeError: ... 'complete'`.

- [ ] **Step 3: Write minimal implementation**

```python
# trader/queue/ledger.py (méthodes de TaskLedger)
    def complete(self, *, task_id, token, now_ms, result=None):
        with self._lock:
            cur = self._conn.execute(
                """UPDATE tasks SET status='done', result=?, updated_at=?
                   WHERE id=? AND claim_token=?""",
                (result, now_ms, task_id, token))
            self._conn.commit()
            return cur.rowcount == 1

    def fail(self, *, task_id, token, now_ms, error, retryable, backoff_base_ms):
        with self._lock:
            row = self._conn.execute(
                "SELECT attempts, max_attempts FROM tasks WHERE id=? AND claim_token=?",
                (task_id, token)).fetchone()
            if row is None:
                self._conn.commit()
                return "stale"
            attempts, max_attempts = row["attempts"], row["max_attempts"]
            if retryable and attempts < max_attempts:
                next_at = now_ms + backoff_base_ms * (2 ** (attempts - 1))
                self._conn.execute(
                    """UPDATE tasks SET status='pending', error=?, scheduled_at=?,
                           claim_token=NULL, claimed_by=NULL, lease_expires_at=NULL,
                           updated_at=? WHERE id=?""",
                    (error, next_at, now_ms, task_id))
                self._conn.commit()
                return "pending"
            self._conn.execute(
                "UPDATE tasks SET status='dead', error=?, updated_at=? WHERE id=?",
                (error, now_ms, task_id))
            self._conn.commit()
            return "dead"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/erwanpesle/Documents/GitHub/casys-trader && python -m pytest tests/queue_ledger/test_ledger.py -q; echo "EXIT=$?"`
Expected: PASS, `EXIT=0`.

- [ ] **Step 5: Commit**

```bash
git add trader/queue/ledger.py tests/queue_ledger/test_ledger.py
git commit -m "feat(queue): complete/fail avec fencing token + backoff exponentiel"
```

---

### Task 7: `heartbeat` (prolonge la lease) + `recover_on_boot`

**Files:**
- Modify: `trader/queue/ledger.py`
- Test: `tests/queue_ledger/test_ledger.py`

**Interfaces:**
- Produces: `TaskLedger.heartbeat(*, task_id, token, now_ms, lease_ms) -> bool` : si `claim_token` matche, `lease_expires_at = now_ms + lease_ms`, retourne True. `TaskLedger.recover_on_boot(*, now_ms) -> int` : toute tâche `running` avec `lease_expires_at < now_ms` repasse `pending` (token/lease effacés), retourne le nombre repending. Une `running` à lease valide (heartbeat récent) N'est PAS reprise.

- [ ] **Step 1: Write the failing test**

```python
# tests/queue_ledger/test_ledger.py (append)
def test_heartbeat_extends_lease_and_boot_recovery(tmp_path):
    led = TaskLedger(tmp_path / "q.db")
    led.enqueue(kind="decide", priority=5, scheduled_at_ms=0, now_ms=0,
                dedup_key="alive", partition_key="C1", resource="acpx")
    led.enqueue(kind="decide", priority=5, scheduled_at_ms=0, now_ms=0,
                dedup_key="dead", partition_key="C2", resource="acpx")
    alive = led.claim(worker_id="w1", token="a", now_ms=0, lease_ms=1000,
                      free_resources=["acpx"])
    stuck = led.claim(worker_id="w2", token="b", now_ms=0, lease_ms=1000,
                      free_resources=["acpx"])
    # 'alive' reçoit un heartbeat à t=900 → lease repoussée à 900+1000
    assert led.heartbeat(task_id=alive["id"], token="a", now_ms=900,
                         lease_ms=1000) is True
    # boot à t=1500 : 'stuck' (lease 1000 expirée) repris, 'alive' (lease 1900) non
    n = led.recover_on_boot(now_ms=1500)
    assert n == 1
    rows = {r["id"]: r["status"] for r in
            led._conn.execute("SELECT id, status FROM tasks")}
    assert rows[alive["id"]] == "running"
    assert rows[stuck["id"]] == "pending"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/erwanpesle/Documents/GitHub/casys-trader && python -m pytest tests/queue_ledger/test_ledger.py::test_heartbeat_extends_lease_and_boot_recovery -q; echo "EXIT=$?"`
Expected: FAIL — `AttributeError: ... 'heartbeat'`.

- [ ] **Step 3: Write minimal implementation**

```python
# trader/queue/ledger.py (méthodes de TaskLedger)
    def heartbeat(self, *, task_id, token, now_ms, lease_ms):
        with self._lock:
            cur = self._conn.execute(
                """UPDATE tasks SET lease_expires_at=?, updated_at=?
                   WHERE id=? AND claim_token=? AND status='running'""",
                (now_ms + lease_ms, now_ms, task_id, token))
            self._conn.commit()
            return cur.rowcount == 1

    def recover_on_boot(self, *, now_ms):
        with self._lock:
            cur = self._conn.execute(
                """UPDATE tasks SET status='pending', claim_token=NULL,
                       claimed_by=NULL, lease_expires_at=NULL, updated_at=?
                   WHERE status='running' AND lease_expires_at < ?""",
                (now_ms, now_ms))
            self._conn.commit()
            return cur.rowcount
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/erwanpesle/Documents/GitHub/casys-trader && python -m pytest tests/queue_ledger/test_ledger.py -q; echo "EXIT=$?"`
Expected: PASS, `EXIT=0`.

- [ ] **Step 5: Commit**

```bash
git add trader/queue/ledger.py tests/queue_ledger/test_ledger.py
git commit -m "feat(queue): heartbeat (prolonge lease) + recover_on_boot (running orphelines)"
```

---

### Task 8: `ResourcePools` — sémaphores nommés + backpressure AIMD

**Files:**
- Create: `trader/queue/pools.py`
- Test: `tests/queue_ledger/test_pools.py`

**Interfaces:**
- Produces: `ResourcePools(limits: dict[str, int])`. `free_resources() -> list[str]` (ressources avec ≥1 permit libre). `try_acquire(resource) -> bool` (non bloquant), `release(resource)`. AIMD : `on_overload(resource)` (limite effective ×0.5, min 1), `on_success(resource)` (limite effective +1, plafonnée à la limite initiale). Les permits libres = limite effective − permits pris.

- [ ] **Step 1: Write the failing test**

```python
# tests/queue_ledger/test_pools.py
from trader.queue.pools import ResourcePools


def test_try_acquire_bounds_and_free_resources():
    pools = ResourcePools({"acpx": 2, "ib": 1})
    assert set(pools.free_resources()) == {"acpx", "ib"}
    assert pools.try_acquire("acpx") is True
    assert pools.try_acquire("acpx") is True
    assert pools.try_acquire("acpx") is False        # limite 2 atteinte
    assert "acpx" not in pools.free_resources()
    pools.release("acpx")
    assert "acpx" in pools.free_resources()


def test_aimd_backpressure():
    pools = ResourcePools({"acpx": 4})
    pools.on_overload("acpx")                          # 4 -> 2
    assert pools.effective_limit("acpx") == 2
    pools.on_overload("acpx")                          # 2 -> 1
    assert pools.effective_limit("acpx") == 1
    pools.on_overload("acpx")                          # plancher 1
    assert pools.effective_limit("acpx") == 1
    pools.on_success("acpx")                           # 1 -> 2
    pools.on_success("acpx"); pools.on_success("acpx")  # 3, 4
    pools.on_success("acpx")                           # plafond = limite initiale 4
    assert pools.effective_limit("acpx") == 4
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/erwanpesle/Documents/GitHub/casys-trader && python -m pytest tests/queue_ledger/test_pools.py -q; echo "EXIT=$?"`
Expected: FAIL — `ModuleNotFoundError: trader.queue.pools`.

- [ ] **Step 3: Write minimal implementation**

```python
# trader/queue/pools.py
"""Pools de ressources : bornage de concurrence par ressource externe + AIMD."""
from __future__ import annotations

import threading


class ResourcePools:
    def __init__(self, limits: dict[str, int]):
        self._max = dict(limits)                       # plafond initial
        self._eff = dict(limits)                       # limite effective (AIMD)
        self._used = {r: 0 for r in limits}
        self._lock = threading.Lock()

    def effective_limit(self, resource) -> int:
        with self._lock:
            return self._eff[resource]

    def free_resources(self) -> list[str]:
        with self._lock:
            return [r for r in self._max
                    if self._used[r] < self._eff[r]]

    def try_acquire(self, resource) -> bool:
        with self._lock:
            if self._used[resource] < self._eff[resource]:
                self._used[resource] += 1
                return True
            return False

    def release(self, resource) -> None:
        with self._lock:
            if self._used[resource] > 0:
                self._used[resource] -= 1

    def on_overload(self, resource) -> None:
        with self._lock:
            self._eff[resource] = max(1, self._eff[resource] // 2)

    def on_success(self, resource) -> None:
        with self._lock:
            self._eff[resource] = min(self._max[resource], self._eff[resource] + 1)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/erwanpesle/Documents/GitHub/casys-trader && python -m pytest tests/queue_ledger/test_pools.py -q; echo "EXIT=$?"`
Expected: PASS, `EXIT=0`.

- [ ] **Step 5: Commit**

```bash
git add trader/queue/pools.py tests/queue_ledger/test_pools.py
git commit -m "feat(queue): ResourcePools (sémaphores nommés + backpressure AIMD)"
```

---

### Task 9: `Worker.run_once` — claim → handler → complete/fail (avec ressources)

**Files:**
- Create: `trader/queue/worker.py`
- Test: `tests/queue_ledger/test_worker.py`

**Interfaces:**
- Consumes: `TaskLedger` (Tasks 1-7), `ResourcePools` (Task 8).
- Produces: `Worker(ledger, pools, handlers: dict[str, callable], worker_id, lease_ms=1_800_000, backoff_base_ms=1000)`. `run_once(*, now_ms, token) -> bool` : calcule `free_resources` via `pools`, `claim`, si rien → False ; sinon `try_acquire(resource)` si `resource` non nul, exécute `handlers[kind](task)`, puis `complete` (succès) ou `fail(retryable=True)` (exception), `release` la ressource, retourne True. Un handler qui lève `RetryableError` → `fail(retryable=True)` ; toute autre exception → `fail(retryable=False)`.

- [ ] **Step 1: Write the failing test**

```python
# tests/queue_ledger/test_worker.py
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/erwanpesle/Documents/GitHub/casys-trader && python -m pytest tests/queue_ledger/test_worker.py -q; echo "EXIT=$?"`
Expected: FAIL — `ModuleNotFoundError: trader.queue.worker`.

- [ ] **Step 3: Write minimal implementation**

```python
# trader/queue/worker.py
"""Worker : claim → acquire ressource → handler → complete/fail → release."""
from __future__ import annotations


class RetryableError(Exception):
    """Le handler signale un échec transitoire → requeue avec backoff."""


class Worker:
    def __init__(self, ledger, pools, handlers, worker_id,
                 lease_ms=1_800_000, backoff_base_ms=1000):
        self._ledger = ledger
        self._pools = pools
        self._handlers = handlers
        self._id = worker_id
        self._lease_ms = lease_ms
        self._backoff_base_ms = backoff_base_ms

    def run_once(self, *, now_ms, token) -> bool:
        free = self._pools.free_resources()
        task = self._ledger.claim(worker_id=self._id, token=token, now_ms=now_ms,
                                  lease_ms=self._lease_ms, free_resources=free)
        if task is None:
            return False
        resource = task["resource"]
        acquired = self._pools.try_acquire(resource) if resource else True
        if resource and not acquired:
            # course rare : la ressource a été prise entre free_resources et claim.
            self._ledger.fail(task_id=task["id"], token=token, now_ms=now_ms,
                              error="resource_unavailable", retryable=True,
                              backoff_base_ms=self._backoff_base_ms)
            return True
        try:
            self._handlers[task["kind"]](task)
            self._ledger.complete(task_id=task["id"], token=token, now_ms=now_ms)
            if resource:
                self._pools.on_success(resource)
        except RetryableError as exc:
            if resource:
                self._pools.on_overload(resource)
            self._ledger.fail(task_id=task["id"], token=token, now_ms=now_ms,
                              error=str(exc), retryable=True,
                              backoff_base_ms=self._backoff_base_ms)
        except Exception as exc:  # noqa: BLE001 — frontière handler
            self._ledger.fail(task_id=task["id"], token=token, now_ms=now_ms,
                              error=f"{type(exc).__name__}: {exc}",
                              retryable=False, backoff_base_ms=self._backoff_base_ms)
        finally:
            if resource:
                self._pools.release(resource)
        return True
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/erwanpesle/Documents/GitHub/casys-trader && python -m pytest tests/queue_ledger/ -q; echo "EXIT=$?"`
Expected: PASS (toute la suite `tests/queue_ledger/`), `EXIT=0`.

- [ ] **Step 5: Commit**

```bash
git add trader/queue/worker.py tests/queue_ledger/test_worker.py
git commit -m "feat(queue): Worker.run_once (claim→handler→complete/fail, resource + AIMD)"
```

---

## Fin de Phase 0

À ce stade : file durable autonome, testée (idempotence, priorité, sérialisation par clé, resource-aware, fencing, backoff, reprise, AIMD, worker), **rien de branché au daemon**. Phases suivantes (plans séparés) :
- **Phase 1** — backends SQLite pour `SimBroker`/`TradePlanStore`/`Scheduler` (API inchangée, migration idempotente) → tue RC-1/2/4.
- **Phase 2** — producteur en shadow (compare file vs chemin synchrone, aucun ordre).
- **Phase 3** — `refresh_symbol`/`decide` (chunké) via file, exécution restant synchrone.
- **Lot B** — après revue Codex de §7 de la spec.
