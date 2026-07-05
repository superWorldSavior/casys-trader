"""task_ledger — file de tâches durable SQLite (Lot A).

Accepte un StateDb partagé (prod/outbox) ou un chemin (connexion dédiée,
shadow/tests). Temps injecté (now_ms) pour déterminisme. Timestamps = epoch ms.
"""
from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from trader.infrastructure.state_db.connection import StateDb

log = logging.getLogger(__name__)

# DDL exécutés un par un dans transaction() — pas executescript (commit implicite)
_DDL = [
    """CREATE TABLE IF NOT EXISTS tasks (
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
)""",
    "CREATE INDEX IF NOT EXISTS idx_claim ON tasks(status, priority, scheduled_at, id)",
    """CREATE UNIQUE INDEX IF NOT EXISTS uniq_running_partition
  ON tasks(partition_key) WHERE status='running' AND partition_key IS NOT NULL""",
    """CREATE UNIQUE INDEX IF NOT EXISTS uniq_active_kind_partition
  ON tasks(kind, partition_key)
  WHERE status IN ('pending','running') AND partition_key IS NOT NULL""",
]


class TaskLedger:
    def __init__(self, db_or_path: "StateDb | str | Path"):
        """Construit le ledger sur un StateDb partagé ou un chemin dédié.

        Args:
            db_or_path: StateDb existant (connexion partagée, prod/outbox) OU
                        chemin str/Path (connexion dédiée interne, shadow/tests).
                        Quand c'est un chemin, crée StateDb(path) — PAS open_state_db
                        (pas de singleton, isolation garantie).
        """
        if isinstance(db_or_path, StateDb):
            self._db = db_or_path
        else:
            self._db = StateDb(db_or_path)
        self.path = self._db.path
        # Crée la table tasks + 3 index dans ce StateDb (idempotent)
        with self._db.transaction() as cur:
            for stmt in _DDL:
                cur.execute(stmt)

    @property
    def _conn(self):
        """Accès direct à la connexion sqlite3 sous-jacente.

        # déprécié : lecture brute hors-lock, préférer les méthodes du ledger
        Rétro-compat pour les tests Phase 0 uniquement.
        NE PAS acquérir de lock ni ouvrir de transaction via cette propriété.
        """
        return self._db._conn

    def count_by_status(self, status: str, *, kind: str | None = None) -> int:
        """Compte les tâches par statut, optionnellement filtrées par kind.

        Exécuté sous le lock du StateDb (via query_one) — lecture cohérente
        avec les transactions de enqueue/claim/fail/complete.

        Args:
            status: Statut exact à filtrer (ex. ``'dead'``, ``'pending'``).
            kind:   Si fourni, filtre aussi sur la colonne ``kind``.

        Returns:
            Nombre de tâches correspondantes (int >= 0).
        """
        if kind is not None:
            row = self._db.query_one(
                "SELECT COUNT(*) FROM tasks WHERE status=? AND kind=?",
                (status, kind),
            )
        else:
            row = self._db.query_one(
                "SELECT COUNT(*) FROM tasks WHERE status=?",
                (status,),
            )
        return int(row[0]) if row else 0

    # ------------------------------------------------------------------
    # Opérations file
    # ------------------------------------------------------------------

    def enqueue(self, *, kind, priority, scheduled_at_ms, now_ms,
                dedup_key=None, partition_key=None, resource=None,
                payload=None, max_attempts=3, parent_id=None):
        """Enfile une tâche en status 'pending'.

        Idempotent : si ``dedup_key`` est déjà présent (conflit UNIQUE),
        l'INSERT est ignoré et retourne ``None``.
        Retourne l'``id`` (int) de la nouvelle ligne, ou ``None`` sur conflit.
        """
        try:
            with self._db.transaction() as cur:
                row = cur.execute(
                    """INSERT INTO tasks(kind, dedup_key, partition_key, resource,
                            priority, payload, status, attempts, max_attempts,
                            scheduled_at, enqueued_seq, parent_id, created_at, updated_at)
                       VALUES(?,?,?,?,?,?, 'pending', 0, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(dedup_key) DO NOTHING
                       RETURNING id""",
                    (kind, dedup_key, partition_key, resource, priority, payload,
                     max_attempts, scheduled_at_ms, now_ms, parent_id, now_ms, now_ms),
                ).fetchone()
                if row:
                    log.debug("[queue.ledger] enqueue kind=%s dedup=%s", kind, dedup_key)
                return int(row["id"]) if row else None
        except sqlite3.IntegrityError as exc:
            if "tasks.kind, tasks.partition_key" in str(exc):
                log.debug(
                    "[queue.ledger] enqueue partition conflict kind=%s partition=%s dedup=%s",
                    kind,
                    partition_key,
                    dedup_key,
                )
                return None
            raise

    def claim(self, *, worker_id, token, now_ms, lease_ms, free_resources):
        """Tente de passer la tâche la plus prioritaire en 'running'.

        Respecte : ``scheduled_at <= now_ms``, ``attempts < max_attempts``,
        ressource dans ``free_resources`` (ou None), ``partition_key`` sans
        tâche running concurrente. Priorité : priority ASC, scheduled_at ASC,
        id ASC. Retourne ``dict`` de la ligne claimée, ou ``None`` si rien à
        claimer.
        """
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
        with self._db.transaction() as cur:
            row = cur.execute(sql, params).fetchone()
            if row:
                log.debug("[queue.ledger] claim id=%s kind=%s partition=%s",
                          row["id"], row["kind"], row["partition_key"])
            return dict(row) if row else None

    def complete(self, *, task_id, token, now_ms, result=None):
        """Marque la tâche comme 'done' et efface le claim.

        Gardé par fencing token : sans effet si ``claim_token`` ne correspond
        pas ou si status != 'running'. Retourne ``True`` si la ligne a été
        modifiée.
        """
        with self._db.transaction() as cur:
            return self.complete_in_tx(cur, task_id=task_id, token=token,
                                       now_ms=now_ms, result=result)

    def complete_in_tx(self, cur, *, task_id, token, now_ms, result=None) -> bool:
        """Variante transactionnelle de complete : écrit sur un curseur fourni.

        N'ouvre PAS de transaction.
        À appeler exclusivement depuis l'intérieur d'un bloc ``with db.transaction() as cur:``.

        Gardé par fencing token : sans effet si ``claim_token`` ne correspond
        pas ou si status != 'running'. Retourne ``True`` si la ligne a été modifiée.

        Args:
            cur:     Curseur SQLite déjà dans une transaction BEGIN IMMEDIATE.
            task_id: ID de la tâche à compléter.
            token:   Token de fencing (claim_token attendu).
            now_ms:  Timestamp courant (epoch ms) pour updated_at.
            result:  Résultat optionnel à sérialiser dans la colonne result (TEXT).

        Returns:
            True si la tâche a été marquée 'done', False si token invalide / non running.
        """
        n = cur.execute(
            """UPDATE tasks
               SET status='done', result=?, updated_at=?,
                   claim_token=NULL, claimed_by=NULL, lease_expires_at=NULL
               WHERE id=? AND claim_token=? AND status='running'""",
            (result, now_ms, task_id, token),
        ).rowcount
        return n == 1

    def fail(self, *, task_id, token, now_ms, error, retryable, backoff_base_ms):
        """Signale l'échec d'une tâche et applique la politique de retry.

        Si ``retryable=True`` et ``attempts < max_attempts`` : replanifie en
        'pending' avec délai ``backoff_base_ms * 2^(attempts-1)``.
        Sinon : passe en 'dead'. Gardé par fencing token (``claim_token``).
        Retourne ``'pending'`` (retry), ``'dead'`` (épuisé) ou ``'stale'``
        (token inconnu — sans effet).
        """
        with self._db.transaction() as cur:
            row = cur.execute(
                "SELECT attempts, max_attempts FROM tasks WHERE id=? AND claim_token=?",
                (task_id, token),
            ).fetchone()
            if row is None:
                return "stale"
            attempts, max_attempts = row["attempts"], row["max_attempts"]
            if retryable and attempts < max_attempts:
                next_at = now_ms + backoff_base_ms * (2 ** (attempts - 1))
                cur.execute(
                    """UPDATE tasks SET status='pending', error=?, scheduled_at=?,
                           claim_token=NULL, claimed_by=NULL, lease_expires_at=NULL,
                           updated_at=? WHERE id=?""",
                    (error, next_at, now_ms, task_id),
                )
                log.warning("[queue.ledger] retry id=%s attempts=%s scheduled=%s",
                            task_id, attempts, next_at)
                return "pending"
            cur.execute(
                """UPDATE tasks
                   SET status='dead', error=?, updated_at=?,
                       claim_token=NULL, claimed_by=NULL, lease_expires_at=NULL
                   WHERE id=?""",
                (error, now_ms, task_id),
            )
            log.warning("[queue.ledger] dead id=%s error=%s", task_id, error)
            return "dead"

    def heartbeat(self, *, task_id, token, now_ms, lease_ms):
        """Prolonge le bail d'une tâche en cours d'exécution.

        Gardé par fencing token. Retourne ``True`` si le bail a été renouvelé,
        ``False`` si la tâche est introuvable ou le token invalide.
        """
        with self._db.transaction() as cur:
            n = cur.execute(
                """UPDATE tasks SET lease_expires_at=?, updated_at=?
                   WHERE id=? AND claim_token=? AND status='running'""",
                (now_ms + lease_ms, now_ms, task_id, token),
            ).rowcount
            return n == 1

    def release_claim(self, *, task_id, token, now_ms) -> bool:
        """Annule un claim sans consommer de tentative (resource miss, etc.).

        Remet status='pending', décrémente attempts (MAX(0,attempts-1)),
        efface les champs de claim. Ne touche PAS scheduled_at.
        WHERE id=? AND claim_token=? AND status='running'.
        Retourne True si la ligne a été modifiée.
        """
        with self._db.transaction() as cur:
            n = cur.execute(
                """UPDATE tasks
                   SET status='pending', attempts=MAX(0, attempts-1),
                       claim_token=NULL, claimed_by=NULL, lease_expires_at=NULL,
                       updated_at=?
                   WHERE id=? AND claim_token=? AND status='running'""",
                (now_ms, task_id, token),
            ).rowcount
            log.debug("[queue.ledger] release_claim id=%s", task_id)
            return n == 1

    def purge_all(self) -> int:
        """Supprime toutes les tâches de la DB (shadow éphémère : purge par cycle).

        Helper de la sonde shadow uniquement. NE modifie PAS la logique métier
        de enqueue/claim/complete/fail. Retourne le nombre de lignes supprimées.
        """
        with self._db.transaction() as cur:
            n = cur.execute("DELETE FROM tasks").rowcount
            return n

    def get(self, task_id: int) -> "dict | None":
        """Retourne la tâche identifiée par *task_id* sous forme de dict.

        Lit sous lock (via query_one) — cohérent avec les transactions de
        enqueue/claim/complete/fail. Retourne None si la tâche est introuvable.
        """
        row = self._db.query_one("SELECT * FROM tasks WHERE id=?", (task_id,))
        return dict(row) if row else None

    def delete_stale_decide(self, current_cycle_id: str, *, now_ms: int) -> int:
        """Supprime les tâches 'decide' périmées d'un cycle précédent.

        Seules les tâches dont ``dedup_key`` ne commence PAS par
        ``{current_cycle_id}:`` sont candidates. Les ``pending`` sont supprimées
        car jamais démarrées ; les ``running`` le sont seulement si leur bail est
        expiré (``lease_expires_at < now_ms``). Un ``running`` vivant reste en
        place et continue de protéger sa partition.

        Retourne le nombre de lignes supprimées.
        """
        pattern = f"{current_cycle_id}:%"
        with self._db.transaction() as cur:
            n = cur.execute(
                """DELETE FROM tasks
                   WHERE kind='decide'
                     AND (dedup_key IS NULL OR dedup_key NOT LIKE ?)
                     AND (
                       status='pending'
                       OR (status='running' AND lease_expires_at < ?)
                     )""",
                (pattern, now_ms),
            ).rowcount
            if n > 0:
                log.info(
                    "[queue.ledger] delete_stale_decide current_cycle=%s deleted=%d",
                    current_cycle_id,
                    n,
                )
            return n

    def recover_on_boot(self, *, now_ms):
        """Remet en 'pending' les tâches 'running' dont le bail a expiré.

        À appeler au démarrage du daemon pour reprendre les tâches orphelines
        d'un crash précédent. Retourne le nombre de tâches réactivées (int).
        """
        with self._db.transaction() as cur:
            n = cur.execute(
                """UPDATE tasks SET status='pending', claim_token=NULL,
                       claimed_by=NULL, lease_expires_at=NULL, updated_at=?
                   WHERE status='running' AND lease_expires_at < ?""",
                (now_ms, now_ms),
            ).rowcount
            if n > 0:
                log.info("[queue.ledger] recover_on_boot repending=%d", n)
            return n
