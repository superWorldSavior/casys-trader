"""SQLite-derived memory index for macro/news situation briefs."""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from trader.domain.situation import NewsMacroBrief, SituationPoint, SituationSection
from trader.infrastructure.state_db.fts_query import sanitize_fts5_query

__all__ = ["SituationMemoryStore"]

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


def _create_schema(conn: sqlite3.Connection) -> None:
    conn.execute(_DDL_NOTES)
    columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(situation_notes)")}
    if "source_names" not in columns:
        conn.execute("ALTER TABLE situation_notes ADD COLUMN source_names TEXT")
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
                        outcome_score, q_value
                    ) VALUES (
                        :note_key, :brief_id, :brief_ref, :as_of, :valid_from, :valid_until,
                        :venue, :section_type, :section_name, :point_index, :point,
                        :source_uuids, :source_names, :symbols, :severity, :signal, :direction, :horizon,
                        :outcome_score, :q_value
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
    }
