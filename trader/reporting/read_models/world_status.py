"""Read-model projector for World Model shadow status, evaluation, and impact."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from trader.infrastructure.state_db.world_model_query import HORIZONS, read_world_model_ledger
from trader.reporting.read_models.world_evaluation import evaluate_shadow
from trader.reporting.read_models.world_impact import evaluate_world_shadow_impact
from trader.reporting.read_models.world_macro_status import read_world_macro_status

_DEFAULT_CONFIG_DIR = Path(__file__).resolve().parents[3] / "config"


def _resource_budget_status(db_path: Path, *, config_dir: str | Path | None = None) -> dict[str, Any]:
    """Live budget overlay. Never creates ``world_model.db``."""

    try:
        from trader.application.world_model.resource_budget import load_world_shadow_resource_budget
        from trader.domain.world_resource import decide_world_resource_budget
        from trader.infrastructure.state_db.world_resource_probe import FilesystemWorldResourceProbe

        budget = load_world_shadow_resource_budget(config_dir or _DEFAULT_CONFIG_DIR)
        usage = FilesystemWorldResourceProbe(db_path).measure()
        return decide_world_resource_budget(budget, usage).to_dict()
    except Exception as exc:  # noqa: BLE001 - status remains readable if the probe fails
        return {
            "status": "unavailable",
            "reason": "probe_error",
            "authority": "shadow_only",
            "decision_effect": "none",
            "recommendation": "NO_GO",
            "causal_claim": False,
            "pnl_claim": False,
            "error": f"{type(exc).__name__}:{exc}",
        }


def read_world_model_status(
    state_dir: str | Path,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Project live world-model status without creating or migrating the ledger."""

    db_path = Path(state_dir) / "world_model.db"
    ledger = read_world_model_ledger(db_path)
    macro = read_world_macro_status(state_dir, now=now)
    base: dict[str, Any] = {
        "schema_version": "world_model_status.v1",
        "authority": "shadow_only",
        "decision_effect": "none",
        "recommendation": "NO_GO",
        "causal_claim": False,
        "pnl_claim": False,
        "actual_trader_contribution": "not_attributable",
        "db_path": str(db_path),
        "exists": bool(ledger.get("exists")),
        "macro": macro,
        "resource_budget": _resource_budget_status(db_path),
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
