"""Store SQLite des learnings de trading — schéma + ingestion idempotente.

Implémente le design 2026-07-02-learnings-recall-design.md §4.1 :
- Table `notes` : colonnes complètes incluant scoring FLAIR et embeddings.
- Index FTS5 `notes_fts` (content table, maintenu par triggers).
- Table `recalls` : trace des notes servies par décision (phase ②).

Le .db est un DÉRIVÉ reconstructible — les JSONL d'archives restent canoniques.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from trader.semantic.catalog import family_for_symbol

# Mode WAL : lecture concurrente daemon + écriture record_recall sans blocage.
_PRAGMAS = [
    "PRAGMA journal_mode=WAL;",
    "PRAGMA synchronous=NORMAL;",
    "PRAGMA foreign_keys=ON;",
]

_DDL_NOTES = """
CREATE TABLE IF NOT EXISTS notes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id     TEXT    UNIQUE NOT NULL,
    ts              TEXT,
    symbol          TEXT,
    family          TEXT,
    venue           TEXT,
    action          TEXT,
    intent          TEXT,
    executed        INTEGER,
    reason          TEXT,
    note            TEXT,
    concepts        TEXT,       -- JSON : vocabulaire TraderNexus
    source          TEXT,       -- runtime | ledger-backfill | consolidated
    valid_from      TEXT,
    valid_until     TEXT,       -- NULL = toujours valide
    superseded_by   INTEGER,    -- FK vers notes.id (None = actif)
    verdict         TEXT,       -- WIN | LOSS | NEUTRAL | UNKNOWN
    forward_return  REAL,
    outcome_score   REAL,       -- FLAIR v1
    q_value         REAL,       -- MemRL (phase ③)
    embedding       BLOB        -- OpenAI float32 LE, pré-calculé batch
)
"""

# Index FTS5 content-table : l'index pointe vers notes.id (content_rowid='id').
# Le contenu réel est dans notes.note ; les triggers maintiennent la cohérence.
_DDL_NOTES_FTS = """
CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts
    USING fts5(note, content='notes', content_rowid='id')
"""

# Triggers de synchronisation FTS5 (pattern SQLite content table).
_TRIGGER_AI = """
CREATE TRIGGER IF NOT EXISTS notes_ai
    AFTER INSERT ON notes
BEGIN
    INSERT INTO notes_fts(rowid, note) VALUES (new.id, new.note);
END
"""

_TRIGGER_AD = """
CREATE TRIGGER IF NOT EXISTS notes_ad
    AFTER DELETE ON notes
BEGIN
    INSERT INTO notes_fts(notes_fts, rowid, note)
        VALUES ('delete', old.id, old.note);
END
"""

_TRIGGER_AU = """
CREATE TRIGGER IF NOT EXISTS notes_au
    AFTER UPDATE ON notes
BEGIN
    INSERT INTO notes_fts(notes_fts, rowid, note)
        VALUES ('delete', old.id, old.note);
    INSERT INTO notes_fts(rowid, note) VALUES (new.id, new.note);
END
"""

_DDL_RECALLS = """
CREATE TABLE IF NOT EXISTS recalls (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id TEXT,
    note_ids    TEXT,       -- JSON array d'entiers
    ts          TEXT
)
"""


def _open_db(db_path: str | Path) -> sqlite3.Connection:
    """Ouvre (ou crée) la base, active WAL, retourne la connexion."""
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    for pragma in _PRAGMAS:
        conn.execute(pragma)
    return conn


def _create_schema(conn: sqlite3.Connection) -> None:
    """Crée le schéma complet si absent (idempotent grâce aux CREATE IF NOT EXISTS)."""
    conn.execute(_DDL_NOTES)
    conn.execute(_DDL_NOTES_FTS)
    conn.execute(_TRIGGER_AI)
    conn.execute(_TRIGGER_AD)
    conn.execute(_TRIGGER_AU)
    conn.execute(_DDL_RECALLS)
    conn.commit()


class LearningsStore:
    """Store SQLite des learnings outcome-weighted.

    Le constructeur crée le schéma si absent ; toutes les opérations sont
    idempotentes (décisions dédupliquées par decision_id UNIQUE).
    """

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path)
        self._conn = _open_db(self._db_path)
        _create_schema(self._conn)

    # ------------------------------------------------------------------
    # Lecture
    # ------------------------------------------------------------------

    def count(self) -> int:
        """Nombre de notes dans le store."""
        row = self._conn.execute("SELECT COUNT(*) FROM notes").fetchone()
        return int(row[0])

    # ------------------------------------------------------------------
    # Ingestion
    # ------------------------------------------------------------------

    def ingest_jsonl(self, path: Path, *, source: str) -> dict:
        """Ingère un fichier JSONL d'archives learnings.

        Format attendu (learnings-from-ledger.jsonl) : chaque ligne est un
        objet JSON avec au minimum ``ts``, ``symbol``, ``note`` ; ``decision_id``
        optionnel (clé de secours ``synth:{ts}|{symbol}`` si absent).

        Retourne ``{"inserted": n, "skipped": n}``.
        """
        path = Path(path)
        if not path.exists():
            return {"inserted": 0, "skipped": 0}

        inserted = 0
        skipped = 0

        with path.open(encoding="utf-8") as fh:
            for raw_line in fh:
                raw_line = raw_line.strip()
                if not raw_line:
                    continue
                try:
                    row = json.loads(raw_line)
                except json.JSONDecodeError:
                    skipped += 1
                    continue

                ts = row.get("ts") or ""
                symbol = row.get("symbol") or ""

                # Clé de dédup
                decision_id = row.get("decision_id")
                if not decision_id:
                    decision_id = f"synth:{ts}|{symbol}"

                # Famille sémantique (None accepté)
                family = family_for_symbol(symbol) if symbol else None

                # Concepts : JSON array ou None
                concepts_raw = row.get("concepts")
                if isinstance(concepts_raw, list):
                    concepts = json.dumps(concepts_raw, ensure_ascii=False)
                elif isinstance(concepts_raw, str):
                    concepts = concepts_raw
                else:
                    concepts = None

                cursor = self._conn.execute(
                    """
                    INSERT OR IGNORE INTO notes (
                        decision_id, ts, symbol, family, venue,
                        action, intent, executed, reason, note,
                        concepts, source, valid_from
                    ) VALUES (
                        :decision_id, :ts, :symbol, :family, :venue,
                        :action, :intent, :executed, :reason, :note,
                        :concepts, :source, :valid_from
                    )
                    """,
                    {
                        "decision_id": decision_id,
                        "ts": ts,
                        "symbol": symbol,
                        "family": family,
                        "venue": row.get("venue"),
                        "action": row.get("action"),
                        "intent": row.get("intent"),
                        "executed": int(bool(row.get("executed"))) if row.get("executed") is not None else None,
                        "reason": row.get("reason"),
                        "note": row.get("note"),
                        "concepts": concepts,
                        "source": source,
                        "valid_from": ts,
                    },
                )
                if cursor.rowcount == 1:
                    inserted += 1
                else:
                    skipped += 1

        self._conn.commit()
        return {"inserted": inserted, "skipped": skipped}
