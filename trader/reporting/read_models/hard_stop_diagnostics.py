"""Post-trade hard-stop counterfactual diagnostics read model."""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path

from trader.domain.market import fx
from trader.reporting.read_models.trade_history import (
    compute_round_trips,
    filter_regime_trips,
)


def select_hard_stop_symbols(
    state_dir: Path,
    *,
    since: str | None = None,
    exclude_symbols: tuple[str, ...] | frozenset[str] = (),
    min_entry_confidence: float | None = None,
) -> list[str]:
    """Return hard-stop symbols after the shared attribution regime filters."""

    trips = compute_round_trips(state_dir)
    trips, _ = filter_regime_trips(
        trips,
        since=since,
        exclude_symbols=exclude_symbols,
        min_entry_confidence=min_entry_confidence,
    )
    return sorted(
        {
            str(trip.get("symbol"))
            for trip in trips
            if trip.get("exit_reason") == "hard_stop" and trip.get("symbol")
        }
    )


def _parse_optional_datetime(value: object) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _bar_field(bar: object, field: str) -> object:
    if isinstance(bar, dict):
        return bar.get(field)
    return getattr(bar, field, None)


def _finite_float(value: object) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _round_metric(value: float | None) -> float | None:
    return None if value is None else round(value, 4)


def _future_bars_after_exit(
    bars: list[object],
    *,
    exit_ts: object,
    limit: int,
) -> list[dict]:
    exit_dt = _parse_optional_datetime(exit_ts)
    if exit_dt is None or limit <= 0:
        return []

    parsed_bars: list[dict] = []
    for bar in bars:
        ts_raw = _bar_field(bar, "ts")
        timestamp = _parse_optional_datetime(ts_raw)
        high = _finite_float(_bar_field(bar, "high"))
        low = _finite_float(_bar_field(bar, "low"))
        close = _finite_float(_bar_field(bar, "close"))
        if timestamp is None or high is None or low is None or close is None:
            continue
        if timestamp <= exit_dt:
            continue
        parsed_bars.append(
            {
                "ts": str(ts_raw),
                "dt": timestamp,
                "high": high,
                "low": low,
                "close": close,
            }
        )

    parsed_bars.sort(key=lambda row: row["dt"])
    return parsed_bars[:limit]


def _position_gross_pnl(
    *,
    side: str,
    entry_price: float,
    quantity: float,
    price: float,
) -> float:
    sign = 1.0 if side == "LONG" else -1.0
    return (price - entry_price) * quantity * sign


def _trip_commission_quality(trip: Mapping[str, object]) -> dict:
    quality = trip.get("commission_quality")
    if not isinstance(quality, Mapping):
        return {
            "status": "unavailable",
            "reason": "commission_quality_missing",
            "models": [],
            "reasons": ["commission_quality_missing"],
        }
    result = dict(quality)
    if quality.get("status") != "available":
        return result
    if (
        _finite_float(trip.get("commission")) is None
        or _finite_float(trip.get("pnl")) is None
    ):
        return {
            **result,
            "status": "unavailable",
            "reason": "commission_economic_fields_invalid",
            "reasons": ["commission_economic_fields_invalid"],
        }
    return result


def _aggregate_commission_quality(cases: list[dict]) -> dict:
    available = 0
    reasons: Counter[str] = Counter()
    models: Counter[str] = Counter()
    for case in cases:
        quality = case["commission_quality"]
        if quality.get("status") == "available":
            available += 1
            continue
        row_reasons = quality.get("reasons")
        values = (
            {str(value) for value in row_reasons if str(value)}
            if isinstance(row_reasons, list)
            else set()
        )
        if not values and quality.get("reason"):
            values.add(str(quality["reason"]))
        reasons.update(values or {"commission_quality_unavailable"})
        row_models = quality.get("models")
        if isinstance(row_models, list):
            models.update({str(value) for value in row_models if str(value)})
    unavailable = len(cases) - available
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
            "total": len(cases),
            "available": available,
            "unavailable": unavailable,
        },
        "models": sorted(models),
        "model_counts": dict(sorted(models.items())),
        "reasons": sorted(reasons),
        "reason_counts": dict(sorted(reasons.items())),
    }


def _diagnose_hard_stop_trip(
    trip: dict,
    bars: list[object],
    *,
    lookahead_bars: int,
) -> dict:
    symbol = str(trip.get("symbol"))
    side = str(trip.get("side"))
    quantity = _finite_float(trip.get("quantity")) or 0.0
    entry_price = _finite_float(trip.get("entry_price")) or 0.0
    exit_price = _finite_float(trip.get("exit_price")) or 0.0
    actual_gross_pnl = _finite_float(trip.get("gross_pnl"))
    commission_quality = _trip_commission_quality(trip)
    commission_available = commission_quality.get("status") == "available"
    actual_pnl = (
        _finite_float(trip.get("pnl")) if commission_available else None
    )
    commission = (
        _finite_float(trip.get("commission")) if commission_available else None
    )
    future_bars = _future_bars_after_exit(
        bars,
        exit_ts=trip.get("exit_ts"),
        limit=lookahead_bars,
    )

    base = {
        "symbol": symbol,
        "side": side,
        "quantity": quantity,
        "entry_ts": trip.get("entry_ts"),
        "exit_ts": trip.get("exit_ts"),
        "entry_price": _round_metric(entry_price),
        "exit_price": _round_metric(exit_price),
        "actual_gross_pnl": _round_metric(actual_gross_pnl),
        "actual_pnl": _round_metric(actual_pnl),
        "commission": _round_metric(commission),
        "commission_quality": commission_quality,
        "economics_scope": (
            "broker_commission_net" if commission_available else "gross_only"
        ),
        "source_plan_id": trip.get("source_plan_id"),
        "future_bars": len(future_bars),
    }

    if fx.currency_for(symbol).upper() != fx.BASE_CCY:
        return {
            **base,
            "verdict": None,
            "verdict_basis": None,
            "counterfactual_quality": {
                "status": "unavailable",
                "reason": "counterfactual_fx_rate_unavailable",
            },
            "hold_to_lookahead_gross_pnl": None,
            "hold_to_lookahead_gross_delta_vs_actual": None,
            "hold_to_lookahead_pnl": None,
            "hold_to_lookahead_delta_vs_actual": None,
            "best_after_stop_gross_pnl": None,
            "best_after_stop_pnl": None,
            "worst_after_stop_gross_pnl": None,
            "worst_after_stop_pnl": None,
            "recovered_to_entry": None,
            "would_have_won_by_lookahead": None,
            "would_have_beaten_stop_by_lookahead": None,
            "would_have_gross_profit_by_lookahead": None,
            "would_have_beaten_stop_gross_by_lookahead": None,
        }

    if (
        not future_bars
        or quantity <= 0.0
        or entry_price <= 0.0
        or side not in ("LONG", "SHORT")
    ):
        return {
            **base,
            "verdict": "unknown_no_future_bars",
            "verdict_basis": None,
            "counterfactual_quality": {
                "status": "unavailable",
                "reason": "counterfactual_market_bars_unavailable",
            },
            "hold_to_lookahead_gross_pnl": None,
            "hold_to_lookahead_gross_delta_vs_actual": None,
            "hold_to_lookahead_pnl": None,
            "hold_to_lookahead_delta_vs_actual": None,
            "best_after_stop_gross_pnl": None,
            "best_after_stop_pnl": None,
            "worst_after_stop_gross_pnl": None,
            "worst_after_stop_pnl": None,
            "recovered_to_entry": None,
            "would_have_won_by_lookahead": None,
            "would_have_beaten_stop_by_lookahead": None,
            "would_have_gross_profit_by_lookahead": None,
            "would_have_beaten_stop_gross_by_lookahead": None,
        }

    lookahead_close = future_bars[-1]["close"]
    if side == "LONG":
        best_price = max(bar["high"] for bar in future_bars)
        worst_price = min(bar["low"] for bar in future_bars)
        recovered_to_entry = best_price >= entry_price
    else:
        best_price = min(bar["low"] for bar in future_bars)
        worst_price = max(bar["high"] for bar in future_bars)
        recovered_to_entry = best_price <= entry_price

    hold_to_lookahead_gross_pnl = _position_gross_pnl(
        side=side,
        entry_price=entry_price,
        quantity=quantity,
        price=lookahead_close,
    )
    best_after_stop_gross_pnl = _position_gross_pnl(
        side=side,
        entry_price=entry_price,
        quantity=quantity,
        price=best_price,
    )
    worst_after_stop_gross_pnl = _position_gross_pnl(
        side=side,
        entry_price=entry_price,
        quantity=quantity,
        price=worst_price,
    )
    actual_gross = actual_gross_pnl
    gross_delta = (
        hold_to_lookahead_gross_pnl - actual_gross
        if actual_gross is not None
        else None
    )
    hold_to_lookahead_pnl = (
        hold_to_lookahead_gross_pnl - commission
        if commission is not None
        else None
    )
    best_after_stop_pnl = (
        best_after_stop_gross_pnl - commission
        if commission is not None
        else None
    )
    worst_after_stop_pnl = (
        worst_after_stop_gross_pnl - commission
        if commission is not None
        else None
    )
    hold_delta = (
        hold_to_lookahead_pnl - actual_pnl
        if hold_to_lookahead_pnl is not None and actual_pnl is not None
        else None
    )
    verdict_delta = hold_delta if hold_delta is not None else gross_delta

    return {
        **base,
        "verdict": (
            "stop_too_early"
            if verdict_delta is not None and verdict_delta > 0.0
            else "stop_helped_or_neutral"
        ),
        "verdict_basis": "net_pnl" if hold_delta is not None else "gross_pnl",
        "counterfactual_quality": {
            "status": "available",
            "reason": None,
            "net_status": (
                "available" if commission_available else "unavailable"
            ),
        },
        "lookahead_last_ts": future_bars[-1]["ts"],
        "lookahead_close": _round_metric(lookahead_close),
        "hold_to_lookahead_gross_pnl": _round_metric(
            hold_to_lookahead_gross_pnl
        ),
        "hold_to_lookahead_gross_delta_vs_actual": _round_metric(gross_delta),
        "hold_to_lookahead_pnl": _round_metric(hold_to_lookahead_pnl),
        "hold_to_lookahead_delta_vs_actual": _round_metric(hold_delta),
        "best_after_stop_price": _round_metric(best_price),
        "best_after_stop_gross_pnl": _round_metric(best_after_stop_gross_pnl),
        "best_after_stop_pnl": _round_metric(best_after_stop_pnl),
        "best_after_stop_delta_vs_actual": _round_metric(
            best_after_stop_pnl - actual_pnl
            if best_after_stop_pnl is not None and actual_pnl is not None
            else None
        ),
        "worst_after_stop_price": _round_metric(worst_price),
        "worst_after_stop_gross_pnl": _round_metric(
            worst_after_stop_gross_pnl
        ),
        "worst_after_stop_pnl": _round_metric(worst_after_stop_pnl),
        "worst_after_stop_delta_vs_actual": _round_metric(
            worst_after_stop_pnl - actual_pnl
            if worst_after_stop_pnl is not None and actual_pnl is not None
            else None
        ),
        "recovered_to_entry": recovered_to_entry,
        "would_have_won_by_lookahead": (
            hold_to_lookahead_pnl > 0.0
            if hold_to_lookahead_pnl is not None
            else None
        ),
        "would_have_beaten_stop_by_lookahead": (
            hold_to_lookahead_pnl > actual_pnl
            if hold_to_lookahead_pnl is not None and actual_pnl is not None
            else None
        ),
        "would_have_gross_profit_by_lookahead": (
            hold_to_lookahead_gross_pnl > 0.0
        ),
        "would_have_beaten_stop_gross_by_lookahead": (
            gross_delta > 0.0 if gross_delta is not None else None
        ),
    }


def compute_hard_stop_diagnostics(
    state_dir: Path,
    bars_by_symbol: dict[str, list[object]],
    *,
    since: str | None = None,
    exclude_symbols: tuple[str, ...] | frozenset[str] = (),
    lookahead_bars: int = 8,
    interval: str = "1h",
    min_entry_confidence: float | None = None,
) -> dict:
    """Compare hard-stop outcomes with a price-only hold counterfactual."""

    trips = compute_round_trips(state_dir)
    trips, regime = filter_regime_trips(
        trips,
        since=since,
        exclude_symbols=exclude_symbols,
        min_entry_confidence=min_entry_confidence,
    )
    hard_stop_trips = [
        trip for trip in trips if trip.get("exit_reason") == "hard_stop"
    ]
    cases = [
        _diagnose_hard_stop_trip(
            trip,
            list(bars_by_symbol.get(str(trip.get("symbol")), [])),
            lookahead_bars=lookahead_bars,
        )
        for trip in hard_stop_trips
    ]
    diagnosed = [
        case
        for case in cases
        if case["hold_to_lookahead_gross_pnl"] is not None
    ]
    commission_quality = _aggregate_commission_quality(cases)
    counterfactual_reasons = Counter(
        str(quality.get("reason") or "counterfactual_unavailable")
        for case in cases
        if isinstance(
            quality := case.get("counterfactual_quality"), Mapping
        )
        and quality.get("status") != "available"
    )
    counterfactual_unavailable = sum(counterfactual_reasons.values())
    counterfactual_quality = {
        "status": (
            "available" if counterfactual_unavailable == 0 else "unavailable"
        ),
        "counts": {
            "total": len(cases),
            "available": len(cases) - counterfactual_unavailable,
            "unavailable": counterfactual_unavailable,
        },
        "reasons": sorted(counterfactual_reasons),
        "reason_counts": dict(sorted(counterfactual_reasons.items())),
    }

    def complete_sum(
        rows: list[dict],
        field: str,
        *,
        empty_value: float | None = 0.0,
    ) -> float | None:
        if not rows:
            return empty_value
        values = [_finite_float(row.get(field)) for row in rows]
        if any(value is None for value in values):
            return None
        return sum(value for value in values if value is not None)

    actual_gross_pnl = complete_sum(cases, "actual_gross_pnl")
    diagnosed_actual_gross_pnl = complete_sum(
        diagnosed, "actual_gross_pnl", empty_value=None
    )
    hold_to_lookahead_gross_pnl = complete_sum(
        diagnosed, "hold_to_lookahead_gross_pnl", empty_value=None
    )
    best_after_stop_gross_pnl = complete_sum(
        diagnosed, "best_after_stop_gross_pnl", empty_value=None
    )
    worst_after_stop_gross_pnl = complete_sum(
        diagnosed, "worst_after_stop_gross_pnl", empty_value=None
    )
    net_available = commission_quality["status"] == "available"
    actual_pnl = complete_sum(cases, "actual_pnl") if net_available else None
    diagnosed_actual_pnl = (
        complete_sum(diagnosed, "actual_pnl", empty_value=None)
        if net_available
        else None
    )
    hold_to_lookahead_pnl = (
        complete_sum(diagnosed, "hold_to_lookahead_pnl", empty_value=None)
        if net_available
        else None
    )
    best_after_stop_pnl = (
        complete_sum(diagnosed, "best_after_stop_pnl", empty_value=None)
        if net_available
        else None
    )
    worst_after_stop_pnl = (
        complete_sum(diagnosed, "worst_after_stop_pnl", empty_value=None)
        if net_available
        else None
    )

    return {
        "lookahead_bars": lookahead_bars,
        "interval": interval,
        "summary": {
            "hard_stops": len(cases),
            "diagnosed": len(diagnosed),
            "unknown": len(cases) - len(diagnosed),
            "stop_too_early": sum(
                1 for case in cases if case["verdict"] == "stop_too_early"
            ),
            "stop_helped_or_neutral": sum(
                1
                for case in cases
                if case["verdict"] == "stop_helped_or_neutral"
            ),
            "commission_quality": commission_quality,
            "counterfactual_quality": counterfactual_quality,
            "actual_gross_pnl": _round_metric(actual_gross_pnl),
            "diagnosed_actual_gross_pnl": _round_metric(
                diagnosed_actual_gross_pnl
            ),
            "hold_to_lookahead_gross_pnl": _round_metric(
                hold_to_lookahead_gross_pnl
            ),
            "hold_to_lookahead_gross_delta_vs_actual": _round_metric(
                hold_to_lookahead_gross_pnl - diagnosed_actual_gross_pnl
                if hold_to_lookahead_gross_pnl is not None
                and diagnosed_actual_gross_pnl is not None
                else None
            ),
            "best_after_stop_gross_pnl": _round_metric(
                best_after_stop_gross_pnl
            ),
            "best_after_stop_gross_delta_vs_actual": _round_metric(
                best_after_stop_gross_pnl - diagnosed_actual_gross_pnl
                if best_after_stop_gross_pnl is not None
                and diagnosed_actual_gross_pnl is not None
                else None
            ),
            "worst_after_stop_gross_pnl": _round_metric(
                worst_after_stop_gross_pnl
            ),
            "worst_after_stop_gross_delta_vs_actual": _round_metric(
                worst_after_stop_gross_pnl - diagnosed_actual_gross_pnl
                if worst_after_stop_gross_pnl is not None
                and diagnosed_actual_gross_pnl is not None
                else None
            ),
            "actual_pnl": _round_metric(actual_pnl),
            "diagnosed_actual_pnl": _round_metric(diagnosed_actual_pnl),
            "hold_to_lookahead_pnl": _round_metric(hold_to_lookahead_pnl),
            "hold_to_lookahead_delta_vs_actual": _round_metric(
                hold_to_lookahead_pnl - diagnosed_actual_pnl
                if hold_to_lookahead_pnl is not None
                and diagnosed_actual_pnl is not None
                else None
            ),
            "best_after_stop_pnl": _round_metric(best_after_stop_pnl),
            "best_after_stop_delta_vs_actual": _round_metric(
                best_after_stop_pnl - diagnosed_actual_pnl
                if best_after_stop_pnl is not None
                and diagnosed_actual_pnl is not None
                else None
            ),
            "worst_after_stop_pnl": _round_metric(worst_after_stop_pnl),
            "worst_after_stop_delta_vs_actual": _round_metric(
                worst_after_stop_pnl - diagnosed_actual_pnl
                if worst_after_stop_pnl is not None
                and diagnosed_actual_pnl is not None
                else None
            ),
        },
        "cases": sorted(
            cases,
            key=lambda case: str(case.get("exit_ts")),
            reverse=True,
        ),
        "regime": regime,
    }


__all__ = ["compute_hard_stop_diagnostics", "select_hard_stop_symbols"]
