"""SQLite-derived memory index for macro/news situation briefs."""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from contextlib import contextmanager
from collections.abc import Collection, Mapping, Sequence
from pathlib import Path
from typing import Any

from trader.domain.situation import NewsMacroBrief, SituationPoint, SituationSection
from trader.infrastructure.state_db.fts_query import sanitize_fts5_query
from trader.infrastructure.state_db.migrations import SITUATION_MEMORY_OUTCOME_COLUMNS

__all__ = ["SituationMemoryStore", "read_situation_notes"]

log = logging.getLogger(__name__)

_PRAGMAS = [
    "PRAGMA journal_mode=WAL;",
    "PRAGMA synchronous=NORMAL;",
    "PRAGMA foreign_keys=ON;",
    "PRAGMA busy_timeout=2000;",
]

_DDL_NOTES = """
CREATE TABLE IF NOT EXISTS situation_notes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    note_key        TEXT UNIQUE NOT NULL,
    brief_id        TEXT NOT NULL,
    brief_ref       TEXT,
    as_of           TEXT,
    valid_from      TEXT,
    valid_until     TEXT,
    venue           TEXT,
    section_type    TEXT,
    section_name    TEXT,
    point_index     INTEGER,
    point           TEXT,
    source_uuids    TEXT,
    source_names    TEXT,
    symbols         TEXT,
    severity        TEXT,
    signal          TEXT,
    direction       TEXT,
    horizon         TEXT,
    event_class     TEXT,
    outcome_score   REAL,
    q_value         REAL,
    embedding       BLOB
)
"""

_DDL_FTS = """
CREATE VIRTUAL TABLE IF NOT EXISTS situation_notes_fts
    USING fts5(point, content='situation_notes', content_rowid='id')
"""

_TRIGGER_AI = """
CREATE TRIGGER IF NOT EXISTS situation_notes_ai
    AFTER INSERT ON situation_notes
BEGIN
    INSERT INTO situation_notes_fts(rowid, point) VALUES (new.id, new.point);
END
"""

_TRIGGER_AD = """
CREATE TRIGGER IF NOT EXISTS situation_notes_ad
    AFTER DELETE ON situation_notes
BEGIN
    INSERT INTO situation_notes_fts(situation_notes_fts, rowid, point)
        VALUES ('delete', old.id, old.point);
END
"""

_TRIGGER_AU = """
CREATE TRIGGER IF NOT EXISTS situation_notes_au
    AFTER UPDATE ON situation_notes
BEGIN
    INSERT INTO situation_notes_fts(situation_notes_fts, rowid, point)
        VALUES ('delete', old.id, old.point);
    INSERT INTO situation_notes_fts(rowid, point) VALUES (new.id, new.point);
END
"""


def _is_fts5_query_error(exc: BaseException) -> bool:
    message = str(exc).lower()
    return "fts5:" in message or "syntax error" in message or "no such column" in message


def _open_db(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    for pragma in _PRAGMAS:
        conn.execute(pragma)
    return conn


_NOTE_COLUMNS = """
    id, brief_id, as_of, venue, section_type, section_name, symbols,
    direction, horizon, outcome_score, q_value, verdict, horizon_sessions,
    forward_return, evaluated_at, coverage_n
"""
_BRIDGE_COLUMNS = (
    "note_key",
    "brief_id",
    "venue",
    "as_of",
    "valid_from",
    "valid_until",
    "point",
    "source_uuids",
    "source_names",
    "symbols",
    "severity",
    "signal",
    "direction",
    "horizon",
    "event_class",
)
_BRIDGE_JSON_COLUMNS = frozenset({"source_uuids", "source_names", "symbols"})


def ensure_notes_schema(db_path: str | Path) -> bool:
    """Run notes migrations on an existing db. Never creates a missing file."""

    path = Path(db_path)
    if not path.is_file():
        return False
    store = SituationMemoryStore(path)
    store.close()
    return True


def read_situation_notes(db_path: str | Path) -> list[dict[str, Any]]:
    """Read-only notes for the ABOUT bridge. Never creates or migrates files."""

    return _read_situation_notes_where(db_path, brief_ids=None)


def read_situation_notes_by_brief_ids(
    db_path: str | Path, brief_ids: Collection[str]
) -> list[dict[str, Any]]:
    """Read-only notes for given briefs. Empty ids read nothing, never all."""

    wanted = [str(item) for item in brief_ids if str(item).strip()]
    if not wanted:
        return []
    return _read_situation_notes_where(db_path, brief_ids=wanted)


def _read_situation_notes_where(
    db_path: str | Path, *, brief_ids: Sequence[str] | None
) -> list[dict[str, Any]]:
    path = Path(db_path)
    if not path.is_file():
        raise FileNotFoundError(f"situation notes db is missing: {path}")
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        conn.row_factory = sqlite3.Row
        query = f"SELECT {', '.join(_BRIDGE_COLUMNS)} FROM situation_notes"
        params: Sequence[str] = ()
        if brief_ids is not None:
            query += f" WHERE brief_id IN ({', '.join('?' * len(brief_ids))})"
            params = list(brief_ids)
        rows = conn.execute(query + " ORDER BY id", params).fetchall()
    finally:
        conn.close()
    notes: list[dict[str, Any]] = []
    for row in rows:
        note = {column: row[column] for column in _BRIDGE_COLUMNS}
        for column in _BRIDGE_JSON_COLUMNS:
            note[column] = [str(item) for item in _json_list(note[column])]
        notes.append(note)
    return notes


def _create_schema(conn: sqlite3.Connection) -> None:
    conn.execute(_DDL_NOTES)
    columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(situation_notes)")}
    if "source_names" not in columns:
        conn.execute("ALTER TABLE situation_notes ADD COLUMN source_names TEXT")
        columns.add("source_names")
    if "event_class" not in columns:
        conn.execute("ALTER TABLE situation_notes ADD COLUMN event_class TEXT")
        columns.add("event_class")
    for name, decl in SITUATION_MEMORY_OUTCOME_COLUMNS:
        if name not in columns:
            conn.execute(f"ALTER TABLE situation_notes ADD COLUMN {name} {decl}")
            columns.add(name)
    conn.execute(_DDL_FTS)
    conn.execute(_TRIGGER_AI)
    conn.execute(_TRIGGER_AD)
    conn.execute(_TRIGGER_AU)
    conn.commit()


class SituationMemoryStore:
    """Derived SQLite/FTS index over append-only news macro briefs."""

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = _open_db(self._db_path)
        _create_schema(self._conn)

    @contextmanager
    def _commit_or_rollback(self):
        try:
            yield
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    def close(self) -> None:
        """Checkpoint WAL and close the connection. Idempotent."""

        with self._lock:
            if self._conn is None:
                return
            try:
                self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except Exception:
                pass
            try:
                self._conn.close()
            finally:
                self._conn = None

    def count(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) FROM situation_notes").fetchone()
        return int(row[0])

    def ingest_brief(self, brief: NewsMacroBrief) -> dict:
        """Idempotently index all points from one brief."""

        rows = list(_rows_for_brief(brief))
        inserted = 0
        skipped = 0
        with self._lock, self._commit_or_rollback():
            for row in rows:
                cursor = self._conn.execute(
                    """
                    INSERT OR IGNORE INTO situation_notes (
                        note_key, brief_id, brief_ref, as_of, valid_from, valid_until,
                        venue, section_type, section_name, point_index, point,
                        source_uuids, source_names, symbols, severity, signal, direction, horizon,
                        event_class, outcome_score, q_value
                    ) VALUES (
                        :note_key, :brief_id, :brief_ref, :as_of, :valid_from, :valid_until,
                        :venue, :section_type, :section_name, :point_index, :point,
                        :source_uuids, :source_names, :symbols, :severity, :signal, :direction, :horizon,
                        :event_class, :outcome_score, :q_value
                    )
                    """,
                    row,
                )
                if cursor.rowcount == 1:
                    inserted += 1
                else:
                    skipped += 1
        return {"inserted": inserted, "skipped": skipped}

    def search(
        self,
        *,
        query: str | None = None,
        venue: str | None = None,
        symbol: str | None = None,
        family: str | None = None,
        active_at: str | None = None,
        limit: int = 8,
    ) -> list[dict]:
        """Search situation notes by FTS and structured filters."""

        limit = max(1, min(int(limit), 50))
        params: dict[str, Any] = {"limit": limit}
        clauses = ["1=1"]
        if venue:
            clauses.append("n.venue = :venue")
            params["venue"] = venue
        if symbol:
            clauses.append("n.symbols LIKE :symbol_like")
            params["symbol_like"] = f'%"{symbol}"%'
        if family:
            clauses.append("n.section_type = 'family' AND n.section_name = :family")
            params["family"] = family
        if active_at:
            clauses.append("(n.valid_from IS NULL OR n.valid_from <= :active_at)")
            clauses.append("(n.valid_until IS NULL OR n.valid_until > :active_at)")
            params["active_at"] = active_at

        where = " AND ".join(clauses)
        with self._lock:
            fts_query = sanitize_fts5_query(query) if query else None
            if query:
                if not fts_query:
                    rows = []
                else:
                    try:
                        rows = self._conn.execute(
                            f"""
                            SELECT n.*
                            FROM situation_notes_fts
                            JOIN situation_notes n ON n.id = situation_notes_fts.rowid
                            WHERE situation_notes_fts MATCH :query AND {where}
                            ORDER BY bm25(situation_notes_fts), n.as_of DESC, n.id DESC
                            LIMIT :limit
                            """,
                            {**params, "query": fts_query},
                        ).fetchall()
                    except sqlite3.OperationalError as exc:
                        if not _is_fts5_query_error(exc):
                            raise
                        log.debug("situation memory FTS query rejected: %s", exc)
                        rows = []
            else:
                rows = self._conn.execute(
                    f"""
                    SELECT n.*
                    FROM situation_notes n
                    WHERE {where}
                    ORDER BY n.as_of DESC, n.id DESC
                    LIMIT :limit
                    """,
                    params,
                ).fetchall()
        return [_row_to_result(row) for row in rows]

    def load_notes(self) -> list[dict[str, Any]]:
        """Load notes for scoring without the embedding blob."""
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {_NOTE_COLUMNS} FROM situation_notes ORDER BY id"
            ).fetchall()
        return [_row_to_outcome(row) for row in rows]

    def load_outcomes(self) -> list[dict[str, Any]]:
        """Load notes that have already received a verdict."""
        with self._lock:
            rows = self._conn.execute(
                f"""
                SELECT {_NOTE_COLUMNS}
                  FROM situation_notes
                 WHERE evaluated_at IS NOT NULL
                 ORDER BY id
                """
            ).fetchall()
        return [_row_to_outcome(row) for row in rows]

    def load_pending_notes(self, *, limit: int | None = None) -> list[dict[str, Any]]:
        """Load notes that have not yet received a verdict. Read-only."""
        sql = f"""
            SELECT {_NOTE_COLUMNS}
              FROM situation_notes
             WHERE evaluated_at IS NULL
             ORDER BY id
        """
        params: dict[str, Any] = {}
        if limit is not None:
            sql += " LIMIT :limit"
            params["limit"] = max(0, int(limit))
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [_row_to_outcome(row) for row in rows]

    def apply_outcomes(self, rows: Sequence[Mapping[str, Any]]) -> None:
        """Update verdict fields in place. Idempotent by note id."""
        if not rows:
            return
        with self._lock, self._commit_or_rollback():
            for row in rows:
                self._conn.execute(
                    """
                    UPDATE situation_notes
                       SET verdict = :verdict,
                           horizon_sessions = :horizon_sessions,
                           forward_return = :forward_return,
                           evaluated_at = :evaluated_at,
                           coverage_n = :coverage_n
                     WHERE id = :id
                    """,
                    {
                        "id": int(row["id"]),
                        "verdict": str(row.get("verdict") or ""),
                        "horizon_sessions": row.get("horizon_sessions"),
                        "forward_return": row.get("forward_return"),
                        "evaluated_at": str(row.get("evaluated_at") or ""),
                        "coverage_n": row.get("coverage_n"),
                    },
                )

    def update_outcome_scores(self, scores: Mapping[int, float]) -> None:
        """Write FLAIR ``outcome_score`` values. Leaves ``q_value`` untouched."""
        if not scores:
            return
        with self._lock, self._commit_or_rollback():
            for note_id, score in scores.items():
                self._conn.execute(
                    "UPDATE situation_notes SET outcome_score = ? WHERE id = ?",
                    (float(score), int(note_id)),
                )


def _rows_for_brief(brief: NewsMacroBrief) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    date_key = brief.as_of[:10]
    brief_ref = json.dumps(brief.ref(date=date_key), ensure_ascii=False, sort_keys=True)

    def add_point(section_type: str, section_name: str, point: SituationPoint, index: int) -> None:
        rows.append(
            {
                "note_key": f"{brief.brief_id}|{section_type}|{section_name}|{index}",
                "brief_id": brief.brief_id,
                "brief_ref": brief_ref,
                "as_of": brief.as_of,
                "valid_from": brief.as_of,
                "valid_until": brief.valid_until,
                "venue": brief.venue,
                "section_type": section_type,
                "section_name": section_name,
                "point_index": index,
                "point": point.point,
                "source_uuids": json.dumps(list(point.source_refs), ensure_ascii=False),
                "source_names": json.dumps(list(point.sources), ensure_ascii=False),
                "symbols": json.dumps(list(point.symbols), ensure_ascii=False),
                "severity": point.severity,
                "signal": point.signal,
                "direction": point.direction,
                "horizon": point.horizon,
                "event_class": point.event_class,
                "outcome_score": 0.0,
                "q_value": None,
            }
        )

    for section_type, sections in (
        ("zone", brief.zones),
        ("family", brief.families),
        ("symbol", brief.symbols),
    ):
        for section in sections:
            _add_section(rows, add_point, section_type, section)
    for idx, point in enumerate(brief.alerts):
        add_point("alert", "alerts", point, idx)
    return rows


def _add_section(
    _rows: list[dict[str, Any]],
    add_point: Any,
    section_type: str,
    section: SituationSection,
) -> None:
    for idx, point in enumerate(section.points):
        add_point(section_type, section.name, point, idx)


def _json_list(value: Any) -> list:
    try:
        parsed = json.loads(value or "[]")
    except json.JSONDecodeError:
        return []
    return parsed if isinstance(parsed, list) else []


def _row_to_result(row: sqlite3.Row) -> dict[str, Any]:
    source_refs = _json_list(row["source_uuids"])
    source_names = _json_list(row["source_names"])
    return {
        "id": row["id"],
        "brief_id": row["brief_id"],
        "brief_ref": json.loads(row["brief_ref"]) if row["brief_ref"] else None,
        "as_of": row["as_of"],
        "valid_until": row["valid_until"],
        "venue": row["venue"],
        "section_type": row["section_type"],
        "section_name": row["section_name"],
        "point": row["point"],
        "sources": source_names or source_refs,
        "source_refs": source_refs,
        "symbols": _json_list(row["symbols"]),
        "severity": row["severity"],
        "signal": row["signal"],
        "direction": row["direction"],
        "horizon": row["horizon"],
        "outcome_score": row["outcome_score"],
        "q_value": row["q_value"],
        "verdict": _optional(row, "verdict"),
        "horizon_sessions": _optional(row, "horizon_sessions"),
        "forward_return": _optional(row, "forward_return"),
        "evaluated_at": _optional(row, "evaluated_at"),
        "coverage_n": _optional(row, "coverage_n"),
    }


def _optional(row: sqlite3.Row, key: str) -> Any:
    try:
        return row[key]
    except (IndexError, KeyError):
        return None


def _row_to_outcome(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "brief_id": row["brief_id"],
        "as_of": row["as_of"],
        "venue": row["venue"] or "",
        "section_type": row["section_type"] or "",
        "section_name": row["section_name"] or "",
        "symbols": row["symbols"],
        "direction": row["direction"],
        "horizon": row["horizon"],
        "outcome_score": None if row["outcome_score"] is None else float(row["outcome_score"]),
        "q_value": row["q_value"],
        "verdict": row["verdict"],
        "horizon_sessions": row["horizon_sessions"],
        "forward_return": None if row["forward_return"] is None else float(row["forward_return"]),
        "evaluated_at": row["evaluated_at"],
        "coverage_n": row["coverage_n"],
    }
