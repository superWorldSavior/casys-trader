"""Append-only JSONL histories and store-attested receipts for source-only macro.

Facts, observations, and collection events are immutable once written. Readiness
is the sidecar receipt stamped after subject fsync. Point-in-time eligibility is
not decided here: the reader returns attested candidates plus first-seen evidence.
"""

from __future__ import annotations

import fcntl
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from trader.domain.world_availability import (
    AvailabilityEvidence,
    PersistedWorldRef,
    WorldAvailabilityReceipt,
    WorldAvailabilitySubjectRef,
    _attest_verified_store_receipt,
    world_subject_content_sha256,
)
from trader.domain.world_episode import parse_utc_timestamp
from trader.domain.world_graph import MacroObservationCursor, MacroObservationCursorReservation
from trader.domain.world_macro import (
    MACRO_WORLD_OBSERVATION_SUBJECT_KIND,
    MacroCollectionCompleted,
    MacroCollectionEvent,
    MacroCollectionEventId,
    MacroCollectionRegistered,
    MacroCollectionRunId,
    MacroCollectionStarted,
    MacroObservationEnvelope,
    MacroObservationId,
    MacroObservationPublished,
    MacroScope,
    MacroSourceCompleted,
    MacroSourceFailed,
    MacroSourceFact,
    MacroSourceFactVersionId,
    MacroWorldObservation,
    parse_macro_collection_event,
    reconcile_macro_source_fact,
    reconcile_macro_world_observation,
)
from trader.infrastructure.state_db._jsonl_store import read_jsonl_objects
from trader.infrastructure.state_db.availability_receipt import (
    UtcClock,
    WorldAvailabilityJsonlReceiptStore,
    append_jsonl_and_fsync,
    default_utc_clock,
    load_receipts,
    parse_world_availability_receipt,
)
from trader.infrastructure.state_db.shadow import write_json_atomic


WORLD_MACRO_STORE_ID = "world-macro-jsonl.v1"
MACRO_SOURCE_FACT_SUBJECT_KIND = "macro_source_fact"
MACRO_COLLECTION_EVENT_SUBJECT_KIND = "macro_collection_event"
_STATUS_SCHEMA = "world_macro_status.v1"
_CURSOR_LOG_GENERATION = 1
_CURSOR_LOG_PATH = "observations/cursor_log.jsonl"
_RESERVATION_PATH = "observations/cursor_reservations/reservations.jsonl"

_EVENT_CLASSES = (
    MacroCollectionRegistered,
    MacroCollectionStarted,
    MacroSourceCompleted,
    MacroSourceFailed,
    MacroObservationPublished,
    MacroCollectionCompleted,
)
_EVENT_ORDER = {
    "macro_collection_registered": 0,
    "macro_collection_started": 1,
    "macro_source_completed": 2,
    "macro_source_failed": 2,
    "macro_observation_published": 3,
    "macro_collection_completed": 4,
}


def _utc(value: datetime, *, field_name: str = "clock") -> datetime:
    if not isinstance(value, datetime):
        raise TypeError(f"{field_name} must return a timezone-aware datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _utc_day(value: datetime) -> str:
    return _utc(value, field_name="timestamp").date().isoformat()


def _scope_key(scope: MacroScope) -> str:
    return f"{scope.kind}:{scope.entity_id}"


def _observation_payload(observation: MacroWorldObservation) -> dict[str, Any]:
    payload = observation.to_dict()
    payload.pop("content_sha256", None)
    return payload


def _receipt_fingerprint(receipt: WorldAvailabilityReceipt) -> str:
    digest = receipt.receipt_sha256
    if not digest:
        raise ValueError("availability receipt is missing receipt_sha256")
    return digest


class WorldMacroStore:
    """JSONL adapter for MacroHistory, WorldMacroObservationReader, and MacroCollectionLedger."""

    def __init__(self, root: str | Path, *, clock: UtcClock | None = None) -> None:
        self._root = Path(root)
        self._clock = clock or default_utc_clock
        self._thread_lock = threading.RLock()
        self._receipts = WorldAvailabilityJsonlReceiptStore(
            self._root,
            store_id=WORLD_MACRO_STORE_ID,
            clock=self._clock,
        )
        self._first_seen_at: dict[str, datetime] = {}
        self._prime_existing_receipts()

    def append_fact(self, fact: MacroSourceFact) -> PersistedWorldRef[MacroSourceFactVersionId]:
        if not isinstance(fact, MacroSourceFact):
            raise TypeError("fact must be MacroSourceFact")
        with self._exclusive_lock():
            existing_row = self._unique_history_row("facts", "fact_version_id", fact.fact_version_id.value)
            if existing_row is not None:
                try:
                    existing = MacroSourceFact.from_mapping(existing_row)
                except (TypeError, ValueError) as exc:
                    raise ValueError("tamper: existing fact cannot be rehydrated") from exc
                reconcile_macro_source_fact(existing, fact)
                payload = dict(existing_row)
                day = _utc_day(existing.ingested_at)
            else:
                payload = fact.to_dict()
                day = _utc_day(fact.ingested_at)
            receipt = self._persist_subject(
                kind=MACRO_SOURCE_FACT_SUBJECT_KIND,
                subject_id=fact.fact_version_id.value,
                payload=payload,
                scope=_scope_key(fact.scope),
                history_path=f"facts/{day}.jsonl",
                receipt_path=f"facts/availability_receipts/{day}.jsonl",
            )
            self._write_status()
            return PersistedWorldRef(identity=fact.fact_version_id, receipt=receipt)

    def append_observation(self, observation: MacroWorldObservation) -> MacroObservationEnvelope:
        if not isinstance(observation, MacroWorldObservation):
            raise TypeError("observation must be MacroWorldObservation")
        with self._exclusive_lock():
            existing_row = self._unique_history_row("observations", "observation_id", observation.observation_id)
            if existing_row is not None:
                try:
                    existing = MacroWorldObservation.from_mapping(existing_row)
                except (TypeError, ValueError) as exc:
                    raise ValueError("tamper: existing observation cannot be rehydrated") from exc
                reconcile_macro_world_observation(existing, observation)
                payload = dict(existing_row)
                day = _utc_day(existing.cutoff_at)
            else:
                payload = _observation_payload(observation)
                if world_subject_content_sha256(payload) != observation.content_sha256:
                    raise ValueError("observation content_sha256 does not match payload")
                day = _utc_day(observation.cutoff_at)
            receipt = self._persist_subject(
                kind=MACRO_WORLD_OBSERVATION_SUBJECT_KIND,
                subject_id=observation.observation_id,
                payload=payload,
                scope=_scope_key(observation.scope),
                history_path=f"observations/{day}.jsonl",
                receipt_path=f"observations/availability_receipts/{day}.jsonl",
            )
            self._assign_observation_cursor_locked(observation.observation_id, receipt)
            first_seen = self._remember_receipt(receipt, seen_at=receipt.ready_at)
            self._write_status()
            return self._observation_envelope(observation, receipt, first_seen_at=first_seen)

    def list_candidates_available_through(
        self, scope: MacroScope, cutoff_at: datetime
    ) -> tuple[MacroObservationEnvelope, ...]:
        if not isinstance(scope, MacroScope):
            raise TypeError("scope must be MacroScope")
        cutoff = parse_utc_timestamp(cutoff_at, "cutoff_at")
        envelopes: list[MacroObservationEnvelope] = []
        for observation, receipt in self._iter_proven_observations():
            if observation.scope != scope:
                continue
            if observation.cutoff_at > cutoff:
                continue
            first_seen = self._remember_receipt(receipt)
            envelopes.append(self._observation_envelope(observation, receipt, first_seen_at=first_seen))
        envelopes.sort(key=lambda item: (item.observation.cutoff_at, item.observation.observation_id))
        return tuple(envelopes)

    def append_event(self, event: MacroCollectionEvent) -> PersistedWorldRef[MacroCollectionEventId]:
        if not isinstance(event, _EVENT_CLASSES):
            raise TypeError("event must be a macro collection event")
        with self._exclusive_lock():
            payload = event.to_dict()
            existing_row = self._unique_history_row("runs/events", "event_id", event.event_id)
            if existing_row is not None:
                if world_subject_content_sha256(existing_row) != world_subject_content_sha256(payload):
                    raise ValueError("conflict: same event id with different content")
                payload = dict(existing_row)
            day = self._event_partition_day(event, payload)
            receipt = self._persist_subject(
                kind=MACRO_COLLECTION_EVENT_SUBJECT_KIND,
                subject_id=event.event_id,
                payload=payload,
                scope=event.run_id,
                history_path=f"runs/events/{day}.jsonl",
                receipt_path=f"runs/availability_receipts/{day}.jsonl",
            )
            self._write_status()
            return PersistedWorldRef(identity=MacroCollectionEventId(event.event_id), receipt=receipt)

    def reserve_activation_cursor(self, bridge_key: str, request_id: str) -> MacroObservationCursorReservation:
        with self._exclusive_lock():
            self._ensure_observation_cursors_locked()
            for existing in self._iter_reservations():
                if existing.bridge_key == str(bridge_key) and existing.request_id == str(request_id):
                    return existing
            cursor = self._head_cursor_locked()
            reservation = MacroObservationCursorReservation(
                bridge_key=str(bridge_key),
                request_id=str(request_id),
                cursor=cursor,
            )
            append_jsonl_and_fsync(self._root / _RESERVATION_PATH, reservation.to_dict())
            return reservation

    def list_available_after(self, cursor: MacroObservationCursor, limit: int) -> tuple[MacroObservationEnvelope, ...]:
        if not isinstance(cursor, MacroObservationCursor):
            raise TypeError("cursor must be MacroObservationCursor")
        limit = int(limit)
        if limit < 0:
            raise ValueError("limit must be non-negative")
        envelopes: list[MacroObservationEnvelope] = []
        with self._exclusive_lock():
            self._ensure_observation_cursors_locked()
            by_id = {observation.observation_id: (observation, receipt) for observation, receipt in self._iter_proven_observations()}
            for item in self._iter_cursor_rows():
                if item.receipt_log_generation != cursor.receipt_log_generation:
                    continue
                if item.ordinal <= cursor.ordinal:
                    continue
                pair = by_id.get(str(item.observation_id))
                if pair is None:
                    continue
                observation, receipt = pair
                first_seen = self._remember_receipt(receipt)
                envelopes.append(self._observation_envelope(observation, receipt, first_seen_at=first_seen))
                if len(envelopes) >= limit:
                    break
        return tuple(envelopes)

    def cursor_for(self, observation_id: str) -> MacroObservationCursor:
        with self._exclusive_lock():
            self._ensure_observation_cursors_locked()
            for item in self._iter_cursor_rows():
                if item.observation_id == observation_id:
                    return item
        raise KeyError(observation_id)

    def load(self, run_id: MacroCollectionRunId) -> tuple[MacroCollectionEvent, ...]:
        if not isinstance(run_id, MacroCollectionRunId):
            raise TypeError("run_id must be MacroCollectionRunId")
        events: list[MacroCollectionEvent] = []
        for payload, receipt in self._iter_proven_event_rows():
            if str(payload.get("run_id") or "") != run_id.value:
                continue
            event = self._rehydrate_event(payload, receipt)
            if event is None:
                continue
            events.append(event)
        events.sort(key=lambda item: (_EVENT_ORDER.get(item.event_type, 99), item.event_id))
        return tuple(events)

    def _iter_cursor_rows(self) -> Iterator[MacroObservationCursor]:
        path = self._root / _CURSOR_LOG_PATH
        if not path.is_file():
            return
        for row in read_jsonl_objects(path):
            try:
                yield MacroObservationCursor.from_mapping(row)
            except (TypeError, ValueError):
                continue

    def _iter_reservations(self) -> Iterator[MacroObservationCursorReservation]:
        path = self._root / _RESERVATION_PATH
        if not path.is_file():
            return
        for row in read_jsonl_objects(path):
            try:
                yield MacroObservationCursorReservation.from_mapping(row)
            except (TypeError, ValueError):
                continue

    def _head_cursor_locked(self) -> MacroObservationCursor:
        head = MacroObservationCursor(receipt_log_generation=_CURSOR_LOG_GENERATION, ordinal=0)
        for item in self._iter_cursor_rows():
            if item.ordinal >= head.ordinal:
                head = item
        return head

    def _assign_observation_cursor_locked(
        self, observation_id: str, receipt: WorldAvailabilityReceipt
    ) -> MacroObservationCursor:
        for item in self._iter_cursor_rows():
            if item.observation_id == observation_id:
                return item
        head = self._head_cursor_locked()
        cursor = MacroObservationCursor(
            receipt_log_generation=_CURSOR_LOG_GENERATION,
            ordinal=head.ordinal + 1,
            receipt_id=receipt.receipt_id,
            observation_id=observation_id,
        )
        append_jsonl_and_fsync(self._root / _CURSOR_LOG_PATH, cursor.to_dict())
        return cursor

    def _ensure_observation_cursors_locked(self) -> None:
        assigned = {item.observation_id for item in self._iter_cursor_rows()}
        for receipt in self._iter_observation_receipts_in_log_order():
            if receipt.subject.subject_id in assigned:
                continue
            self._assign_observation_cursor_locked(receipt.subject.subject_id, receipt)
            assigned.add(receipt.subject.subject_id)

    def _iter_observation_receipts_in_log_order(self) -> Iterator[WorldAvailabilityReceipt]:
        base = self._root / "observations" / "availability_receipts"
        if not base.exists():
            return
        for path in sorted(base.glob("????-??-??.jsonl")):
            if not path.is_file():
                continue
            for row in load_receipts(path):
                parsed = parse_world_availability_receipt(row)
                if parsed is None or parsed.subject.kind != MACRO_WORLD_OBSERVATION_SUBJECT_KIND:
                    continue
                yield parsed

    def _persist_subject(
        self,
        *,
        kind: str,
        subject_id: str,
        payload: Mapping[str, Any],
        scope: str,
        history_path: str,
        receipt_path: str,
    ) -> WorldAvailabilityReceipt:
        subject = WorldAvailabilitySubjectRef(
            kind=kind,
            subject_id=subject_id,
            content_sha256=world_subject_content_sha256(payload),
        )
        persisted = self._receipts.append(
            subject,
            scope=scope,
            payload=payload,
            history_path=history_path,
            receipt_path=receipt_path,
        )
        return persisted.receipt

    def _observation_envelope(
        self,
        observation: MacroWorldObservation,
        receipt: WorldAvailabilityReceipt,
        *,
        first_seen_at: datetime,
    ) -> MacroObservationEnvelope:
        attested = _attest_verified_store_receipt(
            receipt,
            expected_subject=receipt.subject,
            expected_scope=receipt.scope,
            expected_locator=receipt.storage_locator,
        )
        return MacroObservationEnvelope(
            observation=observation,
            persisted=PersistedWorldRef(
                identity=MacroObservationId(observation.observation_id),
                receipt=attested,
            ),
            evidence=AvailabilityEvidence(receipt=attested, first_seen_at=first_seen_at),
        )

    def _iter_proven_observations(self) -> Iterator[tuple[MacroWorldObservation, WorldAvailabilityReceipt]]:
        for payload, receipt in self._iter_joined_subjects(MACRO_WORLD_OBSERVATION_SUBJECT_KIND):
            try:
                observation = MacroWorldObservation.from_mapping(payload)
            except (TypeError, ValueError):
                continue
            if observation.observation_id != receipt.subject.subject_id:
                continue
            if observation.content_sha256 != receipt.subject.content_sha256:
                continue
            if receipt.scope != _scope_key(observation.scope):
                continue
            attested = _attest_verified_store_receipt(
                receipt,
                expected_subject=receipt.subject,
                expected_scope=receipt.scope,
                expected_locator=receipt.storage_locator,
            )
            yield observation, attested

    def _iter_proven_event_rows(self) -> Iterator[tuple[dict[str, Any], WorldAvailabilityReceipt]]:
        yield from self._iter_joined_subjects(MACRO_COLLECTION_EVENT_SUBJECT_KIND)

    def _rehydrate_event(
        self,
        payload: Mapping[str, Any],
        receipt: WorldAvailabilityReceipt,
    ) -> MacroCollectionEvent | None:
        event_type = str(payload.get("event_type") or "")
        try:
            if event_type == "macro_observation_published":
                observation_id = str(payload.get("observation_id") or "")
                envelope = self._published_envelope(observation_id)
                if envelope is None:
                    return None
                event = parse_macro_collection_event({**dict(payload), "envelope": envelope})
            else:
                event = parse_macro_collection_event(payload)
        except (TypeError, ValueError):
            return None
        if event.event_id != receipt.subject.subject_id:
            return None
        if world_subject_content_sha256(event.to_dict()) != receipt.subject.content_sha256:
            return None
        return event

    def _published_envelope(self, observation_id: str) -> MacroObservationEnvelope | None:
        if not observation_id:
            return None
        for observation, receipt in self._iter_proven_observations():
            if observation.observation_id != observation_id:
                continue
            return self._observation_envelope(
                observation,
                receipt,
                first_seen_at=self._remember_receipt(receipt),
            )
        return None

    def _iter_joined_subjects(
        self, kind: str
    ) -> Iterator[tuple[dict[str, Any], WorldAvailabilityReceipt]]:
        grouped: dict[str, list[WorldAvailabilityReceipt]] = {}
        for receipt in self._iter_parsed_receipts():
            if receipt.subject.kind != kind:
                continue
            grouped.setdefault(receipt.subject.subject_id, []).append(receipt)
        for receipts in grouped.values():
            if len(receipts) > 1:
                raise ValueError("multiple availability receipts for the same subject")
            payload = self._history_payload_for(receipts[0])
            if payload is None:
                continue
            yield payload, receipts[0]

    def _history_payload_for(self, receipt: WorldAvailabilityReceipt) -> dict[str, Any] | None:
        locator = receipt.storage_locator
        if locator.kind != "jsonl" or locator.store_id != WORLD_MACRO_STORE_ID or not locator.path:
            return None
        history = self._resolve_under_root(locator.path)
        if history is None or not history.is_file():
            return None
        matches = [
            row
            for row in read_jsonl_objects(history)
            if world_subject_content_sha256(row) == receipt.subject.content_sha256
        ]
        if not matches:
            return None
        return dict(matches[0])

    def _unique_history_row(self, relative_dir: str, identity_field: str, identity: str) -> dict[str, Any] | None:
        found: dict[str, Any] | None = None
        for row in self._iter_history_rows(relative_dir):
            if str(row.get(identity_field) or "") != identity:
                continue
            if found is not None:
                raise ValueError(f"multiple history rows for the same {identity_field}")
            found = dict(row)
        return found

    def _iter_history_rows(self, relative_dir: str) -> Iterator[dict[str, Any]]:
        base = self._root / relative_dir
        if not base.exists():
            return
        for path in sorted(base.glob("????-??-??.jsonl")):
            if not path.is_file():
                continue
            for row in read_jsonl_objects(path):
                yield row

    def _event_partition_day(self, event: MacroCollectionEvent, payload: Mapping[str, Any]) -> str:
        if isinstance(event, MacroCollectionRegistered):
            return _utc_day(event.cutoff_at)
        cutoff_raw = payload.get("cutoff_at")
        if cutoff_raw:
            return _utc_day(parse_utc_timestamp(cutoff_raw, "cutoff_at"))
        for row in self._iter_history_rows("runs/events"):
            if row.get("event_type") != "macro_collection_registered":
                continue
            if str(row.get("run_id") or "") != event.run_id:
                continue
            return _utc_day(parse_utc_timestamp(row.get("cutoff_at"), "cutoff_at"))
        raise ValueError("collection event requires a registered run")

    def _iter_parsed_receipts(self) -> Iterator[WorldAvailabilityReceipt]:
        if not self._root.exists():
            return
        for path in sorted(self._root.rglob("*.jsonl")):
            if not path.is_file():
                continue
            for row in load_receipts(path):
                parsed = parse_world_availability_receipt(row)
                if parsed is None:
                    continue
                if parsed.storage_locator.store_id != WORLD_MACRO_STORE_ID:
                    continue
                yield parsed

    def _prime_existing_receipts(self) -> None:
        for receipt in self._iter_parsed_receipts():
            self._remember_receipt(receipt)

    def _remember_receipt(
        self,
        receipt: WorldAvailabilityReceipt,
        *,
        seen_at: datetime | None = None,
    ) -> datetime:
        key = _receipt_fingerprint(receipt)
        existing = self._first_seen_at.get(key)
        if existing is not None:
            return existing
        stamped = _utc(seen_at if seen_at is not None else self._clock(), field_name="clock")
        self._first_seen_at[key] = stamped
        return stamped

    def _resolve_under_root(self, relative: str) -> Path | None:
        text = str(relative or "").strip()
        if not text or ".." in text:
            return None
        candidate_rel = Path(text)
        if candidate_rel.is_absolute() or candidate_rel.anchor or ".." in candidate_rel.parts:
            return None
        root = self._root.resolve()
        candidate = (root / candidate_rel).resolve()
        if not candidate.is_relative_to(root):
            return None
        return candidate

    def _count_history(self, relative_dir: str) -> int:
        return sum(1 for _row in self._iter_history_rows(relative_dir))

    def _write_status(self) -> None:
        write_json_atomic(
            self._root / "status.json",
            {
                "schema_version": _STATUS_SCHEMA,
                "facts": self._count_history("facts"),
                "observations": self._count_history("observations"),
                "events": self._count_history("runs/events"),
            },
        )

    @contextmanager
    def _exclusive_lock(self):
        with self._thread_lock:
            self._root.mkdir(parents=True, exist_ok=True)
            lock_path = self._root / ".world-macro-store.lock"
            with lock_path.open("a+", encoding="utf-8") as handle:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


__all__ = [
    "MACRO_COLLECTION_EVENT_SUBJECT_KIND",
    "MACRO_SOURCE_FACT_SUBJECT_KIND",
    "WORLD_MACRO_STORE_ID",
    "WorldMacroStore",
]
