"""Store SQLite des learnings de trading — schéma + ingestion + scoring FLAIR.

Implémente le design 2026-07-02-learnings-recall-design.md §4.1 et §4.2 :
- Table `notes` : colonnes complètes incluant scoring FLAIR et embeddings.
- Index FTS5 `notes_fts` (content table, maintenu par triggers).
- Table `recalls` : trace des notes servies par décision (phase ②).
- Scoring FLAIR : lift normalisé par symbole + shrinkage bayésien vers 0.

Le .db est un DÉRIVÉ reconstructible — les JSONL d'archives restent canoniques.
"""
from __future__ import annotations

import json
import math
import sqlite3
import threading
from collections import defaultdict
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from trader.semantic.catalog import family_for_symbol

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
        self._lock = threading.Lock()
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
                })

        with self._lock:
            for params in rows_to_insert:
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

    def apply_verdicts(self, bootstrap_json: "str | Path") -> int:
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
        with self._lock:
            for item in learnings:
                decision_id = item.get("decision_id")
                if not decision_id:
                    continue
                cursor = self._conn.execute(
                    """
                    UPDATE notes
                       SET verdict        = :verdict,
                           forward_return = :forward_return
                     WHERE decision_id = :decision_id
                    """,
                    {
                        "decision_id": str(decision_id),
                        "verdict": item.get("verdict"),
                        "forward_return": item.get("forward_return"),
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

            # Compter wins/losses par symbole et par famille (pour les fallbacks)
            wins_by_sym: dict[str, int] = defaultdict(int)
            losses_by_sym: dict[str, int] = defaultdict(int)
            wins_by_family: dict[str, int] = defaultdict(int)
            losses_by_family: dict[str, int] = defaultdict(int)
            global_wins = 0
            global_losses = 0

            for row in rows:
                verdict = row["verdict"]
                if verdict not in ("WIN", "LOSS"):
                    continue
                sym = row["symbol"] or ""
                fam = row["family"] or ""
                if verdict == "WIN":
                    wins_by_sym[sym] += 1
                    global_wins += 1
                    if fam:
                        wins_by_family[fam] += 1
                else:  # LOSS
                    losses_by_sym[sym] += 1
                    global_losses += 1
                    if fam:
                        losses_by_family[fam] += 1

            # Base rate globale (fallback ultime)
            global_total = global_wins + global_losses
            global_base_rate = global_wins / global_total if global_total > 0 else 0.5

            def _base_rate(sym: str, fam: str) -> float:
                """Retourne la base_rate du symbole, avec fallbacks famille puis global."""
                sym_total = wins_by_sym[sym] + losses_by_sym[sym]
                if sym_total >= 5:
                    return wins_by_sym[sym] / sym_total
                # Fallback famille
                if fam:
                    fam_total = wins_by_family.get(fam, 0) + losses_by_family.get(fam, 0)
                    if fam_total >= 5:
                        return wins_by_family[fam] / fam_total
                # Fallback global
                return global_base_rate

            # Calculer et persister outcome_score pour chaque note
            scored = 0
            base_rates: dict[str, float] = {}

            for row in rows:
                note_id = row["id"]
                verdict = row["verdict"]
                sym = row["symbol"] or ""
                fam = row["family"] or ""

                if verdict in ("WIN", "LOSS"):
                    br = _base_rate(sym, fam)
                    base_rates[sym] = br
                    win_indicator = 1.0 if verdict == "WIN" else 0.0
                    lift = win_indicator - br
                    outcome_score = lift / (1.0 + shrinkage_k)
                else:
                    # NEUTRAL ou UNKNOWN : neutre au ranking
                    outcome_score = 0.0

                self._conn.execute(
                    "UPDATE notes SET outcome_score = :score WHERE id = :id",
                    {"score": outcome_score, "id": note_id},
                )
                scored += 1

            self._conn.commit()
        return {"scored": scored, "base_rates": base_rates}

    # ------------------------------------------------------------------
    # Embeddings
    # ------------------------------------------------------------------

    def backfill_embeddings(self, embedder: Callable[[list[str]], list[bytes]]) -> int:
        """Embède les notes sans embedding (embedding IS NULL).

        L'``embedder`` est un callable ``list[str] → list[bytes]`` (injectable
        pour les tests — aucun appel réseau direct ici).
        Seules les notes avec ``note IS NOT NULL AND embedding IS NULL`` sont traitées.
        Retourne le nombre de notes embeddées.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, note FROM notes WHERE embedding IS NULL AND note IS NOT NULL"
            ).fetchall()

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
    ) -> list[dict]:
        """Recherche hybride : facettes + FTS5 BM25 + cosine numpy + fusion RRF.

        Algorithme (design §4.3, V1) :
        1. Filtre SQL (facettes symbol/family + expiration valid_until).
        2. Classement FTS5 BM25 si ``text_query`` fourni.
        3. Classement cosine brute-force si ``query_vec`` fourni.
        4. Fusion RRF (k=60) des deux classements.
        4b. Si une query est fournie, restriction à l'union FTS ∪ cosine matchés.
        5. Score final = rrf + outcome_score + exp(−age_days/τ) − 1.
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
            final_score = rrf + outcome_score + freshness
            # Tri descendant : on stocke le négatif + note_id comme tie-breaker
            scored.append((-final_score, note_id))

        scored.sort()
        top = scored[:limit]

        # --- 6. Construire le résultat ---
        result: list[dict] = []
        for _, note_id in top:
            row = candidates[note_id]
            note_text = row.get("note") or ""
            result.append(
                {
                    "id": note_id,
                    "ts": row.get("ts"),
                    "symbol": row.get("symbol"),
                    "verdict": row.get("verdict") or "UNKNOWN",
                    "outcome_score": row.get("outcome_score"),
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
