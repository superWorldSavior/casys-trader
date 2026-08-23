"""Append-only sidecar receipts proving when an intelligence artifact became ready.

A receipt is stamped *after* the canonical raw history line is flushed and
fsync'd.  Semantic ``as_of`` timestamps and filesystem mtimes are not
readiness proofs.  Nested history envelopes are not receipts.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any

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


def append_jsonl_and_fsync(path: Path, payload: Mapping[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = jsonl_dumps(payload) + "\n"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line)
        handle.flush()
        os.fsync(handle.fileno())


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


__all__ = [
    "AVAILABILITY_RECEIPT_DIRNAME",
    "AVAILABILITY_RECEIPT_SCHEMA",
    "UtcClock",
    "append_jsonl_and_fsync",
    "build_availability_receipt",
    "default_utc_clock",
    "fsync_path",
    "is_availability_receipt",
    "is_legacy_history_envelope",
    "load_receipts",
    "payload_sha256",
    "receipt_dir",
    "receipt_ready_at",
    "unwrap_history_payload",
    "validate_availability_receipt",
]
