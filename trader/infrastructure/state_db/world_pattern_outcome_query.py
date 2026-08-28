"""Read-only active observed WorldOutcome leaves for the later linking phase.

Opens the ledger with URI ``mode=ro`` and ``PRAGMA query_only=ON``. Matching
never uses this adapter. Missing leaves stay missing; nothing is fabricated.
An operational SQLite failure is a typed infrastructure error, not an empty
success.
"""

from __future__ import annotations

import sqlite3
from collections import Counter, defaultdict
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from trader.application.world_model.pattern_outcome_link import PatternQueryUnavailable
from trader.domain.world_episode import WorldOutcome, parse_utc_timestamp
from trader.infrastructure.state_db.sqlite_in import sqlite_in_chunks, sqlite_placeholders
from trader.infrastructure.state_db.world_pattern_formation_query import _active_outcomes_as_of

_READONLY_TIMEOUT_S = 5.0
_REQUIRED_TABLES = frozenset({"world_outcome_events"})


def _readonly_connection(path: Path) -> sqlite3.Connection:
    uri = f"file:{quote(str(path.resolve()), safe='/')}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=_READONLY_TIMEOUT_S)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def _table_names(connection: sqlite3.Connection) -> set[str]:
    return {str(row[0]) for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _unavailable(exc: BaseException, *, operation: str) -> PatternQueryUnavailable:
    return PatternQueryUnavailable(f"{operation} failed: {type(exc).__name__}:{exc}")


class SqlitePatternOutcomeLeafQuery:
    """Active observed leaves only. Never invents a 4h / 1d / 3d result."""

    def __init__(self, db_path: str | Path) -> None:
        self.path = Path(db_path)

    def load_active_observed_leaves(
        self,
        *,
        episode_ids: Sequence[str],
        horizon_ids: Sequence[str],
        as_of: datetime | str,
    ) -> tuple[WorldOutcome, ...]:
        cutoff = parse_utc_timestamp(as_of, "as_of")
        horizons = tuple(dict.fromkeys(str(item) for item in horizon_ids if str(item).strip()))
        episode_chunks = sqlite_in_chunks(episode_ids, extra_binds=len(horizons) + 1)
        if not episode_chunks or not horizons:
            return ()
        path = self.path
        if not path.exists():
            return ()
        grouped: dict[str, list[sqlite3.Row]] = defaultdict(list)
        try:
            with _readonly_connection(path) as connection:
                tables = _table_names(connection)
                if not _REQUIRED_TABLES.issubset(tables):
                    return ()
                extra = (*horizons, cutoff.isoformat())
                for chunk in episode_chunks:
                    sql = f"""
                    SELECT *
                    FROM world_outcome_events
                    WHERE episode_id IN ({sqlite_placeholders(len(chunk))})
                      AND horizon_code IN ({sqlite_placeholders(len(horizons))})
                      AND recorded_at <= ?
                    ORDER BY episode_id ASC, horizon_code ASC, recorded_at ASC, outcome_event_id ASC
                    """
                    for row in connection.execute(sql, (*chunk, *extra)):
                        grouped[str(row["episode_id"])].append(row)
        except OSError as exc:
            raise _unavailable(exc, operation="load_active_observed_leaves") from exc
        except sqlite3.Error as exc:
            raise _unavailable(exc, operation="load_active_observed_leaves") from exc
        rejections: Counter[str] = Counter()
        leaves: list[WorldOutcome] = []
        for episode_id in sorted(grouped):
            selected = _active_outcomes_as_of(
                grouped.get(episode_id, ()),
                episode_id,
                horizons,
                cutoff,
                rejections,
            )
            leaves.extend(outcome for outcome, _row in selected)
        leaves.sort(key=lambda item: (item.episode_id, item.horizon.horizon_id, item.event_id or ""))
        return tuple(leaves)


__all__ = ["SqlitePatternOutcomeLeafQuery"]
