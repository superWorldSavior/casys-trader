"""StateDb — connexion SQLite partagée (WAL + Lock) pour les stores d'état.

Pattern identique à trader/learnings/store.py (check_same_thread=False,
busy_timeout=5000, row_factory=Row) et trader/queue/ledger.py (threading.Lock).
"""
from __future__ import annotations

import logging
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Generator

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Registre singleton par process (FIX 1 — connexion partagée)
# ---------------------------------------------------------------------------

_DB_REGISTRY: dict[str, "StateDb"] = {}
_DB_REGISTRY_LOCK = threading.Lock()


def open_state_db(db_path: str | Path) -> "StateDb":
    """Retourne (ou crée) la StateDb canonique pour *db_path*.

    Cache module-level keyé par chemin absolu résolu : deux appels avec des
    chemins équivalents (``./state/casys.db`` vs ``/abs/state/casys.db``)
    retournent la MÊME instance. Thread-safe.

    Args:
        db_path: chemin du fichier SQLite (relatif ou absolu).

    Returns:
        StateDb — toujours la même instance pour un chemin résolu donné.
    """
    key = str(Path(db_path).resolve())
    with _DB_REGISTRY_LOCK:
        if key not in _DB_REGISTRY:
            _DB_REGISTRY[key] = StateDb(db_path)
        return _DB_REGISTRY[key]


def close_all_state_dbs() -> None:
    """Ferme toutes les StateDb du registre et vide le cache.

    Thread-safe (sous _DB_REGISTRY_LOCK). Utile pour les tests (isolation) et
    pour un atexit propre. Après appel, open_state_db(même chemin) retourne une
    nouvelle instance (le cache a été vidé).
    """
    with _DB_REGISTRY_LOCK:
        for db in list(_DB_REGISTRY.values()):
            db.close()
        _DB_REGISTRY.clear()


class StateDb:
    """Connexion SQLite partagée (WAL + threading.Lock) — substrat commun des stores.

    Mode autocommit (isolation_level=None) : aucune transaction implicite n'est
    ouverte par sqlite3 legacy. Toutes les transactions sont gérées explicitement
    via transaction(). BEGIN IMMEDIATE ouvre la tx ; COMMIT/ROLLBACK via cursor.

    Attributs publics :
        path   (Path)           — chemin absolu du fichier .db
        _lock  (threading.Lock) — protège self._conn ; NE PAS acquérir manuellement
    """

    def __init__(self, db_path: str | Path) -> None:
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(
            str(self.path), check_same_thread=False, isolation_level=None
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")

    # ------------------------------------------------------------------
    # Cycle de vie
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Checkpoint WAL + ferme la connexion. Idempotent.

        1. Acquiert self._lock.
        2. Si connexion déjà fermée (self._conn is None) → no-op immédiat.
        3. PRAGMA wal_checkpoint(TRUNCATE) (best-effort — ne lève pas).
        4. self._conn.close() puis self._conn = None.

        Un deuxième appel après le premier est silencieux (idempotent).
        """
        with self._lock:
            if self._conn is None:
                return
            try:
                self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except Exception:
                pass  # best-effort : ne pas masquer une fermeture propre
            try:
                self._conn.close()
            finally:
                self._conn = None

    def integrity_check(self) -> list[str]:
        """Exécute PRAGMA integrity_check et retourne les lignes de résultat.

        Sur une base saine, retourne ``['ok']``.
        Sur une base corrompue, retourne la liste des problèmes détectés.

        Returns:
            list[str] — lignes du PRAGMA (ex. ``['ok']`` ou messages d'erreur).
        """
        rows = self.query_all("PRAGMA integrity_check")
        return [row[0] for row in rows]

    # ------------------------------------------------------------------
    # Primitives publiques
    # ------------------------------------------------------------------

    @contextmanager
    def transaction(self) -> Generator[sqlite3.Cursor, None, None]:
        """Context manager : BEGIN IMMEDIATE … COMMIT (ou ROLLBACK sur exception).

        Usage::

            with db.transaction() as cur:
                cur.execute("INSERT INTO …")

        Le lock est tenu pendant toute la durée du bloc.
        Toutes les écritures passent EXCLUSIVEMENT par transaction().
        En mode autocommit (isolation_level=None), BEGIN/COMMIT/ROLLBACK sont émis
        explicitement via cursor — plus robuste que conn.commit()/rollback().
        """
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("BEGIN IMMEDIATE")
            try:
                yield cur
                cur.execute("COMMIT")
            except Exception:
                try:
                    cur.execute("ROLLBACK")
                except Exception:
                    pass
                raise

    def query_one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        """Lit une ligne sous _lock (fetch inclus). Retourne None si absente.

        Toutes les écritures passent EXCLUSIVEMENT par transaction().
        """
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    def query_all(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        """Lit toutes les lignes sous _lock (fetch inclus).

        Toutes les écritures passent EXCLUSIVEMENT par transaction().
        """
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def executescript(self, sql: str) -> None:
        """Execute un script SQL multi-instructions (DDL) sous _lock.

        sqlite3.executescript() émet un COMMIT implicite avant l'exécution —
        ne PAS appeler depuis l'intérieur d'un bloc transaction().
        """
        with self._lock:
            self._conn.executescript(sql)

    def table_is_empty(self, name: str) -> bool:
        """Retourne True si la table `name` ne contient aucune ligne."""
        row = self.query_one(
            f"SELECT COUNT(*) FROM {name}"  # noqa: S608 — name interne seulement
        )
        return row[0] == 0

    # ------------------------------------------------------------------
    # Migrations
    # ------------------------------------------------------------------

    def apply_migrations(self, migrations: list[tuple[int, list[str]]]) -> None:
        """Applique les migrations absentes, idempotent.

        Crée `schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT)`
        si elle n'existe pas (autocommit → DDL immédiatement persisted), puis pour
        chaque (version, [stmt1, stmt2, ...]) :

        - Ouvre BEGIN IMMEDIATE AVANT le check de version (verrou write SQLite acquis).
        - Re-check SELECT sous ce verrou : si présent → COMMIT no-op ; si absent →
          exécute les statements + INSERT version + COMMIT.
        - En cas d'erreur : ROLLBACK + re-raise.
        - Idempotent : re-run complet → no-op silencieux (pas de log).
        - Log ``[state_db] migration v%d appliquée`` uniquement si réellement appliquée.

        Args:
            migrations: liste ordonnée de (version, statements). Chaque migration est
                        une liste explicite de statements SQL — pas de split sur ';'.
        """
        # CREATE TABLE IF NOT EXISTS : DDL autocommitté immédiatement (isolation_level=None).
        with self._lock:
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version    INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL
                )
            """)

        for version, statements in migrations:
            with self._lock:
                cur = self._conn.cursor()
                cur.execute("BEGIN IMMEDIATE")
                already = cur.execute(
                    "SELECT 1 FROM schema_migrations WHERE version = ?",
                    (version,),
                ).fetchone()
                if already is not None:
                    cur.execute("COMMIT")
                else:
                    applied_at = datetime.now(timezone.utc).isoformat()
                    try:
                        for stmt in statements:
                            s = stmt.strip()
                            if s:
                                cur.execute(s)
                        cur.execute(
                            "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                            (version, applied_at),
                        )
                        cur.execute("COMMIT")
                    except Exception:
                        try:
                            cur.execute("ROLLBACK")
                        except Exception:
                            pass
                        raise
                    log.info("[state_db] migration v%d appliquée", version)
