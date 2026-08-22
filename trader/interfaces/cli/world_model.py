"""Read-only CLI projection for the shadow world-model ledger."""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any
from urllib.parse import quote

from trader.application.world_model.evaluation import evaluate_shadow

HORIZONS = ("elapsed_4h.v1", "elapsed_1d.v1")


def read_world_model_status(state_dir: str | Path) -> dict[str, Any]:
    """Read the live WAL-aware SQLite ledger without creating or migrating it."""

    db_path = Path(state_dir) / "world_model.db"
    base: dict[str, Any] = {
        "schema_version": "world_model_status.v1",
        "authority": "shadow_only",
        "decision_effect": "none",
        "db_path": str(db_path),
        "exists": db_path.exists(),
    }
    if not db_path.exists():
        return {
            **base,
            "status": "not_started",
            "counts": {
                "episodes": 0,
                "eligible_episodes": 0,
                "outcome_events": 0,
                "active_outcomes": 0,
                "predictions": 0,
            },
            "observed_by_horizon": {},
            "active_outcomes_by_status": {},
            "pending_horizon_slots": 0,
            "evaluation": evaluate_shadow([], []),
        }

    uri = f"file:{quote(str(db_path.resolve()), safe='/')}?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True)
        connection.row_factory = sqlite3.Row
        try:
            tables = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            required = {
                "world_episodes",
                "world_outcome_events",
                "world_shadow_predictions",
            }
            if not required.issubset(tables):
                return {
                    **base,
                    "status": "schema_unavailable",
                    "missing_tables": sorted(required.difference(tables)),
                }

            episodes = int(connection.execute("SELECT COUNT(*) FROM world_episodes").fetchone()[0])
            eligible = int(
                connection.execute(
                    "SELECT COUNT(*) FROM world_episodes WHERE training_eligible=1"
                ).fetchone()[0]
            )
            outcome_rows = connection.execute(
                "SELECT outcome_event_id, supersedes_outcome_event_id, episode_id, horizon_code, "
                "status, payload_json FROM world_outcome_events "
                "ORDER BY recorded_at, outcome_event_id"
            ).fetchall()
            prediction_rows = connection.execute(
                "SELECT payload_json FROM world_shadow_predictions "
                "ORDER BY recorded_at, prediction_id"
            ).fetchall()
        finally:
            connection.close()
    except (OSError, sqlite3.Error, json.JSONDecodeError) as exc:
        return {
            **base,
            "status": "unavailable",
            "error": f"{type(exc).__name__}:{exc}",
        }

    try:
        superseded_ids = {
            str(row["supersedes_outcome_event_id"])
            for row in outcome_rows
            if row["supersedes_outcome_event_id"]
        }
        active_outcome_rows = [
            row for row in outcome_rows if str(row["outcome_event_id"]) not in superseded_ids
        ]
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
        outcomes = [_json_object(row["payload_json"]) for row in active_outcome_rows]
        predictions = [_json_object(row["payload_json"]) for row in prediction_rows]
        evaluation = evaluate_shadow(predictions, outcomes)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        return {
            **base,
            "status": "unavailable",
            "error": f"{type(exc).__name__}:{exc}",
        }
    return {
        **base,
        "status": "evaluating" if evaluation["status"] == "ready" else "warming_up",
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
        "evaluation": evaluation,
    }


def _json_object(value: object) -> dict[str, Any]:
    parsed = json.loads(str(value))
    return parsed if isinstance(parsed, dict) else {}


__all__ = ["HORIZONS", "read_world_model_status"]
