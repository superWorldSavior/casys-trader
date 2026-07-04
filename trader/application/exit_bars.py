"""Exit-bar fetching and validation for open trade plans."""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import datetime, timezone

from trader.market import market_data as market

DEFAULT_EXIT_INTERVAL = "5m"
DEFAULT_EXIT_LOOKBACK = "1d"

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ExitBarExtremes:
    """High/low window used for deterministic exit-plan evaluation."""

    high: float | None = None
    low: float | None = None


def is_valid_exit_bar(bar: object) -> bool:
    """Return True when a fine-grained exit-check bar is usable."""
    try:
        ts_str = getattr(bar, "ts", None)
        if ts_str is None:
            return False
        parsed = market._parse_ts(str(ts_str))
        if parsed is None:
            return False
        high = getattr(bar, "high", None)
        low = getattr(bar, "low", None)
        if high is None or low is None:
            return False
        if not (math.isfinite(float(high)) and math.isfinite(float(low))):
            return False
        if float(low) > float(high):
            return False
    except Exception:  # noqa: BLE001 - malformed external bar object
        return False
    return True


def bar_ts_after_plan_open(bar_ts: str, opened_at: str | None) -> bool:
    """Return True when bar_ts >= opened_at, preserving legacy fail-open parsing."""
    if not opened_at:
        return True
    try:
        bar_dt = datetime.fromisoformat(str(bar_ts))
        plan_dt = datetime.fromisoformat(opened_at)
        if bar_dt.tzinfo is None:
            bar_dt = bar_dt.replace(tzinfo=timezone.utc)
        if plan_dt.tzinfo is None:
            plan_dt = plan_dt.replace(tzinfo=timezone.utc)
        return bar_dt >= plan_dt
    except (ValueError, TypeError):
        return True


def exit_bar_extremes(
    bars: list,
    *,
    interval: str,
    fine_interval: str,
    fine_window_bars: int,
    opened_at: str | None,
) -> ExitBarExtremes:
    """Return the bar high/low window used for deterministic exit checks."""
    if not bars:
        return ExitBarExtremes()

    if interval == fine_interval:
        window = bars[-fine_window_bars:] if fine_window_bars > 0 else []
        eligible = [bar for bar in window if bar_ts_after_plan_open(bar.ts, opened_at)]
        if not eligible:
            return ExitBarExtremes()
        return ExitBarExtremes(
            high=max(bar.high for bar in eligible),
            low=min(bar.low for bar in eligible),
        )

    last_bar = bars[-1]
    if not bar_ts_after_plan_open(last_bar.ts, opened_at):
        return ExitBarExtremes()
    return ExitBarExtremes(high=last_bar.high, low=last_bar.low)


def fetch_exit_bars_for_open_plans(
    *,
    plan_store: object,
    data_source: object,
    tradable_bars_by_symbol: dict[str, list],
    tradable_prices: dict[str, float],
    now: datetime,
    fallback_interval: str,
    exit_interval: str = DEFAULT_EXIT_INTERVAL,
    exit_lookback: str = DEFAULT_EXIT_LOOKBACK,
) -> tuple[dict[str, list], dict[str, str]]:
    """Fetch fresh fine-grained bars for open plans, falling back per symbol."""
    open_symbols = {plan.symbol for plan in plan_store.open_plans() if plan.symbol in tradable_prices}
    exit_bars: dict[str, list] = dict(tradable_bars_by_symbol)
    intervals: dict[str, str] = {sym: fallback_interval for sym in tradable_bars_by_symbol}

    freshness_budget = market.freshness_budget_minutes(exit_interval)

    for symbol in open_symbols:
        try:
            fine_bars = data_source.get_bars(symbol, lookback=exit_lookback, interval=exit_interval)
        except Exception as exc:  # noqa: BLE001 - never block exits on fine-bar fetch
            log.warning(
                "%s bars fetch failed for %s — falling back to %s (%s)",
                exit_interval,
                symbol,
                fallback_interval,
                exc,
            )
            continue
        if not fine_bars:
            log.warning("%s bars empty for %s — falling back to %s", exit_interval, symbol, fallback_interval)
            continue

        valid_bars = [bar for bar in fine_bars if is_valid_exit_bar(bar)]
        if not valid_bars:
            log.warning(
                "%s bars all invalid for %s — falling back to %s",
                exit_interval,
                symbol,
                fallback_interval,
            )
            continue

        freshness = market.assess_freshness(valid_bars, now=now, max_age_minutes=freshness_budget)
        if not freshness.fresh:
            log.debug(
                "%s bars stale for %s (%s, age=%.1f min) — falling back to %s",
                exit_interval,
                symbol,
                freshness.reason,
                freshness.age_minutes or 0.0,
                fallback_interval,
            )
            continue

        exit_bars[symbol] = valid_bars
        intervals[symbol] = exit_interval

    return exit_bars, intervals
