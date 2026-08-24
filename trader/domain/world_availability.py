"""Shared kernel for durable availability receipts and point-in-time eligibility.

``ready_at`` is store-assigned after fsync. ``from_mapping`` rehydrates a record
only; PIT evidence requires private store attestation after verified readback.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Generic, TypeVar

from trader.domain.world_episode import canonical_sha256, parse_utc_timestamp


WORLD_AVAILABILITY_RECEIPT_SCHEMA = "world_availability_receipt.v1"
STORAGE_KINDS = frozenset({"jsonl", "sqlite"})
ELIGIBILITY_STATUSES = frozenset({"eligible", "availability_unproven", "stale", "superseded"})
T = TypeVar("T")


def _required_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a non-empty string")
    text = value.strip()
    if not text:
        raise ValueError(f"{field_name} must be a non-empty string")
    return text


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def world_subject_content_sha256(payload: Mapping[str, Any]) -> str:
    """Domain-owned content hash for World availability subjects."""

    if not isinstance(payload, Mapping):
        raise TypeError("subject content payload must be a mapping")
    return canonical_sha256(payload)


@dataclass(frozen=True)
class WorldStorageLocator:
    """Canonical JSONL path or SQLite table/row identity, namespaced by a stable store_id."""

    kind: str
    store_id: str
    path: str | None = None
    table: str | None = None
    row_id: str | None = None

    def __post_init__(self) -> None:
        kind = _required_text(self.kind, "kind").lower()
        if kind not in STORAGE_KINDS:
            raise ValueError("storage locator kind must be jsonl or sqlite")
        store_id = _required_text(self.store_id, "store_id")
        path = None if self.path is None else _required_text(self.path, "path")
        table = None if self.table is None else _required_text(self.table, "table")
        row_id = None if self.row_id is None else _required_text(self.row_id, "row_id")
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "store_id", store_id)
        object.__setattr__(self, "path", path)
        object.__setattr__(self, "table", table)
        object.__setattr__(self, "row_id", row_id)
        if kind == "jsonl":
            if path is None or table is not None or row_id is not None:
                raise ValueError("jsonl locator requires store_id and path only")
            return
        if table is None or row_id is None or path is not None:
            raise ValueError("sqlite locator requires store_id, table, and row_id")

    def to_dict(self) -> dict[str, str]:
        if self.kind == "jsonl":
            return {"kind": "jsonl", "store_id": self.store_id, "path": str(self.path)}
        return {
            "kind": "sqlite",
            "store_id": self.store_id,
            "table": str(self.table),
            "row_id": str(self.row_id),
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | WorldStorageLocator) -> WorldStorageLocator:
        if isinstance(value, WorldStorageLocator):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("storage locator must be WorldStorageLocator or a mapping")
        return cls(
            kind=value.get("kind"),
            store_id=value.get("store_id"),
            path=value.get("path"),
            table=value.get("table"),
            row_id=value.get("row_id"),
        )


@dataclass(frozen=True)
class WorldAvailabilitySubjectRef:
    """Identity of the durable subject a receipt attests."""

    kind: str
    subject_id: str
    content_sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", _required_text(self.kind, "kind"))
        object.__setattr__(self, "subject_id", _required_text(self.subject_id, "subject_id"))
        object.__setattr__(self, "content_sha256", _required_text(self.content_sha256, "content_sha256"))

    def to_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "subject_id": self.subject_id, "content_sha256": self.content_sha256}

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | WorldAvailabilitySubjectRef) -> WorldAvailabilitySubjectRef:
        if isinstance(value, WorldAvailabilitySubjectRef):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("subject ref must be WorldAvailabilitySubjectRef or a mapping")
        return cls(
            kind=value.get("kind") or value.get("subject_kind"),
            subject_id=value.get("subject_id"),
            content_sha256=value.get("content_sha256"),
        )


def _receipt_identity_payload(
    *,
    schema_version: str,
    subject: WorldAvailabilitySubjectRef,
    scope: str,
    storage_locator: WorldStorageLocator,
) -> dict[str, Any]:
    return {
        "schema_version": schema_version,
        "subject_kind": subject.kind,
        "subject_id": subject.subject_id,
        "content_sha256": subject.content_sha256,
        "scope": scope,
        "storage_locator": storage_locator.to_dict(),
    }


def _receipt_id_for(identity: Mapping[str, Any]) -> str:
    return f"world-availability-receipt:v1:{canonical_sha256(identity)}"


def _receipt_hash_payload(identity: Mapping[str, Any], *, receipt_id: str, ready_at: datetime) -> dict[str, Any]:
    return {**identity, "receipt_id": receipt_id, "ready_at": _iso(ready_at)}


def _nested_mapping(value: Any, field_name: str) -> Mapping[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, Mapping):
        return value
    raise TypeError(f"{field_name} must be a mapping")


@dataclass(frozen=True)
class WorldAvailabilityReceipt:
    """Integrity record of an availability receipt. Not proof that store I/O occurred."""

    receipt_id: str
    subject: WorldAvailabilitySubjectRef
    scope: str
    storage_locator: WorldStorageLocator
    content_sha256: str
    schema_version: str
    ready_at: datetime | None = field(default=None, init=False)
    receipt_sha256: str | None = field(default=None, init=False)
    _store_attested: bool = field(default=False, init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        subject = (
            self.subject
            if isinstance(self.subject, WorldAvailabilitySubjectRef)
            else WorldAvailabilitySubjectRef.from_mapping(self.subject)
        )
        locator = (
            self.storage_locator
            if isinstance(self.storage_locator, WorldStorageLocator)
            else WorldStorageLocator.from_mapping(self.storage_locator)
        )
        schema_version = _required_text(self.schema_version, "schema_version")
        if schema_version != WORLD_AVAILABILITY_RECEIPT_SCHEMA:
            raise ValueError(f"world availability receipt schema_version must be {WORLD_AVAILABILITY_RECEIPT_SCHEMA}")
        content_sha256 = _required_text(self.content_sha256, "content_sha256")
        if content_sha256 != subject.content_sha256:
            raise ValueError("receipt content_sha256 must match the subject")
        object.__setattr__(self, "receipt_id", _required_text(self.receipt_id, "receipt_id"))
        object.__setattr__(self, "subject", subject)
        object.__setattr__(self, "scope", _required_text(self.scope, "scope"))
        object.__setattr__(self, "storage_locator", locator)
        object.__setattr__(self, "content_sha256", content_sha256)
        object.__setattr__(self, "schema_version", schema_version)

    def to_dict(self) -> dict[str, Any]:
        if self.ready_at is None or self.receipt_sha256 is None:
            raise ValueError("world availability receipt requires store-assigned ready_at")
        payload = _receipt_identity_payload(
            schema_version=self.schema_version,
            subject=self.subject,
            scope=self.scope,
            storage_locator=self.storage_locator,
        )
        payload["receipt_id"] = self.receipt_id
        payload["ready_at"] = _iso(self.ready_at)
        payload["receipt_sha256"] = self.receipt_sha256
        return payload

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | WorldAvailabilityReceipt) -> WorldAvailabilityReceipt:
        if isinstance(value, WorldAvailabilityReceipt):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("availability receipt must be WorldAvailabilityReceipt or a mapping")
        if value.get("ready_at") in (None, ""):
            raise ValueError("world availability receipt requires store-assigned ready_at")
        if value.get("receipt_id") in (None, ""):
            raise ValueError("receipt_id is required")
        if value.get("receipt_sha256") in (None, ""):
            raise ValueError("receipt_sha256 is required")
        ready_at = parse_utc_timestamp(value.get("ready_at"), "ready_at")
        nested_subject = _nested_mapping(value.get("subject"), "subject") or {}
        subject = WorldAvailabilitySubjectRef(
            kind=value.get("subject_kind") or nested_subject.get("kind"),
            subject_id=value.get("subject_id") or nested_subject.get("subject_id"),
            content_sha256=value.get("content_sha256") or nested_subject.get("content_sha256"),
        )
        locator_raw = value.get("storage_locator")
        locator = WorldStorageLocator.from_mapping({} if locator_raw is None else locator_raw)
        schema_version = _required_text(value.get("schema_version"), "schema_version")
        identity = _receipt_identity_payload(
            schema_version=schema_version,
            subject=subject,
            scope=_required_text(value.get("scope"), "scope"),
            storage_locator=locator,
        )
        receipt_id = _receipt_id_for(identity)
        if _required_text(value.get("receipt_id"), "receipt_id") != receipt_id:
            raise ValueError("receipt_id does not match the canonical receipt identity")
        digest = canonical_sha256(_receipt_hash_payload(identity, receipt_id=receipt_id, ready_at=ready_at))
        if _required_text(value.get("receipt_sha256"), "receipt_sha256") != digest:
            raise ValueError("receipt_sha256 does not match the canonical receipt")
        receipt = cls(
            receipt_id=receipt_id,
            subject=subject,
            scope=identity["scope"],
            storage_locator=locator,
            content_sha256=subject.content_sha256,
            schema_version=schema_version,
        )
        object.__setattr__(receipt, "ready_at", ready_at)
        object.__setattr__(receipt, "receipt_sha256", digest)
        return receipt


def _attest_verified_store_receipt(
    receipt: WorldAvailabilityReceipt,
    *,
    expected_subject: WorldAvailabilitySubjectRef,
    expected_scope: str,
    expected_locator: WorldStorageLocator,
) -> WorldAvailabilityReceipt:
    """Mark a World receipt as store-verified after subject+receipt readback. Not exported."""

    if not isinstance(receipt, WorldAvailabilityReceipt):
        raise TypeError("store attestation requires a WorldAvailabilityReceipt")
    if receipt.schema_version != WORLD_AVAILABILITY_RECEIPT_SCHEMA:
        raise ValueError(f"store attestation requires {WORLD_AVAILABILITY_RECEIPT_SCHEMA}")
    if receipt.ready_at is None or receipt.receipt_sha256 is None:
        raise ValueError("store attestation requires a store-stamped receipt")
    if receipt.subject != expected_subject:
        raise ValueError("store attestation subject mismatch")
    if receipt.scope != expected_scope:
        raise ValueError("store attestation scope mismatch")
    if receipt.storage_locator != expected_locator:
        raise ValueError("store attestation locator mismatch")
    object.__setattr__(receipt, "_store_attested", True)
    return receipt


def _require_store_attested_receipt(receipt: WorldAvailabilityReceipt, *, role: str) -> None:
    if not isinstance(receipt, WorldAvailabilityReceipt):
        raise TypeError(f"{role} requires a WorldAvailabilityReceipt")
    if not receipt._store_attested:
        raise ValueError(f"{role} requires a store-attested receipt")
    if receipt.ready_at is None:
        raise ValueError(f"{role} requires a store-stamped receipt")


@dataclass(frozen=True)
class AvailabilityEvidence:
    """Reader-bound receipt: ``effective_ready_at = max(ready_at, first_seen_at)``."""

    receipt: WorldAvailabilityReceipt
    first_seen_at: datetime | str
    effective_ready_at: datetime = field(init=False)

    def __post_init__(self) -> None:
        _require_store_attested_receipt(self.receipt, role="availability evidence")
        first_seen_at = parse_utc_timestamp(self.first_seen_at, "first_seen_at")
        object.__setattr__(self, "first_seen_at", first_seen_at)
        object.__setattr__(self, "effective_ready_at", max(self.receipt.ready_at, first_seen_at))


@dataclass(frozen=True)
class PersistedWorldRef(Generic[T]):
    """Repository handle returned only after the availability receipt is durable."""

    identity: T
    receipt: WorldAvailabilityReceipt

    def __post_init__(self) -> None:
        _require_store_attested_receipt(self.receipt, role="PersistedWorldRef")


@dataclass(frozen=True)
class PointInTimeEligibility:
    status: str
    effective_ready_at: datetime | None = None

    def __post_init__(self) -> None:
        status = _required_text(self.status, "status")
        if status not in ELIGIBILITY_STATUSES:
            allowed = ", ".join(sorted(ELIGIBILITY_STATUSES))
            raise ValueError(f"eligibility status must be one of: {allowed}")
        object.__setattr__(self, "status", status)


@dataclass(frozen=True)
class PointInTimeEligibilityPolicy:
    """Domain PIT gate. Infrastructure does not decide causal admissibility."""

    admitted_versions: frozenset[str] | None = None

    def __post_init__(self) -> None:
        if self.admitted_versions is None:
            return
        if isinstance(self.admitted_versions, (str, bytes, bytearray)):
            raise TypeError("admitted_versions must be a set of version ids")
        object.__setattr__(self, "admitted_versions", frozenset(self.admitted_versions))

    def evaluate(
        self,
        *,
        evidence: AvailabilityEvidence | None,
        cutoff_at: datetime | str,
        valid_until: datetime | str | None = None,
        superseded: bool = False,
        version: str | None = None,
    ) -> PointInTimeEligibility:
        cutoff = parse_utc_timestamp(cutoff_at, "cutoff_at")
        if evidence is None:
            return PointInTimeEligibility(status="availability_unproven")
        if not isinstance(evidence, AvailabilityEvidence):
            raise TypeError("point-in-time policy requires AvailabilityEvidence")
        if not evidence.receipt._store_attested or evidence.receipt.ready_at is None:
            return PointInTimeEligibility(status="availability_unproven")
        effective = evidence.effective_ready_at
        if effective > cutoff:
            return PointInTimeEligibility(status="availability_unproven", effective_ready_at=effective)
        if self.admitted_versions is not None and (
            version is None or _required_text(version, "version") not in self.admitted_versions
        ):
            return PointInTimeEligibility(status="availability_unproven", effective_ready_at=effective)
        if superseded:
            return PointInTimeEligibility(status="superseded", effective_ready_at=effective)
        until = None if valid_until is None else parse_utc_timestamp(valid_until, "valid_until")
        if until is not None and cutoff >= until:
            return PointInTimeEligibility(status="stale", effective_ready_at=effective)
        return PointInTimeEligibility(status="eligible", effective_ready_at=effective)

    def is_eligible(
        self,
        *,
        evidence: AvailabilityEvidence | None,
        cutoff_at: datetime | str,
        valid_until: datetime | str | None = None,
        superseded: bool = False,
        version: str | None = None,
    ) -> bool:
        return (
            self.evaluate(
                evidence=evidence,
                cutoff_at=cutoff_at,
                valid_until=valid_until,
                superseded=superseded,
                version=version,
            ).status
            == "eligible"
        )


__all__ = [
    "AvailabilityEvidence",
    "ELIGIBILITY_STATUSES",
    "PersistedWorldRef",
    "PointInTimeEligibility",
    "PointInTimeEligibilityPolicy",
    "WORLD_AVAILABILITY_RECEIPT_SCHEMA",
    "WorldAvailabilityReceipt",
    "WorldAvailabilitySubjectRef",
    "WorldStorageLocator",
    "world_subject_content_sha256",
]
