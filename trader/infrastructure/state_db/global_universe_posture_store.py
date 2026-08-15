"""Append-only store for the advisory global universe posture."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from trader.domain.universe.global_posture import GlobalUniversePosture
from trader.infrastructure.state_db._jsonl_store import (
    JsonlDayLedger,
    calendar_date_from_as_of,
    read_json_object,
    required_text,
)


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
        self._ledger = JsonlDayLedger(self.base_dir, validate_date=False)

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
        return self._ledger.path_for_date(date, directory=self.failures_dir, validate=True)

    def read_current(self) -> dict[str, Any] | None:
        payload = read_json_object(self.current_path)
        return payload if payload is not None and payload.get("posture_id") else None

    def read_latest_failure(self) -> dict[str, Any] | None:
        """Return the active failed refresh, if any.

        The JSONL ledger is intentionally not used as a fallback here: absence
        of the marker means a later successful posture has cleared the active
        failure, while historical failures remain auditable in ``failures/``.
        """

        return _validated_failure_payload(read_json_object(self.latest_failure_path))

    def append_failure(self, failure: Mapping[str, Any]) -> dict[str, Any]:
        """Append a failed refresh without modifying the success projection."""

        payload = _validate_failure(failure)
        date_key = calendar_date_from_as_of(
            payload["as_of"],
            empty_error="failure.as_of must start with YYYY-MM-DD",
        )
        with self._ledger.write_session() as session:
            session.append(
                payload,
                date=date_key,
                directory=self.failures_dir,
                validate=True,
            )
            session.project_json(self.latest_failure_path, payload)
        return payload

    def append_if_changed(
        self,
        posture: GlobalUniversePosture,
    ) -> tuple[dict[str, Any], dict[str, str], bool]:
        payload = _validated_posture(posture)
        stored, changed = self._ledger.append_if_changed(
            payload,
            date=payload["as_of"][:10],
            latest_path=self.current_path,
            identity_field="posture_id",
            read_current=self.read_current,
            on_unchanged=self._clear_latest_failure,
            after_write=self._clear_latest_failure,
        )
        return stored, _ref(stored), changed

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
    payload["as_of"] = required_text(payload.get("as_of"), field="failure.as_of")
    payload["status"] = required_text(payload.get("status"), field="failure.status")
    if payload["status"] not in {"error", "invalid"}:
        raise ValueError("global universe posture failure status must be error or invalid")
    payload["error_code"] = required_text(
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


def _ref(payload: Mapping[str, Any]) -> dict[str, str]:
    return {
        "posture_id": str(payload.get("posture_id") or ""),
        "as_of": str(payload.get("as_of") or ""),
        "gross_mode": str(payload.get("gross_mode") or ""),
        "net_bias": str(payload.get("net_bias") or ""),
    }


__all__ = ["GlobalUniversePostureStore"]
