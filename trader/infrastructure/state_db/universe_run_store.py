"""Append-only universe-agent run store plus exact prepared projections."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Mapping

from trader.infrastructure.state_db._jsonl_store import (
    JsonlDayLedger,
    calendar_date_from_as_of,
    project_json,
    read_json_object,
    read_projection_or_scan,
    required_text,
    safe_filename_component,
    validated_date,
)


class UniverseRunStore:
    """Persist generic universe-run mappings without depending on domain models.

    ``base_dir`` is the canonical ``state/universe_runs`` directory. When
    ``prepared_dir`` is omitted, exact prepared projections live in the sibling
    ``state/universe_prepared`` directory.
    """

    def __init__(
        self,
        base_dir: str | Path,
        *,
        prepared_dir: str | Path | None = None,
    ) -> None:
        self.base_dir = Path(base_dir)
        self.prepared_dir = (
            Path(prepared_dir) if prepared_dir is not None else self.base_dir.parent / "universe_prepared"
        )
        self._ledger = JsonlDayLedger(self.base_dir)

    def path_for_date(self, date: str) -> Path:
        return self._ledger.path_for_date(date)

    def latest_path_for_venue(self, venue: str) -> Path:
        return self.base_dir / f"latest-{safe_filename_component(required_text(venue, field='venue'))}.json"

    def prepared_path_for_scope(self, scope_id: str) -> Path:
        exact_scope_id = required_text(scope_id, field="scope_id")
        digest = hashlib.sha256(exact_scope_id.encode("utf-8")).hexdigest()
        return self.prepared_dir / f"{digest}.json"

    def append(self, record: Mapping[str, Any], *, date: str | None = None) -> dict[str, str]:
        """Append every attempt and refresh a usable venue projection.

        A transient error must not replace the last successful regional report:
        the gallery keeps rendering that report and receives the newest failure
        as ``latest_failure`` for an explicit warning.  When no success exists,
        the error itself remains the projection so it is still observable.
        """

        payload = _validated_run(record)
        date_key = _record_date(payload, explicit=date)
        latest_path = self.latest_path_for_venue(payload["venue"])

        with self._ledger.write_session() as session:
            session.append(payload, date=date_key)
            latest = read_json_object(latest_path)
            if payload["status"] in {"error", "invalid"} and _is_success_for_venue(
                latest, payload["venue"]
            ):
                session.project_json(latest_path, {**latest, "latest_failure": payload})
            else:
                # A success clears a former failure marker and therefore resets
                # the persisted retry lineage in the readable projection.
                session.project_json(latest_path, payload)

        ref = {
            "date": date_key,
            "venue": payload["venue"],
            "candidate_scope_id": payload["candidate_scope_id"],
            "as_of": payload["as_of"],
            "status": payload["status"],
        }
        agent_run_id = str(payload.get("agent_run_id") or "").strip()
        if agent_run_id:
            ref["agent_run_id"] = agent_run_id
        return ref

    def read_latest(self, venue: str) -> dict[str, Any] | None:
        """Return the latest valid venue projection, with canonical fallback."""

        venue_key = required_text(venue, field="venue")
        return read_projection_or_scan(
            self.latest_path_for_venue(venue_key),
            self.base_dir,
            lambda payload: _is_run_for_venue(payload, venue_key),
        )

    def write_prepared(self, scope_id: str, record: Mapping[str, Any]) -> dict[str, Any]:
        """Atomically write the prepared projection for one exact candidate scope."""

        exact_scope_id = required_text(scope_id, field="scope_id")
        if not isinstance(record, Mapping):
            raise TypeError("prepared universe record must be a mapping")
        payload = dict(record)
        record_scope_id = required_text(
            payload.get("candidate_scope_id"), field="candidate_scope_id"
        )
        if record_scope_id != exact_scope_id:
            raise ValueError("prepared record candidate_scope_id does not match scope_id")

        with self._ledger.write_session():
            project_json(self.prepared_path_for_scope(exact_scope_id), payload)
        return payload

    def read_prepared(self, scope_id: str) -> dict[str, Any] | None:
        """Return a prepared record only when its embedded scope ID matches exactly."""

        exact_scope_id = required_text(scope_id, field="scope_id")
        payload = read_json_object(self.prepared_path_for_scope(exact_scope_id))
        if payload is None:
            return None
        embedded_scope_id = str(payload.get("candidate_scope_id") or "").strip()
        return payload if embedded_scope_id == exact_scope_id else None


def _validated_run(record: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(record, Mapping):
        raise TypeError("universe run record must be a mapping")
    payload = dict(record)
    for field in ("candidate_scope_id", "venue", "as_of", "status"):
        payload[field] = required_text(payload.get(field), field=field)
    return payload


def _record_date(record: Mapping[str, Any], *, explicit: str | None) -> str:
    if explicit is not None:
        return validated_date(explicit)
    return calendar_date_from_as_of(
        record["as_of"],
        empty_error="record.as_of must start with YYYY-MM-DD when date is omitted",
    )


def _is_run_for_venue(payload: dict[str, Any] | None, venue: str) -> bool:
    if payload is None or str(payload.get("venue") or "").strip() != venue:
        return False
    return bool(str(payload.get("candidate_scope_id") or "").strip())


def _is_success_for_venue(payload: dict[str, Any] | None, venue: str) -> bool:
    return _is_run_for_venue(payload, venue) and payload.get("status") == "success"
