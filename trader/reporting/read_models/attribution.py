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
    """Aggregate closed trades by confidence bucket and exit reason."""

    trips = compute_round_trips(state_dir)
    trips, regime = _filter_regime_trips(
        trips,
        since=since,
        exclude_symbols=exclude_symbols,
        min_entry_confidence=min_entry_confidence,
    )
    overall = _aggregate(trips)

    by_confidence: list[dict] = []
    for name, _, _ in _CONFIDENCE_BUCKETS:
        bucket_trips = [
            trip
            for trip in trips
            if _bucket_for(trip["entry_confidence"]) == name
        ]
        if bucket_trips:
            by_confidence.append(
                {"bucket": name, **_aggregate(bucket_trips)}
            )

    by_exit_reason: list[dict] = []
    reasons = sorted(
        {trip["exit_reason"] for trip in trips if trip["exit_reason"]}
    )
    for reason in reasons:
        reason_trips = [
            trip for trip in trips if trip["exit_reason"] == reason
        ]
        by_exit_reason.append(
            {"reason": reason, **_aggregate(reason_trips)}
        )

    holding = [
        trip["holding_minutes"]
        for trip in trips
        if trip["holding_minutes"] is not None
    ]
    return {
        "n_closed_trades": overall["n"],
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
        "recent_trips": _recent_round_trips(trips),
        "regime": regime,
    }


__all__ = [
    "compute_attribution",
    "compute_hard_stop_diagnostics",
    "compute_round_trips",
    "select_hard_stop_symbols",
]
