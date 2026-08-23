"""Append-only SQLite ledger for pattern hypothesis and occurrence events.

Canonical records live in dedicated event tables. Availability receipts are reused
from ``world_availability_receipts`` and stamped only after the subject row is
durable. ``first_seen_at`` is process-local and restart-conservative.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from trader.application.world_model.pattern_ports import (
    OccurrenceEventEnvelope,
    PatternHypothesisEventEnvelope,
    PatternPayloadConflict,
)
from trader.domain.world_availability import (
    AvailabilityEvidence,
    WorldAvailabilityReceipt,
    WorldAvailabilitySubjectRef,
    WorldStorageLocator,
    _attest_verified_store_receipt,
)
from trader.domain.world_episode import canonical_json, canonical_sha256
from trader.domain.world_pattern import (
    PatternEvaluationClosed,
    PatternEvaluationStarted,
    PatternHypothesis,
    PatternHypothesisEvent,
    PatternHypothesisId,
    PatternHypothesisInvalidated,
    PatternHypothesisRegistered,
    PatternOccurrence,
    PatternOccurrenceEvent,
    PatternOccurrenceId,
    PatternOccurrenceInvalidated,
    PatternOccurrenceRecorded,
    PatternOutcomeLink,
    PatternOutcomeLinked,
    PatternOutcomeLinkSuperseded,
    parse_pattern_hypothesis_event,
    parse_pattern_occurrence_event,
)
from trader.infrastructure.state_db.availability_receipt import (
    UtcClock,
    _seal_world_availability_receipt,
    default_utc_clock,
)
from trader.infrastructure.state_db.connection import StateDb
from trader.infrastructure.state_db.world_model_store import (
    WORLD_MODEL_MIGRATIONS,
    WORLD_MODEL_STORE_ID,
    WorldModelConflictError,
    _aware_utc,
    _open_dedicated_state_db,
)


WORLD_PATTERN_TABLES = (
    "world_pattern_hypothesis_events",
    "world_pattern_occurrence_events",
    "world_pattern_outcome_links",
)

_HYPOTHESIS_TABLE = "world_pattern_hypothesis_events"
_OCCURRENCE_TABLE = "world_pattern_occurrence_events"
_LINK_TABLE = "world_pattern_outcome_links"
_HYPOTHESIS_SUBJECT_KIND = "pattern_hypothesis_event"
_OCCURRENCE_SUBJECT_KIND = "pattern_occurrence_event"
_PATTERN_SUBJECT_KINDS = frozenset({_HYPOTHESIS_SUBJECT_KIND, _OCCURRENCE_SUBJECT_KIND})
_HYPOTHESIS_EVENTS = (
    PatternHypothesisRegistered,
    PatternEvaluationStarted,
    PatternEvaluationClosed,
    PatternHypothesisInvalidated,
)
_OCCURRENCE_EVENTS = (
    PatternOccurrenceRecorded,
    PatternOutcomeLinked,
    PatternOutcomeLinkSuperseded,
    PatternOccurrenceInvalidated,
)

__all__ = [
    "WORLD_PATTERN_TABLES",
    "WorldPatternStore",
]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_load(text: str) -> Any:
    return json.loads(text)


def _payload_conflict(event_id: str) -> PatternPayloadConflict:
    return PatternPayloadConflict(
        f"event_id {event_id!r} already exists with different canonical content"
    )


class WorldPatternStore:
    """SQLite adapter for hypothesis/occurrence ledgers and pattern availability."""

    def __init__(self, db_or_path: StateDb | str | Path, *, clock: UtcClock | None = None) -> None:
        if isinstance(db_or_path, StateDb):
            self._db = db_or_path
            self._owns_db = False
        else:
            if Path(db_or_path).name.casefold() == "casys.db":
                raise ValueError("WorldPatternStore requires a dedicated world_model.db, never casys.db")
            self._db = _open_dedicated_state_db(db_or_path)
            self._owns_db = True
        self.path = self._db.path
        if self.path.name.casefold() == "casys.db":
            raise ValueError("WorldPatternStore requires a dedicated world_model.db, never casys.db")
        self._clock = clock or default_utc_clock
        self._first_seen_at: dict[str, datetime] = {}
        self._db.query_one("PRAGMA foreign_keys=ON")
        self._db.apply_migrations(WORLD_MODEL_MIGRATIONS)

    def close(self) -> None:
        if self._owns_db:
            self._db.close()

    def append_event(
        self,
        event: PatternHypothesisEvent | PatternOccurrenceEvent,
    ) -> PatternHypothesisEventEnvelope | OccurrenceEventEnvelope:
        parsed = self._require_event(event)
        if isinstance(parsed, _HYPOTHESIS_EVENTS):
            self._persist_event(
                parsed,
                table=_HYPOTHESIS_TABLE,
                aggregate_id=parsed.hypothesis_id,
                aggregate_column="hypothesis_id",
            )
            receipt = self._commit_availability_receipt(
                subject_kind=_HYPOTHESIS_SUBJECT_KIND,
                subject_id=parsed.event_id,
                payload=parsed.to_dict(),
                table=_HYPOTHESIS_TABLE,
                scope=f"world_pattern:{parsed.hypothesis_id}",
            )
            return self._bind_hypothesis_envelope(parsed, receipt)
        self._persist_event(
            parsed,
            table=_OCCURRENCE_TABLE,
            aggregate_id=parsed.occurrence_id,
            aggregate_column="occurrence_id",
        )
        receipt = self._commit_availability_receipt(
            subject_kind=_OCCURRENCE_SUBJECT_KIND,
            subject_id=parsed.event_id,
            payload=parsed.to_dict(),
            table=_OCCURRENCE_TABLE,
            scope=f"world_pattern:{parsed.occurrence_id}",
        )
        return self._bind_occurrence_envelope(parsed, receipt)

    def load(self, identity: PatternHypothesisId | PatternOccurrenceId) -> PatternHypothesis | PatternOccurrence:
        if isinstance(identity, PatternHypothesisId):
            rows = self._db.query_all(
                f"SELECT * FROM {_HYPOTHESIS_TABLE} WHERE hypothesis_id=? ORDER BY sequence ASC, event_id ASC",  # noqa: S608
                (identity.value,),
            )
            if not rows:
                raise LookupError(identity.value)
            return PatternHypothesis.from_events(tuple(self._rehydrate_hypothesis_row(row) for row in rows))
        if isinstance(identity, PatternOccurrenceId):
            rows = self._db.query_all(
                f"SELECT * FROM {_OCCURRENCE_TABLE} WHERE occurrence_id=? ORDER BY sequence ASC, event_id ASC",  # noqa: S608
                (identity.value,),
            )
            if not rows:
                raise LookupError(identity.value)
            return PatternOccurrence.from_events(tuple(self._rehydrate_occurrence_row(row) for row in rows))
        raise TypeError("load requires PatternHypothesisId or PatternOccurrenceId")

    def evidence_for(
        self,
        event: PatternHypothesisEvent | PatternOccurrenceEvent,
    ) -> AvailabilityEvidence | None:
        parsed = self._require_event(event)
        if isinstance(parsed, _HYPOTHESIS_EVENTS):
            table = _HYPOTHESIS_TABLE
            subject_kind = _HYPOTHESIS_SUBJECT_KIND
        else:
            table = _OCCURRENCE_TABLE
            subject_kind = _OCCURRENCE_SUBJECT_KIND
        receipt = self._lookup_receipt(
            subject_kind=subject_kind,
            subject_id=parsed.event_id,
            content_sha256=canonical_sha256(parsed.to_dict()),
            table=table,
        )
        if receipt is None:
            return None
        first_seen = self._remember_receipt(receipt)
        return AvailabilityEvidence(receipt=receipt, first_seen_at=first_seen)

    def _require_event(
        self,
        event: PatternHypothesisEvent | PatternOccurrenceEvent | Mapping[str, Any],
    ) -> PatternHypothesisEvent | PatternOccurrenceEvent:
        if isinstance(event, _HYPOTHESIS_EVENTS):
            return event
        if isinstance(event, _OCCURRENCE_EVENTS):
            return event
        if isinstance(event, Mapping):
            event_type = str(event.get("event_type") or "")
            if event_type.startswith("pattern_hypothesis_") or event_type == "pattern_evaluation_started" or event_type == "pattern_evaluation_closed":
                return parse_pattern_hypothesis_event(event)
            return parse_pattern_occurrence_event(event)
        raise TypeError("event must be a pattern hypothesis or occurrence event")

    def _persist_event(
        self,
        event: PatternHypothesisEvent | PatternOccurrenceEvent,
        *,
        table: str,
        aggregate_id: str,
        aggregate_column: str,
    ) -> int:
        payload = event.to_dict()
        payload_json = canonical_json(payload)
        payload_sha256 = canonical_sha256(payload)
        recorded_at = _utc_now()
        try:
            with self._db.transaction() as cur:
                existing = cur.execute(
                    f"SELECT payload_sha256, sequence FROM {table} WHERE event_id=?",  # noqa: S608
                    (event.event_id,),
                ).fetchone()
                if existing is not None:
                    if existing["payload_sha256"] != payload_sha256:
                        raise _payload_conflict(event.event_id)
                    return int(existing["sequence"])
                extras = self._event_columns(cur, event)
                count_row = cur.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE {aggregate_column}=?",  # noqa: S608
                    (aggregate_id,),
                ).fetchone()
                persisted_count = int(count_row[0])
                sequence = persisted_count + 1
                if table == _HYPOTHESIS_TABLE:
                    cur.execute(
                        f"""
                        INSERT INTO {_HYPOTHESIS_TABLE}(
                            event_id, hypothesis_id, event_type, sequence,
                            payload_json, payload_sha256, recorded_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            event.event_id,
                            extras["hypothesis_id"],
                            event.event_type,
                            sequence,
                            payload_json,
                            payload_sha256,
                            recorded_at,
                        ),
                    )
                else:
                    cur.execute(
                        f"""
                        INSERT INTO {_OCCURRENCE_TABLE}(
                            event_id, occurrence_id, hypothesis_id, event_type, sequence,
                            cohort_id, cutoff_at, horizon_id, payload_json, payload_sha256, recorded_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            event.event_id,
                            extras["occurrence_id"],
                            extras["hypothesis_id"],
                            event.event_type,
                            sequence,
                            extras["cohort_id"],
                            extras["cutoff_at"],
                            extras["horizon_id"],
                            payload_json,
                            payload_sha256,
                            recorded_at,
                        ),
                    )
                    self._insert_outcome_link(cur, event, recorded_at=recorded_at)
                return sequence
        except sqlite3.IntegrityError:
            existing = self._event_row(table, event.event_id)
            if existing is not None:
                if existing["payload_sha256"] != payload_sha256:
                    raise _payload_conflict(event.event_id) from None
                return int(existing["sequence"])
            raise WorldModelConflictError("conflicting sequence") from None

    def _event_columns(self, cur: sqlite3.Cursor, event: PatternHypothesisEvent | PatternOccurrenceEvent) -> dict[str, str]:
        if isinstance(event, _HYPOTHESIS_EVENTS):
            return {"hypothesis_id": event.hypothesis_id}
        if isinstance(event, PatternOccurrenceRecorded):
            spec = event.occurrence
            return {
                "occurrence_id": spec.occurrence_id,
                "hypothesis_id": spec.hypothesis_id,
                "cohort_id": spec.cohort_id,
                "cutoff_at": spec.to_dict()["cutoff_at"],
                "horizon_id": spec.forecast.horizon_id,
            }
        recorded = cur.execute(
            f"""
            SELECT hypothesis_id, cohort_id, cutoff_at, horizon_id
            FROM {_OCCURRENCE_TABLE}
            WHERE occurrence_id=? AND event_type='pattern_occurrence_recorded'
            """,
            (event.occurrence_id,),
        ).fetchone()
        if recorded is None:
            raise LookupError(event.occurrence_id)
        return {
            "occurrence_id": event.occurrence_id,
            "hypothesis_id": recorded["hypothesis_id"],
            "cohort_id": recorded["cohort_id"],
            "cutoff_at": recorded["cutoff_at"],
            "horizon_id": recorded["horizon_id"],
        }

    def _insert_outcome_link(
        self,
        cur: sqlite3.Cursor,
        event: PatternOccurrenceEvent,
        *,
        recorded_at: str,
    ) -> None:
        link: PatternOutcomeLink | None
        if isinstance(event, PatternOutcomeLinked):
            link = event.link
        elif isinstance(event, PatternOutcomeLinkSuperseded):
            link = event.successor
        else:
            return
        payload = link.to_dict()
        payload_json = canonical_json(payload)
        payload_sha256 = canonical_sha256(payload)
        existing = cur.execute(
            f"SELECT payload_sha256 FROM {_LINK_TABLE} WHERE link_id=?",  # noqa: S608
            (link.link_id,),
        ).fetchone()
        if existing is not None:
            if existing["payload_sha256"] != payload_sha256:
                raise _payload_conflict(link.link_id)
            return
        cur.execute(
            f"""
            INSERT INTO {_LINK_TABLE}(
                link_id, occurrence_id, event_id, horizon_id, world_outcome_event_id,
                world_outcome_content_sha256, supersedes_link_id, payload_json, payload_sha256, recorded_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                link.link_id,
                link.occurrence_id,
                event.event_id,
                link.horizon_id,
                link.world_outcome_event_id,
                link.world_outcome_content_sha256,
                link.supersedes_link_id,
                payload_json,
                payload_sha256,
                recorded_at,
            ),
        )

    def _commit_availability_receipt(
        self,
        *,
        subject_kind: str,
        subject_id: str,
        payload: Mapping[str, Any],
        table: str,
        scope: str,
    ) -> WorldAvailabilityReceipt:
        digest = canonical_sha256(payload)
        existing = self._lookup_receipt(
            subject_kind=subject_kind,
            subject_id=subject_id,
            content_sha256=digest,
            table=table,
        )
        if existing is not None:
            return existing
        subject_row = self._db.query_one(
            f"SELECT payload_sha256 FROM {table} WHERE event_id=?",  # noqa: S608
            (subject_id,),
        )
        if subject_row is None:
            raise ValueError("subject history readback failed")
        if subject_row["payload_sha256"] != digest:
            raise _payload_conflict(subject_id)
        subject = WorldAvailabilitySubjectRef(
            kind=subject_kind,
            subject_id=subject_id,
            content_sha256=digest,
        )
        locator = WorldStorageLocator(
            kind="sqlite",
            store_id=WORLD_MODEL_STORE_ID,
            table=table,
            row_id=subject_id,
        )
        ready_at = _aware_utc(self._clock(), field_name="clock")
        receipt = _seal_world_availability_receipt(
            subject,
            scope=scope,
            storage_locator=locator,
            ready_at=ready_at,
        )
        payload_dict = receipt.to_dict()
        recorded_at = _utc_now()
        try:
            with self._db.transaction() as cur:
                cur.execute(
                    """
                    INSERT INTO world_availability_receipts(
                        receipt_id, subject_kind, subject_id, content_sha256, scope,
                        storage_locator_json, ready_at, receipt_sha256, payload_json, recorded_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        receipt.receipt_id,
                        subject.kind,
                        subject.subject_id,
                        subject.content_sha256,
                        receipt.scope,
                        canonical_json(locator.to_dict()),
                        payload_dict["ready_at"],
                        receipt.receipt_sha256,
                        canonical_json(payload_dict),
                        recorded_at,
                    ),
                )
        except sqlite3.IntegrityError:
            recovered = self._lookup_receipt(
                subject_kind=subject_kind,
                subject_id=subject_id,
                content_sha256=digest,
                table=table,
            )
            if recovered is None:
                raise WorldModelConflictError("conflicting availability receipt for the same subject") from None
            return recovered
        written = self._lookup_receipt(
            subject_kind=subject_kind,
            subject_id=subject_id,
            content_sha256=digest,
            table=table,
        )
        if written is None:
            raise ValueError("availability receipt readback failed")
        self._remember_receipt(written, seen_at=ready_at)
        return written

    def _bind_hypothesis_envelope(
        self,
        event: PatternHypothesisEvent,
        receipt: WorldAvailabilityReceipt | None,
    ) -> PatternHypothesisEventEnvelope:
        evidence = None
        if receipt is not None:
            first_seen = self._remember_receipt(receipt)
            evidence = AvailabilityEvidence(receipt=receipt, first_seen_at=first_seen)
        return PatternHypothesisEventEnvelope(event=event, evidence=evidence)

    def _bind_occurrence_envelope(
        self,
        event: PatternOccurrenceEvent,
        receipt: WorldAvailabilityReceipt | None,
    ) -> OccurrenceEventEnvelope:
        evidence = None
        if receipt is not None:
            first_seen = self._remember_receipt(receipt)
            evidence = AvailabilityEvidence(receipt=receipt, first_seen_at=first_seen)
        return OccurrenceEventEnvelope(event=event, evidence=evidence)

    def _rehydrate_hypothesis_row(self, row: Any) -> PatternHypothesisEvent:
        try:
            payload = _json_load(row["payload_json"])
            event = parse_pattern_hypothesis_event(payload)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("tamper: existing event cannot be rehydrated") from exc
        if canonical_sha256(event.to_dict()) != row["payload_sha256"]:
            raise ValueError("tamper: pattern hypothesis event payload hash mismatch")
        if event.event_id != row["event_id"]:
            raise ValueError("tamper: pattern hypothesis event id mismatch")
        return event

    def _rehydrate_occurrence_row(self, row: Any) -> PatternOccurrenceEvent:
        try:
            payload = _json_load(row["payload_json"])
            event = parse_pattern_occurrence_event(payload)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("tamper: existing event cannot be rehydrated") from exc
        if canonical_sha256(event.to_dict()) != row["payload_sha256"]:
            raise ValueError("tamper: pattern occurrence event payload hash mismatch")
        if event.event_id != row["event_id"]:
            raise ValueError("tamper: pattern occurrence event id mismatch")
        return event

    def _event_row(self, table: str, event_id: str) -> Any:
        return self._db.query_one(
            f"SELECT * FROM {table} WHERE event_id=?",  # noqa: S608
            (event_id,),
        )

    def _lookup_receipt(
        self,
        *,
        subject_kind: str,
        subject_id: str,
        content_sha256: str,
        table: str,
    ) -> WorldAvailabilityReceipt | None:
        row = self._db.query_one(
            """
            SELECT * FROM world_availability_receipts
            WHERE subject_kind=? AND subject_id=? AND content_sha256=?
            """,
            (subject_kind, subject_id, content_sha256),
        )
        if row is None:
            return None
        return self._parse_receipt_row(row, expected_table=table)

    def _parse_receipt_row(
        self,
        row: Any,
        *,
        expected_table: str | None = None,
    ) -> WorldAvailabilityReceipt | None:
        try:
            payload = _json_load(row["payload_json"])
            receipt = WorldAvailabilityReceipt.from_mapping(payload)
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        if (
            receipt.receipt_id != row["receipt_id"]
            or receipt.receipt_sha256 != row["receipt_sha256"]
            or receipt.subject.kind != row["subject_kind"]
            or receipt.subject.subject_id != row["subject_id"]
            or receipt.subject.content_sha256 != row["content_sha256"]
        ):
            return None
        if receipt.subject.kind not in _PATTERN_SUBJECT_KINDS:
            return None
        locator = receipt.storage_locator
        if locator.kind != "sqlite" or locator.store_id != WORLD_MODEL_STORE_ID:
            return None
        if locator.table not in WORLD_PATTERN_TABLES:
            return None
        if expected_table is not None and locator.table != expected_table:
            return None
        try:
            return _attest_verified_store_receipt(
                receipt,
                expected_subject=receipt.subject,
                expected_scope=receipt.scope,
                expected_locator=locator,
            )
        except (TypeError, ValueError):
            return None

    def _remember_receipt(
        self,
        receipt: WorldAvailabilityReceipt,
        *,
        seen_at: datetime | None = None,
    ) -> datetime:
        digest = receipt.receipt_sha256
        if not digest:
            raise ValueError("availability receipt is missing receipt_sha256")
        existing = self._first_seen_at.get(digest)
        if existing is not None:
            return existing
        stamped = _aware_utc(seen_at if seen_at is not None else self._clock(), field_name="clock")
        self._first_seen_at[digest] = stamped
        return stamped
