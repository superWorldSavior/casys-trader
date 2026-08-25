"""Read-only SQLite adapter for the Desktop World Graph explorer.

Opens ``world_model.db`` with URI ``mode=ro``. A missing file stays missing.
This adapter never creates, migrates, or writes the ledger, and it never
instantiates ``WorldGraphStore`` (that constructor applies schema).
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any
from urllib.parse import quote

from trader.application.world_model.graph_explorer_ports import (
    WorldGraphExplorerEventRecord,
    WorldGraphExplorerQuerySnapshot,
)

_REQUIRED_TABLES = frozenset(
    {
        "world_entity_events",
        "world_entity_identity_events",
        "world_relation_events",
        "world_ontology_revisions",
        "world_availability_receipts",
    }
)
_EVENT_TABLES = (
    ("world_entity_events", "world_entity_event", False),
    ("world_entity_identity_events", "world_entity_identity_event", False),
    ("world_relation_events", "world_relation_event", True),
    ("world_ontology_revisions", "world_ontology_revision_event", False),
)


def _readonly_connection(path: Path) -> sqlite3.Connection:
    uri = f"file:{quote(str(path.resolve()), safe='/')}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _table_names(connection: sqlite3.Connection) -> set[str]:
    return {str(row[0]) for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _json_object(text: str, field_name: str) -> dict[str, Any]:
    payload = json.loads(text)
    if not isinstance(payload, dict):
        raise ValueError(f"{field_name} must be a JSON object")
    return payload


class SqliteWorldGraphExplorerQuery:
    """URI ``mode=ro`` loader of current graph event rows. No schema application."""

    def __init__(self, db_path: str | Path) -> None:
        self.path = Path(db_path)

    def load_current_records(self) -> WorldGraphExplorerQuerySnapshot:
        path = self.path
        if not path.exists():
            return WorldGraphExplorerQuerySnapshot(
                status="not_started",
                exists=False,
                missing_tables=(),
                records=(),
            )
        try:
            with _readonly_connection(path) as connection:
                tables = _table_names(connection)
                missing = tuple(sorted(_REQUIRED_TABLES.difference(tables)))
                if missing:
                    return WorldGraphExplorerQuerySnapshot(
                        status="unavailable",
                        exists=True,
                        missing_tables=missing,
                        records=(),
                    )
                receipts = _load_receipts(connection)
                records = _load_event_records(connection, receipts)
        except (OSError, sqlite3.Error, json.JSONDecodeError, TypeError, ValueError) as exc:
            return WorldGraphExplorerQuerySnapshot(
                status="unavailable",
                exists=True,
                missing_tables=(),
                records=(),
                error=f"{type(exc).__name__}:{exc}",
            )
        return WorldGraphExplorerQuerySnapshot(
            status="ready",
            exists=True,
            missing_tables=(),
            records=records,
        )


def _load_receipts(connection: sqlite3.Connection) -> dict[tuple[str, str, str], dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT subject_kind, subject_id, content_sha256, payload_json
        FROM world_availability_receipts
        """
    )
    receipts: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in rows:
        receipts[(str(row["subject_kind"]), str(row["subject_id"]), str(row["content_sha256"]))] = _json_object(
            row["payload_json"],
            "receipt payload_json",
        )
    return receipts


def _load_event_records(
    connection: sqlite3.Connection,
    receipts: dict[tuple[str, str, str], dict[str, Any]],
) -> tuple[WorldGraphExplorerEventRecord, ...]:
    records: list[WorldGraphExplorerEventRecord] = []
    for table, subject_kind, has_family in _EVENT_TABLES:
        columns = "event_id, payload_json, payload_sha256"
        if has_family:
            columns = "event_id, family, payload_json, payload_sha256"
        sql = f"SELECT {columns} FROM {table} ORDER BY sequence ASC, event_id ASC"  # noqa: S608
        for row in connection.execute(sql):
            payload = _json_object(row["payload_json"], "payload_json")
            digest = str(row["payload_sha256"])
            family = str(row["family"]) if has_family else None
            records.append(
                WorldGraphExplorerEventRecord(
                    subject_kind=subject_kind,
                    table=table,
                    family=family,
                    payload=payload,
                    payload_sha256=digest,
                    receipt_payload=receipts.get((subject_kind, str(row["event_id"]), digest)),
                )
            )
    return tuple(records)


__all__ = ["SqliteWorldGraphExplorerQuery"]
