"""Append-only file store for immutable candidate-scope snapshots."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from trader.infrastructure.state_db._jsonl_store import (
    JsonlDayLedger,
    calendar_date_from_as_of,
    find_newest_matching,
    read_jsonl_objects,
    read_projection_or_scan,
    required_text,
    safe_filename_component,
    validated_date,
)


class CandidateScopeStore:
    """Persist generic candidate-scope mappings and per-venue current projections.

    ``base_dir`` is the canonical ``state/candidate_scopes`` directory. Daily
    JSONL files are the source of truth; ``current-<venue>.json`` files are
    replaceable projections used by runtime readers.
    """

    def __init__(self, base_dir: str | Path) -> None:
        self.base_dir = Path(base_dir)
        self._ledger = JsonlDayLedger(self.base_dir)

    def path_for_date(self, date: str) -> Path:
        return self._ledger.path_for_date(date)

    def current_path_for_venue(self, venue: str) -> Path:
        return self.base_dir / f"current-{safe_filename_component(required_text(venue, field='venue'))}.json"

    def append(self, record: Mapping[str, Any], *, date: str | None = None) -> dict[str, str]:
        """Append one canonical record and atomically refresh its venue projection."""

        payload = _validated_scope(record)
        date_key = _record_date(payload, explicit=date)
        with self._ledger.write_session() as session:
            session.append(payload, date=date_key)
            session.project_json(self.current_path_for_venue(payload["venue"]), payload)

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

        venue_key = required_text(venue, field="venue")
        phase = str(scope_phase or "").strip().lower() or None
        return read_projection_or_scan(
            self.current_path_for_venue(venue_key),
            self.base_dir,
            lambda payload: _is_scope_for_venue(payload, venue_key) and _matches_phase(payload, phase),
        )

    def read_by_id(
        self,
        candidate_scope_id: str,
        hint_date: str | None = None,
    ) -> dict[str, Any] | None:
        """Load one immutable scope by id. Hint date first, then dated JSONL scan.

        ``read_latest`` is the wrong join for historical attribution: a close
        scope on J-1 is routinely activated on J.
        """
        scope_id = str(candidate_scope_id or "").strip()
        if not scope_id:
            return None
        date_key = _hint_date_key(hint_date)
        if date_key is not None:
            for payload in reversed(read_jsonl_objects(self.path_for_date(date_key))):
                if _is_scope_id(payload, scope_id):
                    return payload
        return find_newest_matching(self.base_dir, lambda payload: _is_scope_id(payload, scope_id))


def _validated_scope(record: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(record, Mapping):
        raise TypeError("candidate scope record must be a mapping")
    payload = dict(record)
    payload["candidate_scope_id"] = required_text(
        payload.get("candidate_scope_id"), field="candidate_scope_id"
    )
    payload["venue"] = required_text(payload.get("venue"), field="venue")
    payload["as_of"] = required_text(payload.get("as_of"), field="as_of")
    return payload


def _record_date(record: Mapping[str, Any], *, explicit: str | None) -> str:
    if explicit is not None:
        return validated_date(explicit)
    return calendar_date_from_as_of(
        record["as_of"],
        empty_error="record.as_of must start with YYYY-MM-DD when date is omitted",
    )


def _hint_date_key(hint_date: str | None) -> str | None:
    if hint_date is None:
        return None
    text = str(hint_date).strip()
    if not text:
        return None
    candidate = text[:10] if len(text) >= 10 and text[4] == "-" and text[7] == "-" else text
    try:
        return validated_date(candidate)
    except ValueError:
        return None


def _is_scope_id(payload: Mapping[str, Any] | None, candidate_scope_id: str) -> bool:
    if payload is None:
        return False
    return str(payload.get("candidate_scope_id") or "").strip() == candidate_scope_id


def _is_scope_for_venue(payload: dict[str, Any] | None, venue: str) -> bool:
    if payload is None or str(payload.get("venue") or "").strip() != venue:
        return False
    return bool(str(payload.get("candidate_scope_id") or "").strip())


def _matches_phase(payload: Mapping[str, Any] | None, phase: str | None) -> bool:
    if phase is None:
        return True
    return str((payload or {}).get("scope_phase") or "").strip().lower() == phase
