"""Append-only store for the advisory global universe posture."""

from __future__ import annotations

import hashlib
import json
import threading
from datetime import date as date_type
from pathlib import Path
from typing import Any, Mapping

from trader.domain.universe.global_posture import GlobalUniversePosture
from trader.infrastructure.state_db.shadow import write_json_atomic


class GlobalUniversePostureStore:
    """Persist successful postures without hiding their latest failed refresh.

    ``current.json`` is deliberately success-only: consumers can always keep
    using the last advisory posture when the next LLM refresh fails.  Failed
    attempts live in a separate append-only ledger plus ``latest_failure.json``
    so retry state and a future gallery warning remain observable without
    replacing the usable projection.
    """

    def __init__(self, base_dir: str | Path) -> None:
        self.base_dir = Path(base_dir)
        self._write_lock = threading.RLock()

    @property
    def current_path(self) -> Path:
        return self.base_dir / "current.json"

    @property
    def latest_failure_path(self) -> Path:
        return self.base_dir / "latest_failure.json"

    @property
    def failures_dir(self) -> Path:
        return self.base_dir / "failures"

    def failure_path_for_date(self, date: str) -> Path:
        return self.failures_dir / f"{_validated_date(date)}.jsonl"

    def read_current(self) -> dict[str, Any] | None:
        try:
            payload = json.loads(self.current_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) and payload.get("posture_id") else None

    def read_latest_failure(self) -> dict[str, Any] | None:
        """Return the active failed refresh, if any.

        The JSONL ledger is intentionally not used as a fallback here: absence
        of the marker means a later successful posture has cleared the active
        failure, while historical failures remain auditable in ``failures/``.
        """

        try:
            payload = json.loads(self.latest_failure_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return _validated_failure_payload(payload)

    def append_failure(self, failure: Mapping[str, Any]) -> dict[str, Any]:
        """Append a failed refresh without modifying the success projection."""

        payload = _validate_failure(failure)
        date_key = _record_date(payload)
        line = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        with self._write_lock:
            self.failures_dir.mkdir(parents=True, exist_ok=True)
            with self.failure_path_for_date(date_key).open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
            write_json_atomic(self.latest_failure_path, payload)
        return payload

    def append_if_changed(
        self,
        posture: GlobalUniversePosture,
    ) -> tuple[dict[str, Any], dict[str, str], bool]:
        payload = _validated_posture(posture)
        with self._write_lock:
            current = self.read_current()
            if current is not None and current.get("posture_id") == payload["posture_id"]:
                self._clear_latest_failure()
                return current, _ref(current), False
            self.base_dir.mkdir(parents=True, exist_ok=True)
            with (self.base_dir / f"{payload['as_of'][:10]}.jsonl").open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
            write_json_atomic(self.current_path, payload)
            self._clear_latest_failure()
        return payload, _ref(payload), True

    def _clear_latest_failure(self) -> None:
        try:
            self.latest_failure_path.unlink()
        except FileNotFoundError:
            pass


def _validated_posture(posture: GlobalUniversePosture) -> dict[str, Any]:
    if not isinstance(posture, GlobalUniversePosture):
        raise TypeError("global universe posture must be a GlobalUniversePosture")
    payload = posture.to_dict()
    as_of = str(payload.get("as_of") or "").strip()
    if len(as_of) < 10:
        raise ValueError("global universe posture as_of must start with YYYY-MM-DD")
    payload["as_of"] = as_of
    semantic = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    payload["posture_id"] = f"global_universe_posture:v1:{hashlib.sha256(semantic.encode()).hexdigest()[:16]}"
    return payload


def _validate_failure(failure: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(failure, Mapping):
        raise TypeError("global universe posture failure must be a mapping")
    payload = dict(failure)
    payload["as_of"] = _required_text(payload.get("as_of"), field="failure.as_of")
    payload["status"] = _required_text(payload.get("status"), field="failure.status")
    if payload["status"] not in {"error", "invalid"}:
        raise ValueError("global universe posture failure status must be error or invalid")
    payload["error_code"] = _required_text(
        payload.get("error_code"), field="failure.error_code"
    )
    return payload


def _validated_failure_payload(payload: Any) -> dict[str, Any] | None:
    if not isinstance(payload, Mapping):
        return None
    try:
        return _validate_failure(payload)
    except (TypeError, ValueError):
        return None


def _record_date(payload: Mapping[str, Any]) -> str:
    as_of = str(payload.get("as_of") or "").strip()
    if len(as_of) < 10:
        raise ValueError("failure.as_of must start with YYYY-MM-DD")
    return _validated_date(as_of[:10])


def _validated_date(value: str) -> str:
    text = str(value or "").strip()
    try:
        parsed = date_type.fromisoformat(text)
    except ValueError as exc:
        raise ValueError("date must be YYYY-MM-DD") from exc
    if parsed.isoformat() != text:
        raise ValueError("date must be YYYY-MM-DD")
    return text


def _required_text(value: Any, *, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field} must be a non-empty string")
    return text


def _ref(payload: Mapping[str, Any]) -> dict[str, str]:
    return {
        "posture_id": str(payload.get("posture_id") or ""),
        "as_of": str(payload.get("as_of") or ""),
        "gross_mode": str(payload.get("gross_mode") or ""),
        "net_bias": str(payload.get("net_bias") or ""),
    }


__all__ = ["GlobalUniversePostureStore"]
