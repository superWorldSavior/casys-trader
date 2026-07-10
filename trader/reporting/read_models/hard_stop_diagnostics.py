"""Post-trade hard-stop counterfactual diagnostics read model."""

from __future__ import annotations

import math
from datetime import datetime, timezone
from pathlib import Path

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


def _position_pnl(
    *,
    side: str,
    entry_price: float,
    quantity: float,
    price: float,
    commission: float,
) -> float:
    sign = 1.0 if side == "LONG" else -1.0
    return (price - entry_price) * quantity * sign - commission


def _diagnose_hard_stop_trip(
    trip: dict,
    bars: list[object],
    *,
    lookahead_bars: int,
) -> dict:
    symbol = str(trip.get("symbol"))
    side = str(trip.get("side"))
    quantity = float(trip.get("quantity") or 0.0)
    entry_price = float(trip.get("entry_price") or 0.0)
    exit_price = float(trip.get("exit_price") or 0.0)
    actual_pnl = float(trip.get("pnl") or 0.0)
    commission = float(trip.get("commission") or 0.0)
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
        "actual_pnl": _round_metric(actual_pnl),
        "commission": _round_metric(commission),
        "source_plan_id": trip.get("source_plan_id"),
        "future_bars": len(future_bars),
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
            "hold_to_lookahead_pnl": None,
            "hold_to_lookahead_delta_vs_actual": None,
            "best_after_stop_pnl": None,
            "worst_after_stop_pnl": None,
            "recovered_to_entry": None,
            "would_have_won_by_lookahead": None,
            "would_have_beaten_stop_by_lookahead": None,
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

    hold_to_lookahead_pnl = _position_pnl(
        side=side,
        entry_price=entry_price,
        quantity=quantity,
        price=lookahead_close,
        commission=commission,
    )
    best_after_stop_pnl = _position_pnl(
        side=side,
        entry_price=entry_price,
        quantity=quantity,
        price=best_price,
        commission=commission,
    )
    worst_after_stop_pnl = _position_pnl(
        side=side,
        entry_price=entry_price,
        quantity=quantity,
        price=worst_price,
        commission=commission,
    )
    hold_delta = hold_to_lookahead_pnl - actual_pnl

    return {
        **base,
        "verdict": (
            "stop_too_early" if hold_delta > 0.0 else "stop_helped_or_neutral"
        ),
        "lookahead_last_ts": future_bars[-1]["ts"],
        "lookahead_close": _round_metric(lookahead_close),
        "hold_to_lookahead_pnl": _round_metric(hold_to_lookahead_pnl),
        "hold_to_lookahead_delta_vs_actual": _round_metric(hold_delta),
        "best_after_stop_price": _round_metric(best_price),
        "best_after_stop_pnl": _round_metric(best_after_stop_pnl),
        "best_after_stop_delta_vs_actual": _round_metric(
            best_after_stop_pnl - actual_pnl
        ),
        "worst_after_stop_price": _round_metric(worst_price),
        "worst_after_stop_pnl": _round_metric(worst_after_stop_pnl),
        "worst_after_stop_delta_vs_actual": _round_metric(
            worst_after_stop_pnl - actual_pnl
        ),
        "recovered_to_entry": recovered_to_entry,
        "would_have_won_by_lookahead": hold_to_lookahead_pnl > 0.0,
        "would_have_beaten_stop_by_lookahead": (
            hold_to_lookahead_pnl > actual_pnl
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
        case for case in cases if case["hold_to_lookahead_pnl"] is not None
    ]

    actual_pnl = sum(float(case["actual_pnl"] or 0.0) for case in cases)
    diagnosed_actual_pnl = sum(
        float(case["actual_pnl"] or 0.0) for case in diagnosed
    )
    hold_to_lookahead_pnl = sum(
        float(case["hold_to_lookahead_pnl"] or 0.0) for case in diagnosed
    )
    best_after_stop_pnl = sum(
        float(case["best_after_stop_pnl"] or 0.0) for case in diagnosed
    )
    worst_after_stop_pnl = sum(
        float(case["worst_after_stop_pnl"] or 0.0) for case in diagnosed
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
            "actual_pnl": _round_metric(actual_pnl),
            "diagnosed_actual_pnl": _round_metric(diagnosed_actual_pnl),
            "hold_to_lookahead_pnl": _round_metric(hold_to_lookahead_pnl),
            "hold_to_lookahead_delta_vs_actual": _round_metric(
                hold_to_lookahead_pnl - diagnosed_actual_pnl
            ),
            "best_after_stop_pnl": _round_metric(best_after_stop_pnl),
            "best_after_stop_delta_vs_actual": _round_metric(
                best_after_stop_pnl - diagnosed_actual_pnl
            ),
            "worst_after_stop_pnl": _round_metric(worst_after_stop_pnl),
            "worst_after_stop_delta_vs_actual": _round_metric(
                worst_after_stop_pnl - diagnosed_actual_pnl
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
