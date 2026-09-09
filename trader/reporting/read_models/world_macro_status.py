"""Read-only collection, coverage, freshness, and gap projector for source-only macro.

Never creates, migrates, or writes ``state/world_macro`` or ``world_model.db``.
``status.json`` is reconstructible and is not point-in-time authority.
Authority stays ``shadow_only`` with ``decision_effect=none`` and ``recommendation=NO_GO``.
GDELT and ``NewsMacroBrief`` stay excluded.
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

from trader.domain.world_availability import world_subject_content_sha256
from trader.domain.world_cohort import (
    COHORT_AUTHORITY,
    COHORT_DECISION_EFFECT,
    COHORT_RECOMMENDATION,
    COHORT_TRADER_CONTRIBUTION,
)
from trader.domain.world_macro import (
    MACRO_FEATURE_KEYS,
    MACRO_LANE_IDENTITY,
    MacroSourceFact,
    MacroWorldObservation,
)
from trader.infrastructure.state_db.availability_receipt import load_receipts, parse_world_availability_receipt
from trader.infrastructure.state_db.world_prediction_storage_lock import (
    PredictionStorageBusyError,
    shared_prediction_storage_lease,
)


MACRO_STATUS_SCHEMA = "world_macro_status.v1"
WORLD_MACRO_LANE_IDENTITY = MACRO_LANE_IDENTITY
_MACRO_ROOT = "world_macro"
_FACT_KIND = "macro_source_fact"
_OBSERVATION_KIND = "macro_world_observation"
_EVENT_KIND = "macro_collection_event"
_ATTACHED_MACRO = frozenset({"complete", "partial"})
_CLAIM_FIELDS = {
    "authority": COHORT_AUTHORITY,
    "decision_effect": COHORT_DECISION_EFFECT,
    "recommendation": COHORT_RECOMMENDATION,
    "causal_claim": False,
    "pnl_claim": False,
    "actual_trader_contribution": COHORT_TRADER_CONTRIBUTION,
}

_HISTORY_DIRS = (
    ("facts", _FACT_KIND, "fact_version_id"),
    ("observations", _OBSERVATION_KIND, "observation_id"),
    ("runs/events", _EVENT_KIND, "event_id"),
)
_RECEIPT_DIRS = (
    "facts/availability_receipts",
    "observations/availability_receipts",
    "runs/availability_receipts",
)


def read_world_macro_status(
    state_dir: str | Path,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Project collection/coverage/freshness/gaps without mutating absent or partial state."""

    root = Path(state_dir)
    macro_root = root / _MACRO_ROOT
    as_of = _utc(now)
    payload = _bounded_base(macro_root=macro_root, as_of=as_of)
    if not macro_root.exists():
        payload["attach"] = _read_attach(root)
        return payload

    payload["exists"] = True
    try:
        rows, skipped = _scan_histories(macro_root)
        receipts = _scan_receipts(macro_root)
    except OSError as exc:
        payload["status"] = "unavailable"
        payload["error"] = f"{type(exc).__name__}:{exc}"
        payload["attach"] = _read_attach(root)
        return payload

    facts = _parse_facts(rows["facts"])
    observations = _parse_observations(rows["observations"])
    events = list(rows["events"])
    skipped += facts[1] + observations[1]
    fact_rows, fact_skipped = facts
    observation_rows, observation_skipped = observations
    del fact_skipped, observation_skipped

    proven_facts = _proven_count(fact_rows, kind=_FACT_KIND, identity_field="fact_version_id", receipts=receipts)
    proven_observations = _proven_count(
        observation_rows,
        kind=_OBSERVATION_KIND,
        identity_field="observation_id",
        receipts=receipts,
    )
    unproven_facts = len(fact_rows) - proven_facts
    unproven_observations = len(observation_rows) - proven_observations
    collection = _project_collection(
        events=events,
        facts=len(fact_rows),
        proven_facts=proven_facts,
        unproven_facts=unproven_facts,
    )
    coverage = _project_coverage(observation_rows, proven_observations=proven_observations)
    freshness = _project_freshness(observation_rows, as_of=as_of)
    gaps = _project_gaps(
        observations=observation_rows,
        events=events,
        unproven_facts=unproven_facts,
        unproven_observations=unproven_observations,
    )
    payload.update(
        {
            "status": _overall_status(
                exists=True,
                skipped=skipped,
                facts=len(fact_rows),
                observations=len(observation_rows),
                events=len(events),
                unproven_facts=unproven_facts,
            ),
            "collection": collection,
            "coverage": coverage,
            "freshness": freshness,
            "gaps": gaps,
            "attach": _read_attach(root),
        }
    )
    return payload


def _bounded_base(*, macro_root: Path, as_of: datetime) -> dict[str, Any]:
    return {
        "schema_version": MACRO_STATUS_SCHEMA,
        "root": str(macro_root),
        "exists": False,
        "status": "not_started",
        "lane_identity": WORLD_MACRO_LANE_IDENTITY,
        "collection": {
            "status": "not_started",
            "runs": 0,
            "by_status": {},
            "facts": 0,
            "proven_facts": 0,
            "unproven_facts": 0,
            "events": 0,
            "run_ids": [],
        },
        "coverage": {
            "status": "missing",
            "observations": 0,
            "proven_observations": 0,
            "required_sources": 0,
            "fresh_sources": 0,
            "by_scope": [],
            "by_dimension": [],
            "source_coverage_is_not_dimension_coverage": True,
        },
        "freshness": {
            "status": "unknown",
            "as_of": as_of.isoformat(),
            "fresh_observations": 0,
            "stale_observations": 0,
            "unknown_expiry": 0,
            "mtime_is_not_proof": True,
            "indexed_at_is_not_publication": True,
            "period_is_not_known_release": True,
        },
        "gaps": {
            "missing_source_ids": [],
            "failed_source_ids": [],
            "unknown_dimensions": [],
            "unproven_facts": 0,
            "unproven_observations": 0,
            "gdelt": "excluded",
            "news_macro_brief": "excluded",
        },
        "attach": {
            "status": "not_started",
            "exists": False,
            "lane_identity": WORLD_MACRO_LANE_IDENTITY,
            "episodes": 0,
            "macro_present": 0,
            "macro_missing": 0,
        },
        "cohort": {
            "separated": True,
            "status": "separated_from_ml_study",
            "recommendation": COHORT_RECOMMENDATION,
            "see_command": "casys-trader world cohort report",
        },
        **_CLAIM_FIELDS,
    }


def _overall_status(
    *,
    exists: bool,
    skipped: int,
    facts: int,
    observations: int,
    events: int,
    unproven_facts: int,
) -> str:
    if not exists or (facts == 0 and observations == 0 and events == 0 and skipped == 0):
        return "not_started"
    if skipped or (facts > 0 and unproven_facts == facts and observations == 0):
        return "partial"
    return "loaded"


def _scan_histories(macro_root: Path) -> tuple[dict[str, list[dict[str, Any]]], int]:
    rows: dict[str, list[dict[str, Any]]] = {"facts": [], "observations": [], "events": []}
    skipped = 0
    mapping = {"facts": "facts", "observations": "observations", "runs/events": "events"}
    for relative, _kind, _identity in _HISTORY_DIRS:
        parsed, skipped_lines = _read_day_jsonl(macro_root / relative)
        rows[mapping[relative]].extend(parsed)
        skipped += skipped_lines
    return rows, skipped


def _scan_receipts(macro_root: Path) -> dict[tuple[str, str], list[Any]]:
    grouped: dict[tuple[str, str], list[Any]] = defaultdict(list)
    for relative in _RECEIPT_DIRS:
        directory = macro_root / relative
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("????-??-??.jsonl")):
            if not path.is_file():
                continue
            for row in load_receipts(path):
                parsed = parse_world_availability_receipt(row)
                if parsed is None:
                    continue
                grouped[(parsed.subject.kind, parsed.subject.subject_id)].append(parsed)
    return grouped


def _read_day_jsonl(directory: Path) -> tuple[list[dict[str, Any]], int]:
    if not directory.is_dir():
        return [], 0
    rows: list[dict[str, Any]] = []
    skipped = 0
    for path in sorted(directory.glob("????-??-??.jsonl")):
        if not path.is_file():
            continue
        parsed, skipped_lines = _read_jsonl_rows(path)
        rows.extend(parsed)
        skipped += skipped_lines
    return rows, skipped


def _read_jsonl_rows(path: Path) -> tuple[list[dict[str, Any]], int]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return [], 0
    rows: list[dict[str, Any]] = []
    skipped = 0
    for line in lines:
        if not line.strip():
            continue
        try:
            payload: Any = json.loads(line)
        except json.JSONDecodeError:
            skipped += 1
            continue
        if isinstance(payload, dict):
            rows.append(payload)
        else:
            skipped += 1
    return rows, skipped


def _parse_facts(rows: Sequence[Mapping[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    parsed: list[dict[str, Any]] = []
    skipped = 0
    for row in rows:
        try:
            fact = MacroSourceFact.from_mapping(row)
        except (TypeError, ValueError):
            skipped += 1
            continue
        payload = dict(row)
        payload["fact_version_id"] = fact.fact_version_id.value
        parsed.append(payload)
    return parsed, skipped


def _parse_observations(rows: Sequence[Mapping[str, Any]]) -> tuple[list[MacroWorldObservation], int]:
    parsed: list[MacroWorldObservation] = []
    skipped = 0
    for row in rows:
        try:
            parsed.append(MacroWorldObservation.from_mapping(row))
        except (TypeError, ValueError):
            skipped += 1
    return parsed, skipped


def _proven_count(
    rows: Sequence[Any],
    *,
    kind: str,
    identity_field: str,
    receipts: Mapping[tuple[str, str], Sequence[Any]],
) -> int:
    proven = 0
    for row in rows:
        if isinstance(row, MacroWorldObservation):
            subject_id = row.observation_id
            digest = row.content_sha256
            history = {key: value for key, value in row.to_dict().items() if key != "content_sha256"}
            history_digest = world_subject_content_sha256(history)
        else:
            subject_id = str(row.get(identity_field) or "")
            try:
                history_digest = world_subject_content_sha256(row)
            except (TypeError, ValueError):
                continue
            digest = str(row.get("content_sha256") or history_digest)
        matches = receipts.get((kind, subject_id), ())
        if any(item.subject.content_sha256 in {digest, history_digest} for item in matches):
            proven += 1
    return proven


def _project_collection(
    *,
    events: Sequence[Mapping[str, Any]],
    facts: int,
    proven_facts: int,
    unproven_facts: int,
) -> dict[str, Any]:
    by_run: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for event in events:
        run_id = str(event.get("run_id") or "").strip()
        if run_id:
            by_run[run_id].append(event)
    by_status: Counter[str] = Counter()
    run_ids = sorted(by_run)
    for run_id in run_ids:
        by_status[_run_status(by_run[run_id])] += 1
    status = _collection_status(by_status, facts=facts, events=len(events))
    return {
        "status": status,
        "runs": len(run_ids),
        "by_status": dict(sorted(by_status.items())),
        "facts": facts,
        "proven_facts": proven_facts,
        "unproven_facts": unproven_facts,
        "events": len(events),
        "run_ids": run_ids,
    }


def _run_status(events: Sequence[Mapping[str, Any]]) -> str:
    completed = [item for item in events if item.get("event_type") == "macro_collection_completed"]
    if completed:
        status = str(completed[-1].get("status") or "").strip()
        return status or "failed"
    types = {str(item.get("event_type") or "") for item in events}
    if "macro_collection_started" in types:
        return "collecting"
    if "macro_collection_registered" in types:
        return "registered"
    return "unknown"


def _collection_status(by_status: Mapping[str, int], *, facts: int, events: int) -> str:
    if not by_status:
        if facts or events:
            return "partial"
        return "not_started"
    if len(by_status) == 1:
        return next(iter(by_status))
    if "collecting" in by_status:
        return "collecting"
    if "completed_partial" in by_status:
        return "completed_partial"
    if "failed" in by_status and "completed" in by_status:
        return "completed_partial"
    return "partial"


def _project_coverage(
    observations: Sequence[MacroWorldObservation],
    *,
    proven_observations: int,
) -> dict[str, Any]:
    if not observations:
        return {
            "status": "missing",
            "observations": 0,
            "proven_observations": 0,
            "required_sources": 0,
            "fresh_sources": 0,
            "by_scope": [],
            "by_dimension": [],
            "source_coverage_is_not_dimension_coverage": True,
        }
    by_scope: dict[tuple[str, str], MacroWorldObservation] = {}
    for observation in sorted(observations, key=lambda item: (item.cutoff_at, item.observation_id)):
        by_scope[(observation.scope.kind, observation.scope.entity_id)] = observation
    selected = [by_scope[key] for key in sorted(by_scope)]
    statuses = {item.coverage.status for item in selected}
    if statuses == {"complete"}:
        status = "complete"
    elif statuses <= {"missing"}:
        status = "missing"
    elif statuses <= {"unknown"}:
        status = "unknown"
    else:
        status = "partial"
    by_dimension: dict[str, dict[str, Any]] = {}
    for key in MACRO_FEATURE_KEYS:
        values = {item.features[key] for item in selected}
        coverages = {next(dim.coverage_status for dim in item.dimensions if dim.dimension == key) for item in selected}
        by_dimension[key] = {
            "dimension": key,
            "values": sorted(values),
            "coverage_statuses": sorted(coverages),
            "unknown": "unknown" in values or coverages <= {"unknown", "missing"},
        }
    return {
        "status": status,
        "observations": len(observations),
        "proven_observations": proven_observations,
        "required_sources": sum(item.coverage.required_sources for item in selected),
        "fresh_sources": sum(item.coverage.fresh_sources for item in selected),
        "source_coverage_is_not_dimension_coverage": True,
        "by_scope": [
            {
                "scope": item.scope.to_dict(),
                "status": item.coverage.status,
                "required_sources": item.coverage.required_sources,
                "fresh_sources": item.coverage.fresh_sources,
                "missing_source_ids": list(item.coverage.missing_source_ids),
                "observation_id": item.observation_id,
                "dimensions": [
                    {
                        "dimension": dim.dimension,
                        "value": dim.value,
                        "coverage_status": dim.coverage_status,
                        "method": dim.method,
                    }
                    for dim in item.dimensions
                ],
            }
            for item in selected
        ],
        "by_dimension": [by_dimension[key] for key in MACRO_FEATURE_KEYS],
    }


def _project_freshness(observations: Sequence[MacroWorldObservation], *, as_of: datetime) -> dict[str, Any]:
    fresh = 0
    stale = 0
    unknown = 0
    for observation in observations:
        if observation.valid_until is None:
            unknown += 1
            continue
        if as_of < observation.valid_until:
            fresh += 1
        else:
            stale += 1
    if stale:
        status = "stale"
    elif fresh:
        status = "fresh"
    else:
        status = "unknown"
    return {
        "status": status,
        "as_of": as_of.isoformat(),
        "fresh_observations": fresh,
        "stale_observations": stale,
        "unknown_expiry": unknown,
        "mtime_is_not_proof": True,
        "indexed_at_is_not_publication": True,
        "period_is_not_known_release": True,
    }


def _project_gaps(
    *,
    observations: Sequence[MacroWorldObservation],
    events: Sequence[Mapping[str, Any]],
    unproven_facts: int,
    unproven_observations: int,
) -> dict[str, Any]:
    missing: set[str] = set()
    unknown_dimensions: set[str] = set()
    for observation in observations:
        missing.update(observation.coverage.missing_source_ids)
        for name in MACRO_FEATURE_KEYS:
            dimension = next(item for item in observation.dimensions if item.dimension == name)
            if observation.features[name] == "unknown" or dimension.coverage_status in {"unknown", "missing"}:
                unknown_dimensions.add(name)
    failed = sorted(
        {
            str(event.get("source_id") or "").strip()
            for event in events
            if event.get("event_type") == "macro_source_failed" and str(event.get("source_id") or "").strip()
        }
    )
    return {
        "missing_source_ids": sorted(missing),
        "failed_source_ids": failed,
        "unknown_dimensions": sorted(unknown_dimensions),
        "unproven_facts": unproven_facts,
        "unproven_observations": unproven_observations,
        "gdelt": "excluded",
        "news_macro_brief": "excluded",
    }


def _read_attach(state_dir: Path) -> dict[str, Any]:
    db_path = state_dir / "world_model.db"
    payload = {
        "status": "not_started",
        "exists": False,
        "lane_identity": WORLD_MACRO_LANE_IDENTITY,
        "episodes": 0,
        "macro_present": 0,
        "macro_missing": 0,
    }
    if not db_path.exists():
        return payload
    payload["exists"] = True
    try:
        with shared_prediction_storage_lease(db_path, create=False):
            uri = f"file:{quote(str(db_path.resolve()), safe='/')}?mode=ro&immutable=1"
            connection = sqlite3.connect(uri, uri=True)
            try:
                tables = {str(row[0]) for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if "world_episodes" not in tables:
                    payload["status"] = "schema_unavailable"
                    return payload
                rows = connection.execute("SELECT payload_json FROM world_episodes").fetchall()
            finally:
                connection.close()
    except (PredictionStorageBusyError, OSError, sqlite3.Error) as exc:
        payload["status"] = "unavailable"
        payload["error"] = f"{type(exc).__name__}:{exc}"
        return payload

    present = 0
    missing = 0
    for (raw,) in rows:
        status = _episode_macro_status(raw)
        if status in _ATTACHED_MACRO:
            present += 1
        else:
            missing += 1
    payload["episodes"] = present + missing
    payload["macro_present"] = present
    payload["macro_missing"] = missing
    if present and missing:
        payload["status"] = "partial"
    elif present:
        payload["status"] = "attached"
    else:
        payload["status"] = "unattached"
    return payload


def _episode_macro_status(raw: object) -> str | None:
    if not isinstance(raw, str):
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return _find_macro_status(payload)


def _find_macro_status(value: object) -> str | None:
    if isinstance(value, Mapping):
        features = value.get("categorical_features")
        if isinstance(features, Mapping):
            status = features.get("macro_status")
            if isinstance(status, str) and status.strip():
                return status.strip()
        nested = value.get("observation")
        if nested is not None:
            found = _find_macro_status(nested)
            if found is not None:
                return found
        context = value.get("context")
        if context is not None:
            found = _find_macro_status(context)
            if found is not None:
                return found
        return None
    if isinstance(value, (list, tuple)):
        for item in value:
            found = _find_macro_status(item)
            if found is not None:
                return found
    return None


def _utc(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


__all__ = ["MACRO_STATUS_SCHEMA", "WORLD_MACRO_LANE_IDENTITY", "read_world_macro_status"]
