"""Decision-to-outcome attribution aggregation read model.

Round-trip reconstruction lives in :mod:`trade_history`; hard-stop
counterfactual diagnostics live in :mod:`hard_stop_diagnostics`. This module
keeps the historical public imports while owning confidence calibration and
exit-reason aggregation.
"""

from __future__ import annotations

from pathlib import Path

from trader.reporting.read_models.hard_stop_diagnostics import (
    compute_hard_stop_diagnostics,
    select_hard_stop_symbols,
)
from trader.reporting.read_models.trade_history import (
    aggregate_position_cycles,
    compute_round_trips,
    filter_regime_trips as _filter_regime_trips,
)

_RECENT_TRIPS_LIMIT = 25
_CONFIDENCE_BUCKETS = [
    ("0.0-0.5", 0.0, 0.5),
    ("0.5-0.7", 0.5, 0.7),
    ("0.7-0.85", 0.7, 0.85),
    ("0.85-1.0", 0.85, 1.0001),
]


def _bucket_for(confidence: float | None) -> str | None:
    if confidence is None:
        return None
    for name, low, high in _CONFIDENCE_BUCKETS:
        if low <= confidence < high:
            return name
    return None


def _aggregate(trips: list[dict]) -> dict:
    pnls = [trip["pnl"] for trip in trips]
    wins = [pnl for pnl in pnls if pnl > 0]
    return {
        "n": len(trips),
        "total_pnl": round(sum(pnls), 4),
        "total_gross_pnl": round(
            sum(trip["gross_pnl"] for trip in trips),
            4,
        ),
        "total_commission": round(
            sum(trip["commission"] for trip in trips),
            4,
        ),
        "win_rate": (len(wins) / len(trips)) if trips else None,
        "avg_pnl": (sum(pnls) / len(trips)) if trips else None,
    }


def _recent_round_trips(
    trips: list[dict],
    *,
    limit: int = _RECENT_TRIPS_LIMIT,
) -> list[dict]:
    if limit <= 0:
        return []
    return sorted(
        trips,
        key=lambda trip: str(trip["exit_ts"]),
        reverse=True,
    )[:limit]


def compute_attribution(
    state_dir: Path,
    *,
    since: str | None = None,
    exclude_symbols: tuple[str, ...] | frozenset[str] = (),
    min_entry_confidence: float | None = None,
) -> dict:
    """Aggregate completed position cycles while retaining exit-leg mechanics."""

    exit_legs = compute_round_trips(state_dir)
    position_cycles = aggregate_position_cycles(exit_legs)
    position_cycles, regime = _filter_regime_trips(
        position_cycles,
        since=since,
        exclude_symbols=exclude_symbols,
        min_entry_confidence=min_entry_confidence,
    )
    selected_cycle_keys = {
        (str(cycle.get("symbol") or ""), str(cycle["position_cycle_id"]))
        for cycle in position_cycles
        if cycle.get("position_cycle_id")
    }
    completed_exit_legs = [
        leg
        for leg in exit_legs
        if (
            str(leg.get("symbol") or ""),
            str(leg.get("position_cycle_id") or ""),
        )
        in selected_cycle_keys
    ]
    # Defensive compatibility for callers supplying/reconstructing legacy rows
    # without cycle metadata.  Canonical fill reconstruction always supplies it.
    legacy_legs = [leg for leg in exit_legs if not leg.get("position_cycle_id")]
    if legacy_legs:
        filtered_legacy, _ = _filter_regime_trips(
            legacy_legs,
            since=since,
            exclude_symbols=exclude_symbols,
            min_entry_confidence=min_entry_confidence,
        )
        completed_exit_legs.extend(filtered_legacy)

    overall = _aggregate(position_cycles)

    by_confidence: list[dict] = []
    for name, _, _ in _CONFIDENCE_BUCKETS:
        bucket_cycles = [
            cycle
            for cycle in position_cycles
            if _bucket_for(cycle["entry_confidence"]) == name
        ]
        if bucket_cycles:
            by_confidence.append(
                {"bucket": name, **_aggregate(bucket_cycles)}
            )

    by_exit_reason: list[dict] = []
    reasons = sorted(
        {
            leg["exit_reason"]
            for leg in completed_exit_legs
            if leg["exit_reason"]
        }
    )
    for reason in reasons:
        reason_legs = [
            leg
            for leg in completed_exit_legs
            if leg["exit_reason"] == reason
        ]
        by_exit_reason.append(
            {"reason": reason, **_aggregate(reason_legs)}
        )

    holding = [
        cycle["holding_minutes"]
        for cycle in position_cycles
        if cycle["holding_minutes"] is not None
    ]
    return {
        "summary_grain": "flat_to_flat_position_cycle",
        "mechanism_grain": "exit_leg",
        "n_closed_trades": overall["n"],
        "n_closed_position_cycles": overall["n"],
        "n_exit_legs": len(completed_exit_legs),
        "realized_pnl": overall["total_pnl"],
        "realized_gross_pnl": overall["total_gross_pnl"],
        "total_commissions": overall["total_commission"],
        "win_rate": overall["win_rate"],
        "avg_pnl": overall["avg_pnl"],
        "avg_holding_minutes": (
            sum(holding) / len(holding) if holding else None
        ),
        "by_confidence": by_confidence,
        "by_exit_reason": by_exit_reason,
        "recent_trips": _recent_round_trips(position_cycles),
        "recent_exit_legs": _recent_round_trips(completed_exit_legs),
        "regime": regime,
    }


__all__ = [
    "compute_attribution",
    "compute_hard_stop_diagnostics",
    "compute_round_trips",
    "select_hard_stop_symbols",
]
