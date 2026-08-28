"""Read-only WAL-aware query adapter for the dedicated world-model ledger.

This adapter never creates, migrates, or writes ``world_model.db``.  A missing
file stays missing.  Indexed ``model_kind``, ``move_class``, and cohort
lineage columns are the storage authority over nested payload claims.
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any
from urllib.parse import quote

from trader.domain.world_episode import DEFAULT_WORLD_HORIZONS
from trader.infrastructure.state_db.sqlite_in import sqlite_in_chunks, sqlite_placeholders

HORIZONS = tuple(item.horizon_id for item in DEFAULT_WORLD_HORIZONS)
_REQUIRED_TABLES = frozenset(
    {
        "world_episodes",
        "world_outcome_events",
        "world_shadow_predictions",
    }
)
_COHORT_TABLES = frozenset(
    {
        "world_cohort_manifests",
        "world_cohort_events",
        "world_cohort_slots",
        "world_episodes",
        "world_outcome_events",
        "world_shadow_predictions",
    }
)
_PREDICTION_COHORT_FIELDS = (
    "study_cohort_id",
    "lane_id",
    "manifest_sha256",
    "feature_contract_fingerprint",
    "feature_mask_fingerprint",
)
_EPISODE_SLOT_FIELDS = ("venue", "symbol", "bar_interval", "as_of_bar_ts")
_PATTERN_TABLES = frozenset(
    {
        "world_pattern_hypothesis_events",
        "world_pattern_occurrence_events",
        "world_pattern_outcome_links",
    }
)
_PATTERN_REQUIRED_TABLES = _PATTERN_TABLES
_PATTERN_OCCURRENCE_FIELDS = (
    "event_id",
    "occurrence_id",
    "hypothesis_id",
    "event_type",
    "sequence",
    "cohort_id",
    "cutoff_at",
    "horizon_id",
)
_PATTERN_HYPOTHESIS_FIELDS = ("event_id", "hypothesis_id", "event_type", "sequence")
_PATTERN_LINK_FIELDS = (
    "link_id",
    "occurrence_id",
    "event_id",
    "horizon_id",
    "world_outcome_event_id",
    "world_outcome_content_sha256",
    "supersedes_link_id",
)
_PATTERN_LIFECYCLE_TABLE = "world_pattern_lifecycle_events"
_PATTERN_LIFECYCLE_FIELDS = (
    "event_id",
    "event_type",
    "evaluation_cohort_id",
    "started_event_id",
    "manifest_sha256",
    "formation_cutoff",
    "evaluation_start_not_before",
    "formation_dataset_fingerprint",
    "evaluation_dataset_fingerprint",
    "selected_count",
)
_KNOWN_TABLES = _COHORT_TABLES | _PATTERN_TABLES | frozenset({"world_availability_receipts", _PATTERN_LIFECYCLE_TABLE})


def read_world_model_ledger(db_path: str | Path) -> dict[str, Any]:
    """Read reconstructed active-leaf rows without creating the ledger."""

    path = Path(db_path)
    if not path.exists():
        return {
            "status": "not_started",
            "exists": False,
            "counts": _empty_counts(),
            "observed_by_horizon": {},
            "active_outcomes_by_status": {},
            "pending_horizon_slots": 0,
            "predictions_by_model": {},
            "episodes": [],
            "outcomes": [],
            "predictions": [],
        }

    try:
        with _readonly_connection(path) as connection:
            tables = _table_names(connection)
            if not _REQUIRED_TABLES.issubset(tables):
                return {
                    "status": "schema_unavailable",
                    "exists": True,
                    "missing_tables": sorted(_REQUIRED_TABLES.difference(tables)),
                }
            episodes = int(connection.execute("SELECT COUNT(*) FROM world_episodes").fetchone()[0])
            eligible = int(
                connection.execute("SELECT COUNT(*) FROM world_episodes WHERE training_eligible=1").fetchone()[0]
            )
            outcome_rows = _fetch_outcomes(connection)
            prediction_rows = _fetch_predictions(connection)
            prediction_episode_rows = _fetch_prediction_episode_views(
                connection,
                {str(row["episode_id"]) for row in prediction_rows if row["episode_id"] not in (None, "")},
            )
    except (OSError, sqlite3.Error, json.JSONDecodeError) as exc:
        return {
            "status": "unavailable",
            "exists": True,
            "error": f"{type(exc).__name__}:{exc}",
        }

    try:
        return _project_loaded_ledger(
            episodes=episodes,
            eligible=eligible,
            outcome_rows=outcome_rows,
            prediction_rows=prediction_rows,
            prediction_episode_rows=prediction_episode_rows,
        )
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        return {
            "status": "unavailable",
            "exists": True,
            "error": f"{type(exc).__name__}:{exc}",
        }


def read_world_cohort_ledger(db_path: str | Path, cohort_id: str) -> dict[str, Any]:
    """Read one reconstructible cohort without creating or migrating the ledger."""

    path = Path(db_path)
    if not path.exists():
        return {
            "status": "not_started",
            "exists": False,
            "cohort_id": cohort_id,
            "manifest": None,
            "events": [],
            "slots": [],
            "episodes": [],
            "outcomes": [],
            "predictions": [],
            "receipts": [],
        }

    try:
        with _readonly_connection(path) as connection:
            tables = _table_names(connection)
            if not _COHORT_TABLES.issubset(tables):
                return {
                    "status": "schema_unavailable",
                    "exists": True,
                    "cohort_id": cohort_id,
                    "missing_tables": sorted(_COHORT_TABLES.difference(tables)),
                }
            prediction_columns = _table_columns(connection, "world_shadow_predictions")
            if "study_cohort_id" not in prediction_columns:
                return {
                    "status": "schema_unavailable",
                    "exists": True,
                    "cohort_id": cohort_id,
                    "missing_tables": ["world_shadow_predictions.study_cohort_id"],
                }
            manifest_row = connection.execute(
                "SELECT payload_json FROM world_cohort_manifests WHERE cohort_id=?",
                (cohort_id,),
            ).fetchone()
            if manifest_row is None:
                return {
                    "status": "unknown_cohort",
                    "exists": True,
                    "cohort_id": cohort_id,
                    "manifest": None,
                    "events": [],
                    "slots": [],
                    "episodes": [],
                    "outcomes": [],
                    "predictions": [],
                    "receipts": [],
                }
            event_rows = connection.execute(
                "SELECT payload_json FROM world_cohort_events WHERE cohort_id=? ORDER BY sequence ASC, event_id ASC",
                (cohort_id,),
            ).fetchall()
            slot_rows = connection.execute(
                "SELECT payload_json FROM world_cohort_slots WHERE cohort_id=? ORDER BY as_of_bar_ts ASC, slot_id ASC",
                (cohort_id,),
            ).fetchall()
            receipt_rows: list[sqlite3.Row] = []
            if "world_availability_receipts" in tables:
                receipt_rows = connection.execute(
                    "SELECT payload_json FROM world_availability_receipts "
                    "WHERE subject_kind='world_cohort_event' ORDER BY ready_at, receipt_id"
                ).fetchall()
            prediction_rows = _fetch_predictions(connection, study_cohort_id=cohort_id)
            episode_ids = {str(row["episode_id"]) for row in prediction_rows if row["episode_id"]}
            slots = [_json_object(row["payload_json"]) for row in slot_rows]
            for slot in slots:
                refs = slot.get("episode_refs_by_contract")
                if isinstance(refs, dict):
                    episode_ids.update(str(value) for value in refs.values() if value)
            episodes = _fetch_episodes(connection, episode_ids)
            outcomes = _fetch_outcomes(connection, episode_ids=episode_ids)
    except (OSError, sqlite3.Error, json.JSONDecodeError) as exc:
        return {
            "status": "unavailable",
            "exists": True,
            "cohort_id": cohort_id,
            "error": f"{type(exc).__name__}:{exc}",
        }

    try:
        return {
            "status": "loaded",
            "exists": True,
            "cohort_id": cohort_id,
            "manifest": _json_object(manifest_row["payload_json"]),
            "events": [_json_object(row["payload_json"]) for row in event_rows],
            "slots": slots,
            "episodes": [_project_episode(row) for row in episodes],
            "outcomes": [_project_outcome(row) for row in outcomes],
            "predictions": [_project_prediction(row) for row in prediction_rows],
            "receipts": [_json_object(row["payload_json"]) for row in receipt_rows],
        }
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        return {
            "status": "unavailable",
            "exists": True,
            "cohort_id": cohort_id,
            "error": f"{type(exc).__name__}:{exc}",
        }


def read_world_cohort_catalog(db_path: str | Path) -> dict[str, Any]:
    """List reconstructible cohort snapshots without creating or migrating the ledger."""

    path = Path(db_path)
    if not path.exists():
        return {"status": "not_started", "exists": False, "cohorts": []}

    try:
        with _readonly_connection(path) as connection:
            tables = _table_names(connection)
            if not _COHORT_TABLES.issubset(tables):
                return {
                    "status": "schema_unavailable",
                    "exists": True,
                    "missing_tables": sorted(_COHORT_TABLES.difference(tables)),
                    "cohorts": [],
                }
            manifest_rows = connection.execute(
                "SELECT cohort_id, payload_json FROM world_cohort_manifests ORDER BY cohort_id ASC"
            ).fetchall()
            event_rows = connection.execute(
                "SELECT cohort_id, payload_json FROM world_cohort_events "
                "ORDER BY cohort_id ASC, sequence ASC, event_id ASC"
            ).fetchall()
    except (OSError, sqlite3.Error, json.JSONDecodeError) as exc:
        return {
            "status": "unavailable",
            "exists": True,
            "error": f"{type(exc).__name__}:{exc}",
            "cohorts": [],
        }

    try:
        events_by_cohort: dict[str, list[dict[str, Any]]] = {}
        for row in event_rows:
            cohort_id = str(row["cohort_id"])
            events_by_cohort.setdefault(cohort_id, []).append(_json_object(row["payload_json"]))
        return {
            "status": "loaded",
            "exists": True,
            "cohorts": [
                {
                    "cohort_id": str(row["cohort_id"]),
                    "manifest": _json_object(row["payload_json"]),
                    "events": events_by_cohort.get(str(row["cohort_id"]), []),
                }
                for row in manifest_rows
            ],
        }
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        return {
            "status": "unavailable",
            "exists": True,
            "error": f"{type(exc).__name__}:{exc}",
            "cohorts": [],
        }


def read_world_pattern_ledger(db_path: str | Path, cohort_id: str | None = None) -> dict[str, Any]:
    """Read reconstructible pattern events without creating or migrating the ledger.

    Hypothesis events for an evaluating cohort are included even when that
    hypothesis has zero occurrences. Outcome rows are optional.
    """

    path = Path(db_path)
    empty = {
        "cohort_id": cohort_id,
        "hypothesis_events": [],
        "occurrence_events": [],
        "outcome_links": [],
        "lifecycle_events": [],
        "episodes": [],
        "outcomes": [],
        "predictions": [],
        "receipts": [],
    }
    if not path.exists():
        return {"status": "not_started", "exists": False, **empty}

    try:
        with _readonly_connection(path) as connection:
            tables = _table_names(connection)
            if not _PATTERN_REQUIRED_TABLES.issubset(tables):
                return {
                    "status": "schema_unavailable",
                    "exists": True,
                    "cohort_id": cohort_id,
                    "missing_tables": sorted(_PATTERN_REQUIRED_TABLES.difference(tables)),
                }
            occurrence_rows = _fetch_pattern_occurrence_events(connection, cohort_id)
            hypothesis_ids = {
                str(row["hypothesis_id"])
                for row in occurrence_rows
                if "hypothesis_id" in row.keys() and row["hypothesis_id"]
            }
            hypothesis_ids.update(_hypothesis_ids_started_for_cohort(connection, cohort_id))
            occurrence_ids = {
                str(row["occurrence_id"])
                for row in occurrence_rows
                if "occurrence_id" in row.keys() and row["occurrence_id"]
            }
            hypothesis_rows = _fetch_pattern_hypothesis_events(connection, hypothesis_ids)
            link_rows = _fetch_pattern_outcome_links(connection, occurrence_ids)
            lifecycle_rows = (
                _fetch_pattern_lifecycle_events(connection, cohort_id) if _PATTERN_LIFECYCLE_TABLE in tables else []
            )
            episode_ids = _pattern_episode_ids(occurrence_rows)
            prediction_rows: list[sqlite3.Row] = []
            if "world_shadow_predictions" in tables:
                prediction_columns = _table_columns(connection, "world_shadow_predictions")
                if cohort_id is not None and "study_cohort_id" in prediction_columns:
                    prediction_rows = _fetch_predictions(connection, study_cohort_id=cohort_id)
                else:
                    prediction_rows = _fetch_predictions(connection)
                episode_ids.update(str(row["episode_id"]) for row in prediction_rows if row["episode_id"])
            episodes = _fetch_episodes(connection, episode_ids) if "world_episodes" in tables else []
            outcomes = _fetch_outcomes(connection, episode_ids=episode_ids) if "world_outcome_events" in tables else []
            receipt_rows: list[sqlite3.Row] = []
            if "world_availability_receipts" in tables:
                receipt_rows = connection.execute(
                    "SELECT payload_json FROM world_availability_receipts "
                    "WHERE subject_kind IN ('pattern_hypothesis_event', 'pattern_occurrence_event') "
                    "ORDER BY ready_at, receipt_id"
                ).fetchall()
    except (OSError, sqlite3.Error, json.JSONDecodeError) as exc:
        return {
            "status": "unavailable",
            "exists": True,
            "cohort_id": cohort_id,
            "error": f"{type(exc).__name__}:{exc}",
        }

    try:
        return {
            "status": "loaded",
            "exists": True,
            "cohort_id": cohort_id,
            "hypothesis_events": [_project_pattern_hypothesis_event(row) for row in hypothesis_rows],
            "occurrence_events": [_project_pattern_occurrence_event(row) for row in occurrence_rows],
            "outcome_links": [_project_pattern_outcome_link(row) for row in link_rows],
            "lifecycle_events": [_project_pattern_lifecycle_event(row) for row in lifecycle_rows],
            "episodes": [_project_episode(row) for row in episodes],
            "outcomes": [_project_outcome(row) for row in outcomes],
            "predictions": [_project_prediction(row) for row in prediction_rows],
            "receipts": [_json_object(row["payload_json"]) for row in receipt_rows],
        }
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        return {
            "status": "unavailable",
            "exists": True,
            "cohort_id": cohort_id,
            "error": f"{type(exc).__name__}:{exc}",
        }


def _readonly_connection(path: Path) -> sqlite3.Connection:
    uri = f"file:{quote(str(path.resolve()), safe='/')}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _table_names(connection: sqlite3.Connection) -> set[str]:
    return {str(row[0]) for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _table_columns(connection: sqlite3.Connection, table: str) -> frozenset[str]:
    if table not in _KNOWN_TABLES:
        raise ValueError(f"unknown world-model table: {table}")
    return frozenset(str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})"))


def _fetch_predictions(
    connection: sqlite3.Connection,
    *,
    study_cohort_id: str | None = None,
) -> list[sqlite3.Row]:
    columns = _table_columns(connection, "world_shadow_predictions")
    selected = [
        name
        for name in (
            "prediction_id",
            "episode_id",
            "horizon_code",
            "model_kind",
            "model_version",
            "predicted_at",
            "recorded_at",
            "payload_json",
            *_PREDICTION_COHORT_FIELDS,
        )
        if name in columns
    ]
    sql = f"SELECT {', '.join(selected)} FROM world_shadow_predictions"
    params: tuple[str, ...] = ()
    if study_cohort_id is not None:
        sql += " WHERE study_cohort_id=?"
        params = (study_cohort_id,)
    sql += " ORDER BY recorded_at, prediction_id"
    return list(connection.execute(sql, params).fetchall())


def _fetch_outcomes(
    connection: sqlite3.Connection,
    *,
    episode_ids: set[str] | None = None,
) -> list[sqlite3.Row]:
    columns = _table_columns(connection, "world_outcome_events")
    selected = [
        name
        for name in (
            "outcome_event_id",
            "supersedes_outcome_event_id",
            "episode_id",
            "horizon_code",
            "status",
            "move_class",
            "training_eligible",
            "label_available_at",
            "sealed_at",
            "payload_json",
            "evidence_sha256",
            "label_json",
            "evidence_json",
        )
        if name in columns
    ]
    sql = f"SELECT {', '.join(selected)} FROM world_outcome_events"
    order = " ORDER BY recorded_at, outcome_event_id" if "recorded_at" in columns else " ORDER BY outcome_event_id"
    if episode_ids is None:
        return list(connection.execute(sql + order).fetchall())
    if not episode_ids:
        return []
    rows: list[sqlite3.Row] = []
    for chunk in sqlite_in_chunks(episode_ids):
        chunk_sql = f"{sql} WHERE episode_id IN ({sqlite_placeholders(len(chunk))}){order}"
        rows.extend(connection.execute(chunk_sql, chunk).fetchall())

    def _sort_key(row: sqlite3.Row) -> tuple[str, str]:
        keys = row.keys()
        recorded = str(row["recorded_at"]) if "recorded_at" in keys else ""
        return (recorded, str(row["outcome_event_id"]))

    rows.sort(key=_sort_key)
    return rows


def _fetch_pattern_occurrence_events(
    connection: sqlite3.Connection,
    cohort_id: str | None,
) -> list[sqlite3.Row]:
    columns = _table_columns(connection, "world_pattern_occurrence_events")
    selected = [name for name in (*_PATTERN_OCCURRENCE_FIELDS, "payload_json") if name in columns]
    sql = f"SELECT {', '.join(selected)} FROM world_pattern_occurrence_events"
    params: tuple[str, ...] = ()
    if cohort_id is not None:
        sql += " WHERE cohort_id=?"
        params = (cohort_id,)
    sql += " ORDER BY sequence ASC, event_id ASC"
    return list(connection.execute(sql, params).fetchall())


def _hypothesis_ids_started_for_cohort(connection: sqlite3.Connection, cohort_id: str | None) -> set[str]:
    columns = _table_columns(connection, "world_pattern_hypothesis_events")
    selected = [name for name in (*_PATTERN_HYPOTHESIS_FIELDS, "payload_json") if name in columns]
    sql = f"SELECT {', '.join(selected)} FROM world_pattern_hypothesis_events"
    rows = list(connection.execute(sql).fetchall())
    started: set[str] = set()
    for row in rows:
        hypothesis_id = str(row["hypothesis_id"]) if "hypothesis_id" in row.keys() and row["hypothesis_id"] else ""
        if not hypothesis_id:
            continue
        if cohort_id is None:
            started.add(hypothesis_id)
            continue
        if str(row["event_type"]) != "pattern_evaluation_started":
            continue
        payload = _json_object(row["payload_json"]) if "payload_json" in row.keys() else {}
        if str(payload.get("evaluation_cohort_id") or "") == cohort_id:
            started.add(hypothesis_id)
    return started


def _fetch_pattern_hypothesis_events(
    connection: sqlite3.Connection,
    hypothesis_ids: set[str],
) -> list[sqlite3.Row]:
    if not hypothesis_ids:
        return []
    columns = _table_columns(connection, "world_pattern_hypothesis_events")
    selected = [name for name in (*_PATTERN_HYPOTHESIS_FIELDS, "payload_json") if name in columns]
    rows: list[sqlite3.Row] = []
    for chunk in sqlite_in_chunks(hypothesis_ids):
        sql = (
            f"SELECT {', '.join(selected)} FROM world_pattern_hypothesis_events "
            f"WHERE hypothesis_id IN ({sqlite_placeholders(len(chunk))}) "
            "ORDER BY hypothesis_id ASC, sequence ASC, event_id ASC"
        )
        rows.extend(connection.execute(sql, chunk).fetchall())
    rows.sort(key=lambda row: (str(row["hypothesis_id"] or ""), int(row["sequence"] or 0), str(row["event_id"] or "")))
    return rows


def _fetch_pattern_outcome_links(
    connection: sqlite3.Connection,
    occurrence_ids: set[str],
) -> list[sqlite3.Row]:
    if not occurrence_ids:
        return []
    columns = _table_columns(connection, "world_pattern_outcome_links")
    selected = [name for name in (*_PATTERN_LINK_FIELDS, "payload_json") if name in columns]
    rows: list[sqlite3.Row] = []
    for chunk in sqlite_in_chunks(occurrence_ids):
        sql = (
            f"SELECT {', '.join(selected)} FROM world_pattern_outcome_links "
            f"WHERE occurrence_id IN ({sqlite_placeholders(len(chunk))}) "
            "ORDER BY occurrence_id ASC, horizon_id ASC, link_id ASC"
        )
        rows.extend(connection.execute(sql, chunk).fetchall())
    rows.sort(
        key=lambda row: (str(row["occurrence_id"] or ""), str(row["horizon_id"] or ""), str(row["link_id"] or ""))
    )
    return rows


def _fetch_pattern_lifecycle_events(
    connection: sqlite3.Connection,
    cohort_id: str | None,
) -> list[sqlite3.Row]:
    columns = _table_columns(connection, _PATTERN_LIFECYCLE_TABLE)
    selected = [name for name in (*_PATTERN_LIFECYCLE_FIELDS, "payload_json") if name in columns]
    sql = f"SELECT {', '.join(selected)} FROM {_PATTERN_LIFECYCLE_TABLE}"
    params: tuple[str, ...] = ()
    if cohort_id is not None:
        sql += " WHERE evaluation_cohort_id=?"
        params = (cohort_id,)
    sql += " ORDER BY evaluation_cohort_id ASC, event_id ASC"
    return list(connection.execute(sql, params).fetchall())


def _pattern_episode_ids(rows: list[sqlite3.Row]) -> set[str]:
    episode_ids: set[str] = set()
    for row in rows:
        payload = _json_object(row["payload_json"]) if "payload_json" in row.keys() else {}
        occurrence = payload.get("occurrence")
        forecast = occurrence.get("forecast") if isinstance(occurrence, dict) else payload.get("forecast")
        if isinstance(forecast, dict):
            episode_id = forecast.get("episode_id")
            if episode_id:
                episode_ids.add(str(episode_id))
    return episode_ids


def _project_pattern_hypothesis_event(row: sqlite3.Row) -> dict[str, Any]:
    payload = _json_object(row["payload_json"])
    projected = {**payload}
    for field in _PATTERN_HYPOTHESIS_FIELDS:
        if field in row.keys() and row[field] not in (None, ""):
            projected[field] = row[field]
    return projected


def _project_pattern_occurrence_event(row: sqlite3.Row) -> dict[str, Any]:
    payload = _json_object(row["payload_json"])
    projected = {**payload}
    for field in _PATTERN_OCCURRENCE_FIELDS:
        if field in row.keys() and row[field] not in (None, ""):
            projected[field] = row[field]
    occurrence = projected.get("occurrence")
    if isinstance(occurrence, dict):
        if projected.get("cohort_id"):
            occurrence["cohort_id"] = projected["cohort_id"]
        if projected.get("hypothesis_id"):
            occurrence["hypothesis_id"] = projected["hypothesis_id"]
        if projected.get("occurrence_id"):
            occurrence["occurrence_id"] = projected["occurrence_id"]
        forecast = occurrence.get("forecast")
        if isinstance(forecast, dict) and projected.get("horizon_id"):
            forecast["horizon_id"] = projected["horizon_id"]
    return projected


def _project_pattern_outcome_link(row: sqlite3.Row) -> dict[str, Any]:
    payload = _json_object(row["payload_json"])
    projected = {**payload}
    for field in _PATTERN_LINK_FIELDS:
        if field in row.keys() and row[field] not in (None, ""):
            projected[field] = row[field]
    return projected


def _project_pattern_lifecycle_event(row: sqlite3.Row) -> dict[str, Any]:
    payload = _json_object(row["payload_json"])
    projected = {**payload}
    for field in _PATTERN_LIFECYCLE_FIELDS:
        if field in row.keys() and row[field] not in (None, ""):
            projected[field] = row[field] if field != "selected_count" else int(row[field])
    return projected


def _fetch_episodes(connection: sqlite3.Connection, episode_ids: set[str]) -> list[sqlite3.Row]:
    if not episode_ids:
        return []
    columns = _table_columns(connection, "world_episodes")
    selected = [
        name
        for name in (
            "episode_id",
            "venue",
            "symbol",
            "bar_interval",
            "as_of_bar_ts",
            "feature_contract_version",
            "sampling_policy_version",
            "payload_json",
        )
        if name in columns
    ]
    return _fetch_episode_rows_by_ids(connection, selected=selected, episode_ids=episode_ids)


def _fetch_episode_rows_by_ids(
    connection: sqlite3.Connection,
    *,
    selected: list[str],
    episode_ids: set[str],
) -> list[sqlite3.Row]:
    rows: list[sqlite3.Row] = []
    for chunk in sqlite_in_chunks(episode_ids):
        sql = (
            f"SELECT {', '.join(selected)} FROM world_episodes WHERE episode_id IN ({sqlite_placeholders(len(chunk))})"
        )
        rows.extend(connection.execute(sql, chunk).fetchall())
    rows.sort(key=lambda row: (str(row["as_of_bar_ts"] or ""), str(row["episode_id"] or "")))
    return rows


def _fetch_prediction_episode_views(
    connection: sqlite3.Connection,
    episode_ids: set[str],
) -> list[sqlite3.Row]:
    """Load one lean canonical input reference per predicted episode.

    Evaluation needs the market anchor, feature contract, and context status,
    not another copy of the full immutable observation.  The JSON extraction
    is deliberately projected once per episode rather than once per prediction.
    """

    if not episode_ids:
        return []
    columns = _table_columns(connection, "world_episodes")
    selected = [
        name
        for name in (
            "episode_id",
            "venue",
            "symbol",
            "bar_interval",
            "as_of_bar_ts",
            "feature_contract_version",
        )
        if name in columns
    ]
    if "payload_json" in columns:
        selected.append(
            "COALESCE("
            "json_extract(payload_json, '$.observation.context.status'), "
            "json_extract(payload_json, '$.context.status')"
            ") AS context_status"
        )
    return _fetch_episode_rows_by_ids(connection, selected=selected, episode_ids=episode_ids)


def _project_loaded_ledger(
    *,
    episodes: int,
    eligible: int,
    outcome_rows: list[sqlite3.Row],
    prediction_rows: list[sqlite3.Row],
    prediction_episode_rows: list[sqlite3.Row],
) -> dict[str, Any]:
    superseded_ids = {
        str(row["supersedes_outcome_event_id"])
        for row in outcome_rows
        if "supersedes_outcome_event_id" in row.keys() and row["supersedes_outcome_event_id"]
    }
    active_outcome_rows = [row for row in outcome_rows if str(row["outcome_event_id"]) not in superseded_ids]
    active_statuses = Counter(str(row["status"]).lower() for row in active_outcome_rows)
    observed_slots = {
        (str(row["episode_id"]), str(row["horizon_code"]))
        for row in active_outcome_rows
        if str(row["status"]).lower() == "observed"
    }
    observed = Counter(horizon for _episode, horizon in observed_slots)
    terminal_slots = {
        (str(row["episode_id"]), str(row["horizon_code"]))
        for row in active_outcome_rows
        if str(row["status"]).lower() in {"observed", "missing", "unknown"}
    }
    outcomes = [_project_outcome(row) for row in active_outcome_rows]
    predictions = [_project_prediction(row) for row in prediction_rows]
    predictions_by_model = Counter(
        f"{str(row['model_kind'] or 'unknown-model')}@{str(row['model_version'] or 'unknown-version')}"
        for row in prediction_rows
    )
    return {
        "status": "loaded",
        "exists": True,
        "counts": {
            "episodes": episodes,
            "eligible_episodes": eligible,
            "outcome_events": len(outcome_rows),
            "active_outcomes": len(active_outcome_rows),
            "predictions": len(prediction_rows),
        },
        "observed_by_horizon": dict(sorted(observed.items())),
        "active_outcomes_by_status": dict(sorted(active_statuses.items())),
        "pending_horizon_slots": max(0, episodes * len(HORIZONS) - len(terminal_slots)),
        "predictions_by_model": dict(sorted(predictions_by_model.items())),
        "episodes": [dict(row) for row in prediction_episode_rows],
        "outcomes": outcomes,
        "predictions": predictions,
    }


def _project_prediction(row: sqlite3.Row) -> dict[str, Any]:
    payload = _json_object(row["payload_json"])
    projected = {
        **payload,
        "prediction_id": row["prediction_id"],
        "episode_id": row["episode_id"],
        "horizon_code": row["horizon_code"],
        "model_kind": row["model_kind"],
        "model_id": row["model_kind"],
        "model_version": row["model_version"],
        "predicted_at": row["predicted_at"],
        "ready_at": row["recorded_at"],
        "recorded_at": row["recorded_at"],
    }
    keys = set(row.keys())
    for field in _PREDICTION_COHORT_FIELDS:
        if field not in keys:
            continue
        value = row[field]
        if value not in (None, ""):
            projected[field] = value
            nested = projected.get("prediction")
            if isinstance(nested, dict):
                nested[field] = value
            record = projected.get("prediction_record")
            if isinstance(record, dict):
                record[field] = value
    return projected


def _project_outcome(row: sqlite3.Row) -> dict[str, Any]:
    payload = _json_object(row["payload_json"])
    projected = {
        **payload,
        "outcome_event_id": row["outcome_event_id"],
        "episode_id": row["episode_id"],
        "horizon_code": row["horizon_code"],
        "horizon_id": payload.get("horizon_id") or row["horizon_code"],
        "status": row["status"],
        "move_class": row["move_class"],
        "direction": row["move_class"],
        "training_eligible": bool(row["training_eligible"]),
        "label_available_at": row["label_available_at"],
        "sealed_at": row["sealed_at"],
    }
    keys = set(row.keys())
    if "evidence_sha256" in keys and row["evidence_sha256"] not in (None, ""):
        projected["evidence_sha256"] = row["evidence_sha256"]
    if "label_json" in keys and row["label_json"] not in (None, ""):
        label = _json_object(row["label_json"])
        if label:
            projected["label"] = {**(_as_dict(projected.get("label"))), **label}
            if projected["label"].get("target_at") and not projected.get("target_at"):
                projected["target_at"] = projected["label"]["target_at"]
    if "evidence_json" in keys and row["evidence_json"] not in (None, ""):
        evidence = _json_object(row["evidence_json"])
        if evidence:
            projected["evidence"] = evidence
    return projected


def _project_episode(row: sqlite3.Row) -> dict[str, Any]:
    payload = _json_object(row["payload_json"]) if "payload_json" in row.keys() else {}
    projected = {**payload, "episode_id": row["episode_id"]}
    for field in (*_EPISODE_SLOT_FIELDS, "feature_contract_version", "sampling_policy_version"):
        if field in row.keys() and row[field] not in (None, ""):
            projected[field] = row[field]
    return projected


def _empty_counts() -> dict[str, int]:
    return {
        "episodes": 0,
        "eligible_episodes": 0,
        "outcome_events": 0,
        "active_outcomes": 0,
        "predictions": 0,
    }


def _json_object(value: object) -> dict[str, Any]:
    parsed = json.loads(str(value))
    return parsed if isinstance(parsed, dict) else {}


def _as_dict(value: object) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


__all__ = [
    "HORIZONS",
    "read_world_cohort_catalog",
    "read_world_cohort_ledger",
    "read_world_model_ledger",
    "read_world_pattern_ledger",
]
