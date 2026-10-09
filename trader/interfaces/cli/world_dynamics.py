"""Thin adapter for an offline World dynamics replay, evaluation, and rollout."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from trader.domain.world_cohort import COHORT_AUTHORITY, COHORT_DECISION_EFFECT, COHORT_RECOMMENDATION


def dispatch_world_dynamics(args: Any, *, state_dir: str | Path) -> tuple[dict[str, Any], int]:
    from trader.reporting.read_models.world_dynamics import build_world_dynamics_payload

    try:
        payload = build_world_dynamics_payload(
            db_path=Path(state_dir) / "world_model.db",
            venue=args.venue,
            symbol=args.symbol,
            bar_interval=args.interval,
            market_contract_version=args.market_contract_version,
            sampling_policy_version=args.sampling_policy_version,
            start_at=args.start,
            as_of=args.as_of,
            limit=args.limit,
            steps=args.steps,
            paths=args.paths,
            min_support=args.min_support,
            seed=args.seed,
        )
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        return {
            "command": "dynamics",
            "status": "unavailable",
            "error": {"code": getattr(exc, "code", "invalid_request"), "message": str(exc)},
            "authority": COHORT_AUTHORITY,
            "decision_effect": COHORT_DECISION_EFFECT,
            "recommendation": COHORT_RECOMMENDATION,
            "causal_claim": False,
            "pnl_claim": False,
        }, 1
    if not isinstance(payload, Mapping):
        raise TypeError("World dynamics reporting payload must be an object")
    result = {"command": "dynamics", **payload}
    error_statuses = {"unavailable", "schema_unavailable", "missing_db", "invalid_request", "limit_exceeded"}
    return result, int(result.get("status") in error_statuses or bool(result.get("error")))


__all__ = ["dispatch_world_dynamics"]
