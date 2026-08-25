"""Consumer-owned read query port for the Desktop World Graph explorer.

Adapters load persisted graph rows. Application code owns parsers, folds,
attestation, and point-in-time eligibility. This port has no write methods
and does not accept an arbitrary historical cutoff.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

WORLD_GRAPH_EXPLORER_SCHEMA = "world_graph_explorer.v1"
WORLD_GRAPH_EXPLORER_VIEW = "current_published_overview"
WORLD_GRAPH_EXPLORER_QUERY_STATUSES = frozenset({"ready", "not_started", "unavailable"})


@dataclass(frozen=True)
class WorldGraphExplorerEventRecord:
    """One persisted graph event plus its receipt payload, if any."""

    subject_kind: str
    table: str
    family: str | None
    payload: Mapping[str, object]
    payload_sha256: str
    receipt_payload: Mapping[str, object] | None


@dataclass(frozen=True)
class WorldGraphExplorerQuerySnapshot:
    """Typed load result. Missing files stay missing; schema gaps stay unavailable."""

    status: str
    exists: bool
    missing_tables: tuple[str, ...]
    records: tuple[WorldGraphExplorerEventRecord, ...]
    error: str | None = None

    def __post_init__(self) -> None:
        status = str(self.status or "").strip()
        if status not in WORLD_GRAPH_EXPLORER_QUERY_STATUSES:
            allowed = ", ".join(sorted(WORLD_GRAPH_EXPLORER_QUERY_STATUSES))
            raise ValueError(f"query status must be one of: {allowed}")
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "exists", bool(self.exists))
        object.__setattr__(self, "missing_tables", tuple(self.missing_tables))
        object.__setattr__(self, "records", tuple(self.records))
        error = None if self.error is None else str(self.error)
        object.__setattr__(self, "error", error)


class WorldGraphExplorerQuery(Protocol):
    """Read-only current-graph query. Implementations must not write or migrate."""

    def load_current_records(self) -> WorldGraphExplorerQuerySnapshot: ...


__all__ = [
    "WORLD_GRAPH_EXPLORER_SCHEMA",
    "WORLD_GRAPH_EXPLORER_VIEW",
    "WorldGraphExplorerEventRecord",
    "WorldGraphExplorerQuery",
    "WorldGraphExplorerQuerySnapshot",
]
