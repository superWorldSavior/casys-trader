"""Read-only memory dashboard: learnings.db, ledger, situation, sync status.

Pure aggregation — never creates files, never opens SQLite read-write
(LearningsStore / SituationMemoryStore would create a schema).
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

NOTE_EXCERPT_CHARS = 60
_VERDICT_RANK = {"WIN": 0, "LOSS": 1, "NEUTRAL": 2, "UNKNOWN": 3}

__all__ = [
    "NOTE_EXCERPT_CHARS",
    "CoverageStats",
    "GlobalRuleRow",
    "MemoryReport",
    "NoteRankRow",
    "NoteRanks",
    "RecallUtility",
    "SituationStats",
    "StoreStats",
    "SyncHealth",
    "compute_global_rules",
    "compute_memory_health",
    "compute_memory_report",
    "compute_note_ranks",
    "compute_recall_coverage",
    "compute_recall_utility",
    "compute_situation_stats",
    "compute_store_stats",
    "compute_sync_health",
]


@dataclass(frozen=True)
class StoreStats:
    available: bool
    missing_reason: str | None = None
    n_notes: int = 0
    by_source: tuple[tuple[str, int], ...] = ()
    by_verdict: tuple[tuple[str, int], ...] = ()
    n_with_embedding: int = 0
    pct_with_embedding: float | None = None
    n_recalls: int = 0
    n_active_rules: int = 0
    period_from: str | None = None
    period_to: str | None = None
    file_size_bytes: int = 0


@dataclass(frozen=True)
class CoverageStats:
    available: bool
    missing_reason: str | None = None
    n_authentic_llm: int = 0
    n_with_recall: int = 0
    pct: float | None = None


@dataclass(frozen=True)
class RecallUtility:
    available: bool
    missing_reason: str | None = None
    n_evaluated: int = 0
    n_plus: int = 0
    n_zero: int = 0
    n_minus: int = 0
    useful_rate: float | None = None
    base_rate: float | None = None
    lift: float | None = None


@dataclass(frozen=True)
class NoteRankRow:
    symbol: str
    family: str
    excerpt: str
    outcome_score: float | None
    q_value: float | None
    q_updates: int


@dataclass(frozen=True)
class NoteRanks:
    top_outcome: tuple[NoteRankRow, ...] = ()
    flop_outcome: tuple[NoteRankRow, ...] = ()
    top_q: tuple[NoteRankRow, ...] = ()
    flop_q: tuple[NoteRankRow, ...] = ()


@dataclass(frozen=True)
class GlobalRuleRow:
    rule_id: str
    q_value: float
    q_updates: int
    utility: str


@dataclass(frozen=True)
class SituationStats:
    available: bool
    missing_reason: str | None = None
    n_notes: int = 0
    n_evaluated: int = 0
    by_verdict: tuple[tuple[str, int], ...] = ()


@dataclass(frozen=True)
class SyncHealth:
    available: bool
    missing_reason: str | None = None
    status: str | None = None
    as_of: str | None = None
    age_seconds: float | None = None


@dataclass(frozen=True)
class MemoryReport:
    store: StoreStats
    coverage: CoverageStats
    utility: RecallUtility
    ranks: NoteRanks
    rules: tuple[GlobalRuleRow, ...]
    situation: SituationStats
    health: SyncHealth


def _missing(path: Path) -> str:
    return f"introuvable : {path}"


def _open_sqlite_ro(path: Path) -> sqlite3.Connection | None:
    if not path.is_file():
        return None
    try:
        conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        conn.execute("SELECT 1")
        return conn
    except sqlite3.Error:
        return None


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    ).fetchone()
    return row is not None


def _label_verdict(raw: object) -> str:
    text = "" if raw is None else str(raw).strip()
    return text or "(none)"


def _sort_counts(rows: list[tuple[str, int]]) -> tuple[tuple[str, int], ...]:
    return tuple(sorted(rows, key=lambda item: (-item[1], item[0])))


def _sort_verdicts(rows: list[tuple[str, int]]) -> tuple[tuple[str, int], ...]:
    def key(item: tuple[str, int]) -> tuple[int, int, str]:
        name = item[0]
        if name == "(none)":
            return (1, 99, name)
        return (0, _VERDICT_RANK.get(name, 50), name)

    return tuple(sorted(rows, key=key))


def _excerpt(text: object, *, limit: int = NOTE_EXCERPT_CHARS) -> str:
    cleaned = " ".join(str(text or "").split())
    return cleaned[:limit]


def _citation_utility(q_value: float, q_updates: int) -> str:
    from trader.agent.learnings.consolidator import citation_utility

    return citation_utility(q_value=q_value, q_updates=q_updates)


def compute_store_stats(db_path: Path) -> StoreStats:
    db_path = Path(db_path)
    if not db_path.is_file():
        return StoreStats(available=False, missing_reason=_missing(db_path))
    conn = _open_sqlite_ro(db_path)
    if conn is None:
        return StoreStats(available=False, missing_reason=f"illisible : {db_path}")
    try:
        if not _table_exists(conn, "notes"):
            return StoreStats(available=False, missing_reason=f"table notes absente : {db_path}")
        n_notes = int(conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0])
        by_source = _sort_counts(
            [
                (_label_verdict(row["source"]), int(row["n"]))
                for row in conn.execute(
                    "SELECT source, COUNT(*) AS n FROM notes GROUP BY source"
                )
            ]
        )
        by_verdict = _sort_verdicts(
            [
                (_label_verdict(row["verdict"]), int(row["n"]))
                for row in conn.execute(
                    "SELECT verdict, COUNT(*) AS n FROM notes GROUP BY verdict"
                )
            ]
        )
        n_with_embedding = int(
            conn.execute(
                "SELECT COUNT(*) FROM notes WHERE embedding IS NOT NULL"
            ).fetchone()[0]
        )
        pct = (100.0 * n_with_embedding / n_notes) if n_notes else None
        n_recalls = 0
        if _table_exists(conn, "recalls"):
            n_recalls = int(conn.execute("SELECT COUNT(*) FROM recalls").fetchone()[0])
        n_active_rules = 0
        if _table_exists(conn, "global_rules"):
            n_active_rules = int(
                conn.execute(
                    "SELECT COUNT(*) FROM global_rules WHERE active=1"
                ).fetchone()[0]
            )
        period = conn.execute(
            "SELECT MIN(ts), MAX(ts) FROM notes WHERE ts IS NOT NULL AND TRIM(ts) != ''"
        ).fetchone()
        return StoreStats(
            available=True,
            n_notes=n_notes,
            by_source=by_source,
            by_verdict=by_verdict,
            n_with_embedding=n_with_embedding,
            pct_with_embedding=pct,
            n_recalls=n_recalls,
            n_active_rules=n_active_rules,
            period_from=period[0],
            period_to=period[1],
            file_size_bytes=int(db_path.stat().st_size),
        )
    except sqlite3.Error as exc:
        return StoreStats(available=False, missing_reason=f"lecture impossible : {exc}")
    finally:
        conn.close()


def _recall_decision_ids(conn: sqlite3.Connection) -> set[str]:
    if not _table_exists(conn, "recalls"):
        return set()
    return {
        str(row[0])
        for row in conn.execute(
            "SELECT DISTINCT decision_id FROM recalls "
            "WHERE decision_id IS NOT NULL AND TRIM(decision_id) != ''"
        )
    }


def _authentic_llm_ids(journal: Path) -> tuple[set[str] | None, str | None]:
    import duckdb

    escaped = journal.as_posix().replace("'", "''")
    con = None
    try:
        con = duckdb.connect()
        con.execute(
            f"""
            CREATE VIEW d AS
            SELECT * FROM read_json_auto(
                '{escaped}',
                format='newline_delimited',
                union_by_name=true,
                maximum_object_size=33554432
            )
            """
        )
        columns = {str(row[0]) for row in con.execute("DESCRIBE d").fetchall()}
        if "decision_id" not in columns or "model_called" not in columns:
            return None, f"colonnes decision_id/model_called absentes : {journal}"
        where = [
            "coalesce(TRY_CAST(model_called AS BOOLEAN), false)",
            "decision_id IS NOT NULL",
            "TRIM(CAST(decision_id AS VARCHAR)) != ''",
        ]
        if "action" in columns and "decision_source" in columns:
            where.append(
                "NOT ("
                "CAST(action AS VARCHAR) ILIKE '%hold%' "
                "AND lower(coalesce(CAST(decision_source AS VARCHAR), '')) "
                "IN ('infra', 'infra_hold')"
                ")"
            )
        rows = con.execute(
            "SELECT DISTINCT CAST(decision_id AS VARCHAR) FROM d WHERE "
            + " AND ".join(where)
        ).fetchall()
        return {str(row[0]) for row in rows if row[0]}, None
    except Exception as exc:  # noqa: BLE001 - operator CLI must not traceback
        return None, f"lecture journal impossible : {exc}"
    finally:
        if con is not None:
            try:
                con.close()
            except Exception:
                pass


def compute_recall_coverage(db_path: Path, journal: Path) -> CoverageStats:
    db_path = Path(db_path)
    journal = Path(journal)
    if not journal.is_file():
        return CoverageStats(available=False, missing_reason=_missing(journal))
    authentic, error = _authentic_llm_ids(journal)
    if authentic is None:
        return CoverageStats(available=False, missing_reason=error)
    recall_ids: set[str] = set()
    if db_path.is_file():
        conn = _open_sqlite_ro(db_path)
        if conn is None:
            return CoverageStats(available=False, missing_reason=f"illisible : {db_path}")
        try:
            recall_ids = _recall_decision_ids(conn)
        finally:
            conn.close()
    n_authentic = len(authentic)
    n_with = len(authentic & recall_ids)
    return CoverageStats(
        available=True,
        n_authentic_llm=n_authentic,
        n_with_recall=n_with,
        pct=(100.0 * n_with / n_authentic) if n_authentic else None,
    )


def compute_recall_utility(db_path: Path) -> RecallUtility:
    db_path = Path(db_path)
    if not db_path.is_file():
        return RecallUtility(available=False, missing_reason=_missing(db_path))
    conn = _open_sqlite_ro(db_path)
    if conn is None:
        return RecallUtility(available=False, missing_reason=f"illisible : {db_path}")
    try:
        n_plus = n_zero = n_minus = 0
        if _table_exists(conn, "recalls"):
            for row in conn.execute(
                "SELECT reward FROM recalls WHERE reward IS NOT NULL"
            ):
                try:
                    reward = float(row[0])
                except (TypeError, ValueError):
                    continue
                if reward > 0:
                    n_plus += 1
                elif reward < 0:
                    n_minus += 1
                else:
                    n_zero += 1
        n_win = n_loss = 0
        if _table_exists(conn, "notes"):
            for row in conn.execute(
                "SELECT verdict, COUNT(*) AS n FROM notes "
                "WHERE verdict IN ('WIN', 'LOSS') GROUP BY verdict"
            ):
                if row["verdict"] == "WIN":
                    n_win = int(row["n"])
                elif row["verdict"] == "LOSS":
                    n_loss = int(row["n"])
        decided = n_plus + n_minus
        useful = (n_plus / decided) if decided else None
        base_den = n_win + n_loss
        base = (n_win / base_den) if base_den else None
        lift = (useful - base) if useful is not None and base is not None else None
        return RecallUtility(
            available=True,
            n_evaluated=n_plus + n_zero + n_minus,
            n_plus=n_plus,
            n_zero=n_zero,
            n_minus=n_minus,
            useful_rate=useful,
            base_rate=base,
            lift=lift,
        )
    except sqlite3.Error as exc:
        return RecallUtility(available=False, missing_reason=f"lecture impossible : {exc}")
    finally:
        conn.close()


def _rank_row(row: sqlite3.Row) -> NoteRankRow:
    return NoteRankRow(
        symbol=str(row["symbol"] or ""),
        family=str(row["family"] or ""),
        excerpt=_excerpt(row["note"]),
        outcome_score=None if row["outcome_score"] is None else float(row["outcome_score"]),
        q_value=None if row["q_value"] is None else float(row["q_value"]),
        q_updates=int(row["q_updates"] or 0),
    )


def compute_note_ranks(db_path: Path, *, limit: int = 10) -> NoteRanks:
    db_path = Path(db_path)
    empty = NoteRanks()
    if not db_path.is_file() or limit <= 0:
        return empty
    conn = _open_sqlite_ro(db_path)
    if conn is None or not _table_exists(conn, "notes"):
        if conn is not None:
            conn.close()
        return empty
    try:
        top_outcome = [
            _rank_row(row)
            for row in conn.execute(
                "SELECT symbol, family, note, outcome_score, q_value, q_updates "
                "FROM notes WHERE outcome_score IS NOT NULL "
                "ORDER BY outcome_score DESC, id ASC LIMIT ?",
                (limit,),
            )
        ]
        flop_outcome = [
            _rank_row(row)
            for row in conn.execute(
                "SELECT symbol, family, note, outcome_score, q_value, q_updates "
                "FROM notes WHERE outcome_score IS NOT NULL "
                "ORDER BY outcome_score ASC, id ASC LIMIT ?",
                (limit,),
            )
        ]
        top_q = [
            _rank_row(row)
            for row in conn.execute(
                "SELECT symbol, family, note, outcome_score, q_value, q_updates "
                "FROM notes WHERE q_value IS NOT NULL "
                "ORDER BY q_value DESC, q_updates DESC, id ASC LIMIT ?",
                (limit,),
            )
        ]
        flop_q = [
            _rank_row(row)
            for row in conn.execute(
                "SELECT symbol, family, note, outcome_score, q_value, q_updates "
                "FROM notes WHERE q_value IS NOT NULL "
                "ORDER BY q_value ASC, q_updates DESC, id ASC LIMIT ?",
                (limit,),
            )
        ]
        return NoteRanks(
            top_outcome=tuple(top_outcome),
            flop_outcome=tuple(flop_outcome),
            top_q=tuple(top_q),
            flop_q=tuple(flop_q),
        )
    except sqlite3.Error:
        return empty
    finally:
        conn.close()


def compute_global_rules(db_path: Path) -> tuple[GlobalRuleRow, ...]:
    db_path = Path(db_path)
    conn = _open_sqlite_ro(db_path) if db_path.is_file() else None
    if conn is None or not _table_exists(conn, "global_rules"):
        if conn is not None:
            conn.close()
        return ()
    try:
        rows = conn.execute(
            "SELECT rule_id, q_value, q_updates FROM global_rules "
            "WHERE active=1 ORDER BY rule_id"
        ).fetchall()
        return tuple(
            GlobalRuleRow(
                rule_id=str(row["rule_id"]),
                q_value=float(row["q_value"] or 0.0),
                q_updates=int(row["q_updates"] or 0),
                utility=_citation_utility(
                    float(row["q_value"] or 0.0),
                    int(row["q_updates"] or 0),
                ),
            )
            for row in rows
        )
    except sqlite3.Error:
        return ()
    finally:
        conn.close()


def _active_rule_dicts(db_path: Path) -> list[dict[str, object]]:
    """Raw active-rule scores — no consolidator import (runtime_state boundary)."""

    db_path = Path(db_path)
    conn = _open_sqlite_ro(db_path) if db_path.is_file() else None
    if conn is None or not _table_exists(conn, "global_rules"):
        if conn is not None:
            conn.close()
        return []
    try:
        return [
            {
                "rule_id": str(row["rule_id"]),
                "q_value": float(row["q_value"] or 0.0),
                "q_updates": int(row["q_updates"] or 0),
            }
            for row in conn.execute(
                "SELECT rule_id, q_value, q_updates FROM global_rules "
                "WHERE active=1 ORDER BY rule_id"
            )
        ]
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def compute_situation_stats(db_path: Path) -> SituationStats:
    db_path = Path(db_path)
    if not db_path.is_file():
        return SituationStats(available=False, missing_reason=_missing(db_path))
    conn = _open_sqlite_ro(db_path)
    if conn is None:
        return SituationStats(available=False, missing_reason=f"illisible : {db_path}")
    try:
        if not _table_exists(conn, "situation_notes"):
            return SituationStats(
                available=False,
                missing_reason=f"table situation_notes absente : {db_path}",
            )
        n_notes = int(conn.execute("SELECT COUNT(*) FROM situation_notes").fetchone()[0])
        columns = {
            str(row[1]) for row in conn.execute("PRAGMA table_info(situation_notes)")
        }
        if "verdict" not in columns:
            return SituationStats(available=True, n_notes=n_notes)
        n_evaluated = int(
            conn.execute(
                "SELECT COUNT(*) FROM situation_notes "
                "WHERE verdict IS NOT NULL AND TRIM(verdict) != ''"
            ).fetchone()[0]
        )
        by_verdict = _sort_verdicts(
            [
                (_label_verdict(row["verdict"]), int(row["n"]))
                for row in conn.execute(
                    "SELECT verdict, COUNT(*) AS n FROM situation_notes GROUP BY verdict"
                )
            ]
        )
        return SituationStats(
            available=True,
            n_notes=n_notes,
            n_evaluated=n_evaluated,
            by_verdict=by_verdict,
        )
    except sqlite3.Error as exc:
        return SituationStats(available=False, missing_reason=f"lecture impossible : {exc}")
    finally:
        conn.close()


def _parse_ts(raw: object) -> datetime | None:
    text = str(raw or "").strip()
    if not text:
        return None
    candidate = f"{text[:-1]}+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def compute_sync_health(status_path: Path, *, now: datetime | None = None) -> SyncHealth:
    status_path = Path(status_path)
    if not status_path.is_file():
        return SyncHealth(available=False, missing_reason=_missing(status_path))
    try:
        payload = json.loads(status_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return SyncHealth(available=False, missing_reason=f"illisible : {status_path}")
    if not isinstance(payload, dict):
        return SyncHealth(available=False, missing_reason=f"illisible : {status_path}")
    as_of = payload.get("as_of")
    parsed = _parse_ts(as_of)
    clock = now or datetime.now(timezone.utc)
    if clock.tzinfo is None:
        clock = clock.replace(tzinfo=timezone.utc)
    age = (clock - parsed).total_seconds() if parsed is not None else None
    status = payload.get("status")
    return SyncHealth(
        available=True,
        status=None if status is None else str(status),
        as_of=None if as_of is None else str(as_of),
        age_seconds=age,
    )


def compute_memory_report(
    state_dir: Path,
    *,
    now: datetime | None = None,
) -> MemoryReport:
    state_dir = Path(state_dir)
    db_path = state_dir / "learnings.db"
    return MemoryReport(
        store=compute_store_stats(db_path),
        coverage=compute_recall_coverage(db_path, state_dir / "decisions.jsonl"),
        utility=compute_recall_utility(db_path),
        ranks=compute_note_ranks(db_path),
        rules=compute_global_rules(db_path),
        situation=compute_situation_stats(state_dir / "situation_memory.db"),
        health=compute_sync_health(state_dir / "learnings_sync_status.json", now=now),
    )


def _situation_job_error(status_path: Path) -> str | None:
    if not Path(status_path).is_file():
        return None
    try:
        payload = json.loads(Path(status_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    outcomes = payload.get("outcomes")
    if not isinstance(outcomes, dict):
        return None
    situation = outcomes.get("situation_notes")
    if not isinstance(situation, dict):
        return None
    error = situation.get("error")
    if error in (None, ""):
        return None
    return str(error)


def compute_memory_health(
    state_dir: Path,
    *,
    now: datetime | None = None,
) -> dict[str, object]:
    """Compact cockpit snapshot. Never raises. Does not import consolidator."""

    try:
        state_dir = Path(state_dir)
        db_path = state_dir / "learnings.db"
        store = compute_store_stats(db_path)
        utility = (
            compute_recall_utility(db_path)
            if store.available
            else RecallUtility(available=False)
        )
        health = compute_sync_health(
            state_dir / "learnings_sync_status.json",
            now=now,
        )
        situation = compute_situation_stats(state_dir / "situation_memory.db")
        return {
            "store_available": store.available,
            "n_notes": store.n_notes if store.available else None,
            "n_evaluated_recalls": utility.n_evaluated if utility.available else None,
            "useful_rate": utility.useful_rate if utility.available else None,
            "base_rate": utility.base_rate if utility.available else None,
            "lift": utility.lift if utility.available else None,
            "active_rules": _active_rule_dicts(db_path) if store.available else [],
            "sync_available": health.available,
            "sync_status": health.status,
            "sync_as_of": health.as_of,
            "situation_available": situation.available,
            "situation_n": situation.n_notes if situation.available else None,
            "situation_n_evaluated": situation.n_evaluated if situation.available else None,
            "situation_error": _situation_job_error(state_dir / "learnings_sync_status.json"),
        }
    except Exception:
        return {
            "store_available": False,
            "n_notes": None,
            "n_evaluated_recalls": None,
            "useful_rate": None,
            "base_rate": None,
            "lift": None,
            "active_rules": [],
            "sync_available": False,
            "sync_status": None,
            "sync_as_of": None,
            "situation_available": False,
            "situation_n": None,
            "situation_n_evaluated": None,
            "situation_error": None,
        }
