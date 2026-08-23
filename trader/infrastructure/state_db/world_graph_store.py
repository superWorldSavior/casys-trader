"""Append-only SQLite ledger for V3 world-graph events and snapshots.

Canonical records live in dedicated event/snapshot tables. Availability receipts
are reused from ``world_availability_receipts`` (COHORT-3) and stamped only after
the subject row is durable. NetworkX projections are never persisted.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from trader.application.world_model.graph_ports import (
    WorldEntityEventEnvelope,
    WorldEntityEventId,
    WorldEntityIdentityEventEnvelope,
    WorldEntityIdentityEventId,
    WorldGraphSnapshotId,
    WorldOntologyRevisionEventEnvelope,
    WorldOntologyRevisionEventId,
    WorldRelationEventEnvelope,
    WorldRelationEventId,
)
from trader.domain.world_availability import (
    AvailabilityEvidence,
    PersistedWorldRef,
    PointInTimeEligibilityPolicy,
    WorldAvailabilityReceipt,
    WorldAvailabilitySubjectRef,
    WorldStorageLocator,
    _attest_verified_store_receipt,
)
from trader.domain.world_episode import canonical_json, canonical_sha256, parse_utc_timestamp
from trader.domain.world_graph import (
    FORBIDDEN_RELATION_KINDS,
    KnowledgeWorldRelationAsserted,
    KnowledgeWorldRelationEvent,
    KnowledgeWorldRelationRetired,
    StructuralWorldRelationAsserted,
    StructuralWorldRelationEvent,
    StructuralWorldRelationRetired,
    WorldEntityAsserted,
    WorldEntityEvent,
    WorldEntityIdentityEvent,
    WorldEntityIdentityLinked,
    WorldEntityIdentityLinkSuperseded,
    WorldEntityIdentityUnlinked,
    WorldEntityRetired,
    WorldEntitySuperseded,
    WorldGraphSnapshot,
    WorldOntologyRevisionEvent,
    WorldOntologyRevisionPublished,
    WorldOntologyRevisionSuperseded,
    WorldRelationEvent,
    parse_world_entity_event,
    parse_world_entity_identity_event,
    parse_world_ontology_revision_event,
    parse_world_relation_event,
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


WORLD_GRAPH_TABLES = (
    "world_entity_events",
    "world_entity_identity_events",
    "world_relation_events",
    "world_ontology_revisions",
    "world_graph_snapshots",
    "world_graph_snapshot_members",
)

_ENTITY_SUBJECT_KIND = "world_entity_event"
_IDENTITY_SUBJECT_KIND = "world_entity_identity_event"
_RELATION_SUBJECT_KIND = "world_relation_event"
_REVISION_SUBJECT_KIND = "world_ontology_revision_event"
_SNAPSHOT_SUBJECT_KIND = "world_graph_snapshot"
_GRAPH_SUBJECT_KINDS = frozenset(
    {
        _ENTITY_SUBJECT_KIND,
        _IDENTITY_SUBJECT_KIND,
        _RELATION_SUBJECT_KIND,
        _REVISION_SUBJECT_KIND,
        _SNAPSHOT_SUBJECT_KIND,
    }
)

__all__ = [
    "WORLD_GRAPH_TABLES",
    "WorldGraphStore",
]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_load(text: str) -> Any:
    return json.loads(text)


def _conflict(id_column: str, identity: str) -> WorldModelConflictError:
    return WorldModelConflictError(
        f"{id_column} {identity!r} already exists with different canonical content"
    )


def _assert_persistable_payload(payload: Mapping[str, Any]) -> None:
    rendered = canonical_json(payload)
    lowered = rendered.lower()
    if "networkx" in lowered or "multidigraph" in lowered or "pickle" in lowered:
        raise ValueError("NetworkX graphs must not be persisted")
    compacted = rendered.replace(" ", "").upper()
    for kind in FORBIDDEN_RELATION_KINDS:
        if f'"KIND":"{kind}"' in compacted:
            raise ValueError("CAUSES edges are forbidden")


def _entity_extras(event: WorldEntityEvent) -> dict[str, Any]:
    return {"entity_kind": event.entity.kind, "entity_id": event.entity.entity_id}


def _identity_extras(event: WorldEntityIdentityEvent) -> dict[str, Any]:
    if isinstance(event, WorldEntityIdentityLinked):
        return {"link_id": event.link.link_id}
    if isinstance(event, WorldEntityIdentityUnlinked):
        return {"link_id": event.link_id}
    if isinstance(event, WorldEntityIdentityLinkSuperseded):
        return {"link_id": event.successor.link_id}
    raise TypeError(f"unsupported identity event: {type(event).__name__}")


def _relation_extras(event: WorldRelationEvent) -> dict[str, Any]:
    if isinstance(event, (StructuralWorldRelationAsserted, KnowledgeWorldRelationAsserted)):
        return {
            "family": event.family,
            "relation_id": event.relation.relation_id,
            "relation_kind": event.relation.kind,
        }
    if isinstance(event, (StructuralWorldRelationRetired, KnowledgeWorldRelationRetired)):
        return {
            "family": event.family,
            "relation_id": event.relation_id,
            "relation_kind": None,
        }
    raise TypeError(f"unsupported relation event: {type(event).__name__}")


def _revision_extras(event: WorldOntologyRevisionEvent) -> dict[str, Any]:
    if isinstance(event, WorldOntologyRevisionPublished):
        return {"revision_id": event.revision.revision_id, "successor_revision_id": None}
    if isinstance(event, WorldOntologyRevisionSuperseded):
        return {
            "revision_id": event.revision_id,
            "successor_revision_id": event.successor_revision_id,
        }
    raise TypeError(f"unsupported revision event: {type(event).__name__}")


@dataclass(frozen=True)
class _GraphEventLog:
    table: str
    subject_kind: str
    id_type: Callable[[str], Any]
    envelope_cls: type
    parse: Callable[[Any], Any]
    allowed: tuple[type, ...]
    extra_columns: tuple[str, ...]
    extras: Callable[[Any], dict[str, Any]]
    family: str | None = None


_ENTITY_LOG = _GraphEventLog(
    table="world_entity_events",
    subject_kind=_ENTITY_SUBJECT_KIND,
    id_type=WorldEntityEventId,
    envelope_cls=WorldEntityEventEnvelope,
    parse=parse_world_entity_event,
    allowed=(WorldEntityAsserted, WorldEntityRetired, WorldEntitySuperseded),
    extra_columns=("entity_kind", "entity_id"),
    extras=_entity_extras,
)
_IDENTITY_LOG = _GraphEventLog(
    table="world_entity_identity_events",
    subject_kind=_IDENTITY_SUBJECT_KIND,
    id_type=WorldEntityIdentityEventId,
    envelope_cls=WorldEntityIdentityEventEnvelope,
    parse=parse_world_entity_identity_event,
    allowed=(WorldEntityIdentityLinked, WorldEntityIdentityUnlinked, WorldEntityIdentityLinkSuperseded),
    extra_columns=("link_id",),
    extras=_identity_extras,
)
_STRUCTURAL_LOG = _GraphEventLog(
    table="world_relation_events",
    subject_kind=_RELATION_SUBJECT_KIND,
    id_type=WorldRelationEventId,
    envelope_cls=WorldRelationEventEnvelope,
    parse=parse_world_relation_event,
    allowed=(StructuralWorldRelationAsserted, StructuralWorldRelationRetired),
    extra_columns=("family", "relation_id", "relation_kind"),
    extras=_relation_extras,
    family="structural",
)
_KNOWLEDGE_LOG = _GraphEventLog(
    table="world_relation_events",
    subject_kind=_RELATION_SUBJECT_KIND,
    id_type=WorldRelationEventId,
    envelope_cls=WorldRelationEventEnvelope,
    parse=parse_world_relation_event,
    allowed=(KnowledgeWorldRelationAsserted, KnowledgeWorldRelationRetired),
    extra_columns=("family", "relation_id", "relation_kind"),
    extras=_relation_extras,
    family="knowledge",
)
_REVISION_LOG = _GraphEventLog(
    table="world_ontology_revisions",
    subject_kind=_REVISION_SUBJECT_KIND,
    id_type=WorldOntologyRevisionEventId,
    envelope_cls=WorldOntologyRevisionEventEnvelope,
    parse=parse_world_ontology_revision_event,
    allowed=(WorldOntologyRevisionPublished, WorldOntologyRevisionSuperseded),
    extra_columns=("revision_id", "successor_revision_id"),
    extras=_revision_extras,
)
_LOGS_BY_TABLE = {
    "world_entity_events": _ENTITY_LOG,
    "world_entity_identity_events": _IDENTITY_LOG,
    "world_relation_events": _STRUCTURAL_LOG,
    "world_ontology_revisions": _REVISION_LOG,
}


def _snapshot_members(snapshot: WorldGraphSnapshot) -> tuple[dict[str, Any], ...]:
    rows: list[dict[str, Any]] = []

    def add(kind: str, member_id: str, payload: Mapping[str, Any], content_sha256: str | None) -> None:
        rows.append(
            {
                "member_kind": kind,
                "member_id": member_id,
                "content_sha256": content_sha256,
                "ordinal": sum(1 for item in rows if item["member_kind"] == kind),
                "payload": dict(payload),
            }
        )

    for ref in snapshot.entity_revision_refs:
        add("entity_revision", ref.entity.node_id, ref.to_dict(), ref.content_sha256)
    for ref in snapshot.identity_link_refs:
        add("identity_link", ref.link_id, ref.to_dict(), ref.content_sha256)
    for ref in sorted(snapshot.structural_relation_refs, key=lambda item: item.relation_id):
        add("structural_relation", ref.relation_id, ref.to_dict(), ref.content_sha256)
    for ref in sorted(snapshot.knowledge_relation_refs, key=lambda item: item.relation_id):
        add("knowledge_relation", ref.relation_id, ref.to_dict(), ref.content_sha256)
    for artifact_id in snapshot.artifact_refs:
        add("artifact", artifact_id, {"artifact_id": artifact_id}, None)
    return tuple(rows)


class WorldGraphStore:
    """SQLite adapter for ``WorldGraphLedger`` and ``WorldGraphSnapshotLedger``."""

    def __init__(self, db_or_path: StateDb | str | Path, *, clock: UtcClock | None = None) -> None:
        if isinstance(db_or_path, StateDb):
            self._db = db_or_path
            self._owns_db = False
        else:
            if Path(db_or_path).name.casefold() == "casys.db":
                raise ValueError("WorldGraphStore requires a dedicated world_model.db, never casys.db")
            self._db = _open_dedicated_state_db(db_or_path)
            self._owns_db = True
        self.path = self._db.path
        if self.path.name.casefold() == "casys.db":
            raise ValueError("WorldGraphStore requires a dedicated world_model.db, never casys.db")
        self._clock = clock or default_utc_clock
        self._first_seen_at: dict[str, datetime] = {}
        self._db.query_one("PRAGMA foreign_keys=ON")
        self._db.apply_migrations(WORLD_MODEL_MIGRATIONS)
        self._prime_existing_receipts()

    def close(self) -> None:
        if self._owns_db:
            self._db.close()

    def append_entity_event(self, event: WorldEntityEvent) -> PersistedWorldRef[WorldEntityEventId]:
        return self._append_event(event, _ENTITY_LOG)

    def append_identity_event(
        self, event: WorldEntityIdentityEvent
    ) -> PersistedWorldRef[WorldEntityIdentityEventId]:
        return self._append_event(event, _IDENTITY_LOG)

    def append_structural_relation_event(
        self, event: StructuralWorldRelationEvent
    ) -> PersistedWorldRef[WorldRelationEventId]:
        return self._append_event(event, _STRUCTURAL_LOG)

    def append_knowledge_relation_event(
        self, event: KnowledgeWorldRelationEvent
    ) -> PersistedWorldRef[WorldRelationEventId]:
        return self._append_event(event, _KNOWLEDGE_LOG)

    def append_revision_event(
        self, event: WorldOntologyRevisionEvent
    ) -> PersistedWorldRef[WorldOntologyRevisionEventId]:
        return self._append_event(event, _REVISION_LOG)

    def list_entity_events_available_through(self, cutoff_at: datetime) -> tuple[WorldEntityEventEnvelope, ...]:
        return self._list_available(_ENTITY_LOG, cutoff_at)

    def list_identity_events_available_through(
        self, cutoff_at: datetime
    ) -> tuple[WorldEntityIdentityEventEnvelope, ...]:
        return self._list_available(_IDENTITY_LOG, cutoff_at)

    def list_structural_relation_events_available_through(
        self, cutoff_at: datetime
    ) -> tuple[WorldRelationEventEnvelope, ...]:
        return self._list_available(_STRUCTURAL_LOG, cutoff_at)

    def list_knowledge_relation_events_available_through(
        self, cutoff_at: datetime
    ) -> tuple[WorldRelationEventEnvelope, ...]:
        return self._list_available(_KNOWLEDGE_LOG, cutoff_at)

    def list_revision_events_available_through(
        self, cutoff_at: datetime
    ) -> tuple[WorldOntologyRevisionEventEnvelope, ...]:
        return self._list_available(_REVISION_LOG, cutoff_at)

    def append(self, snapshot: WorldGraphSnapshot) -> PersistedWorldRef[WorldGraphSnapshotId]:
        if not isinstance(snapshot, WorldGraphSnapshot):
            raise TypeError("snapshot must be WorldGraphSnapshot")
        payload = snapshot.to_dict()
        _assert_persistable_payload(payload)
        self._persist_snapshot(snapshot, payload)
        receipt = self._commit_availability_receipt(
            subject_kind=_SNAPSHOT_SUBJECT_KIND,
            subject_id=snapshot.snapshot_id,
            payload=payload,
            table="world_graph_snapshots",
        )
        return PersistedWorldRef(identity=WorldGraphSnapshotId(snapshot.snapshot_id), receipt=receipt)

    def get(self, snapshot_id: WorldGraphSnapshotId) -> WorldGraphSnapshot | None:
        identity = str(snapshot_id)
        row = self._db.query_one("SELECT * FROM world_graph_snapshots WHERE snapshot_id=?", (identity,))
        if row is None:
            return None
        try:
            payload = _json_load(row["payload_json"])
            snapshot = WorldGraphSnapshot.from_mapping(payload)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("tamper: existing graph snapshot cannot be rehydrated") from exc
        if canonical_sha256(snapshot.to_dict()) != row["payload_sha256"]:
            raise ValueError("tamper: graph snapshot payload hash mismatch")
        if snapshot.snapshot_id != row["snapshot_id"]:
            raise ValueError("tamper: graph snapshot id mismatch")
        return snapshot

    def _append_event(self, event: Any, log: _GraphEventLog) -> PersistedWorldRef[Any]:
        parsed = log.parse(event)
        if not isinstance(parsed, log.allowed):
            allowed = ", ".join(cls.__name__ for cls in log.allowed)
            raise TypeError(f"event must be one of: {allowed}")
        if log.family is not None and parsed.family != log.family:
            raise TypeError(f"event family must be {log.family}")
        self._persist_graph_event(parsed, log=log)
        receipt = self._commit_availability_receipt(
            subject_kind=log.subject_kind,
            subject_id=parsed.event_id,
            payload=parsed.to_dict(),
            table=log.table,
        )
        return PersistedWorldRef(identity=log.id_type(parsed.event_id), receipt=receipt)

    def _persist_graph_event(self, event: Any, *, log: _GraphEventLog) -> int:
        payload = event.to_dict()
        _assert_persistable_payload(payload)
        payload_json = canonical_json(payload)
        payload_sha256 = canonical_sha256(payload)
        extras = log.extras(event)
        recorded_at = _utc_now()
        last_error: sqlite3.IntegrityError | None = None
        for _attempt in range(8):
            try:
                with self._db.transaction() as cur:
                    existing = cur.execute(
                        f"SELECT payload_sha256, sequence FROM {log.table} WHERE event_id=?",  # noqa: S608
                        (event.event_id,),
                    ).fetchone()
                    if existing is not None:
                        if existing["payload_sha256"] != payload_sha256:
                            raise _conflict("event_id", event.event_id)
                        return int(existing["sequence"])
                    count = int(cur.execute(f"SELECT COUNT(*) FROM {log.table}").fetchone()[0])  # noqa: S608
                    sequence = count + 1
                    columns = ("event_id", "event_type", *log.extra_columns, "sequence", "payload_json", "payload_sha256")
                    values = {
                        "event_id": event.event_id,
                        "event_type": event.event_type,
                        **extras,
                        "sequence": sequence,
                        "payload_json": payload_json,
                        "payload_sha256": payload_sha256,
                    }
                    placeholders = ", ".join("?" for _ in columns)
                    column_sql = ", ".join(columns)
                    cur.execute(
                        f"INSERT INTO {log.table}({column_sql}, recorded_at) "  # noqa: S608
                        f"VALUES ({placeholders}, ?)",
                        tuple(values[column] for column in columns) + (recorded_at,),
                    )
                    return sequence
            except sqlite3.IntegrityError as exc:
                last_error = exc
                existing = self._event_row(log.table, event.event_id)
                if existing is not None:
                    if existing["payload_sha256"] != payload_sha256:
                        raise _conflict("event_id", event.event_id) from None
                    return int(existing["sequence"])
        raise WorldModelConflictError("conflicting sequence") from last_error

    def _persist_snapshot(self, snapshot: WorldGraphSnapshot, payload: Mapping[str, Any]) -> None:
        payload_json = canonical_json(payload)
        payload_sha256 = canonical_sha256(payload)
        recorded_at = _utc_now()
        members = _snapshot_members(snapshot)
        try:
            with self._db.transaction() as cur:
                existing = cur.execute(
                    "SELECT payload_sha256 FROM world_graph_snapshots WHERE snapshot_id=?",
                    (snapshot.snapshot_id,),
                ).fetchone()
                if existing is not None:
                    if existing["payload_sha256"] != payload_sha256:
                        raise _conflict("snapshot_id", snapshot.snapshot_id)
                    return
                cur.execute(
                    """
                    INSERT INTO world_graph_snapshots(
                        snapshot_id, root_episode_id, root_entity_kind, root_entity_id, cutoff_at,
                        ontology_revision, ontology_hash, identity_map_hash, scope_mapping_id,
                        scope_mapping_hash, status, payload_json, payload_sha256, recorded_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        snapshot.snapshot_id,
                        snapshot.root_episode_id,
                        snapshot.root_entity.kind,
                        snapshot.root_entity.entity_id,
                        payload["cutoff_at"],
                        snapshot.ontology_revision,
                        snapshot.ontology_hash,
                        snapshot.identity_map_hash,
                        snapshot.scope_mapping_id,
                        snapshot.scope_mapping_hash,
                        snapshot.status,
                        payload_json,
                        payload_sha256,
                        recorded_at,
                    ),
                )
                for member in members:
                    member_json = canonical_json(member["payload"])
                    cur.execute(
                        """
                        INSERT INTO world_graph_snapshot_members(
                            snapshot_id, member_kind, member_id, content_sha256, ordinal,
                            payload_json, payload_sha256, recorded_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            snapshot.snapshot_id,
                            member["member_kind"],
                            member["member_id"],
                            member["content_sha256"],
                            member["ordinal"],
                            member_json,
                            canonical_sha256(member["payload"]),
                            recorded_at,
                        ),
                    )
        except sqlite3.IntegrityError:
            existing = self._db.query_one(
                "SELECT payload_sha256 FROM world_graph_snapshots WHERE snapshot_id=?",
                (snapshot.snapshot_id,),
            )
            if existing is not None:
                if existing["payload_sha256"] != payload_sha256:
                    raise _conflict("snapshot_id", snapshot.snapshot_id) from None
                return
            raise WorldModelConflictError("conflicting graph snapshot") from None

    def _list_available(self, log: _GraphEventLog, cutoff_at: datetime) -> tuple[Any, ...]:
        cutoff = parse_utc_timestamp(cutoff_at, "cutoff_at")
        sql = f"SELECT * FROM {log.table}"  # noqa: S608
        params: list[Any] = []
        if log.family is not None:
            sql += " WHERE family=?"
            params.append(log.family)
        sql += " ORDER BY sequence ASC, event_id ASC"
        envelopes: list[Any] = []
        for row in self._db.query_all(sql, tuple(params)):
            event = self._rehydrate_event_row(row, log=log)
            receipt = self._lookup_receipt(
                subject_kind=log.subject_kind,
                subject_id=event.event_id,
                content_sha256=canonical_sha256(event.to_dict()),
                table=log.table,
            )
            if receipt is None:
                continue
            first_seen = self._remember_receipt(receipt)
            evidence = AvailabilityEvidence(receipt=receipt, first_seen_at=first_seen)
            if not PointInTimeEligibilityPolicy().is_eligible(evidence=evidence, cutoff_at=cutoff):
                continue
            envelopes.append(log.envelope_cls(event=event, evidence=evidence))
        return tuple(envelopes)

    def _rehydrate_event_row(self, row: Any, *, log: _GraphEventLog) -> Any:
        try:
            payload = _json_load(row["payload_json"])
            event = log.parse(payload)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("tamper: existing event cannot be rehydrated") from exc
        digest = canonical_sha256(event.to_dict())
        if digest != row["payload_sha256"]:
            raise ValueError("tamper: graph event payload hash mismatch")
        if event.event_id != row["event_id"]:
            raise ValueError("tamper: graph event id mismatch")
        return event

    def _event_row(self, table: str, event_id: str) -> Any:
        return self._db.query_one(
            f"SELECT * FROM {table} WHERE event_id=?",  # noqa: S608
            (event_id,),
        )

    def _commit_availability_receipt(
        self,
        *,
        subject_kind: str,
        subject_id: str,
        payload: Mapping[str, Any],
        table: str,
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
        id_column = "snapshot_id" if table == "world_graph_snapshots" else "event_id"
        subject_row = self._db.query_one(
            f"SELECT 1 FROM {table} WHERE {id_column}=?",  # noqa: S608
            (subject_id,),
        )
        if subject_row is None:
            raise ValueError("subject history readback failed")
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
            scope=f"world_graph:{subject_kind}",
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
        if receipt.subject.kind not in _GRAPH_SUBJECT_KINDS:
            return None
        locator = receipt.storage_locator
        if locator.kind != "sqlite" or locator.store_id != WORLD_MODEL_STORE_ID:
            return None
        if locator.table not in WORLD_GRAPH_TABLES:
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

    def _prime_existing_receipts(self) -> None:
        rows = self._db.query_all("SELECT * FROM world_availability_receipts")
        for row in rows:
            parsed = self._parse_receipt_row(row)
            if parsed is None:
                continue
            self._remember_receipt(parsed)

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
