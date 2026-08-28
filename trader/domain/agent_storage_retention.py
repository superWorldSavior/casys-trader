"""Lifecycle objects for agent-generated storage retention.

These objects describe what may be archived. They deliberately carry no
filesystem traversal or deletion behavior: adapters must prove inactivity,
immutability and archive integrity before acting on a declared source.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path, PurePosixPath


class AgentStorageCategory(StrEnum):
    SESSIONS = "sessions"
    LOGS = "logs"
    TRACES = "traces"
    SNAPSHOTS = "snapshots"


class AgentStorageLayout(StrEnum):
    ACPX_SESSIONS = "acpx_sessions"
    RECURSIVE_FILES = "recursive_files"
    GROK_SESSIONS = "grok_sessions"


def _source_id(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or "/" in value or "\\" in value:
        raise ValueError("source_id must be a non-empty basename")
    return value.strip()


def is_lexically_confined(path: Path, root: Path) -> bool:
    """Return True when ``path`` is the authority root or a descendant, without following links."""

    candidate = Path(os.path.normpath(os.path.abspath(path)))
    authority = Path(os.path.normpath(os.path.abspath(root)))
    try:
        candidate.relative_to(authority)
    except ValueError:
        return False
    return True


@dataclass(frozen=True)
class ArchiveUnit:
    """One indivisible historical object and its stable archive identity."""

    path: Path
    arcname: str
    recorded_at: datetime
    size: int
    identity: str | None = None
    fingerprint: str | None = None
    guard_root: Path | None = None
    active_registry: Path | None = None
    closure_record: Path | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.arcname, str):
            raise TypeError("arcname must be a string")
        relative = PurePosixPath(self.arcname)
        if not self.arcname or relative.is_absolute() or ".." in relative.parts:
            raise ValueError("arcname must be a safe relative path")
        if not isinstance(self.recorded_at, datetime) or self.recorded_at.tzinfo is None:
            raise ValueError("recorded_at must be timezone-aware")
        if isinstance(self.size, bool) or not isinstance(self.size, int) or self.size < 0:
            raise ValueError("size must be a non-negative integer")
        object.__setattr__(self, "path", Path(self.path))
        object.__setattr__(self, "arcname", str(relative))
        object.__setattr__(self, "recorded_at", self.recorded_at.astimezone(timezone.utc))
        for field in ("guard_root", "active_registry", "closure_record"):
            value = getattr(self, field)
            if value is not None:
                object.__setattr__(self, field, Path(value))


@dataclass(frozen=True)
class ArchiveSource:
    """Declared lifecycle policy for one generated subtree."""

    source_id: str
    root: Path
    category: AgentStorageCategory | str
    layout: AgentStorageLayout | str
    apply_enabled: bool = True
    protection_reason: str | None = None
    authority_root: Path | None = None
    allows_external_root: bool = False

    def __post_init__(self) -> None:
        source_id = _source_id(self.source_id)
        category = AgentStorageCategory(self.category)
        layout = AgentStorageLayout(self.layout)
        if layout in {AgentStorageLayout.ACPX_SESSIONS, AgentStorageLayout.GROK_SESSIONS}:
            if category is not AgentStorageCategory.SESSIONS:
                raise ValueError(f"{layout.value} layout requires the sessions category")
        reason = self.protection_reason.strip() if isinstance(self.protection_reason, str) else None
        if not self.apply_enabled and not reason:
            raise ValueError("a report-only source requires protection_reason")
        if self.allows_external_root and source_id != "acpx":
            raise ValueError("only the ACPX source may declare an external root")
        root = Path(self.root)
        authority = Path(self.authority_root) if self.authority_root is not None else root
        if not is_lexically_confined(root, authority):
            raise ValueError("root escapes authority_root")
        object.__setattr__(self, "source_id", source_id)
        object.__setattr__(self, "root", root)
        object.__setattr__(self, "category", category)
        object.__setattr__(self, "layout", layout)
        object.__setattr__(self, "protection_reason", reason)
        object.__setattr__(self, "authority_root", authority)
        object.__setattr__(self, "allows_external_root", bool(self.allows_external_root))


PENDING_RECONCILIATION_SCHEMA = "agent_storage_pending_reconciliation.v1"


class RetentionProvider(StrEnum):
    ACPX = "acpx"
    GROK = "grok"


def acpx_stream_lock_is_recoverable(*, pid: int | None, owner_alive: bool | None) -> bool:
    """Return True only when an O_EXCL stream lock records a pid proven dead.

    ACPX and retention both use exclusive-create files. A live owner, a missing
    pid, or an unproven liveness check stays fail-closed: ACPX does not flock,
    so file presence plus a live pid is the ownership proof. A dead pid is the
    crash-recovery signal; PID reuse is treated as live (fail closed).
    """

    if pid is None or owner_alive is None:
        return False
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return False
    return owner_alive is False


def provider_for_source_id(source_id: str) -> RetentionProvider:
    if source_id == "acpx":
        return RetentionProvider.ACPX
    if source_id in {"grok-home", "grok-home-medium"}:
        return RetentionProvider.GROK
    raise ValueError(f"source_id {source_id!r} has no metadata-reconciliation provider")


def _identity(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or "/" in value or "\\" in value or "\x00" in value:
        raise ValueError("identity must be a non-empty basename")
    return value.strip()


@dataclass(frozen=True)
class PendingReconciliationEntry:
    """Identities whose sources were committed to archive but whose metadata is still live."""

    source_id: str
    provider: RetentionProvider | str
    identities: tuple[str, ...]

    def __post_init__(self) -> None:
        source_id = _source_id(self.source_id)
        provider = RetentionProvider(self.provider)
        if provider_for_source_id(source_id) is not provider:
            raise ValueError("source_id does not match provider")
        identities = tuple(sorted({_identity(item) for item in self.identities}))
        if not identities:
            raise ValueError("identities must be non-empty")
        object.__setattr__(self, "source_id", source_id)
        object.__setattr__(self, "provider", provider)
        object.__setattr__(self, "identities", identities)


@dataclass(frozen=True)
class PendingReconciliationJournal:
    """Durable, replayable set of metadata identities waiting for provider reconciliation."""

    schema_version: str = PENDING_RECONCILIATION_SCHEMA
    entries: tuple[PendingReconciliationEntry, ...] = ()
    updated_at: datetime | None = None
    last_error: str | None = None

    def __post_init__(self) -> None:
        if self.schema_version != PENDING_RECONCILIATION_SCHEMA:
            raise ValueError("schema_version must be agent_storage_pending_reconciliation.v1")
        entries = tuple(
            item if isinstance(item, PendingReconciliationEntry) else PendingReconciliationEntry(**item)
            for item in self.entries
        )
        by_source = {entry.source_id: entry for entry in entries}
        if len(by_source) != len(entries):
            raise ValueError("pending reconciliation entries must be unique per source_id")
        stamped = self.updated_at
        if stamped is not None:
            if not isinstance(stamped, datetime) or stamped.tzinfo is None:
                raise ValueError("updated_at must be timezone-aware")
            stamped = stamped.astimezone(timezone.utc)
        error = self.last_error
        if error is not None:
            if not isinstance(error, str) or not error.strip():
                raise ValueError("last_error must be a non-empty string")
            error = error.strip()
        object.__setattr__(self, "entries", tuple(by_source[key] for key in sorted(by_source)))
        object.__setattr__(self, "updated_at", stamped)
        object.__setattr__(self, "last_error", error)

    def identities_for(self, source_id: str) -> frozenset[str]:
        for entry in self.entries:
            if entry.source_id == source_id:
                return frozenset(entry.identities)
        return frozenset()

    def merge(self, source_id: str, identities: Iterable[str]) -> PendingReconciliationJournal:
        incoming = {item for item in identities if item}
        if not incoming:
            return self
        entry = PendingReconciliationEntry(
            source_id=source_id,
            provider=provider_for_source_id(source_id),
            identities=tuple(incoming | set(self.identities_for(source_id))),
        )
        others = tuple(item for item in self.entries if item.source_id != source_id)
        return replace(self, entries=(*others, entry), updated_at=datetime.now(timezone.utc))

    def without_identities(self, source_id: str, identities: Iterable[str]) -> PendingReconciliationJournal:
        drop = set(identities)
        remaining: list[PendingReconciliationEntry] = []
        for entry in self.entries:
            if entry.source_id != source_id:
                remaining.append(entry)
                continue
            kept = tuple(item for item in entry.identities if item not in drop)
            if kept:
                remaining.append(replace(entry, identities=kept))
        return replace(
            self,
            entries=tuple(remaining),
            last_error=None,
            updated_at=datetime.now(timezone.utc),
        )

    def with_error(self, message: str) -> PendingReconciliationJournal:
        return replace(self, last_error=message, updated_at=datetime.now(timezone.utc))

    def to_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "updated_at": None if self.updated_at is None else self.updated_at.isoformat(),
            "last_error": self.last_error,
            "entries": [
                {
                    "source_id": entry.source_id,
                    "provider": entry.provider.value,
                    "identities": list(entry.identities),
                }
                for entry in self.entries
            ],
        }

    @classmethod
    def from_payload(cls, payload: object) -> PendingReconciliationJournal:
        if not isinstance(payload, dict):
            raise ValueError("pending reconciliation payload must be an object")
        schema = payload.get("schema_version")
        if schema != PENDING_RECONCILIATION_SCHEMA:
            raise ValueError("schema_version must be agent_storage_pending_reconciliation.v1")
        raw_entries = payload.get("entries", [])
        if not isinstance(raw_entries, list):
            raise ValueError("entries must be a list")
        entries = []
        for item in raw_entries:
            if not isinstance(item, dict):
                raise ValueError("pending reconciliation entry must be an object")
            entries.append(
                PendingReconciliationEntry(
                    source_id=str(item.get("source_id", "")),
                    provider=str(item.get("provider", "")),
                    identities=tuple(item.get("identities") or ()),
                )
            )
        updated_at = payload.get("updated_at")
        stamped = None
        if isinstance(updated_at, str) and updated_at.strip():
            stamped = datetime.fromisoformat(updated_at.strip().replace("Z", "+00:00"))
        last_error = payload.get("last_error")
        return cls(
            schema_version=PENDING_RECONCILIATION_SCHEMA,
            entries=tuple(entries),
            updated_at=stamped,
            last_error=last_error if isinstance(last_error, str) else None,
        )


QUARANTINE_DIR_NAME = ".retention-quarantine"
QUARANTINE_INTENT_DIR_NAME = ".intent"
QUARANTINE_INTENT_SCHEMA = "agent_storage_quarantine_intent.v1"


class QuarantineRecoveryKind(StrEnum):
    RESTORE = "restore"
    FINALIZE = "finalize"
    DISCARD = "discard"
    FAIL_CLOSED = "fail_closed"


class QuarantineUnitAction(StrEnum):
    RESTORE = "restore"
    FINALIZE = "finalize"
    KEEP_LIVE = "keep_live"
    FAIL_CLOSED = "fail_closed"


def _relative_under(path: Path, root: Path) -> Path:
    if not is_lexically_confined(path, root):
        raise ValueError("path escapes guard_root")
    candidate = Path(os.path.normpath(os.path.abspath(path)))
    authority = Path(os.path.normpath(os.path.abspath(root)))
    return candidate.relative_to(authority)


def planned_quarantine_path(live_path: Path, guard_root: Path) -> Path:
    """Return the hidden quarantine location for a live path under ``guard_root``."""

    relative = _relative_under(live_path, guard_root)
    return Path(os.path.normpath(os.path.abspath(guard_root))) / QUARANTINE_DIR_NAME / relative


def quarantine_root(guard_root: Path) -> Path:
    return Path(os.path.normpath(os.path.abspath(guard_root))) / QUARANTINE_DIR_NAME


def quarantine_intent_dir(guard_root: Path) -> Path:
    return quarantine_root(guard_root) / QUARANTINE_INTENT_DIR_NAME


def _transaction_id(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or "/" in value or "\\" in value or "\x00" in value:
        raise ValueError("transaction_id must be a non-empty basename")
    rendered = value.strip()
    if rendered.startswith("."):
        raise ValueError("transaction_id must be a non-empty basename")
    return rendered


def _fingerprint(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ValueError("fingerprint must be a non-empty string")
    return value.strip()


def _archive_sha256(value: object) -> str:
    if not isinstance(value, str) or not value.startswith("sha256:"):
        raise ValueError("published archive sha256 must be a sha256 digest")
    digest = value.removeprefix("sha256:")
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise ValueError("published archive sha256 must be a sha256 digest")
    return value


def _optional_path(value: object) -> Path | None:
    if value is None:
        return None
    if not isinstance(value, (str, Path)) or (isinstance(value, str) and not value.strip()):
        raise ValueError("path must be a filesystem location")
    return Path(value)


def _required_path(value: object, field: str) -> Path:
    path = _optional_path(value)
    if path is None:
        raise ValueError(f"{field} must be a filesystem location")
    return path


def _utc_datetime(value: object, field: str) -> datetime:
    if isinstance(value, str) and value.strip():
        try:
            value = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"{field} must be timezone-aware") from exc
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(timezone.utc)


@dataclass(frozen=True)
class QuarantineUnitIntent:
    """Planned live and quarantine locations for one archive unit."""

    live_path: Path
    quarantine_path: Path
    guard_root: Path
    fingerprint: str
    arcname: str
    size: int
    recorded_at: datetime
    identity: str | None = None
    active_registry: Path | None = None
    closure_record: Path | None = None

    def __post_init__(self) -> None:
        guard = Path(os.path.normpath(os.path.abspath(self.guard_root)))
        live = Path(self.live_path)
        quarantine = Path(self.quarantine_path)
        expected = planned_quarantine_path(live, guard)
        if Path(os.path.normpath(os.path.abspath(quarantine))) != expected:
            raise ValueError("quarantine path must be the planned location under the hidden quarantine tree")
        relative = PurePosixPath(self.arcname)
        if not self.arcname or relative.is_absolute() or ".." in relative.parts:
            raise ValueError("arcname must be a safe relative path")
        if isinstance(self.size, bool) or not isinstance(self.size, int) or self.size < 0:
            raise ValueError("size must be a non-negative integer")
        identity = None if self.identity is None else _identity(self.identity)
        object.__setattr__(self, "live_path", Path(os.path.normpath(os.path.abspath(live))))
        object.__setattr__(self, "quarantine_path", expected)
        object.__setattr__(self, "guard_root", guard)
        object.__setattr__(self, "fingerprint", _fingerprint(self.fingerprint))
        object.__setattr__(self, "arcname", str(relative))
        object.__setattr__(self, "recorded_at", _utc_datetime(self.recorded_at, "recorded_at"))
        object.__setattr__(self, "identity", identity)
        for field in ("active_registry", "closure_record"):
            value = getattr(self, field)
            if value is not None:
                object.__setattr__(self, field, Path(value))

    @classmethod
    def from_archive_unit(cls, unit: ArchiveUnit) -> QuarantineUnitIntent:
        if unit.guard_root is None:
            raise ValueError("archive unit is missing guard_root")
        if unit.fingerprint is None:
            raise ValueError("archive unit is missing fingerprint")
        return cls(
            live_path=_live_path_for_intent(unit),
            quarantine_path=planned_quarantine_path(_live_path_for_intent(unit), unit.guard_root),
            guard_root=unit.guard_root,
            fingerprint=unit.fingerprint,
            arcname=unit.arcname,
            size=unit.size,
            recorded_at=unit.recorded_at,
            identity=unit.identity,
            active_registry=unit.active_registry,
            closure_record=unit.closure_record,
        )

    def to_payload(self) -> dict[str, object]:
        return {
            "live_path": str(self.live_path),
            "quarantine_path": str(self.quarantine_path),
            "guard_root": str(self.guard_root),
            "fingerprint": self.fingerprint,
            "arcname": self.arcname,
            "size": self.size,
            "recorded_at": self.recorded_at.isoformat(),
            "identity": self.identity,
            "active_registry": None if self.active_registry is None else str(self.active_registry),
            "closure_record": None if self.closure_record is None else str(self.closure_record),
        }

    @classmethod
    def from_payload(cls, payload: object) -> QuarantineUnitIntent:
        if not isinstance(payload, dict):
            raise ValueError("quarantine unit intent must be an object")
        return cls(
            live_path=_required_path(payload.get("live_path"), "live_path"),
            quarantine_path=_required_path(payload.get("quarantine_path"), "quarantine_path"),
            guard_root=_required_path(payload.get("guard_root"), "guard_root"),
            fingerprint=str(payload.get("fingerprint", "")),
            arcname=str(payload.get("arcname", "")),
            size=payload.get("size", -1),
            recorded_at=payload.get("recorded_at"),
            identity=payload.get("identity"),
            active_registry=_optional_path(payload.get("active_registry")),
            closure_record=_optional_path(payload.get("closure_record")),
        )

    def as_archive_unit(self, *, quarantined: bool = True) -> ArchiveUnit:
        return ArchiveUnit(
            path=self.quarantine_path if quarantined else self.live_path,
            arcname=self.arcname,
            recorded_at=self.recorded_at,
            size=self.size,
            identity=self.identity,
            fingerprint=self.fingerprint,
            guard_root=self.guard_root,
            active_registry=self.active_registry,
            closure_record=self.closure_record,
        )


def _live_path_for_intent(unit: ArchiveUnit) -> Path:
    if unit.guard_root is None:
        return unit.path
    quarantine = quarantine_root(unit.guard_root)
    try:
        relative = _relative_under(unit.path, quarantine)
    except ValueError:
        return unit.path
    return Path(os.path.normpath(os.path.abspath(unit.guard_root))) / relative


@dataclass(frozen=True)
class QuarantineDeletionAuthority:
    """Proof that missing sources may be finalized rather than restored."""

    archive_verified: bool
    published_archive: Path | None
    published_archive_sha256: str | None = None
    journaled_identities: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        published = None if self.published_archive is None else Path(self.published_archive)
        digest = None if self.published_archive_sha256 is None else _archive_sha256(self.published_archive_sha256)
        verified = bool(self.archive_verified)
        if verified != (published is not None and digest is not None):
            raise ValueError("a verified archive must record the published path and sha256")
        object.__setattr__(self, "archive_verified", verified)
        object.__setattr__(self, "published_archive", published)
        object.__setattr__(self, "published_archive_sha256", digest)
        object.__setattr__(self, "journaled_identities", frozenset(self.journaled_identities))

    @classmethod
    def none(cls) -> QuarantineDeletionAuthority:
        return cls(
            archive_verified=False,
            published_archive=None,
            published_archive_sha256=None,
            journaled_identities=frozenset(),
        )

    def covers(self, identities: Iterable[str]) -> bool:
        if not self.archive_verified or self.published_archive is None or self.published_archive_sha256 is None:
            return False
        required = frozenset(item for item in identities if item)
        return required <= self.journaled_identities


@dataclass(frozen=True)
class ObservedQuarantineUnit:
    """Filesystem observation for one planned unit. Presence is observed, never inferred from phase."""

    unit: QuarantineUnitIntent
    live_exists: bool
    quarantine_exists: bool
    quarantine_matches_intent: bool = True


@dataclass(frozen=True)
class TransactionRecoveryPlan:
    kind: QuarantineRecoveryKind
    reason: str
    restore: tuple[QuarantineUnitIntent, ...] = ()
    finalize: tuple[QuarantineUnitIntent, ...] = ()


@dataclass(frozen=True)
class QuarantineTransactionIntent:
    """Durable, typed intent recorded before the first live-to-quarantine rename."""

    transaction_id: str
    units: tuple[QuarantineUnitIntent, ...]
    schema_version: str = QUARANTINE_INTENT_SCHEMA
    source_id: str | None = None
    created_at: datetime | None = None
    destination: Path | None = None
    published_archive: Path | None = None
    published_archive_sha256: str | None = None
    archive_verified: bool = False

    def __post_init__(self) -> None:
        if self.schema_version != QUARANTINE_INTENT_SCHEMA:
            raise ValueError("schema_version must be agent_storage_quarantine_intent.v1")
        units = tuple(
            item if isinstance(item, QuarantineUnitIntent) else QuarantineUnitIntent.from_payload(item)
            for item in self.units
        )
        if not units:
            raise ValueError("quarantine intent units must be non-empty")
        live_paths = {unit.live_path for unit in units}
        if len(live_paths) != len(units):
            raise ValueError("quarantine intent units must be unique per live_path")
        identities = tuple(unit.identity for unit in units if unit.identity is not None)
        source_id = None if self.source_id is None else _source_id(self.source_id)
        if identities and source_id is None:
            raise ValueError("source_id is required when units have identities")
        if source_id is not None and identities:
            provider_for_source_id(source_id)
        stamped = None if self.created_at is None else _utc_datetime(self.created_at, "created_at")
        published = _optional_path(self.published_archive)
        digest = None if self.published_archive_sha256 is None else _archive_sha256(self.published_archive_sha256)
        verified = bool(self.archive_verified)
        if verified != (published is not None and digest is not None):
            raise ValueError("a verified intent must record the published archive path and sha256")
        object.__setattr__(self, "transaction_id", _transaction_id(self.transaction_id))
        object.__setattr__(self, "units", units)
        object.__setattr__(self, "source_id", source_id)
        object.__setattr__(self, "created_at", stamped)
        object.__setattr__(self, "destination", _optional_path(self.destination))
        object.__setattr__(self, "published_archive", published)
        object.__setattr__(self, "published_archive_sha256", digest)
        object.__setattr__(self, "archive_verified", verified)

    def identities(self) -> frozenset[str]:
        return frozenset(unit.identity for unit in self.units if unit.identity is not None)

    def guard_roots(self) -> tuple[Path, ...]:
        return tuple(dict.fromkeys(unit.guard_root for unit in self.units))

    def deletion_authority(self, journal: PendingReconciliationJournal) -> QuarantineDeletionAuthority:
        journaled = journal.identities_for(self.source_id) if self.source_id is not None else frozenset()
        return QuarantineDeletionAuthority(
            archive_verified=self.archive_verified,
            published_archive=self.published_archive,
            published_archive_sha256=self.published_archive_sha256,
            journaled_identities=journaled,
        )

    def with_units(self, units: Iterable[QuarantineUnitIntent]) -> QuarantineTransactionIntent:
        return replace(self, units=tuple(units))

    def with_verified_archive(self, published: Path, sha256: str) -> QuarantineTransactionIntent:
        return replace(
            self,
            published_archive=Path(published),
            published_archive_sha256=_archive_sha256(sha256),
            archive_verified=True,
        )

    def to_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "transaction_id": self.transaction_id,
            "source_id": self.source_id,
            "created_at": None if self.created_at is None else self.created_at.isoformat(),
            "destination": None if self.destination is None else str(self.destination),
            "published_archive": None if self.published_archive is None else str(self.published_archive),
            "published_archive_sha256": self.published_archive_sha256,
            "archive_verified": self.archive_verified,
            "units": [unit.to_payload() for unit in self.units],
        }

    @classmethod
    def from_payload(cls, payload: object) -> QuarantineTransactionIntent:
        if not isinstance(payload, dict):
            raise ValueError("quarantine intent payload must be an object")
        schema = payload.get("schema_version")
        if schema != QUARANTINE_INTENT_SCHEMA:
            raise ValueError("schema_version must be agent_storage_quarantine_intent.v1")
        raw_units = payload.get("units", [])
        if not isinstance(raw_units, list):
            raise ValueError("units must be a list")
        return cls(
            schema_version=QUARANTINE_INTENT_SCHEMA,
            transaction_id=str(payload.get("transaction_id", "")),
            source_id=payload.get("source_id"),
            created_at=payload.get("created_at"),
            destination=_optional_path(payload.get("destination")),
            published_archive=_optional_path(payload.get("published_archive")),
            published_archive_sha256=payload.get("published_archive_sha256"),
            archive_verified=bool(payload.get("archive_verified", False)),
            units=tuple(QuarantineUnitIntent.from_payload(item) for item in raw_units),
        )

    @classmethod
    def from_archive_units(
        cls,
        units: Iterable[ArchiveUnit],
        *,
        transaction_id: str,
        source_id: str | None = None,
        destination: Path | None = None,
        created_at: datetime | None = None,
    ) -> QuarantineTransactionIntent:
        return cls(
            transaction_id=transaction_id,
            source_id=source_id,
            created_at=created_at if created_at is not None else datetime.now(timezone.utc),
            destination=destination,
            units=tuple(QuarantineUnitIntent.from_archive_unit(unit) for unit in units),
        )


def decide_unit_recovery(
    observation: ObservedQuarantineUnit,
    authority: QuarantineDeletionAuthority,
) -> QuarantineUnitAction:
    unit = observation.unit
    if observation.live_exists and observation.quarantine_exists:
        return QuarantineUnitAction.FAIL_CLOSED
    if observation.quarantine_exists and not observation.quarantine_matches_intent:
        return QuarantineUnitAction.FAIL_CLOSED
    identity = unit.identity
    identities = frozenset({identity} if identity else ())
    if observation.live_exists:
        if identity is not None and identity in authority.journaled_identities:
            return QuarantineUnitAction.FAIL_CLOSED
        return QuarantineUnitAction.KEEP_LIVE
    if authority.covers(identities):
        return QuarantineUnitAction.FINALIZE
    if identity is not None and identity in authority.journaled_identities:
        return QuarantineUnitAction.FAIL_CLOSED
    if observation.quarantine_exists:
        return QuarantineUnitAction.RESTORE
    return QuarantineUnitAction.FAIL_CLOSED


def decide_transaction_recovery(
    intent: QuarantineTransactionIntent,
    observations: Iterable[ObservedQuarantineUnit],
    authority: QuarantineDeletionAuthority,
) -> TransactionRecoveryPlan:
    observed = tuple(observations)
    by_live = {item.unit.live_path: item for item in observed}
    if len(by_live) != len(intent.units) or any(unit.live_path not in by_live for unit in intent.units):
        return TransactionRecoveryPlan(
            kind=QuarantineRecoveryKind.FAIL_CLOSED,
            reason="recovery observations do not match the durable intent",
        )
    actions = {unit.live_path: decide_unit_recovery(by_live[unit.live_path], authority) for unit in intent.units}
    failed = next(
        (
            (unit, actions[unit.live_path])
            for unit in intent.units
            if actions[unit.live_path] is QuarantineUnitAction.FAIL_CLOSED
        ),
        None,
    )
    if failed is not None:
        unit, _action = failed
        observation = by_live[unit.live_path]
        if observation.live_exists and observation.quarantine_exists:
            reason = "live and quarantine copies both exist"
        elif observation.quarantine_exists and not observation.quarantine_matches_intent:
            reason = "quarantine fingerprint does not match the durable intent"
        elif observation.live_exists:
            reason = "refusing metadata reconciliation for a live session"
        elif unit.identity is not None and unit.identity in authority.journaled_identities:
            reason = "journaled identity lacks verified archive authority"
        else:
            reason = "source missing without deletion authority"
        return TransactionRecoveryPlan(kind=QuarantineRecoveryKind.FAIL_CLOSED, reason=reason)

    restore = tuple(unit for unit in intent.units if actions[unit.live_path] is QuarantineUnitAction.RESTORE)
    finalize = tuple(unit for unit in intent.units if actions[unit.live_path] is QuarantineUnitAction.FINALIZE)
    if restore and finalize:
        return TransactionRecoveryPlan(
            kind=QuarantineRecoveryKind.FAIL_CLOSED,
            reason="transaction mixes restore and finalize",
        )
    if restore:
        return TransactionRecoveryPlan(
            kind=QuarantineRecoveryKind.RESTORE,
            reason="no deletion authority; restore quarantined sources to live",
            restore=restore,
        )
    if finalize:
        return TransactionRecoveryPlan(
            kind=QuarantineRecoveryKind.FINALIZE,
            reason="verified archive and journal prove deletion authority",
            finalize=finalize,
        )
    return TransactionRecoveryPlan(
        kind=QuarantineRecoveryKind.DISCARD,
        reason="every planned source is already live",
    )


SESSION_DOCS_VACUUM_JOURNAL_SCHEMA = "agent_storage_session_docs_vacuum.v1"


def session_docs_vacuum_journal_path(sqlite_path: Path) -> Path:
    """Sidecar journal next to the SQLite file; never a path computed from a relative escape."""

    path = Path(sqlite_path)
    name = path.name
    if not name or name in {".", ".."} or "/" in name or "\\" in name or "\x00" in name:
        raise ValueError("sqlite path must have a basename")
    return path.with_name(f"{name}.vacuum-journal")


@dataclass(frozen=True)
class SessionDocsVacuumJournal:
    """Durable intent to DELETE archived identities then VACUUM session_docs.

    Written before the first mutation. An interrupted run leaves this sidecar so
    the next apply can replay DELETE (idempotent) and VACUUM even when no rows
    still match. Dropped only after VACUUM succeeds.
    """

    sqlite_name: str
    identities: tuple[str, ...]
    created_at: datetime
    schema_version: str = SESSION_DOCS_VACUUM_JOURNAL_SCHEMA

    def __post_init__(self) -> None:
        if self.schema_version != SESSION_DOCS_VACUUM_JOURNAL_SCHEMA:
            raise ValueError("schema_version must be agent_storage_session_docs_vacuum.v1")
        name = self.sqlite_name.strip() if isinstance(self.sqlite_name, str) else ""
        if not name or "/" in name or "\\" in name or name in {".", ".."} or "\x00" in name:
            raise ValueError("sqlite_name must be a basename")
        identities = tuple(sorted({_identity(item) for item in self.identities}))
        if not identities:
            raise ValueError("identities must be non-empty")
        object.__setattr__(self, "sqlite_name", name)
        object.__setattr__(self, "identities", identities)
        object.__setattr__(self, "created_at", _utc_datetime(self.created_at, "created_at"))

    def to_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "sqlite_name": self.sqlite_name,
            "identities": list(self.identities),
            "created_at": self.created_at.isoformat(),
        }

    @classmethod
    def from_payload(cls, payload: object) -> SessionDocsVacuumJournal:
        if not isinstance(payload, dict):
            raise ValueError("session_docs vacuum journal must be an object")
        raw_identities = payload.get("identities", [])
        if not isinstance(raw_identities, list):
            raise ValueError("identities must be a list")
        return cls(
            schema_version=str(payload.get("schema_version") or ""),
            sqlite_name=str(payload.get("sqlite_name") or ""),
            identities=tuple(raw_identities),
            created_at=payload.get("created_at"),  # type: ignore[arg-type]
        )


@dataclass(frozen=True)
class PurgeSource:
    """Explicitly regenerable files; reusable configuration never belongs here."""

    source_id: str
    root: Path
    authority_root: Path | None = None

    def __post_init__(self) -> None:
        root = Path(self.root)
        authority = Path(self.authority_root) if self.authority_root is not None else root
        if not is_lexically_confined(root, authority):
            raise ValueError("root escapes authority_root")
        object.__setattr__(self, "source_id", _source_id(self.source_id))
        object.__setattr__(self, "root", root)
        object.__setattr__(self, "authority_root", authority)


__all__ = [
    "AgentStorageCategory",
    "AgentStorageLayout",
    "ArchiveSource",
    "ArchiveUnit",
    "ObservedQuarantineUnit",
    "PENDING_RECONCILIATION_SCHEMA",
    "PendingReconciliationEntry",
    "PendingReconciliationJournal",
    "PurgeSource",
    "QUARANTINE_DIR_NAME",
    "QUARANTINE_INTENT_DIR_NAME",
    "QUARANTINE_INTENT_SCHEMA",
    "QuarantineDeletionAuthority",
    "QuarantineRecoveryKind",
    "QuarantineTransactionIntent",
    "QuarantineUnitAction",
    "QuarantineUnitIntent",
    "RetentionProvider",
    "SESSION_DOCS_VACUUM_JOURNAL_SCHEMA",
    "SessionDocsVacuumJournal",
    "TransactionRecoveryPlan",
    "acpx_stream_lock_is_recoverable",
    "decide_transaction_recovery",
    "decide_unit_recovery",
    "is_lexically_confined",
    "planned_quarantine_path",
    "provider_for_source_id",
    "quarantine_intent_dir",
    "quarantine_root",
    "session_docs_vacuum_journal_path",
]
