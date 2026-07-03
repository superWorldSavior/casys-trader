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


class StateDb:
    """Connexion SQLite partagée (WAL + threading.Lock) — substrat commun des stores.

    Attributs publics :
        path   (Path)           — chemin absolu du fichier .db
        _lock  (threading.Lock) — protège self._conn ; NE PAS acquérir manuellement
    """

    def __init__(self, db_path: str | Path) -> None:
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")

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
        Ne PAS appeler db.execute() / db.executescript() à l'intérieur
        (deadlock) — utiliser le cursor yielded directement.
        """
        with self._lock:
            cur = self._conn.cursor()
            try:
                cur.execute("BEGIN IMMEDIATE")
                yield cur
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        """Execute une requête SQL sous _lock, retourne le cursor.

        Appeler .fetchone() / .fetchall() immédiatement sur le cursor retourné.
        Ne PAS utiliser depuis l'intérieur d'un bloc transaction() (deadlock).
        """
        with self._lock:
            return self._conn.execute(sql, params)

    def executescript(self, sql: str) -> None:
        """Execute un script SQL multi-instructions (DDL) sous _lock.

        sqlite3.executescript() émet un COMMIT implicite avant l'exécution —
        ne PAS appeler depuis l'intérieur d'un bloc transaction().
        """
        with self._lock:
            self._conn.executescript(sql)

    def table_is_empty(self, name: str) -> bool:
        """Retourne True si la table `name` ne contient aucune ligne."""
        with self._lock:
            row = self._conn.execute(
                f"SELECT COUNT(*) FROM {name}"  # noqa: S608 — name interne seulement
            ).fetchone()
            return row[0] == 0

    # ------------------------------------------------------------------
    # Migrations
    # ------------------------------------------------------------------

    def apply_migrations(self, migrations: list[tuple[int, str]]) -> None:
        """Applique les migrations absentes, idempotent.

        Crée `schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT)`
        si elle n'existe pas, puis pour chaque (version, sql) dont la version
        n'est pas encore enregistrée : exécute le sql dans une transaction et
        insère la ligne dans schema_migrations.

        Un sql peut contenir plusieurs statements séparés par ';'.
        Re-run complet → no-op (versions déjà présentes ignorées).

        Args:
            migrations: liste ordonnée de (version, sql).
        """
        # Crée schema_migrations si absente (DDL idempotent).
        with self._lock:
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version    INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL
                )
            """)
            self._conn.commit()

        for version, sql in migrations:
            with self._lock:
                already = self._conn.execute(
                    "SELECT 1 FROM schema_migrations WHERE version = ?",
                    (version,),
                ).fetchone()
                if already is not None:
                    continue  # déjà appliquée → skip

                applied_at = datetime.now(timezone.utc).isoformat()
                try:
                    cur = self._conn.cursor()
                    cur.execute("BEGIN IMMEDIATE")
                    for stmt in sql.split(";"):
                        stmt = stmt.strip()
                        if stmt:
                            cur.execute(stmt)
                    cur.execute(
                        "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                        (version, applied_at),
                    )
                    self._conn.commit()
                except Exception:
                    self._conn.rollback()
                    raise

            log.info("[state_db] migration v%d appliquée", version)
