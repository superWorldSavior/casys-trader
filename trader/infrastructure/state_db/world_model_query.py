"""Read-only WAL-aware query adapter for the dedicated world-model ledger.

This adapter never creates, migrates, or writes ``world_model.db``.  A missing
file stays missing.  Indexed ``model_kind`` and ``move_class`` are the storage
authority over nested payload claims.
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any
from urllib.parse import quote

from trader.domain.world_episode import DEFAULT_WORLD_HORIZONS

HORIZONS = tuple(item.horizon_id for item in DEFAULT_WORLD_HORIZONS)
_REQUIRED_TABLES = frozenset(
    {
        "world_episodes",
        "world_outcome_events",
        "world_shadow_predictions",
    }
)


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
            "outcomes": [],
            "predictions": [],
        }

    uri = f"file:{quote(str(path.resolve()), safe='/')}?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True)
        connection.row_factory = sqlite3.Row
        try:
            tables = {
                str(row[0])
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
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
            outcome_rows = connection.execute(
                "SELECT outcome_event_id, supersedes_outcome_event_id, episode_id, horizon_code, "
                "status, move_class, training_eligible, label_available_at, sealed_at, payload_json "
                "FROM world_outcome_events "
                "ORDER BY recorded_at, outcome_event_id"
            ).fetchall()
            prediction_rows = connection.execute(
                "SELECT prediction_id, episode_id, horizon_code, model_kind, model_version, "
                "predicted_at, recorded_at, payload_json FROM world_shadow_predictions "
                "ORDER BY recorded_at, prediction_id"
            ).fetchall()
        finally:
            connection.close()
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
        )
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        return {
            "status": "unavailable",
            "exists": True,
            "error": f"{type(exc).__name__}:{exc}",
        }


def _project_loaded_ledger(
    *,
    episodes: int,
    eligible: int,
    outcome_rows: list[sqlite3.Row],
    prediction_rows: list[sqlite3.Row],
) -> dict[str, Any]:
    superseded_ids = {
        str(row["supersedes_outcome_event_id"])
        for row in outcome_rows
        if row["supersedes_outcome_event_id"]
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
    outcomes = [
        {
            **_json_object(row["payload_json"]),
            "outcome_event_id": row["outcome_event_id"],
            "episode_id": row["episode_id"],
            "horizon_code": row["horizon_code"],
            "status": row["status"],
            "move_class": row["move_class"],
            # Indexed values are the immutable storage authority.  A
            # historical payload can contain a stale/contradictory
            # direction, but evaluation must score the indexed outcome.
            "direction": row["move_class"],
            "training_eligible": bool(row["training_eligible"]),
            "label_available_at": row["label_available_at"],
            "sealed_at": row["sealed_at"],
        }
        for row in active_outcome_rows
    ]
    predictions = [
        {
            **_json_object(row["payload_json"]),
            "prediction_id": row["prediction_id"],
            "episode_id": row["episode_id"],
            "horizon_code": row["horizon_code"],
            "model_kind": row["model_kind"],
            # The index, rather than a nested payload claim, determines
            # the cohort used for grouping and paired shadow scoring.
            "model_id": row["model_kind"],
            "model_version": row["model_version"],
            "predicted_at": row["predicted_at"],
            # ``predicted_at`` is the frozen market cutoff. ``ready_at``
            # is the actual append time and is the only clock admissible
            # for a future pre-decision Trader attribution link.
            "ready_at": row["recorded_at"],
            "recorded_at": row["recorded_at"],
        }
        for row in prediction_rows
    ]
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
        "outcomes": outcomes,
        "predictions": predictions,
    }


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


__all__ = ["HORIZONS", "read_world_model_ledger"]
