"""Append-only file store for immutable candidate-scope snapshots."""

from __future__ import annotations

import json
import threading
from datetime import date as date_type
from pathlib import Path
from typing import Any, Mapping

from trader.infrastructure.state_db.shadow import write_json_atomic


class CandidateScopeStore:
    """Persist generic candidate-scope mappings and per-venue current projections.

    ``base_dir`` is the canonical ``state/candidate_scopes`` directory. Daily
    JSONL files are the source of truth; ``current-<venue>.json`` files are
    replaceable projections used by runtime readers.
    """

    def __init__(self, base_dir: str | Path) -> None:
        self.base_dir = Path(base_dir)
        self._write_lock = threading.RLock()

    def path_for_date(self, date: str) -> Path:
        return self.base_dir / f"{_validated_date(date)}.jsonl"

    def current_path_for_venue(self, venue: str) -> Path:
        return self.base_dir / f"current-{_safe_component(_required_text(venue, field='venue'))}.json"

    def append(self, record: Mapping[str, Any], *, date: str | None = None) -> dict[str, str]:
        """Append one canonical record and atomically refresh its venue projection."""

        payload = _validated_scope(record)
        date_key = _record_date(payload, explicit=date)
        line = json.dumps(payload, ensure_ascii=False, sort_keys=True)

        with self._write_lock:
            self.base_dir.mkdir(parents=True, exist_ok=True)
            with self.path_for_date(date_key).open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
            write_json_atomic(self.current_path_for_venue(payload["venue"]), payload)

        return {
            "date": date_key,
            "venue": payload["venue"],
            "candidate_scope_id": payload["candidate_scope_id"],
            "as_of": payload["as_of"],
        }

    def read_current(self, venue: str) -> dict[str, Any] | None:
        """Return the current valid projection, falling back to canonical JSONL."""

        return self.read_latest(venue)

    def read_latest(
        self,
        venue: str,
        *,
        scope_phase: str | None = None,
    ) -> dict[str, Any] | None:
        """Return the newest scope, optionally restricted to one phase."""

        venue_key = _required_text(venue, field="venue")
        phase = str(scope_phase or "").strip().lower() or None
        payload = _read_json_object(self.current_path_for_venue(venue_key))
        if _is_scope_for_venue(payload, venue_key) and _matches_phase(payload, phase):
            return payload

        for path in sorted(self.base_dir.glob("????-??-??.jsonl"), reverse=True):
            for candidate in reversed(_read_jsonl_objects(path)):
                if _is_scope_for_venue(candidate, venue_key) and _matches_phase(
                    candidate,
                    phase,
                ):
                    return candidate
        return None


def _validated_scope(record: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(record, Mapping):
        raise TypeError("candidate scope record must be a mapping")
    payload = dict(record)
    payload["candidate_scope_id"] = _required_text(
        payload.get("candidate_scope_id"), field="candidate_scope_id"
    )
    payload["venue"] = _required_text(payload.get("venue"), field="venue")
    payload["as_of"] = _required_text(payload.get("as_of"), field="as_of")
    return payload


def _record_date(record: Mapping[str, Any], *, explicit: str | None) -> str:
    if explicit is not None:
        return _validated_date(explicit)
    as_of = str(record["as_of"])
    if len(as_of) < 10:
        raise ValueError("record.as_of must start with YYYY-MM-DD when date is omitted")
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


def _safe_component(value: str) -> str:
    safe = "".join(ch for ch in value if ch.isalnum() or ch in ("_", "-"))
    if not safe:
        raise ValueError("value does not contain a safe filename component")
    return safe


def _read_json_object(path: Path) -> dict[str, Any] | None:
    try:
        payload: Any = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return payload if isinstance(payload, dict) else None


def _read_jsonl_objects(path: Path) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    records: list[dict[str, Any]] = []
    for line in lines:
        try:
            payload: Any = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            records.append(payload)
    return records


def _is_scope_for_venue(payload: dict[str, Any] | None, venue: str) -> bool:
    if payload is None or str(payload.get("venue") or "").strip() != venue:
        return False
    return bool(str(payload.get("candidate_scope_id") or "").strip())


def _matches_phase(payload: Mapping[str, Any] | None, phase: str | None) -> bool:
    if phase is None:
        return True
    return str((payload or {}).get("scope_phase") or "").strip().lower() == phase
