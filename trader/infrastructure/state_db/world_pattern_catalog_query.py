"""Read-only reconstruction of pattern hypotheses and occurrences.

Opens the ledger with URI ``mode=ro`` and ``PRAGMA query_only=ON``. A missing
file stays missing. This adapter never constructs ``WorldPatternStore``, never
migrates schema, and never invents occurrences for evaluating hypotheses.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from pathlib import Path
from urllib.parse import quote

from trader.domain.world_pattern import PatternHypothesis, PatternOccurrence
from trader.infrastructure.state_db.world_pattern_store import (
    rehydrate_pattern_hypothesis_event,
    rehydrate_pattern_occurrence_event,
)

_READONLY_TIMEOUT_S = 5.0
_HYPOTHESIS_TABLE = "world_pattern_hypothesis_events"
_OCCURRENCE_TABLE = "world_pattern_occurrence_events"
_REQUIRED_TABLES = frozenset({_HYPOTHESIS_TABLE, _OCCURRENCE_TABLE})


def _readonly_connection(path: Path) -> sqlite3.Connection:
    uri = f"file:{quote(str(path.resolve()), safe='/')}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=_READONLY_TIMEOUT_S)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def _table_names(connection: sqlite3.Connection) -> set[str]:
    return {str(row[0]) for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _placeholders(items: Sequence[str]) -> str:
    return ",".join("?" for _ in items)


class SqlitePatternCatalogQuery:
    """Hypothesis-first catalog. Evaluating hypotheses stay visible with zero occurrences."""

    def __init__(self, db_path: str | Path) -> None:
        self.path = Path(db_path)

    def list_evaluating_hypotheses(
        self,
        *,
        evaluation_cohort_id: str,
        evaluation_dataset_fingerprint: str,
        hypothesis_ids: Sequence[str] | None = None,
    ) -> tuple[PatternHypothesis, ...]:
        requested = None if hypothesis_ids is None else frozenset(hypothesis_ids)
        evaluating: list[PatternHypothesis] = []
        for hypothesis in self.list_hypotheses():
            if hypothesis.status != "evaluating":
                continue
            if hypothesis.evaluation_cohort_id != evaluation_cohort_id:
                continue
            if hypothesis.evaluation_dataset_fingerprint != evaluation_dataset_fingerprint:
                continue
            if requested is not None and hypothesis.hypothesis_id not in requested:
                continue
            evaluating.append(hypothesis)
        return tuple(evaluating)

    def list_hypotheses(self) -> tuple[PatternHypothesis, ...]:
        return tuple(self._load_hypothesis(item) for item in self.list_hypothesis_ids())

    def list_hypothesis_ids(self) -> tuple[str, ...]:
        rows = self._query(
            f"SELECT DISTINCT hypothesis_id FROM {_HYPOTHESIS_TABLE} ORDER BY hypothesis_id ASC"  # noqa: S608
        )
        return tuple(str(row["hypothesis_id"]) for row in rows)

    def list_recorded_occurrences(
        self,
        *,
        evaluation_cohort_id: str | None = None,
        hypothesis_ids: Sequence[str] | None = None,
        occurrence_ids: Sequence[str] | None = None,
    ) -> tuple[PatternOccurrence, ...]:
        return tuple(
            self._load_occurrence(item)
            for item in self.list_occurrence_ids(
                evaluation_cohort_id=evaluation_cohort_id,
                hypothesis_ids=hypothesis_ids,
                occurrence_ids=occurrence_ids,
            )
        )

    def list_occurrence_ids(
        self,
        *,
        evaluation_cohort_id: str | None = None,
        hypothesis_ids: Sequence[str] | None = None,
        occurrence_ids: Sequence[str] | None = None,
    ) -> tuple[str, ...]:
        clauses = ["event_type = 'pattern_occurrence_recorded'"]
        params: list[str] = []
        if evaluation_cohort_id is not None:
            clauses.append("cohort_id = ?")
            params.append(evaluation_cohort_id)
        if hypothesis_ids is not None:
            if not hypothesis_ids:
                return ()
            clauses.append(f"hypothesis_id IN ({_placeholders(hypothesis_ids)})")
            params.extend(hypothesis_ids)
        if occurrence_ids is not None:
            if not occurrence_ids:
                return ()
            clauses.append(f"occurrence_id IN ({_placeholders(occurrence_ids)})")
            params.extend(occurrence_ids)
        sql = (
            f"SELECT DISTINCT occurrence_id FROM {_OCCURRENCE_TABLE} "  # noqa: S608
            f"WHERE {' AND '.join(clauses)} ORDER BY occurrence_id ASC"
        )
        rows = self._query(sql, tuple(params))
        return tuple(str(row["occurrence_id"]) for row in rows)

    def _load_hypothesis(self, hypothesis_id: str) -> PatternHypothesis:
        rows = self._query(
            f"SELECT * FROM {_HYPOTHESIS_TABLE} WHERE hypothesis_id=? ORDER BY sequence ASC, event_id ASC",  # noqa: S608
            (hypothesis_id,),
        )
        if not rows:
            raise LookupError(hypothesis_id)
        return PatternHypothesis.from_events(tuple(rehydrate_pattern_hypothesis_event(row) for row in rows))

    def _load_occurrence(self, occurrence_id: str) -> PatternOccurrence:
        rows = self._query(
            f"SELECT * FROM {_OCCURRENCE_TABLE} WHERE occurrence_id=? ORDER BY sequence ASC, event_id ASC",  # noqa: S608
            (occurrence_id,),
        )
        if not rows:
            raise LookupError(occurrence_id)
        return PatternOccurrence.from_events(tuple(rehydrate_pattern_occurrence_event(row) for row in rows))

    def _query(self, sql: str, params: tuple[object, ...] = ()) -> tuple[sqlite3.Row, ...]:
        path = self.path
        if not path.exists():
            return ()
        try:
            with _readonly_connection(path) as connection:
                tables = _table_names(connection)
                if not _REQUIRED_TABLES.issubset(tables):
                    return ()
                return tuple(connection.execute(sql, params).fetchall())
        except (OSError, sqlite3.Error):
            return ()


__all__ = ["SqlitePatternCatalogQuery"]
