"""task_ledger — file de tâches durable SQLite (Lot A).

Une connexion partagée (WAL) protégée par un Lock, comme learnings/store.py.
Temps injecté (now_ms) pour déterminisme. Timestamps = epoch ms (int).
"""
from __future__ import annotations

import logging
import sqlite3
import threading
from pathlib import Path

log = logging.getLogger(__name__)

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

    def enqueue(self, *, kind, priority, scheduled_at_ms, now_ms,
                dedup_key=None, partition_key=None, resource=None,
                payload=None, max_attempts=3, parent_id=None):
        with self._lock:
            try:
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
                if row:
                    log.debug("[queue.ledger] enqueue kind=%s dedup=%s", kind, dedup_key)
                return int(row["id"]) if row else None
            except Exception:
                self._conn.rollback()
                raise

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
                if row:
                    log.debug("[queue.ledger] claim id=%s kind=%s partition=%s",
                              row["id"], row["kind"], row["partition_key"])
                return dict(row) if row else None
            except Exception:
                self._conn.rollback()
                raise

    def complete(self, *, task_id, token, now_ms, result=None):
        with self._lock:
            try:
                cur = self._conn.execute(
                    """UPDATE tasks
                       SET status='done', result=?, updated_at=?,
                           claim_token=NULL, claimed_by=NULL, lease_expires_at=NULL
                       WHERE id=? AND claim_token=? AND status='running'""",
                    (result, now_ms, task_id, token))
                self._conn.commit()
                return cur.rowcount == 1
            except Exception:
                self._conn.rollback()
                raise

    def fail(self, *, task_id, token, now_ms, error, retryable, backoff_base_ms):
        with self._lock:
            try:
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
                    log.warning("[queue.ledger] retry id=%s attempts=%s scheduled=%s",
                                task_id, attempts, next_at)
                    return "pending"
                self._conn.execute(
                    """UPDATE tasks
                       SET status='dead', error=?, updated_at=?,
                           claim_token=NULL, claimed_by=NULL, lease_expires_at=NULL
                       WHERE id=?""",
                    (error, now_ms, task_id))
                self._conn.commit()
                log.warning("[queue.ledger] dead id=%s error=%s", task_id, error)
                return "dead"
            except Exception:
                self._conn.rollback()
                raise

    def heartbeat(self, *, task_id, token, now_ms, lease_ms):
        with self._lock:
            try:
                cur = self._conn.execute(
                    """UPDATE tasks SET lease_expires_at=?, updated_at=?
                       WHERE id=? AND claim_token=? AND status='running'""",
                    (now_ms + lease_ms, now_ms, task_id, token))
                self._conn.commit()
                return cur.rowcount == 1
            except Exception:
                self._conn.rollback()
                raise

    def release_claim(self, *, task_id, token, now_ms) -> bool:
        """Annule un claim sans consommer de tentative (resource miss, etc.).

        Remet status='pending', décrémente attempts (MAX(0,attempts-1)),
        efface les champs de claim. Ne touche PAS scheduled_at.
        WHERE id=? AND claim_token=? AND status='running'.
        Retourne True si la ligne a été modifiée.
        """
        with self._lock:
            try:
                cur = self._conn.execute(
                    """UPDATE tasks
                       SET status='pending', attempts=MAX(0, attempts-1),
                           claim_token=NULL, claimed_by=NULL, lease_expires_at=NULL,
                           updated_at=?
                       WHERE id=? AND claim_token=? AND status='running'""",
                    (now_ms, task_id, token))
                self._conn.commit()
                log.debug("[queue.ledger] release_claim id=%s", task_id)
                return cur.rowcount == 1
            except Exception:
                self._conn.rollback()
                raise

    def recover_on_boot(self, *, now_ms):
        with self._lock:
            try:
                cur = self._conn.execute(
                    """UPDATE tasks SET status='pending', claim_token=NULL,
                           claimed_by=NULL, lease_expires_at=NULL, updated_at=?
                       WHERE status='running' AND lease_expires_at < ?""",
                    (now_ms, now_ms))
                self._conn.commit()
                n = cur.rowcount
                if n > 0:
                    log.info("[queue.ledger] recover_on_boot repending=%d", n)
                return n
            except Exception:
                self._conn.rollback()
                raise
