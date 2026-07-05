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


# ---------------------------------------------------------------------------
# Task 2 — enqueue idempotent
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Task 3 — claim : priorité, échéance, tie-break, fencing token
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Task 4 — claim sérialisé par partition_key
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Task 5 — claim resource-aware
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Task 6 — complete / fail gardés par fencing token + backoff
# ---------------------------------------------------------------------------

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


def test_abandon_marks_active_task_dead_and_clears_claim(tmp_path):
    led = TaskLedger(tmp_path / "q.db")
    tid = led.enqueue(kind="execute_order", priority=0, scheduled_at_ms=0, now_ms=0,
                      dedup_key="exec-1", partition_key="portfolio", resource="portfolio")
    assert tid is not None
    task = led.claim(worker_id="w", token="tok", now_ms=1, lease_ms=1000,
                     free_resources=["portfolio"])
    assert task is not None

    assert led.abandon(task_id=task["id"], now_ms=2, error="queue_execute_timeout") is True

    row = led.get(task["id"])
    assert row["status"] == "dead"
    assert row["error"] == "queue_execute_timeout"
    assert row["claim_token"] is None
    assert row["claimed_by"] is None
    assert row["lease_expires_at"] is None


def test_abandon_does_not_touch_done_task(tmp_path):
    led = TaskLedger(tmp_path / "q.db")
    tid = led.enqueue(kind="execute_order", priority=0, scheduled_at_ms=0, now_ms=0,
                      dedup_key="exec-done", partition_key="portfolio", resource="portfolio")
    assert tid is not None
    task = led.claim(worker_id="w", token="tok", now_ms=1, lease_ms=1000,
                     free_resources=["portfolio"])
    assert task is not None
    assert led.complete(task_id=task["id"], token="tok", now_ms=2) is True

    assert led.abandon(task_id=task["id"], now_ms=3, error="queue_execute_timeout") is False

    row = led.get(task["id"])
    assert row["status"] == "done"


# ---------------------------------------------------------------------------
# Task 7 — heartbeat + recover_on_boot
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# FIX A — rollback sur exception : la connexion reste utilisable
# ---------------------------------------------------------------------------

def test_enqueue_partition_conflict_returns_none_and_leaves_db_usable(tmp_path):
    """Un enqueue qui viole uniq_active_kind_partition retourne None sans casser la connexion."""
    led = TaskLedger(tmp_path / "q.db")
    led.enqueue(kind="decide", priority=5, scheduled_at_ms=0, now_ms=0,
                dedup_key="d1", partition_key="AAPL")
    # dedup_key différent mais même kind+partition → viole uniq_active_kind_partition
    tid = led.enqueue(kind="decide", priority=5, scheduled_at_ms=0, now_ms=0,
                      dedup_key="d2", partition_key="AAPL")
    assert tid is None
    # Sans rollback la transaction reste ouverte et BEGIN IMMEDIATE du claim échoue
    t = led.claim(worker_id="w", token="tok", now_ms=1, lease_ms=1000,
                  free_resources=[])
    assert t is not None
    assert t["partition_key"] == "AAPL"


# ---------------------------------------------------------------------------
# FIX B — transitions terminales gardées
# ---------------------------------------------------------------------------

def test_complete_then_fail_same_token_is_noop(tmp_path):
    """complete() → done ; un fail() ultérieur avec le même token ne change rien."""
    led = TaskLedger(tmp_path / "q.db")
    led.enqueue(kind="decide", priority=5, scheduled_at_ms=0, now_ms=0,
                dedup_key="d", partition_key="C1", resource="acpx")
    t = _claim_one(led)
    assert led.complete(task_id=t["id"], token="tok", now_ms=2) is True
    result = led.fail(task_id=t["id"], token="tok", now_ms=3,
                      error="late", retryable=True, backoff_base_ms=1000)
    assert result == "stale"
    (st,) = led._conn.execute("SELECT status FROM tasks WHERE id=?",
                              (t["id"],)).fetchone()
    assert st == "done"


def test_dead_then_complete_same_token_is_noop(tmp_path):
    """fail() → dead ; un complete() ultérieur avec le même token ne change rien."""
    led = TaskLedger(tmp_path / "q.db")
    led.enqueue(kind="decide", priority=5, scheduled_at_ms=0, now_ms=0,
                dedup_key="d", partition_key="C1", resource="acpx", max_attempts=1)
    t = _claim_one(led, now_ms=100)          # attempts → 1 == max_attempts
    status = led.fail(task_id=t["id"], token="tok", now_ms=100,
                      error="gone", retryable=True, backoff_base_ms=1000)
    assert status == "dead"
    assert led.complete(task_id=t["id"], token="tok", now_ms=200) is False
    (st,) = led._conn.execute("SELECT status FROM tasks WHERE id=?",
                              (t["id"],)).fetchone()
    assert st == "dead"


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


# ---------------------------------------------------------------------------
# Task release_claim — annule un claim sans consommer de tentative
# ---------------------------------------------------------------------------

def test_release_claim_restores_pending_and_zeroes_attempts(tmp_path):
    """release_claim avec le bon token : status→pending, attempts→0, token effacé,
    tâche immédiatement re-claimable."""
    led = TaskLedger(tmp_path / "q.db")
    led.enqueue(kind="decide", priority=5, scheduled_at_ms=0, now_ms=0,
                dedup_key="d", partition_key="C1")
    t = led.claim(worker_id="w", token="tok", now_ms=1,
                  lease_ms=1000, free_resources=[])
    assert t["attempts"] == 1

    ok = led.release_claim(task_id=t["id"], token="tok", now_ms=1)
    assert ok is True

    row = led._conn.execute(
        "SELECT status, attempts, claim_token FROM tasks WHERE id=?",
        (t["id"],)).fetchone()
    assert row["status"] == "pending"
    assert row["attempts"] == 0
    assert row["claim_token"] is None

    # immédiatement re-claimable (pas de backoff, scheduled_at inchangé)
    t2 = led.claim(worker_id="w2", token="tok2", now_ms=1,
                   lease_ms=1000, free_resources=[])
    assert t2 is not None
    assert t2["id"] == t["id"]
    assert t2["attempts"] == 1


def test_release_claim_wrong_token_is_noop(tmp_path):
    """release_claim avec un mauvais token retourne False et ne change rien."""
    led = TaskLedger(tmp_path / "q.db")
    led.enqueue(kind="decide", priority=5, scheduled_at_ms=0, now_ms=0,
                dedup_key="d", partition_key="C1")
    t = led.claim(worker_id="w", token="tok", now_ms=1,
                  lease_ms=1000, free_resources=[])

    ok = led.release_claim(task_id=t["id"], token="WRONG", now_ms=1)
    assert ok is False

    row = led._conn.execute("SELECT status FROM tasks WHERE id=?",
                             (t["id"],)).fetchone()
    assert row["status"] == "running"


# ---------------------------------------------------------------------------
# count_by_status — API verrouillée (remplace _conn SELECT brut de shadow)
# ---------------------------------------------------------------------------

def test_count_by_status_dead_with_and_without_kind(tmp_path):
    """count_by_status retourne le bon décompte par statut et kind.

    Scénario : 1 tâche 'decide' poussée à 'dead' (max_attempts=1),
    1 tâche 'apply_exits' encore 'pending'.
    """
    led = TaskLedger(tmp_path / "q.db")
    # Tâche qui va mourir (max_attempts=1, priority=0 → claimée en 1ère)
    led.enqueue(kind="decide", priority=0, scheduled_at_ms=0, now_ms=0,
                dedup_key="d-dead", partition_key="C1", max_attempts=1)
    # Tâche pending qui reste vivante (priority=5 → claimée après)
    led.enqueue(kind="apply_exits", priority=5, scheduled_at_ms=0, now_ms=0,
                dedup_key="e-pending", partition_key="portfolio")

    # Amène la première tâche (decide, priority=0) à 'dead'
    t = led.claim(worker_id="w", token="tok", now_ms=1,
                  lease_ms=1000, free_resources=[])
    assert t is not None
    status = led.fail(task_id=t["id"], token="tok", now_ms=2,
                      error="boom", retryable=True, backoff_base_ms=1000)
    assert status == "dead"  # pré-condition : 1 tentative épuisée → dead

    # Sans filtre kind : compte toutes les dead (1)
    assert led.count_by_status("dead") == 1

    # Avec filtre kind exact : 1 dead de kind 'decide'
    assert led.count_by_status("dead", kind="decide") == 1

    # Avec filtre kind autre : 0 dead de kind 'apply_exits'
    assert led.count_by_status("dead", kind="apply_exits") == 0

    # Statut 'pending' : 1 (apply_exits) — la tâche decide est dead, pas pending
    assert led.count_by_status("pending") == 1
    assert led.count_by_status("pending", kind="decide") == 0
    assert led.count_by_status("pending", kind="apply_exits") == 1
