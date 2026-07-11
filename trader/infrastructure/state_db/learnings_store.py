"""Store SQLite des learnings de trading — schéma + ingestion + recall.

Implémente le design 2026-07-02-learnings-recall-design.md §4.1 et §4.2 :
- Table `notes` : colonnes complètes incluant scoring FLAIR et embeddings.
- Index FTS5 `notes_fts` (content table, maintenu par triggers).
- Table `recalls` : trace des notes servies par décision (phase ②).
- Appel du scoring FLAIR pur, injecté par callable.

Le .db est un DÉRIVÉ reconstructible — les JSONL d'archives restent canoniques.
"""
from __future__ import annotations

import json
import math
import sqlite3
import threading
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from trader.domain.learnings.scoring import compute_outcome_scores as default_outcome_scorer
from trader.domain.semantic.catalog import family_for_symbol

__all__ = ["LearningsStore"]

# Mode WAL : lecture concurrente daemon + écriture record_recall sans blocage.
_PRAGMAS = [
    "PRAGMA journal_mode=WAL;",
    "PRAGMA synchronous=NORMAL;",
    "PRAGMA foreign_keys=ON;",
    "PRAGMA busy_timeout=2000;",
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
    q_updates       INTEGER NOT NULL DEFAULT 0,
    -- A note is eligible for curation when curation_revision > curated_revision.
    -- The snapshot revision is recorded rather than a timestamp so an outcome
    -- arriving while the consolidator is running cannot be accidentally lost.
    curation_revision INTEGER NOT NULL DEFAULT 1,
    curated_revision  INTEGER NOT NULL DEFAULT 0,
    curation_updated_at TEXT,
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
    ts          TEXT,
    verdict     TEXT,
    reward      REAL,
    forward_return REAL,
    evaluated_at TEXT
)
"""

# The global-rule space is intentionally separate from ``notes``.  Note Q-values
# answer "was this explicitly recalled historical experience useful?" while a
# rule Q-value answers "did the trader say this displayed global rule mattered?".
_DDL_GLOBAL_RULES = """
CREATE TABLE IF NOT EXISTS global_rules (
    rule_id     TEXT PRIMARY KEY,
    q_value     REAL NOT NULL DEFAULT 0,
    q_updates   INTEGER NOT NULL DEFAULT 0,
    active      INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT,
    retired_at  TEXT,
    updated_at  TEXT
)
"""

_DDL_GLOBAL_RULE_CITATIONS = """
CREATE TABLE IF NOT EXISTS global_rule_citations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id     TEXT NOT NULL UNIQUE,
    rule_ids        TEXT NOT NULL, -- JSON array, at most 3 stable rule IDs
    ts              TEXT NOT NULL,
    verdict         TEXT,
    reward          REAL,
    forward_return  REAL,
    evaluated_at    TEXT
)
"""

_INDEX_GLOBAL_RULE_CITATIONS_PENDING = """
CREATE INDEX IF NOT EXISTS global_rule_citations_pending
    ON global_rule_citations(evaluated_at, ts)
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
    conn.execute(_DDL_GLOBAL_RULES)
    conn.execute(_DDL_GLOBAL_RULE_CITATIONS)
    conn.execute(_INDEX_GLOBAL_RULE_CITATIONS_PENDING)
    _ensure_column(conn, "notes", "q_updates", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "notes", "curation_revision", "INTEGER NOT NULL DEFAULT 1")
    _ensure_column(conn, "notes", "curated_revision", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "notes", "curation_updated_at", "TEXT")
    _ensure_column(conn, "recalls", "verdict", "TEXT")
    _ensure_column(conn, "recalls", "reward", "REAL")
    _ensure_column(conn, "recalls", "forward_return", "REAL")
    _ensure_column(conn, "recalls", "evaluated_at", "TEXT")
    conn.commit()


def _ensure_column(
    conn: sqlite3.Connection,
    table: str,
    column: str,
    declaration: str,
) -> None:
    """Apply additive SQLite migrations to existing derived stores."""

    columns = {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")


def _utc_lifecycle_clock(now: datetime | None) -> datetime:
    resolved = now or datetime.now(timezone.utc)
    if resolved.tzinfo is None:
        raise ValueError("global-rule lifecycle clock must be timezone-aware")
    return resolved.astimezone(timezone.utc)


def _parse_lifecycle_ts(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


class LearningsStore:
    """Store SQLite des learnings outcome-weighted.

    Le constructeur crée le schéma si absent ; toutes les opérations sont
    idempotentes (décisions dédupliquées par decision_id UNIQUE).
    """

    def __init__(
        self,
        db_path: str | Path,
        *,
        outcome_scorer: Callable[..., dict] = default_outcome_scorer,
    ) -> None:
        self._db_path = Path(db_path)
        self._lock = threading.Lock()
        self._outcome_scorer = outcome_scorer
        self._conn = _open_db(self._db_path)
        _create_schema(self._conn)

    # ------------------------------------------------------------------
    # Lecture
    # ------------------------------------------------------------------

    def count(self) -> int:
        """Nombre de notes dans le store."""
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) FROM notes").fetchone()
        return int(row[0])

    def count_missing_embeddings(self) -> int:
        """Nombre de notes encore privées de vecteur."""

        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM notes WHERE embedding IS NULL"
            ).fetchone()
        return int(row[0])

    def feedback_by_decision_ids(self, decision_ids: list[str]) -> dict[str, dict]:
        """Return chronological FLAIR feedback keyed by source decision ID.

        This is intentionally a lookup, not a ranking API: callers use it to
        annotate ``last_llm_review`` and ``recent_decisions`` without changing
        their chronological order.
        """

        unique_ids = list(dict.fromkeys(str(item) for item in decision_ids if item))
        if not unique_ids:
            return {}
        result: dict[str, dict] = {}
        with self._lock:
            # SQLite's bound-variable maximum is commonly 999.  The cockpit
            # sends only a handful, but chunking keeps this store API general.
            for start in range(0, len(unique_ids), 900):
                chunk = unique_ids[start:start + 900]
                placeholders = ", ".join("?" for _ in chunk)
                rows = self._conn.execute(
                    f"""
                    SELECT decision_id, verdict, forward_return, outcome_score,
                           q_value, q_updates
                    FROM notes
                    WHERE decision_id IN ({placeholders})
                    """,
                    chunk,
                ).fetchall()
                for row in rows:
                    verdict = row["verdict"]
                    result[str(row["decision_id"])] = {
                        "status": "pending" if verdict is None else "evaluated",
                        "verdict": verdict,
                        "forward_return": row["forward_return"],
                        "outcome_score": row["outcome_score"],
                        "q_value": row["q_value"],
                        "q_updates": int(row["q_updates"] or 0),
                    }
        return result

    def close(self) -> None:
        """Ferme explicitement la connexion des workers courts."""

        with self._lock:
            self._conn.close()

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

        rows_to_insert = []
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

                rows_to_insert.append({
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
                    "curation_updated_at": ts or datetime.now(timezone.utc).isoformat(),
                })

        with self._lock:
            for params in rows_to_insert:
                cursor = self._conn.execute(
                    """
                    INSERT OR IGNORE INTO notes (
                        decision_id, ts, symbol, family, venue,
                        action, intent, executed, reason, note,
                        concepts, source, valid_from, curation_updated_at
                    ) VALUES (
                        :decision_id, :ts, :symbol, :family, :venue,
                        :action, :intent, :executed, :reason, :note,
                        :concepts, :source, :valid_from, :curation_updated_at
                    )
                    """,
                    params,
                )
                if cursor.rowcount == 1:
                    inserted += 1
                else:
                    skipped += 1
            self._conn.commit()
        return {"inserted": inserted, "skipped": skipped}

    # ------------------------------------------------------------------
    # Scoring FLAIR
    # ------------------------------------------------------------------

    def apply_verdicts(
        self,
        bootstrap_json: "str | Path",
        *,
        only_missing: bool = False,
    ) -> int:
        """Met à jour verdict et forward_return à partir du fichier de bootstrap FLAIR.

        Format attendu : fichier JSON produit par scripts/learnings_outcome_bootstrap.py,
        avec une clé ``"learnings"`` contenant une liste d'objets ayant au moins
        ``decision_id``, ``verdict``, et ``forward_return``.

        Retourne le nombre de notes effectivement mises à jour (rowcount > 0).
        """
        bootstrap_json = Path(bootstrap_json)
        if not bootstrap_json.exists():
            return 0

        data = json.loads(bootstrap_json.read_text(encoding="utf-8"))
        learnings = data.get("learnings", [])

        updated = 0
        changed_at = datetime.now(timezone.utc).isoformat()
        with self._lock:
            for item in learnings:
                decision_id = item.get("decision_id")
                if not decision_id:
                    continue
                missing_clause = " AND verdict IS NULL" if only_missing else ""
                cursor = self._conn.execute(
                    f"""
                    UPDATE notes
                       SET verdict        = :verdict,
                           forward_return = :forward_return,
                           curation_revision = curation_revision + 1,
                           curation_updated_at = :changed_at
                     WHERE decision_id = :decision_id
                     {missing_clause}
                       AND (
                           verdict IS NOT :verdict
                           OR forward_return IS NOT :forward_return
                       )
                    """,
                    {
                        "decision_id": str(decision_id),
                        "verdict": item.get("verdict"),
                        "forward_return": item.get("forward_return"),
                        "changed_at": changed_at,
                    },
                )
                if cursor.rowcount > 0:
                    updated += 1

            self._conn.commit()
        return updated

    def compute_outcome_scores(self, *, shrinkage_k: float = 5.0) -> dict:
        """Calcule et persiste les outcome_scores FLAIR pour toutes les notes scorées.

        Algorithme (design §4.2, V1 n=1 par note) :

        - ``base_rate(sym)`` = wins / (wins + losses) des notes WIN/LOSS du symbole
          (≥ 5 scorables WIN/LOSS requis ; sinon fallback base_rate famille ; sinon globale).
        - ``lift(note)`` = (1.0 si WIN, sinon 0.0) − base_rate  [notes WIN/LOSS seulement]
        - ``outcome_score`` = lift / (1 + shrinkage_k)  [n=1 en V1, shrinkage vers 0]
        - NEUTRAL / UNKNOWN → ``outcome_score = 0.0``

        Retourne ``{"scored": n, "base_rates": {sym: rate, ...}}``.
        """
        with self._lock:
            # Charger toutes les notes avec un verdict posé
            rows = self._conn.execute(
                "SELECT id, symbol, family, verdict FROM notes WHERE verdict IS NOT NULL"
            ).fetchall()

            if not rows:
                return {"scored": 0, "base_rates": {}}

            result = self._outcome_scorer(rows, shrinkage_k=shrinkage_k)
            scores = result.get("scores", {})
            changed_at = datetime.now(timezone.utc).isoformat()

            for note_id, outcome_score in scores.items():
                self._conn.execute(
                    """
                    UPDATE notes
                    SET outcome_score = :score,
                        curation_revision = curation_revision + 1,
                        curation_updated_at = :changed_at
                    WHERE id = :id AND outcome_score IS NOT :score
                    """,
                    {"score": outcome_score, "id": note_id, "changed_at": changed_at},
                )

            self._conn.commit()
        return {
            "scored": result.get("scored", 0),
            "base_rates": result.get("base_rates", {}),
        }

    # ------------------------------------------------------------------
    # Curation / consolidation candidates
    # ------------------------------------------------------------------

    def curation_counts(self) -> dict:
        """Return the pending curation work split by first-pass vs feedback.

        A note begins at revision 1 / curated revision 0.  Any later FLAIR or
        note-MemRL update increments ``curation_revision``.  The distinction is
        deliberately revision based rather than timestamp based: a failed
        consolidation cannot lose an outcome which arrived while its LLM call
        was in flight.
        """

        with self._lock:
            row = self._conn.execute(
                """
                SELECT
                    COALESCE(SUM(CASE
                        WHEN curation_revision > curated_revision
                         AND curated_revision = 0 THEN 1 ELSE 0 END), 0) AS new_count,
                    COALESCE(SUM(CASE
                        WHEN curation_revision > curated_revision
                         AND curated_revision > 0 THEN 1 ELSE 0 END), 0) AS feedback_count,
                    MIN(CASE WHEN curation_revision > curated_revision
                        THEN COALESCE(curation_updated_at, ts) END) AS oldest_changed_at
                FROM notes
                """
            ).fetchone()
        new_count = int(row["new_count"] or 0)
        feedback_count = int(row["feedback_count"] or 0)
        return {
            "new": new_count,
            "feedback": feedback_count,
            "changed": new_count + feedback_count,
            "oldest_changed_at": row["oldest_changed_at"],
        }

    def select_curation_candidates(
        self,
        *,
        recent_limit: int = 50,
        positive_limit: int = 15,
        counterexample_limit: int = 15,
        max_per_symbol: int = 5,
    ) -> list[dict]:
        """Select a bounded, diversified, outcome-aware curation pool.

        The first tranche is work which changed since a successful
        consolidation, including pending notes.  It is followed by the best
        successful historical confirmations and strongest losses.  A note is
        emitted only once and no symbol can dominate the pool.  Every selected
        row includes the snapshot ``curation_revision`` that must later be
        passed to :meth:`mark_curation_candidates_curated` after a successful
        consolidation.
        """

        recent_limit = max(0, int(recent_limit))
        positive_limit = max(0, int(positive_limit))
        counterexample_limit = max(0, int(counterexample_limit))
        max_per_symbol = max(1, int(max_per_symbol))
        pool_limit = recent_limit + positive_limit + counterexample_limit
        if pool_limit == 0:
            return []

        fields = """
            id, decision_id, ts, symbol, family, action, intent, executed,
            note, verdict, forward_return, outcome_score, q_value, q_updates,
            curation_revision, curated_revision
        """
        with self._lock:
            changed = self._conn.execute(
                f"""
                SELECT {fields}
                FROM notes
                WHERE curation_revision > curated_revision
                ORDER BY ts DESC, id DESC
                """
            ).fetchall()
            positives = self._conn.execute(
                f"""
                SELECT {fields}
                FROM notes
                WHERE verdict = 'WIN'
                ORDER BY
                    (outcome_score IS NULL) ASC,
                    outcome_score DESC,
                    (forward_return IS NULL) ASC,
                    forward_return DESC,
                    q_value DESC,
                    ts DESC,
                    id DESC
                """
            ).fetchall()
            counterexamples = self._conn.execute(
                f"""
                SELECT {fields}
                FROM notes
                WHERE verdict = 'LOSS'
                ORDER BY
                    (outcome_score IS NULL) ASC,
                    outcome_score ASC,
                    (forward_return IS NULL) ASC,
                    forward_return ASC,
                    q_value ASC,
                    ts DESC,
                    id DESC
                """
            ).fetchall()

        selected: list[dict] = []
        selected_ids: set[int] = set()
        per_symbol: dict[str, int] = {}

        def add(rows: list[sqlite3.Row], allowance: int) -> None:
            added = 0
            for raw in rows:
                if len(selected) >= pool_limit or added >= allowance:
                    return
                note_id = int(raw["id"])
                if note_id in selected_ids:
                    continue
                symbol_key = str(raw["symbol"] or "__unknown__")
                if per_symbol.get(symbol_key, 0) >= max_per_symbol:
                    continue
                row = dict(raw)
                verdict = row.get("verdict")
                row.update(
                    {
                        "note_id": note_id,
                        "text": row.get("note") or "",
                        "status": "pending" if verdict is None else "evaluated",
                    }
                )
                selected.append(row)
                selected_ids.add(note_id)
                per_symbol[symbol_key] = per_symbol.get(symbol_key, 0) + 1
                added += 1

        add(changed, recent_limit)
        add(positives, positive_limit)
        add(counterexamples, counterexample_limit)
        return selected

    def mark_curation_candidates_curated(self, candidates: list[dict]) -> int:
        """Acknowledge exactly the candidate revisions sent to a successful LLM.

        ``candidates`` must be the dictionaries returned by
        :meth:`select_curation_candidates`.  Advancing only to their snapshot
        revision preserves a newer feedback update for the next attempt.
        """

        snapshots: dict[int, int] = {}
        for candidate in candidates:
            try:
                note_id = int(candidate.get("note_id", candidate.get("id")))
                revision = int(candidate["curation_revision"])
            except (AttributeError, KeyError, TypeError, ValueError) as exc:
                raise ValueError("invalid curation candidate snapshot") from exc
            snapshots[note_id] = max(snapshots.get(note_id, 0), revision)

        updated = 0
        with self._lock:
            for note_id, revision in snapshots.items():
                cursor = self._conn.execute(
                    """
                    UPDATE notes
                    SET curated_revision = ?
                    WHERE id = ? AND curated_revision < ?
                    """,
                    (revision, note_id, revision),
                )
                updated += max(int(cursor.rowcount), 0)
            self._conn.commit()
        return updated

    # ------------------------------------------------------------------
    # Embeddings
    # ------------------------------------------------------------------

    def backfill_embeddings(
        self,
        embedder: Callable[[list[str]], list[bytes]],
        *,
        limit: int | None = None,
    ) -> int:
        """Embède les notes sans embedding (embedding IS NULL).

        L'``embedder`` est un callable ``list[str] → list[bytes]`` (injectable
        pour les tests — aucun appel réseau direct ici).
        Seules les notes avec ``note IS NOT NULL AND embedding IS NULL`` sont traitées.
        Retourne le nombre de notes embeddées.
        """
        sql = (
            "SELECT id, note FROM notes "
            "WHERE embedding IS NULL AND note IS NOT NULL ORDER BY id"
        )
        params: tuple = ()
        if limit is not None:
            sql += " LIMIT ?"
            params = (max(0, int(limit)),)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()

        if not rows:
            return 0

        ids = [r["id"] for r in rows]
        texts = [r["note"] for r in rows]

        # L'appel réseau sort du verrou pour ne pas bloquer les readers.
        blobs = embedder(texts)

        with self._lock:
            for note_id, blob in zip(ids, blobs):
                self._conn.execute(
                    "UPDATE notes SET embedding = ? WHERE id = ?",
                    (blob, note_id),
                )
            self._conn.commit()
        return len(ids)

    # ------------------------------------------------------------------
    # Recherche hybride
    # ------------------------------------------------------------------

    def search(
        self,
        *,
        query_vec: bytes | None = None,
        text_query: str | None = None,
        symbol: str | None = None,
        family: str | None = None,
        limit: int = 8,
        now: datetime | None = None,
        tau_days: float = 30.0,
        memrl_weight: float = 0.2,
        memrl_shrinkage_k: float = 5.0,
    ) -> list[dict]:
        """Recherche hybride : facettes + FTS5 BM25 + cosine numpy + fusion RRF.

        Algorithme (design §4.3, V1) :
        1. Filtre SQL (facettes symbol/family + expiration valid_until).
        2. Classement FTS5 BM25 si ``text_query`` fourni.
        3. Classement cosine brute-force si ``query_vec`` fourni.
        4. Fusion RRF (k=60) des deux classements.
        4b. Si une query est fournie, restriction à l'union FTS ∪ cosine matchés.
        5. Score final = RRF + FLAIR + fraîcheur + Q-value MemRL shrinkée.
        6. Retourne les ``limit`` meilleurs résultats, note tronquée à 240 c.
        """
        if now is None:
            now = datetime.now(timezone.utc)
        # Chaîne vide ≠ query : sinon la restriction FTS∪cosine (étape 4b)
        # éliminerait tout sur une query vide (régression re-review Codex).
        text_query = text_query or None
        query_vec = query_vec or None
        now_ts = now.isoformat()
        tau_days = max(tau_days, 1e-9)

        # --- 1. Filtre SQL facetté (sans préfixe table) ---
        base_clauses = ["(valid_until IS NULL OR valid_until > :now_ts)"]
        params: dict = {"now_ts": now_ts}

        if symbol:
            base_clauses.append("symbol = :symbol")
            params["symbol"] = symbol
        if family:
            base_clauses.append("family = :family")
            params["family"] = family

        base_where = " AND ".join(base_clauses)

        # Filtre avec préfixe n. pour les JOINs FTS5
        fts_clauses = ["(n.valid_until IS NULL OR n.valid_until > :now_ts)"]
        if symbol:
            fts_clauses.append("n.symbol = :symbol")
        if family:
            fts_clauses.append("n.family = :family")
        fts_where = " AND ".join(fts_clauses)

        # --- DB reads sous verrou ---
        with self._lock:
            # --- Récupérer tous les candidats facettés ---
            base_rows = self._conn.execute(
                f"""
                SELECT id, ts, symbol, family, verdict, outcome_score,
                       q_value, q_updates,
                       embedding, valid_from, note
                FROM notes
                WHERE {base_where}
                """,
                params,
            ).fetchall()

            if not base_rows:
                return []

            candidates: dict[int, dict] = {r["id"]: dict(r) for r in base_rows}
            candidate_ids: set[int] = set(candidates)

            # --- 2. Classement FTS5 (si text_query) ---
            fts_ranked: list[int] = []
            if text_query:
                fts_rows = self._conn.execute(
                    f"""
                    SELECT n.id
                    FROM notes_fts
                    JOIN notes n ON n.id = notes_fts.rowid
                    WHERE notes_fts MATCH :text_query AND {fts_where}
                    ORDER BY bm25(notes_fts)
                    """,
                    {**params, "text_query": text_query},
                ).fetchall()
                fts_ranked = [r[0] for r in fts_rows if r[0] in candidate_ids]

        # --- 3. Classement cosine (si query_vec) — hors verrou (CPU-only) ---
        cosine_ranked: list[int] = []
        if query_vec is not None:
            query_arr = np.frombuffer(query_vec, dtype=np.float32)
            query_norm = float(np.linalg.norm(query_arr))

            sims: list[tuple[float, int]] = []
            for note_id, row in candidates.items():
                blob = row.get("embedding")
                if blob:
                    note_arr = np.frombuffer(blob, dtype=np.float32)
                    note_norm = float(np.linalg.norm(note_arr))
                    if query_norm > 0 and note_norm > 0:
                        cos_sim = float(np.dot(query_arr, note_arr)) / (query_norm * note_norm)
                    else:
                        cos_sim = 0.0
                    sims.append((-cos_sim, note_id))  # signe négatif → sort ascendant = meilleur en premier

            sims.sort()
            cosine_ranked = [note_id for _, note_id in sims]

        # --- 4. Fusion RRF (k=60) ---
        rrf_k = 60
        rrf_scores: dict[int, float] = {}
        for rank, nid in enumerate(fts_ranked, 1):
            rrf_scores[nid] = rrf_scores.get(nid, 0.0) + 1.0 / (rrf_k + rank)
        for rank, nid in enumerate(cosine_ranked, 1):
            rrf_scores[nid] = rrf_scores.get(nid, 0.0) + 1.0 / (rrf_k + rank)

        # --- 4b. Restriction aux matchés si une query est fournie ---
        # Sans query : facettes seules → tous les candidats sont éligibles.
        # Avec query : seuls les résultats FTS ∪ cosine remontent (évite qu'une
        # note haute outcome+fraîcheur sans rapport avec la query ne passe).
        if text_query is not None or query_vec is not None:
            matched_ids = set(rrf_scores)
            if not matched_ids:
                return []
            candidates = {nid: row for nid, row in candidates.items() if nid in matched_ids}

        # --- 5. Score final et tri ---
        scored: list[tuple[float, int]] = []
        for note_id, row in candidates.items():
            rrf = rrf_scores.get(note_id, 0.0)
            outcome_score = row.get("outcome_score") or 0.0

            vf_str = row.get("valid_from") or row.get("ts") or ""
            try:
                if vf_str:
                    note_dt = datetime.fromisoformat(vf_str.replace("Z", "+00:00"))
                    if note_dt.tzinfo is None:
                        note_dt = note_dt.replace(tzinfo=timezone.utc)
                    age_days = max(0.0, (now - note_dt).total_seconds() / 86400.0)
                else:
                    age_days = 0.0
            except (ValueError, TypeError):
                age_days = 0.0

            freshness = math.exp(-age_days / tau_days) - 1.0
            q_value = float(row.get("q_value") or 0.0)
            q_updates = max(int(row.get("q_updates") or 0), 0)
            q_confidence = q_updates / (q_updates + max(memrl_shrinkage_k, 1e-9))
            q_decay = math.exp(-age_days / tau_days)
            memrl_score = float(memrl_weight) * q_value * q_confidence * q_decay
            final_score = rrf + outcome_score + freshness + memrl_score
            # Tri descendant : on stocke le négatif + note_id comme tie-breaker
            scored.append((-final_score, note_id))

        scored.sort()
        top = scored[:limit]

        # --- 6. Construire le résultat ---
        result: list[dict] = []
        for _, note_id in top:
            row = candidates[note_id]
            note_text = row.get("note") or ""
            row_q_updates = max(int(row.get("q_updates") or 0), 0)
            result.append(
                {
                    "id": note_id,
                    "ts": row.get("ts"),
                    "symbol": row.get("symbol"),
                    "verdict": row.get("verdict") or "UNKNOWN",
                    "outcome_score": row.get("outcome_score"),
                    "q_value": row.get("q_value"),
                    "q_updates": row_q_updates,
                    "note": note_text[:240],
                }
            )

        return result

    # ------------------------------------------------------------------
    # Trace des injections
    # ------------------------------------------------------------------

    def record_recall(self, *, decision_id: str, note_ids: list[int]) -> None:
        """Trace l'injection de notes pour une décision (design §4.4, invariant lecture seule de l'outil).

        Appelé par le daemon après une tournée d'outils, pas par le handler.
        Écrit une ligne dans ``recalls(decision_id, note_ids JSON, ts ISO)`` .
        """
        ts = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self._conn.execute(
                "INSERT INTO recalls (decision_id, note_ids, ts) VALUES (?, ?, ?)",
                (decision_id, json.dumps(note_ids), ts),
            )
            self._conn.commit()

    def pending_outcome_notes(
        self,
        *,
        mature_before: str | None = None,
        limit: int = 64,
    ) -> list[dict]:
        """Return notes whose original decision has not received a verdict yet."""

        with self._lock:
            sql = """
                SELECT id, decision_id, ts, symbol, family, action, intent,
                       executed, note, verdict, forward_return, outcome_score,
                       q_value, q_updates
                FROM notes
                WHERE verdict IS NULL
            """
            params: list[object] = []
            if mature_before is not None:
                sql += " AND ts <= ?"
                params.append(mature_before)
            sql += " ORDER BY ts DESC, id DESC LIMIT ?"
            params.append(max(0, int(limit)))
            rows = self._conn.execute(sql, tuple(params)).fetchall()
        return [dict(row) for row in rows]

    def update_note_outcomes(self, rows: list[dict]) -> int:
        """Persist mature FLAIR outcomes by note id."""

        updated = 0
        changed_at = datetime.now(timezone.utc).isoformat()
        with self._lock:
            for row in rows:
                verdict = row.get("verdict")
                if verdict is None:
                    continue
                cursor = self._conn.execute(
                    """
                    UPDATE notes
                    SET verdict = ?, forward_return = ?,
                        curation_revision = curation_revision + 1,
                        curation_updated_at = ?
                    WHERE id = ? AND verdict IS NULL
                    """,
                    (verdict, row.get("forward_return"), changed_at, row.get("id")),
                )
                updated += max(int(cursor.rowcount), 0)
            self._conn.commit()
        return updated

    def pending_recall_decision_ids(
        self,
        *,
        mature_before: str | None = None,
        limit: int = 64,
    ) -> list[str]:
        """Return recalled decisions that still need a delayed MemRL reward."""

        with self._lock:
            sql = """
                SELECT decision_id, MIN(id) AS first_id
                FROM recalls
                WHERE evaluated_at IS NULL AND decision_id IS NOT NULL
            """
            params: list[object] = []
            if mature_before is not None:
                sql += " AND ts <= ?"
                params.append(mature_before)
            sql += " GROUP BY decision_id ORDER BY first_id DESC LIMIT ?"
            params.append(max(0, int(limit)))
            rows = self._conn.execute(sql, tuple(params)).fetchall()
        return [str(row["decision_id"]) for row in rows if row["decision_id"]]

    def apply_recall_outcome(
        self,
        *,
        decision_id: str,
        verdict: str,
        reward: float | None,
        forward_return: float | None,
        evaluated_at: str,
        alpha: float = 0.1,
    ) -> dict:
        """Apply one delayed decision reward to every note recalled for it."""

        learning_rate = min(max(float(alpha), 0.0), 1.0)
        note_ids: set[int] = set()
        with self._lock:
            recall_rows = self._conn.execute(
                "SELECT id, note_ids FROM recalls WHERE decision_id=? AND evaluated_at IS NULL",
                (decision_id,),
            ).fetchall()
            for recall_row in recall_rows:
                try:
                    raw_ids = json.loads(recall_row["note_ids"] or "[]")
                except json.JSONDecodeError:
                    raw_ids = []
                note_ids.update(note_id for note_id in raw_ids if isinstance(note_id, int))

            if recall_rows:
                self._conn.execute(
                    """
                    UPDATE recalls
                    SET verdict=?, reward=?, forward_return=?, evaluated_at=?
                    WHERE decision_id=? AND evaluated_at IS NULL
                    """,
                    (
                        verdict,
                        None if reward is None else float(reward),
                        forward_return,
                        evaluated_at,
                        decision_id,
                    ),
                )

            notes_updated = 0
            for note_id in sorted(note_ids) if reward is not None else []:
                row = self._conn.execute(
                    "SELECT q_value FROM notes WHERE id=?",
                    (note_id,),
                ).fetchone()
                if row is None:
                    continue
                old_q = float(row["q_value"] or 0.0)
                new_q = old_q + learning_rate * (float(reward) - old_q)
                cursor = self._conn.execute(
                    """
                    UPDATE notes
                    SET q_value=?, q_updates=q_updates+1,
                        curation_revision=curation_revision+1,
                        curation_updated_at=?
                    WHERE id=?
                    """,
                    (new_q, datetime.now(timezone.utc).isoformat(), note_id),
                )
                notes_updated += max(int(cursor.rowcount), 0)
            self._conn.commit()
        return {
            "recalls_updated": len(recall_rows),
            "notes_updated": notes_updated,
            "note_ids": sorted(note_ids),
        }

    # ------------------------------------------------------------------
    # MemRL for displayed global rules (a separate space from note recall)
    # ------------------------------------------------------------------

    def sync_global_rules(
        self,
        active_rule_ids: list[str],
        *,
        ts: str | None = None,
        now: datetime | None = None,
    ) -> dict:
        """Synchronize the citable global-rule set without deleting history.

        A later consolidation calls this with its ten current stable IDs.  Rules
        absent from the set become inactive (and cannot be newly cited), but
        their existing Q history remains available for audit and delayed
        outcomes of already-recorded citations can still update it.

        ``ts`` is retained as a compatibility input for callers that used to
        pass a business watermark.  It never controls lifecycle timestamps;
        those use a non-decreasing UTC wall clock, optionally injected via
        ``now`` for deterministic tests.
        """

        rule_ids = list(dict.fromkeys(str(rule_id).strip() for rule_id in active_rule_ids if str(rule_id).strip()))
        _ = ts
        lifecycle_clock = _utc_lifecycle_clock(now)
        with self._lock:
            existing_rows = self._conn.execute(
                "SELECT rule_id, active, created_at, retired_at, updated_at FROM global_rules"
            ).fetchall()
            existing = {str(row["rule_id"]): int(row["active"] or 0) for row in existing_rows}
            previous_clocks = [
                parsed
                for row in existing_rows
                for column in ("created_at", "retired_at", "updated_at")
                if (parsed := _parse_lifecycle_ts(row[column])) is not None
            ]
            if previous_clocks:
                lifecycle_clock = max(lifecycle_clock, max(previous_clocks))
            changed_at = lifecycle_clock.isoformat()
            for rule_id in rule_ids:
                self._conn.execute(
                    """
                    INSERT INTO global_rules (rule_id, active, created_at, updated_at)
                    VALUES (?, 1, ?, ?)
                    ON CONFLICT(rule_id) DO UPDATE SET
                        active=1,
                        retired_at=NULL,
                        updated_at=excluded.updated_at
                    """,
                    (rule_id, changed_at, changed_at),
                )

            if rule_ids:
                placeholders = ", ".join("?" for _ in rule_ids)
                cursor = self._conn.execute(
                    f"""
                    UPDATE global_rules
                    SET active=0, retired_at=?, updated_at=?
                    WHERE active=1 AND rule_id NOT IN ({placeholders})
                    """,
                    (changed_at, changed_at, *rule_ids),
                )
            else:
                cursor = self._conn.execute(
                    """
                    UPDATE global_rules
                    SET active=0, retired_at=?, updated_at=?
                    WHERE active=1
                    """,
                    (changed_at, changed_at),
                )
            retired = max(int(cursor.rowcount), 0)
            self._conn.commit()
        return {
            "created": sum(rule_id not in existing for rule_id in rule_ids),
            "reactivated": sum(existing.get(rule_id) == 0 for rule_id in rule_ids),
            "retired": retired,
            "active": len(rule_ids),
        }

    def global_rule_scores(
        self,
        rule_ids: list[str] | None = None,
        *,
        active_only: bool = False,
    ) -> dict[str, dict]:
        """Return stable global-rule MemRL scores, including retired audit rows."""

        clauses: list[str] = []
        params: list[object] = []
        if active_only:
            clauses.append("active=1")
        if rule_ids is not None:
            cleaned = list(dict.fromkeys(str(rule_id).strip() for rule_id in rule_ids if str(rule_id).strip()))
            if not cleaned:
                return {}
            clauses.append("rule_id IN (" + ", ".join("?" for _ in cleaned) + ")")
            params.extend(cleaned)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self._lock:
            rows = self._conn.execute(
                "SELECT rule_id, q_value, q_updates, active, created_at, retired_at, updated_at "
                "FROM global_rules" + where + " ORDER BY rule_id",
                tuple(params),
            ).fetchall()
        return {
            str(row["rule_id"]): {
                "q_value": float(row["q_value"] or 0.0),
                "q_updates": int(row["q_updates"] or 0),
                "active": bool(row["active"]),
                "created_at": row["created_at"],
                "retired_at": row["retired_at"],
                "updated_at": row["updated_at"],
            }
            for row in rows
        }

    def record_global_rule_citation(
        self,
        *,
        decision_id: str,
        rule_ids: list[str],
        ts: str | None = None,
    ) -> bool:
        """Record the rules an LLM says actually affected one decision.

        The store rejects invented, inactive, duplicate, or over-limit IDs.
        Prompt-level validation remains necessary, but this provides a durable
        last line of defence before a rule can accrue MemRL credit.
        """

        cleaned = [str(rule_id).strip() for rule_id in rule_ids if str(rule_id).strip()]
        if not decision_id:
            raise ValueError("decision_id is required for a global rule citation")
        if not cleaned or len(cleaned) > 3 or len(set(cleaned)) != len(cleaned):
            raise ValueError("global rule citations require one to three distinct rule IDs")
        citation_ts = ts or datetime.now(timezone.utc).isoformat()
        with self._lock:
            placeholders = ", ".join("?" for _ in cleaned)
            active_rows = self._conn.execute(
                f"SELECT rule_id FROM global_rules WHERE active=1 AND rule_id IN ({placeholders})",
                tuple(cleaned),
            ).fetchall()
            active_ids = {str(row["rule_id"]) for row in active_rows}
            if active_ids != set(cleaned):
                unknown = sorted(set(cleaned) - active_ids)
                raise ValueError(f"cannot cite inactive or unknown global rules: {unknown}")
            cursor = self._conn.execute(
                """
                INSERT INTO global_rule_citations (decision_id, rule_ids, ts)
                VALUES (?, ?, ?)
                ON CONFLICT(decision_id) DO NOTHING
                """,
                (str(decision_id), json.dumps(cleaned), citation_ts),
            )
            self._conn.commit()
        return bool(cursor.rowcount)

    def pending_global_rule_decision_ids(
        self,
        *,
        mature_before: str | None = None,
        limit: int = 64,
    ) -> list[str]:
        """Return cited decisions whose delayed global-rule reward is pending."""

        with self._lock:
            sql = """
                SELECT decision_id
                FROM global_rule_citations
                WHERE evaluated_at IS NULL
            """
            params: list[object] = []
            if mature_before is not None:
                sql += " AND ts <= ?"
                params.append(mature_before)
            sql += " ORDER BY ts DESC, id DESC LIMIT ?"
            params.append(max(0, int(limit)))
            rows = self._conn.execute(sql, tuple(params)).fetchall()
        return [str(row["decision_id"]) for row in rows]

    def apply_global_rule_outcome(
        self,
        *,
        decision_id: str,
        verdict: str,
        reward: float | None,
        forward_return: float | None,
        evaluated_at: str,
        alpha: float = 0.1,
    ) -> dict:
        """Apply one idempotent delayed reward to explicitly cited global rules."""

        learning_rate = min(max(float(alpha), 0.0), 1.0)
        with self._lock:
            citation = self._conn.execute(
                """
                SELECT id, rule_ids
                FROM global_rule_citations
                WHERE decision_id=? AND evaluated_at IS NULL
                """,
                (decision_id,),
            ).fetchone()
            if citation is None:
                return {"citations_updated": 0, "rules_updated": 0, "rule_ids": []}
            try:
                rule_ids = json.loads(citation["rule_ids"] or "[]")
            except json.JSONDecodeError:
                rule_ids = []
            rule_ids = sorted({str(rule_id) for rule_id in rule_ids if str(rule_id).strip()})
            self._conn.execute(
                """
                UPDATE global_rule_citations
                SET verdict=?, reward=?, forward_return=?, evaluated_at=?
                WHERE id=? AND evaluated_at IS NULL
                """,
                (
                    verdict,
                    None if reward is None else float(reward),
                    forward_return,
                    evaluated_at,
                    citation["id"],
                ),
            )

            rules_updated = 0
            if reward is not None:
                for rule_id in rule_ids:
                    row = self._conn.execute(
                        "SELECT q_value FROM global_rules WHERE rule_id=?",
                        (rule_id,),
                    ).fetchone()
                    if row is None:
                        continue
                    old_q = float(row["q_value"] or 0.0)
                    new_q = old_q + learning_rate * (float(reward) - old_q)
                    cursor = self._conn.execute(
                        """
                        UPDATE global_rules
                        SET q_value=?, q_updates=q_updates+1, updated_at=?
                        WHERE rule_id=?
                        """,
                        (new_q, datetime.now(timezone.utc).isoformat(), rule_id),
                    )
                    rules_updated += max(int(cursor.rowcount), 0)
            self._conn.commit()
        return {
            "citations_updated": 1,
            "rules_updated": rules_updated,
            "rule_ids": rule_ids,
        }
