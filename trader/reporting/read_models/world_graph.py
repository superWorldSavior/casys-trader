"""Read-only graph V3 budgets and gaps. Never creates ``world_model.db``.

Cohort activation is reconstructed from persisted graph-lane evidence. Authority
stays ``shadow_only`` with ``decision_effect=none``. Missing files stay missing.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from trader.domain.world_cohort import (
    COHORT_AUTHORITY,
    COHORT_DECISION_EFFECT,
    COHORT_RECOMMENDATION,
    COHORT_TRADER_CONTRIBUTION,
    CohortPhase,
    WorldCohort,
)
from trader.domain.world_feature_contract import (
    WORLD_GRAPH_V3_PATH_RULE_VERSION,
    WORLD_GRAPH_V3_WINDOWS_AND_DECAY,
)
from trader.domain.world_graph import GRAPH_TRAVERSAL_POLICY_VERSION
from trader.infrastructure.state_db.world_model_query import read_world_cohort_catalog
from trader.reporting.read_models.world_patterns import read_world_pattern_report
from trader.reporting.read_models.world_status import read_world_model_status


GRAPH_STATUS_SCHEMA = "world_graph_status.v1"
GRAPH_REPORT_SCHEMA = "world_graph_report.v1"
_GRAPH_V3_FLAG = "CASYS_WORLD_MODEL_GRAPH_V3_ENABLED"
_CLAIM_FIELDS = {
    "authority": COHORT_AUTHORITY,
    "decision_effect": COHORT_DECISION_EFFECT,
    "recommendation": COHORT_RECOMMENDATION,
    "causal_claim": False,
    "pnl_claim": False,
    "actual_trader_contribution": COHORT_TRADER_CONTRIBUTION,
}


def _graph_v3_budgets() -> dict[str, Any]:
    return {
        "max_depth": 4,
        "max_paths_per_root": 32,
        "policy_version": GRAPH_TRAVERSAL_POLICY_VERSION,
        "path_rule_version": WORLD_GRAPH_V3_PATH_RULE_VERSION,
        "windows_and_decay": dict(WORLD_GRAPH_V3_WINDOWS_AND_DECAY),
    }


def _v3_prediction_count(world_status: Mapping[str, Any]) -> int:
    by_model = world_status.get("predictions_by_model") or {}
    if not isinstance(by_model, Mapping):
        return 0
    return sum(int(count or 0) for key, count in by_model.items() if "graph.v3" in str(key))


def _graph_cohort_activation(catalog: Mapping[str, Any]) -> tuple[str, str | None]:
    status = str(catalog.get("status") or "unavailable")
    if status == "not_started" or not catalog.get("exists"):
        return "not_started", None
    if status != "loaded":
        return status, None
    graph_cohorts: list[WorldCohort] = []
    for item in catalog.get("cohorts") or ():
        if not isinstance(item, Mapping):
            continue
        try:
            cohort = WorldCohort.reconstruct(item.get("manifest") or {}, item.get("events") or ())
        except (TypeError, ValueError):
            continue
        if cohort.manifest.has_graph_lanes():
            graph_cohorts.append(cohort)
    if not graph_cohorts:
        return "no_graph_cohort", None
    collecting = [cohort for cohort in graph_cohorts if cohort.phase is CohortPhase.COLLECTING]
    chosen = min(collecting or graph_cohorts, key=lambda cohort: cohort.cohort_id)
    return chosen.phase.value, chosen.cohort_id


def _graph_v3_gaps(state_dir: str | Path, *, world_status: Mapping[str, Any] | None = None) -> dict[str, Any]:
    db_path = Path(state_dir) / "world_model.db"
    catalog = read_world_cohort_catalog(db_path)
    activation, graph_cohort_id = _graph_cohort_activation(catalog)
    status = world_status if world_status is not None else read_world_model_status(state_dir)
    return {
        "cohort_activation": activation,
        "graph_cohort_id": graph_cohort_id,
        "v3_predictions": _v3_prediction_count(status),
        "flag_default": 0,
    }


def read_world_graph_status(state_dir: str | Path) -> dict[str, Any]:
    """Read-only graph V3 budgets/gaps. Never creates world_model.db."""

    world = read_world_model_status(state_dir)
    exists = bool(world.get("exists"))
    status = "not_started" if not exists else str(world.get("status") or "unavailable")
    return {
        "schema_version": GRAPH_STATUS_SCHEMA,
        "command": "status",
        "status": status,
        "exists": exists,
        "flag": _GRAPH_V3_FLAG,
        "flag_default": 0,
        "budgets": _graph_v3_budgets(),
        "gaps": _graph_v3_gaps(state_dir, world_status=world),
        **_CLAIM_FIELDS,
    }


def read_world_graph_report(state_dir: str | Path, cohort_id: str | None = None) -> dict[str, Any]:
    """Read-only graph V3 report. Never activates a cohort or claims causality/PnL."""

    payload: dict[str, Any] = {
        "schema_version": GRAPH_REPORT_SCHEMA,
        "command": "report",
        "status": "not_started",
        "exists": False,
        "cohort_id": cohort_id,
        "budgets": _graph_v3_budgets(),
        "gaps": _graph_v3_gaps(state_dir),
        **_CLAIM_FIELDS,
    }
    if not cohort_id:
        return payload
    patterns = read_world_pattern_report(state_dir, cohort_id)
    payload["status"] = str(patterns.get("status") or "not_started")
    payload["exists"] = bool(patterns.get("exists"))
    payload["patterns"] = patterns
    return payload


__all__ = ["read_world_graph_report", "read_world_graph_status"]
