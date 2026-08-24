"""Append-only sidecar receipts proving when an intelligence artifact became ready.

A receipt is stamped *after* the canonical raw history line is flushed and
fsync'd.  Semantic ``as_of`` timestamps and filesystem mtimes are not
readiness proofs.  Nested history envelopes are not receipts.
"""

from __future__ import annotations

import fcntl
import os
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any

from trader.domain.world_availability import (
    PersistedWorldRef,
    WORLD_AVAILABILITY_RECEIPT_SCHEMA,
    WorldAvailabilityReceipt,
    WorldAvailabilitySubjectRef,
    WorldStorageLocator,
    _attest_verified_store_receipt,
    _iso,
    _receipt_hash_payload,
    _receipt_id_for,
    _receipt_identity_payload,
    world_subject_content_sha256,
)
from trader.domain.world_episode import canonical_sha256
from trader.infrastructure.state_db._jsonl_store import jsonl_dumps, read_jsonl_objects


AVAILABILITY_RECEIPT_SCHEMA = "availability_receipt.v1"
AVAILABILITY_RECEIPT_DIRNAME = "availability_receipts"

UtcClock = Callable[[], datetime]


def default_utc_clock() -> datetime:
    return datetime.now(timezone.utc)


def _utc(value: datetime, *, field_name: str = "ready_at") -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def payload_sha256(payload: Mapping[str, Any]) -> str:
    """SHA-256 of the exact canonical JSONL encoding of a raw history payload."""

    if not isinstance(payload, Mapping):
        raise TypeError("receipt payload must be a mapping")
    return sha256(jsonl_dumps(payload).encode("utf-8")).hexdigest()


def fsync_path(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _append_jsonl_flush(path: Path, payload: Mapping[str, Any]) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = jsonl_dumps(payload) + "\n"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line)
        handle.flush()
    return path


def append_jsonl_and_fsync(path: Path, payload: Mapping[str, Any]) -> None:
    fsync_path(_append_jsonl_flush(path, payload))


def build_availability_receipt(
    *,
    artifact_id: str,
    artifact_ref: Mapping[str, str],
    payload: Mapping[str, Any],
    history_ref: Mapping[str, str],
    ready_at: datetime,
) -> dict[str, Any]:
    artifact = str(artifact_id or "").strip()
    if not artifact:
        raise ValueError("availability receipt requires artifact_id")
    if not isinstance(artifact_ref, Mapping) or not isinstance(history_ref, Mapping):
        raise TypeError("artifact_ref and history_ref must be mappings")
    if not artifact_ref or not history_ref:
        raise ValueError("availability receipt requires nonempty artifact_ref and history_ref")
    ref_id = str(artifact_ref.get("brief_id") or "").strip()
    if not ref_id or ref_id != artifact:
        raise ValueError("artifact_ref.brief_id must equal artifact_id")
    history_path = str(history_ref.get("path") or "").strip()
    history_scope = str(history_ref.get("scope") or "").strip()
    encoding = str(history_ref.get("encoding") or "").strip()
    if not history_path or not history_scope or encoding != "jsonl":
        raise ValueError("history_ref requires path, exact scope, and encoding=jsonl")
    return {
        "schema_version": AVAILABILITY_RECEIPT_SCHEMA,
        "artifact_id": artifact,
        "artifact_ref": dict(artifact_ref),
        "payload_sha256": payload_sha256(payload),
        "history_ref": dict(history_ref),
        "ready_at": _utc(ready_at).isoformat(),
    }


def is_availability_receipt(row: Mapping[str, Any] | None) -> bool:
    if not isinstance(row, Mapping):
        return False
    return (
        row.get("schema_version") == AVAILABILITY_RECEIPT_SCHEMA
        and bool(str(row.get("artifact_id") or "").strip())
        and isinstance(row.get("artifact_ref"), Mapping)
        and isinstance(row.get("history_ref"), Mapping)
        and bool(str(row.get("payload_sha256") or "").strip())
        and bool(str(row.get("ready_at") or "").strip())
        and not isinstance(row.get("payload"), Mapping)
    )


def is_legacy_history_envelope(row: Mapping[str, Any] | None) -> bool:
    """True for the nested spike envelope that must not prove readiness."""

    if not isinstance(row, Mapping):
        return False
    return row.get("schema_version") == AVAILABILITY_RECEIPT_SCHEMA and isinstance(row.get("payload"), Mapping)


def unwrap_history_payload(row: Mapping[str, Any]) -> Mapping[str, Any]:
    """Return the nested brief, whether the line is a legacy envelope or a raw row."""

    if is_legacy_history_envelope(row):
        payload = row["payload"]
        if isinstance(payload, Mapping):
            return payload
    return row


def receipt_ready_at(row: Mapping[str, Any]) -> datetime | None:
    raw = str(row.get("ready_at") or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def validate_availability_receipt(
    row: Mapping[str, Any],
    payload: Mapping[str, Any],
    *,
    expected_artifact_id: str,
    expected_scope: str,
    expected_history_path: str,
) -> datetime | None:
    """Return ``ready_at`` only when the sidecar row cryptographically matches."""

    if not is_availability_receipt(row):
        return None
    artifact_id = str(row.get("artifact_id") or "").strip()
    expected_id = str(expected_artifact_id or "").strip()
    if not artifact_id or not expected_id or artifact_id != expected_id:
        return None
    artifact_ref = row.get("artifact_ref")
    if not isinstance(artifact_ref, Mapping) or not artifact_ref:
        return None
    ref_id = str(artifact_ref.get("brief_id") or "").strip()
    if not ref_id or ref_id != artifact_id:
        return None
    history_ref = row.get("history_ref")
    if not isinstance(history_ref, Mapping) or not history_ref:
        return None
    scope = str(history_ref.get("scope") or "").strip()
    expected_scope_text = str(expected_scope or "").strip()
    if not scope or not expected_scope_text or scope != expected_scope_text:
        return None
    history_path = str(history_ref.get("path") or "").strip()
    expected_path = str(expected_history_path or "").strip()
    if not history_path or not expected_path or history_path != expected_path:
        return None
    encoding = str(history_ref.get("encoding") or "").strip()
    if encoding != "jsonl":
        return None
    digest = str(row.get("payload_sha256") or "").strip().lower()
    try:
        expected_digest = payload_sha256(payload)
    except (TypeError, ValueError):
        return None
    if digest != expected_digest:
        return None
    return receipt_ready_at(row)


def load_receipts(path: Path) -> list[dict[str, Any]]:
    return [row for row in read_jsonl_objects(path) if isinstance(row, dict)]


def receipt_dir(base_dir: Path) -> Path:
    return Path(base_dir) / AVAILABILITY_RECEIPT_DIRNAME


def parse_world_availability_receipt(
    row: Mapping[str, Any] | None,
    payload: Mapping[str, Any] | None = None,
) -> WorldAvailabilityReceipt | None:
    """Rehydrate the current World availability receipt schema only."""

    if payload is not None and not isinstance(payload, Mapping):
        return None
    if not isinstance(row, Mapping):
        return None
    schema = str(row.get("schema_version") or "").strip()
    if schema != WORLD_AVAILABILITY_RECEIPT_SCHEMA:
        return None
    try:
        return WorldAvailabilityReceipt.from_mapping(row)
    except (TypeError, ValueError, AttributeError):
        return None


def _relative_store_path(value: str, *, field_name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field_name} must be a relative path")
    if ".." in text:
        raise ValueError(f"{field_name} must not contain parent traversal")
    candidate = Path(text)
    if candidate.is_absolute() or candidate.anchor:
        raise ValueError(f"{field_name} must be a relative path")
    if ".." in candidate.parts:
        raise ValueError(f"{field_name} must not contain parent traversal")
    return candidate.as_posix()


def _resolve_under_root(root: Path, relative: str, *, field_name: str) -> Path:
    root_resolved = Path(root).resolve()
    candidate = (root_resolved / relative).resolve()
    if not candidate.is_relative_to(root_resolved):
        raise ValueError(f"{field_name} must be a relative path")
    return candidate


def _seal_world_availability_receipt(
    subject: WorldAvailabilitySubjectRef,
    *,
    scope: str,
    storage_locator: WorldStorageLocator,
    ready_at: datetime,
) -> WorldAvailabilityReceipt:
    """Private hash bind used after durable subject write. Not exported."""

    stamped_at = _utc(ready_at)
    identity = _receipt_identity_payload(
        schema_version=WORLD_AVAILABILITY_RECEIPT_SCHEMA,
        subject=subject,
        scope=scope,
        storage_locator=storage_locator,
    )
    receipt_id = _receipt_id_for(identity)
    return WorldAvailabilityReceipt.from_mapping(
        {
            **identity,
            "receipt_id": receipt_id,
            "ready_at": _iso(stamped_at),
            "receipt_sha256": canonical_sha256(
                _receipt_hash_payload(identity, receipt_id=receipt_id, ready_at=stamped_at)
            ),
        }
    )


class WorldAvailabilityJsonlReceiptStore:
    """JSONL subject+receipt store. Owns root, store_id, and clock; stamps ready_at only after subject fsync."""

    def __init__(self, root: Path, *, store_id: str, clock: UtcClock | None = None) -> None:
        store_key = str(store_id or "").strip()
        if not store_key:
            raise ValueError("store_id must be a non-empty string")
        if Path(store_key).is_absolute() or ".." in store_key:
            raise ValueError("store_id must be a stable repository id, not a filesystem path")
        self._root = Path(root)
        self._store_id = store_key
        self._clock = clock or default_utc_clock

    def append(
        self,
        subject: WorldAvailabilitySubjectRef,
        *,
        scope: str,
        payload: Mapping[str, Any],
        history_path: str,
        receipt_path: str,
    ) -> PersistedWorldRef[WorldAvailabilitySubjectRef]:
        if not isinstance(subject, WorldAvailabilitySubjectRef):
            raise TypeError("subject must be WorldAvailabilitySubjectRef")
        if world_subject_content_sha256(payload) != subject.content_sha256:
            raise ValueError("subject content_sha256 does not match payload")
        history_rel = _relative_store_path(history_path, field_name="history_path")
        receipt_rel = _relative_store_path(receipt_path, field_name="receipt_path")
        if history_rel == receipt_rel:
            raise ValueError("history_path and receipt_path must be distinct")
        history_abs = _resolve_under_root(self._root, history_rel, field_name="history_path")
        receipt_abs = _resolve_under_root(self._root, receipt_rel, field_name="receipt_path")
        if history_abs == receipt_abs:
            raise ValueError("history_path and receipt_path must be distinct")
        locator = WorldStorageLocator(kind="jsonl", store_id=self._store_id, path=history_rel)
        with self._exclusive_lock():
            return self._append_locked(
                subject,
                scope=scope,
                payload=payload,
                history_abs=history_abs,
                receipt_abs=receipt_abs,
                locator=locator,
            )

    @contextmanager
    def _exclusive_lock(self):
        self._root.mkdir(parents=True, exist_ok=True)
        lock_path = self._root / ".world-availability-receipts.lock"
        with lock_path.open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _append_locked(
        self,
        subject: WorldAvailabilitySubjectRef,
        *,
        scope: str,
        payload: Mapping[str, Any],
        history_abs: Path,
        receipt_abs: Path,
        locator: WorldStorageLocator,
    ) -> PersistedWorldRef[WorldAvailabilitySubjectRef]:
        matches = _receipts_for_natural_key(self._root, self._store_id, subject)
        if matches:
            return self._complete_existing_receipt(
                subject,
                scope=scope,
                locator=locator,
                history_abs=history_abs,
                matches=matches,
            )

        if not _subject_payload_is_durable(history_abs, subject.content_sha256):
            append_jsonl_and_fsync(history_abs, payload)
            written_subjects = [row for row in read_jsonl_objects(history_abs) if isinstance(row, dict)]
            if not written_subjects:
                raise ValueError("subject history readback failed")
            if world_subject_content_sha256(written_subjects[-1]) != subject.content_sha256:
                raise ValueError("subject history readback hash mismatch")
        else:
            fsync_path(history_abs)
            _require_durable_subject_payload(history_abs, subject.content_sha256)

        ready_at = _utc(self._clock(), field_name="clock")
        receipt = _seal_world_availability_receipt(
            subject,
            scope=scope,
            storage_locator=locator,
            ready_at=ready_at,
        )
        append_jsonl_and_fsync(receipt_abs, receipt.to_dict())
        return self._attest_after_receipt_fsync(
            subject,
            scope=scope,
            locator=locator,
            receipt_abs=receipt_abs,
            expected_sha256=receipt.receipt_sha256,
        )

    def _complete_existing_receipt(
        self,
        subject: WorldAvailabilitySubjectRef,
        *,
        scope: str,
        locator: WorldStorageLocator,
        history_abs: Path,
        matches: list[tuple[Path, WorldAvailabilityReceipt]],
    ) -> PersistedWorldRef[WorldAvailabilitySubjectRef]:
        _assert_unique_compatible_receipt(matches, scope=scope, locator=locator)
        receipt_abs, _existing = matches[0]
        if history_abs.exists():
            fsync_path(history_abs)
        fsync_path(receipt_abs)
        _require_durable_subject_payload(history_abs, subject.content_sha256)
        refreshed = _receipts_for_natural_key(self._root, self._store_id, subject)
        _assert_unique_compatible_receipt(refreshed, scope=scope, locator=locator)
        _path, parsed = refreshed[0]
        attested = _attest_verified_store_receipt(
            parsed,
            expected_subject=subject,
            expected_scope=scope,
            expected_locator=locator,
        )
        return PersistedWorldRef(identity=subject, receipt=attested)

    def _attest_after_receipt_fsync(
        self,
        subject: WorldAvailabilitySubjectRef,
        *,
        scope: str,
        locator: WorldStorageLocator,
        receipt_abs: Path,
        expected_sha256: str | None,
    ) -> PersistedWorldRef[WorldAvailabilitySubjectRef]:
        written_receipts = load_receipts(receipt_abs)
        if not written_receipts:
            raise ValueError("availability receipt readback failed")
        parsed = parse_world_availability_receipt(written_receipts[-1])
        if parsed is None or parsed.receipt_sha256 != expected_sha256:
            raise ValueError("availability receipt readback failed")
        matches = _receipts_for_natural_key(self._root, self._store_id, subject)
        _assert_unique_compatible_receipt(matches, scope=scope, locator=locator)
        attested = _attest_verified_store_receipt(
            parsed,
            expected_subject=subject,
            expected_scope=scope,
            expected_locator=locator,
        )
        return PersistedWorldRef(identity=subject, receipt=attested)


def _subject_payload_is_durable(history_abs: Path, content_sha256: str) -> bool:
    if not history_abs.exists():
        return False
    return any(
        world_subject_content_sha256(row) == content_sha256
        for row in read_jsonl_objects(history_abs)
        if isinstance(row, dict)
    )


def _require_durable_subject_payload(history_abs: Path, content_sha256: str) -> None:
    if not _subject_payload_is_durable(history_abs, content_sha256):
        raise ValueError("subject history readback failed")


def _iter_store_receipts(root: Path, store_id: str) -> Iterator[tuple[Path, WorldAvailabilityReceipt]]:
    if not root.exists():
        return
    for path in sorted(root.rglob("*.jsonl")):
        if not path.is_file():
            continue
        for row in load_receipts(path):
            parsed = parse_world_availability_receipt(row)
            if parsed is None:
                continue
            if parsed.storage_locator.store_id != store_id:
                continue
            yield path, parsed


def _receipts_for_natural_key(
    root: Path,
    store_id: str,
    subject: WorldAvailabilitySubjectRef,
) -> list[tuple[Path, WorldAvailabilityReceipt]]:
    matches: list[tuple[Path, WorldAvailabilityReceipt]] = []
    for path, parsed in _iter_store_receipts(root, store_id):
        if (
            parsed.subject.kind == subject.kind
            and parsed.subject.subject_id == subject.subject_id
            and parsed.subject.content_sha256 == subject.content_sha256
        ):
            matches.append((path, parsed))
    return matches


def _assert_unique_compatible_receipt(
    matches: list[tuple[Path, WorldAvailabilityReceipt]],
    *,
    scope: str,
    locator: WorldStorageLocator,
) -> None:
    if not matches:
        raise ValueError("availability receipt readback failed")
    for _path, parsed in matches:
        if parsed.scope != scope or parsed.storage_locator != locator:
            raise ValueError("conflicting availability receipt for the same subject")
    if len(matches) > 1:
        raise ValueError("multiple availability receipts for the same subject")


__all__ = [
    "AVAILABILITY_RECEIPT_DIRNAME",
    "AVAILABILITY_RECEIPT_SCHEMA",
    "WORLD_AVAILABILITY_RECEIPT_SCHEMA",
    "UtcClock",
    "WorldAvailabilityJsonlReceiptStore",
    "append_jsonl_and_fsync",
    "build_availability_receipt",
    "default_utc_clock",
    "fsync_path",
    "is_availability_receipt",
    "is_legacy_history_envelope",
    "load_receipts",
    "parse_world_availability_receipt",
    "payload_sha256",
    "receipt_dir",
    "receipt_ready_at",
    "unwrap_history_payload",
    "validate_availability_receipt",
]
