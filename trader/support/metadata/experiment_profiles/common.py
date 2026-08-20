"""Shared primitives for non-secret execution-profile fingerprints."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path

_MODEL_PROFILE_FIELDS = frozenset(
    (
        "configured_model",
        "transport",
        "agent",
        "reasoning_effort",
        "profile_fingerprint",
    )
)


def _clean(value: object) -> str | None:
    cleaned = str(value or "").strip()
    return cleaned or None


def _canonical_fingerprint(value: object) -> str | None:
    try:
        canonical = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError):
        return None
    return "sha256:" + hashlib.sha256(canonical).hexdigest()


def _stable_file_sha256(path: Path) -> str | None:
    """Hash one regular file while rejecting concurrent replacement/mutation."""

    try:
        with path.open("rb") as handle:
            before = os.fstat(handle.fileno())
            if not stat.S_ISREG(before.st_mode):
                return None
            digest = hashlib.sha256()
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
            after = os.fstat(handle.fileno())
        current = path.stat()
    except OSError:
        return None
    stable_fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns")
    if any(
        getattr(before, field) != getattr(after, field) or getattr(after, field) != getattr(current, field)
        for field in stable_fields
    ):
        return None
    return "sha256:" + digest.hexdigest()


def _stable_json_object(path: Path) -> dict[str, object] | None:
    """Read one JSON object atomically enough for cohort identity capture."""

    try:
        with path.open("rb") as handle:
            before = os.fstat(handle.fileno())
            if not stat.S_ISREG(before.st_mode):
                return None
            payload = handle.read()
            after = os.fstat(handle.fileno())
        current = path.stat()
    except FileNotFoundError:
        return {}
    except OSError:
        return None
    stable_fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns")
    if any(
        getattr(before, field) != getattr(after, field) or getattr(after, field) != getattr(current, field)
        for field in stable_fields
    ):
        return None
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None
