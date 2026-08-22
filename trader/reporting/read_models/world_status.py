"""Read-model projector for World Model shadow status, evaluation, and impact."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from trader.infrastructure.state_db.world_model_query import HORIZONS, read_world_model_ledger
from trader.reporting.read_models.world_evaluation import evaluate_shadow
from trader.reporting.read_models.world_impact import evaluate_world_shadow_impact


def read_world_model_status(state_dir: str | Path) -> dict[str, Any]:
    """Project live world-model status without creating or migrating the ledger."""

    db_path = Path(state_dir) / "world_model.db"
    ledger = read_world_model_ledger(db_path)
    base: dict[str, Any] = {
        "schema_version": "world_model_status.v1",
        "authority": "shadow_only",
        "decision_effect": "none",
        "db_path": str(db_path),
        "exists": bool(ledger.get("exists")),
    }
    status = str(ledger.get("status") or "unavailable")
    if status == "not_started":
        return {
            **base,
            "status": "not_started",
            "counts": ledger["counts"],
            "observed_by_horizon": ledger["observed_by_horizon"],
            "active_outcomes_by_status": ledger["active_outcomes_by_status"],
            "pending_horizon_slots": ledger["pending_horizon_slots"],
            "predictions_by_model": ledger["predictions_by_model"],
            "evaluation": evaluate_shadow([], []),
            "impact": evaluate_world_shadow_impact([], []),
        }
    if status == "schema_unavailable":
        return {
            **base,
            "status": "schema_unavailable",
            "missing_tables": ledger.get("missing_tables", []),
        }
    if status != "loaded":
        payload = {**base, "status": "unavailable"}
        if "error" in ledger:
            payload["error"] = ledger["error"]
        return payload

    evaluation = evaluate_shadow(ledger["predictions"], ledger["outcomes"])
    impact = evaluate_world_shadow_impact(ledger["predictions"], ledger["outcomes"])
    return {
        **base,
        "status": "evaluating" if evaluation["status"] == "ready" else "warming_up",
        "counts": ledger["counts"],
        "observed_by_horizon": ledger["observed_by_horizon"],
        "active_outcomes_by_status": ledger["active_outcomes_by_status"],
        "pending_horizon_slots": ledger["pending_horizon_slots"],
        "predictions_by_model": ledger["predictions_by_model"],
        "evaluation": evaluation,
        "impact": impact,
    }


__all__ = ["HORIZONS", "read_world_model_status"]
