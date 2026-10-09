"""Read-only ledger composition and projections for shadow dynamics runs."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np

from trader.application.world_model.dynamics_model import (
    COORDINATE_CODEC_VERSION,
    COORDINATE_NAMES,
    COORDINATE_SCALES,
    DECODE_POLICY,
)
from trader.application.world_model.dynamics_service import (
    DynamicsReplayRequest,
    DynamicsReplayResult,
    run_dynamics_replay,
)
from trader.infrastructure.state_db.world_dynamics_query import SqliteWorldDynamicsQuery


def build_world_dynamics_payload(
    *, db_path: str | Path, venue: str, symbol: str, bar_interval: str,
    market_contract_version: str, sampling_policy_version: str,
    start_at: datetime | str, as_of: datetime | str, limit: int,
    steps: int = 4, paths: int = 200, min_support: int = 40, seed: int = 0,
) -> dict[str, Any]:
    request = DynamicsReplayRequest(as_of=as_of, steps=steps, paths=paths, min_support=min_support, seed=seed)
    evidence = SqliteWorldDynamicsQuery(db_path).read_episodes(
        venue=venue, symbol=symbol, bar_interval=bar_interval,
        market_contract_version=market_contract_version, sampling_policy_version=sampling_policy_version,
        start_at=start_at, as_of=request.as_of, limit=limit,
    )
    result = run_dynamics_replay(evidence, request)
    return project_world_dynamics_result(result, request, {
        "venue": venue, "symbol": symbol, "bar_interval": bar_interval,
        "market_contract_version": market_contract_version,
        "sampling_policy_version": sampling_policy_version,
        "start_at": start_at.isoformat() if isinstance(start_at, datetime) else start_at,
        "limit": limit,
        "evidence_kind": "world_episode",
        "availability_policy": "max(available_at,captured_at,recorded_at)",
        "missing_bars_policy": "exclude_gaps_no_fetch_or_backfill",
    })


def project_world_dynamics_result(
    result: DynamicsReplayResult,
    request: DynamicsReplayRequest,
    scope: dict[str, Any],
) -> dict[str, Any]:
    """Project either ledger replay or automatic sensor evidence without I/O."""

    distribution: list[dict[str, Any]] = []
    for index in range(request.steps) if result.trajectories else ():
        bars = [trajectory.bars[index] for trajectory in result.trajectories]
        distribution.append({
            "step": index + 1, "end_at": bars[0].end_at.isoformat(),
            "close_quantiles": dict(zip(("p10", "p50", "p90"),
                                        (float(value) for value in np.quantile([bar.close for bar in bars], [.1, .5, .9])))),
            "volume_quantiles": dict(zip(("p10", "p50", "p90"),
                                         (float(value) for value in np.quantile([bar.volume for bar in bars], [.1, .5, .9])))),
        })
    origins = []
    for origin in result.evaluation_origins:
        row = asdict(origin)
        row["prediction_at"] = origin.prediction_at.isoformat()
        row["training_cutoff"] = origin.training_cutoff.isoformat()
        origins.append(row)
    return {
        "schema_version": "world_dynamics_report.v2",
        "status": result.status,
        "authority": "shadow_only", "decision_effect": "none", "recommendation": "NO_GO",
        "causal_claim": False, "pnl_claim": False,
        "scope": {
            **scope,
            "as_of": request.as_of.isoformat(), "seed": request.seed,
            "steps": request.steps, "paths": request.paths, "min_support": request.min_support,
            "context_policy": "market_only", "time_grid": "fixed_interval_no_exchange_calendar",
        },
        "data": {
            "read_evidence": result.read_evidence, "unique_anchors": result.unique_anchors,
            "adjacent_transitions": result.transitions, "exclusions": dict(result.exclusions),
            "availability_policy": scope.get("availability_policy"),
            "missing_bars_policy": scope.get("missing_bars_policy"),
            "bar_revision_policy": "immutable_first_known_bar_reject_ambiguous_first_clock",
        },
        "model": {
            "model_id": result.model_id, "fingerprint": result.model_fingerprint,
            "support": result.support, "residual_support": result.residual_support,
            "training_cutoff": result.training_cutoff.isoformat() if result.training_cutoff else None,
            "coordinate_codec_version": COORDINATE_CODEC_VERSION,
            "transition_coordinates": list(COORDINATE_NAMES),
            "coordinate_scales": list(COORDINATE_SCALES),
            "uncertainty": "joint_past_residual_bootstrap",
            "decoder_policy": DECODE_POLICY,
            "uncertainty_calibrated": False,
            "random_generator": "numpy.PCG64",
            "configuration": dict(result.model_diagnostics),
        },
        "evaluation": {
            "protocol": "chronological_last_30pct_prequential.v1",
            "authority": "retrospective_exploration",
            "origin_selection": "evenly_spaced_before_future_label_inspection",
            "max_evaluation_origins": request.max_evaluation_origins,
            "origins": len(origins), "scores": [asdict(score) for score in result.scores],
            "paired_origins": origins,
            "limitations": ["overlapping_origins_are_correlated", "no_prospective_or_pnl_claim"],
        },
        "rollout": {
            "origin_evidence_id": result.latest_origin_id,
            "origin_end_at": result.latest_origin_end_at.isoformat() if result.latest_origin_end_at else None,
            "paths": len(result.trajectories), "steps": request.steps,
            "distribution": distribution,
            "sample_trajectories": [trajectory.to_dict() for trajectory in result.trajectories[:3]],
            "trajectory_ids": [trajectory.trajectory_id for trajectory in result.trajectories],
        },
    }
