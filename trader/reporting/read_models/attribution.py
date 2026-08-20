"""Decision-to-outcome attribution aggregation read model.

Round-trip reconstruction lives in :mod:`trade_history`; hard-stop
counterfactual diagnostics live in :mod:`hard_stop_diagnostics`. This module
keeps the historical public imports while owning confidence calibration and
exit-reason aggregation.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
import math
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


def _finite_number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _quality_values(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return sorted(
        {
            str(item).strip()
            for item in value
            if item is not None and str(item).strip()
        }
    )


def _commission_quality(trips: list[dict]) -> dict:
    available = 0
    reasons: Counter[str] = Counter()
    models: Counter[str] = Counter()

    for trip in trips:
        quality = trip.get("commission_quality")
        if not isinstance(quality, Mapping):
            reasons["commission_quality_missing"] += 1
            continue

        row_available = quality.get("status") == "available"
        if row_available and (
            _finite_number(trip.get("commission")) is None
            or _finite_number(trip.get("pnl")) is None
        ):
            row_available = False
            reasons["commission_economic_fields_invalid"] += 1

        if row_available:
            available += 1
            continue

        row_reasons = _quality_values(quality.get("reasons"))
        if not row_reasons and quality.get("reason"):
            row_reasons = [str(quality["reason"])]
        if not row_reasons:
            row_reasons = ["commission_quality_unavailable"]
        reasons.update(row_reasons)
        models.update(_quality_values(quality.get("models")))

    unavailable = len(trips) - available
    return {
        "status": "available" if unavailable == 0 else "unavailable",
        "reason": (
            None
            if unavailable == 0
            else (
                next(iter(reasons))
                if len(reasons) == 1
                else "multiple_commission_quality_failures"
            )
        ),
        "counts": {
            "total": len(trips),
            "available": available,
            "unavailable": unavailable,
        },
        "models": sorted(models),
        "model_counts": dict(sorted(models.items())),
        "reasons": sorted(reasons),
        "reason_counts": dict(sorted(reasons.items())),
    }


def _aggregate(trips: list[dict]) -> dict:
    quality = _commission_quality(trips)
    gross_values = [_finite_number(trip.get("gross_pnl")) for trip in trips]
    total_gross_pnl = (
        round(sum(value for value in gross_values if value is not None), 4)
        if all(value is not None for value in gross_values)
        else None
    )

    pnls: list[float] = []
    commissions: list[float] = []
    if quality["status"] == "available":
        pnls = [float(trip["pnl"]) for trip in trips]
        commissions = [float(trip["commission"]) for trip in trips]
    wins = [pnl for pnl in pnls if pnl > 0]
    economics_available = quality["status"] == "available"
    return {
        "n": len(trips),
        "total_pnl": round(sum(pnls), 4) if economics_available else None,
        "total_gross_pnl": total_gross_pnl,
        "total_commission": (
            round(sum(commissions), 4) if economics_available else None
        ),
        "win_rate": (
            (len(wins) / len(trips))
            if economics_available and trips
            else None
        ),
        "avg_pnl": (
            (sum(pnls) / len(trips))
            if economics_available and trips
            else None
        ),
        "commission_quality": quality,
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
        "commission_quality": overall["commission_quality"],
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
