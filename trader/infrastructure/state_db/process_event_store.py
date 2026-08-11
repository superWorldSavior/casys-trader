"""Store SQLite append-only des événements de processus L2."""

from __future__ import annotations

import json
from typing import Any

from trader.domain.process_trace import ProcessEvent
from trader.infrastructure.state_db.connection import StateDb

_EVENT_COLUMNS = (
    "event_id",
    "process_type",
    "process_version",
    "process_instance_id",
    "attempt_id",
    "runtime_run_id",
    "work_object_type",
    "work_object_key",
    "event_type",
    "ts",
    "caused_by_json",
    "terminal_result",
    "outcome_code",
    "effect_status",
    "effect_refs_json",
    "version_pins_json",
)


class ProcessEventConflictError(ValueError):
    """Un ``event_id`` existant a été réutilisé avec un contenu différent."""


class ProcessEventStore:
    """Persiste et relit les événements sans remplacer l'historique.

    La migration v4 doit être appliquée avant construction. ``append`` retourne
    ``True`` pour une insertion, ``False`` pour le replay strictement identique.
    """

    def __init__(self, db: StateDb) -> None:
        self._db = db

    def append(self, event: ProcessEvent) -> bool:
        """Ajoute ``event`` de manière idempotente et fail-closed sur conflit."""

        columns = _event_columns(event)
        placeholders = ", ".join("?" for _ in _EVENT_COLUMNS)
        column_names = ", ".join(_EVENT_COLUMNS)
        with self._db.transaction() as cur:
            cur.execute(
                f"INSERT INTO process_events({column_names})"  # noqa: S608 — colonnes constantes
                f" VALUES ({placeholders}) ON CONFLICT(event_id) DO NOTHING",
                columns,
            )
            if cur.rowcount == 1:
                return True

            row = cur.execute(
                f"SELECT {column_names} FROM process_events WHERE event_id=?",  # noqa: S608
                (event.event_id,),
            ).fetchone()
            if row is None or _canonical_row_columns(row) != columns:
                raise ProcessEventConflictError(f"event_id {event.event_id!r} already exists with different content")
            return False

    def get(self, event_id: str) -> ProcessEvent | None:
        row = self._db.query_one(
            "SELECT * FROM process_events WHERE event_id=?",
            (event_id,),
        )
        return None if row is None else _row_to_event(row)

    def read_instance(self, process_instance_id: str) -> list[ProcessEvent]:
        """Retourne l'histoire d'une instance dans l'ordre d'insertion."""

        rows = self._db.query_all(
            "SELECT * FROM process_events WHERE process_instance_id=? ORDER BY seq",
            (process_instance_id,),
        )
        return [_row_to_event(row) for row in rows]

    def latest_for_work(
        self,
        *,
        process_type: str,
        work_object_type: str,
        work_object_key: str,
    ) -> ProcessEvent | None:
        """Retourne le dernier événement connu pour un objet de travail exact."""

        row = self._db.query_one(
            """SELECT * FROM process_events
               WHERE process_type=? AND work_object_type=? AND work_object_key=?
               ORDER BY seq DESC LIMIT 1""",
            (process_type, work_object_type, work_object_key),
        )
        return None if row is None else _row_to_event(row)


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _event_columns(event: ProcessEvent) -> tuple[Any, ...]:
    return (
        event.event_id,
        event.process_type,
        event.process_version,
        event.process_instance_id,
        event.attempt_id,
        event.runtime_run_id,
        event.work_object_type,
        event.work_object_key,
        event.event_type,
        event.ts,
        _canonical_json(event.caused_by),
        event.terminal_result,
        event.outcome_code,
        event.effect_status,
        _canonical_json(event.effect_refs),
        None if event.version_pins is None else _canonical_json(event.version_pins),
    )


def _canonical_row_columns(row) -> tuple[Any, ...]:
    values = tuple(row[name] for name in _EVENT_COLUMNS)
    mutable = list(values)
    mutable[10] = _canonical_json(json.loads(mutable[10]))
    mutable[14] = _canonical_json(json.loads(mutable[14]))
    if mutable[15] is not None:
        mutable[15] = _canonical_json(json.loads(mutable[15]))
    return tuple(mutable)


def _row_to_event(row) -> ProcessEvent:
    return ProcessEvent(
        event_id=row["event_id"],
        process_type=row["process_type"],
        process_version=row["process_version"],
        process_instance_id=row["process_instance_id"],
        attempt_id=row["attempt_id"],
        runtime_run_id=row["runtime_run_id"],
        work_object_type=row["work_object_type"],
        work_object_key=row["work_object_key"],
        event_type=row["event_type"],
        ts=row["ts"],
        caused_by=tuple(json.loads(row["caused_by_json"])),
        terminal_result=row["terminal_result"],
        outcome_code=row["outcome_code"],
        effect_status=row["effect_status"],
        effect_refs=tuple(json.loads(row["effect_refs_json"])),
        version_pins=(None if row["version_pins_json"] is None else json.loads(row["version_pins_json"])),
    )


__all__ = ["ProcessEventConflictError", "ProcessEventStore"]
