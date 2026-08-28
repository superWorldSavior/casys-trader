"""Read-only reconstruction of pattern hypotheses and occurrences.

Opens the ledger with URI ``mode=ro`` and ``PRAGMA query_only=ON``. A missing
file stays missing. This adapter never constructs ``WorldPatternStore``, never
migrates schema, and never invents occurrences for evaluating hypotheses.

Selected ``hypothesis_ids`` are filtered in SQL before aggregate reconstruction
and share one read-only connection. Omitting IDs is the inspection path: the
full hypothesis event stream is reconstructed in that same connection.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TypeVar
from urllib.parse import quote

from trader.domain.world_pattern import PatternHypothesis, PatternOccurrence
from trader.infrastructure.state_db.sqlite_in import sqlite_in_chunk_size, sqlite_in_chunks, sqlite_placeholders
from trader.infrastructure.state_db.world_pattern_store import (
    reconstruct_pattern_hypotheses,
    reconstruct_pattern_occurrences,
)

_READONLY_TIMEOUT_S = 5.0
_HYPOTHESIS_TABLE = "world_pattern_hypothesis_events"
_OCCURRENCE_TABLE = "world_pattern_occurrence_events"
_REQUIRED_TABLES = frozenset({_HYPOTHESIS_TABLE, _OCCURRENCE_TABLE})
T = TypeVar("T")


def _readonly_connection(path: Path) -> sqlite3.Connection:
    uri = f"file:{quote(str(path.resolve()), safe='/')}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=_READONLY_TIMEOUT_S)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def _table_names(connection: sqlite3.Connection) -> set[str]:
    return {str(row[0]) for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}


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
        requested = None if hypothesis_ids is None else tuple(hypothesis_ids)
        if requested is not None and not sqlite_in_chunks(requested):
            return ()
        return self._read(
            lambda connection: _evaluating_hypotheses_on(
                connection,
                evaluation_cohort_id=evaluation_cohort_id,
                evaluation_dataset_fingerprint=evaluation_dataset_fingerprint,
                hypothesis_ids=requested,
            ),
            default=(),
        )

    def list_hypotheses(self) -> tuple[PatternHypothesis, ...]:
        """Inspection path: reconstruct every hypothesis from the full event stream."""

        return self._read(
            lambda connection: reconstruct_pattern_hypotheses(_hypothesis_event_rows(connection, None)),
            default=(),
        )

    def list_hypothesis_ids(self) -> tuple[str, ...]:
        rows = self._read(
            lambda connection: tuple(
                connection.execute(
                    f"SELECT DISTINCT hypothesis_id FROM {_HYPOTHESIS_TABLE} ORDER BY hypothesis_id ASC"  # noqa: S608
                ).fetchall()
            ),
            default=(),
        )
        return tuple(str(row["hypothesis_id"]) for row in rows)

    def list_recorded_occurrences(
        self,
        *,
        evaluation_cohort_id: str | None = None,
        hypothesis_ids: Sequence[str] | None = None,
        occurrence_ids: Sequence[str] | None = None,
    ) -> tuple[PatternOccurrence, ...]:
        return self._read(
            lambda connection: _recorded_occurrences_on(
                connection,
                evaluation_cohort_id=evaluation_cohort_id,
                hypothesis_ids=hypothesis_ids,
                occurrence_ids=occurrence_ids,
            ),
            default=(),
        )

    def list_occurrence_ids(
        self,
        *,
        evaluation_cohort_id: str | None = None,
        hypothesis_ids: Sequence[str] | None = None,
        occurrence_ids: Sequence[str] | None = None,
    ) -> tuple[str, ...]:
        return self._read(
            lambda connection: _occurrence_ids_on(
                connection,
                evaluation_cohort_id=evaluation_cohort_id,
                hypothesis_ids=hypothesis_ids,
                occurrence_ids=occurrence_ids,
            ),
            default=(),
        )

    def _read(self, callback: Callable[[sqlite3.Connection], T], *, default: T) -> T:
        path = self.path
        if not path.exists():
            return default
        try:
            with _readonly_connection(path) as connection:
                tables = _table_names(connection)
                if not _REQUIRED_TABLES.issubset(tables):
                    return default
                return callback(connection)
        except (OSError, sqlite3.Error):
            return default


def _evaluating_hypotheses_on(
    connection: sqlite3.Connection,
    *,
    evaluation_cohort_id: str,
    evaluation_dataset_fingerprint: str,
    hypothesis_ids: Sequence[str] | None,
) -> tuple[PatternHypothesis, ...]:
    hypotheses = reconstruct_pattern_hypotheses(_hypothesis_event_rows(connection, hypothesis_ids))
    return tuple(
        hypothesis
        for hypothesis in hypotheses
        if hypothesis.status == "evaluating"
        and hypothesis.evaluation_cohort_id == evaluation_cohort_id
        and hypothesis.evaluation_dataset_fingerprint == evaluation_dataset_fingerprint
    )


def _hypothesis_event_rows(
    connection: sqlite3.Connection,
    hypothesis_ids: Sequence[str] | None,
) -> list[sqlite3.Row]:
    if hypothesis_ids is None:
        return list(
            connection.execute(
                f"SELECT * FROM {_HYPOTHESIS_TABLE} ORDER BY hypothesis_id ASC, sequence ASC, event_id ASC"  # noqa: S608
            ).fetchall()
        )
    rows: list[sqlite3.Row] = []
    for chunk in sqlite_in_chunks(hypothesis_ids):
        sql = (
            f"SELECT * FROM {_HYPOTHESIS_TABLE} "  # noqa: S608
            f"WHERE hypothesis_id IN ({sqlite_placeholders(len(chunk))}) "
            "ORDER BY hypothesis_id ASC, sequence ASC, event_id ASC"
        )
        rows.extend(connection.execute(sql, chunk).fetchall())
    return rows


def _recorded_occurrences_on(
    connection: sqlite3.Connection,
    *,
    evaluation_cohort_id: str | None,
    hypothesis_ids: Sequence[str] | None,
    occurrence_ids: Sequence[str] | None,
) -> tuple[PatternOccurrence, ...]:
    ids = _occurrence_ids_on(
        connection,
        evaluation_cohort_id=evaluation_cohort_id,
        hypothesis_ids=hypothesis_ids,
        occurrence_ids=occurrence_ids,
    )
    if not ids:
        return ()
    rows: list[sqlite3.Row] = []
    for chunk in sqlite_in_chunks(ids):
        sql = (
            f"SELECT * FROM {_OCCURRENCE_TABLE} "  # noqa: S608
            f"WHERE occurrence_id IN ({sqlite_placeholders(len(chunk))}) "
            "ORDER BY occurrence_id ASC, sequence ASC, event_id ASC"
        )
        rows.extend(connection.execute(sql, chunk).fetchall())
    return reconstruct_pattern_occurrences(rows)


def _occurrence_ids_on(
    connection: sqlite3.Connection,
    *,
    evaluation_cohort_id: str | None,
    hypothesis_ids: Sequence[str] | None,
    occurrence_ids: Sequence[str] | None,
) -> tuple[str, ...]:
    h_chunks, o_chunks, extra_binds = _occurrence_filter_chunks(
        hypothesis_ids=hypothesis_ids,
        occurrence_ids=occurrence_ids,
        extra_binds=1 if evaluation_cohort_id is not None else 0,
    )
    if h_chunks == () or o_chunks == ():
        return ()
    found: list[str] = []
    seen: set[str] = set()
    for h_chunk in h_chunks:
        for o_chunk in o_chunks:
            clauses = ["event_type = 'pattern_occurrence_recorded'"]
            params: list[object] = []
            if evaluation_cohort_id is not None:
                clauses.append("cohort_id = ?")
                params.append(evaluation_cohort_id)
            if h_chunk is not None:
                clauses.append(f"hypothesis_id IN ({sqlite_placeholders(len(h_chunk))})")
                params.extend(h_chunk)
            if o_chunk is not None:
                clauses.append(f"occurrence_id IN ({sqlite_placeholders(len(o_chunk))})")
                params.extend(o_chunk)
            sql = (
                f"SELECT DISTINCT occurrence_id FROM {_OCCURRENCE_TABLE} "  # noqa: S608
                f"WHERE {' AND '.join(clauses)} ORDER BY occurrence_id ASC"
            )
            for row in connection.execute(sql, tuple(params)):
                occurrence_id = str(row["occurrence_id"])
                if occurrence_id not in seen:
                    seen.add(occurrence_id)
                    found.append(occurrence_id)
    return tuple(sorted(found))


def _occurrence_filter_chunks(
    *,
    hypothesis_ids: Sequence[str] | None,
    occurrence_ids: Sequence[str] | None,
    extra_binds: int,
) -> tuple[tuple[tuple[str, ...] | None, ...], tuple[tuple[str, ...] | None, ...], int]:
    unbounded = sum(1 for item in (hypothesis_ids, occurrence_ids) if item is not None)
    size = sqlite_in_chunk_size(unbounded_in_count=max(1, unbounded), extra_binds=extra_binds)
    if hypothesis_ids is None:
        h_chunks: tuple[tuple[str, ...] | None, ...] = (None,)
    else:
        chunks = sqlite_in_chunks(hypothesis_ids, size=size)
        if not chunks:
            return (), (None,), extra_binds
        h_chunks = chunks
    if occurrence_ids is None:
        o_chunks: tuple[tuple[str, ...] | None, ...] = (None,)
    else:
        chunks = sqlite_in_chunks(occurrence_ids, size=size)
        if not chunks:
            return h_chunks, (), extra_binds
        o_chunks = chunks
    return h_chunks, o_chunks, extra_binds


__all__ = ["SqlitePatternCatalogQuery"]
