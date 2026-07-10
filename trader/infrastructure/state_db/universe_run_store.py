"""Append-only universe-agent run store plus exact prepared projections."""

from __future__ import annotations

import hashlib
import json
import threading
from datetime import date as date_type
from pathlib import Path
from typing import Any, Mapping

from trader.infrastructure.state_db.shadow import write_json_atomic


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
        self._write_lock = threading.RLock()

    def path_for_date(self, date: str) -> Path:
        return self.base_dir / f"{_validated_date(date)}.jsonl"

    def latest_path_for_venue(self, venue: str) -> Path:
        return self.base_dir / f"latest-{_safe_component(_required_text(venue, field='venue'))}.json"

    def prepared_path_for_scope(self, scope_id: str) -> Path:
        exact_scope_id = _required_text(scope_id, field="scope_id")
        digest = hashlib.sha256(exact_scope_id.encode("utf-8")).hexdigest()
        return self.prepared_dir / f"{digest}.json"

    def append(self, record: Mapping[str, Any], *, date: str | None = None) -> dict[str, str]:
        """Append any run status and atomically refresh the venue's latest run."""

        payload = _validated_run(record)
        date_key = _record_date(payload, explicit=date)
        line = json.dumps(payload, ensure_ascii=False, sort_keys=True)

        with self._write_lock:
            self.base_dir.mkdir(parents=True, exist_ok=True)
            with self.path_for_date(date_key).open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
            write_json_atomic(self.latest_path_for_venue(payload["venue"]), payload)

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

        venue_key = _required_text(venue, field="venue")
        payload = _read_json_object(self.latest_path_for_venue(venue_key))
        if _is_run_for_venue(payload, venue_key):
            return payload

        for path in sorted(self.base_dir.glob("????-??-??.jsonl"), reverse=True):
            for candidate in reversed(_read_jsonl_objects(path)):
                if _is_run_for_venue(candidate, venue_key):
                    return candidate
        return None

    def write_prepared(self, scope_id: str, record: Mapping[str, Any]) -> dict[str, Any]:
        """Atomically write the prepared projection for one exact candidate scope."""

        exact_scope_id = _required_text(scope_id, field="scope_id")
        if not isinstance(record, Mapping):
            raise TypeError("prepared universe record must be a mapping")
        payload = dict(record)
        record_scope_id = _required_text(
            payload.get("candidate_scope_id"), field="candidate_scope_id"
        )
        if record_scope_id != exact_scope_id:
            raise ValueError("prepared record candidate_scope_id does not match scope_id")

        with self._write_lock:
            write_json_atomic(self.prepared_path_for_scope(exact_scope_id), payload)
        return payload

    def read_prepared(self, scope_id: str) -> dict[str, Any] | None:
        """Return a prepared record only when its embedded scope ID matches exactly."""

        exact_scope_id = _required_text(scope_id, field="scope_id")
        payload = _read_json_object(self.prepared_path_for_scope(exact_scope_id))
        if payload is None:
            return None
        embedded_scope_id = str(payload.get("candidate_scope_id") or "").strip()
        return payload if embedded_scope_id == exact_scope_id else None


def _validated_run(record: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(record, Mapping):
        raise TypeError("universe run record must be a mapping")
    payload = dict(record)
    for field in ("candidate_scope_id", "venue", "as_of", "status"):
        payload[field] = _required_text(payload.get(field), field=field)
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


def _is_run_for_venue(payload: dict[str, Any] | None, venue: str) -> bool:
    if payload is None or str(payload.get("venue") or "").strip() != venue:
        return False
    return bool(str(payload.get("candidate_scope_id") or "").strip())
