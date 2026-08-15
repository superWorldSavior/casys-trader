"""Append-only store for the compact global situation digest."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from trader.infrastructure.state_db._jsonl_store import JsonlDayLedger, read_json_object


class GlobalSituationDigestStore:
    """Persist changed digest projections and expose one atomic current payload."""

    def __init__(self, base_dir: str | Path) -> None:
        self.base_dir = Path(base_dir)
        self._ledger = JsonlDayLedger(self.base_dir, validate_date=False)

    @property
    def current_path(self) -> Path:
        return self.base_dir / "current.json"

    def read_current(self) -> dict[str, Any] | None:
        payload = read_json_object(self.current_path)
        return payload if payload is not None and payload.get("digest_id") else None

    def append_if_changed(self, digest: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, str], bool]:
        payload = _validated_digest(digest)
        stored, changed = self._ledger.append_if_changed(
            payload,
            date=payload["as_of"][:10],
            latest_path=self.current_path,
            identity_field="digest_id",
            read_current=self.read_current,
        )
        return stored, _ref(stored), changed


def _validated_digest(digest: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(digest, Mapping):
        raise TypeError("global situation digest must be a mapping")
    payload = dict(digest)
    as_of = str(payload.get("as_of") or "").strip()
    if len(as_of) < 10:
        raise ValueError("global situation digest as_of must start with YYYY-MM-DD")
    payload["as_of"] = as_of
    semantic = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    payload["digest_id"] = f"global_situation_digest:v1:{hashlib.sha256(semantic.encode()).hexdigest()[:16]}"
    return payload


def _ref(payload: Mapping[str, Any]) -> dict[str, str]:
    return {
        "digest_id": str(payload.get("digest_id") or ""),
        "as_of": str(payload.get("as_of") or ""),
    }


__all__ = ["GlobalSituationDigestStore"]
