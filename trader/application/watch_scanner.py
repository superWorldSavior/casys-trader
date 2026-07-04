"""Application services for indicator-watch scanning."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Callable

from trader.market import market_data as market
from trader.planning.indicator_watch import evaluate_indicator_watches, watch_market_requests
from trader.scheduling.scheduler import Scheduler

log = logging.getLogger(__name__)

MarketErrorClassifier = Callable[[market.MarketError], bool]
WarningLogger = Callable[[str, object, object, object], None]


def _default_connection_error(_: market.MarketError) -> bool:
    return False


def scan_indicator_watches(
    symbols: list[str],
    *,
    sched: Scheduler,
    now: datetime,
    data_source: object,
    is_connection_market_error: MarketErrorClassifier = _default_connection_error,
    log_warning: WarningLogger | None = None,
) -> list[dict]:
    """Evaluate active indicator watches and wake symbols whose watches trigger."""
    watches = [watch for watch in sched.active_indicator_watches(now=now) if watch.get("symbol") in symbols]
    if not watches:
        return []

    warning = log.warning if log_warning is None else log_warning
    bars_by_key: dict[tuple[str, str], list] = {}
    for symbol, interval, lookback in watch_market_requests(watches, universe_symbols=symbols):
        try:
            bars_by_key[(symbol, interval)] = data_source.get_bars(symbol, lookback=lookback, interval=interval)
        except market.MarketError as exc:
            if is_connection_market_error(exc):
                raise
            warning("indicator_watch data unavailable %s/%s: %s", symbol, interval, exc.code)

    triggered = evaluate_indicator_watches(watches, bars_by_key, now=now)
    for event in triggered:
        sched.remove_indicator_watch(str(event["watch_id"]))
        sched.set_symbol_next_wake(str(event["symbol"]), now.isoformat())
    return triggered
