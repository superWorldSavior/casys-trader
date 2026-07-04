"""Application services for indicator-watch scanning."""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import datetime, timezone
from typing import Callable, Protocol

from trader.market import market_data as market
from trader.planning.indicator_watch import evaluate_indicator_watches, watch_market_requests
from trader.planning.scheduler import Scheduler

log = logging.getLogger(__name__)

MarketErrorClassifier = Callable[[market.MarketError], bool]
WarningLogger = Callable[[str, object, object, object], None]


class TradePlanLike(Protocol):
    id: str
    symbol: str
    exit_watch: dict | None


class TradePlanStoreLike(Protocol):
    def open_plans(self) -> list[TradePlanLike]: ...

    def upsert(self, plan: TradePlanLike) -> None: ...


def _default_connection_error(_: market.MarketError) -> bool:
    return False


def exit_watch_cooldown_elapsed(watch: dict, *, now: datetime) -> bool:
    last_raw = watch.get("last_triggered_at")
    if not last_raw:
        return True
    try:
        last = datetime.fromisoformat(str(last_raw))
    except ValueError:
        return True
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    cooldown = float(watch.get("cooldown_minutes") or 15.0)
    return (now - last).total_seconds() >= cooldown * 60.0


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


def scan_exit_watches(
    *,
    plan_store: TradePlanStoreLike,
    bars_by_symbol: dict[str, list],
    symbols: list[str],
    now: datetime,
    dry_run: bool,
    bars_interval: str,
    data_source: object,
    is_connection_market_error: MarketErrorClassifier = _default_connection_error,
    log_warning: WarningLogger | None = None,
) -> list[dict]:
    """Evaluate exit watches attached to open trade plans and persist cooldowns."""
    plans_by_watch_id: dict[str, TradePlanLike] = {}
    watches: list[dict] = []
    for plan in plan_store.open_plans():
        if plan.symbol not in symbols or not isinstance(plan.exit_watch, dict):
            continue
        if not exit_watch_cooldown_elapsed(plan.exit_watch, now=now):
            continue
        watch = dict(plan.exit_watch)
        watch["symbol"] = plan.symbol
        watch["on_trigger"] = "WAKE"
        watch["source"] = "exit_watch"
        watches.append(watch)
        plans_by_watch_id[str(watch["id"])] = plan
    if not watches:
        return []

    warning = log.warning if log_warning is None else log_warning
    bars_by_key: dict[tuple[str, str], list] = {
        (symbol, bars_interval): bars for symbol, bars in bars_by_symbol.items()
    }
    for symbol, interval, lookback in watch_market_requests(watches, universe_symbols=symbols):
        key = (symbol, interval)
        if key in bars_by_key:
            continue
        try:
            bars_by_key[key] = data_source.get_bars(symbol, lookback=lookback, interval=interval)
        except market.MarketError as exc:
            if is_connection_market_error(exc):
                raise
            warning("exit_watch data unavailable %s/%s: %s", symbol, interval, exc.code)

    triggered = evaluate_indicator_watches(watches, bars_by_key, now=now)
    enriched: list[dict] = []
    for event in triggered:
        plan = plans_by_watch_id.get(str(event["watch_id"]))
        if plan is None:
            continue
        event = {
            **event,
            "source": "exit_watch",
            "plan_id": plan.id,
            "on_trigger": "WAKE",
        }
        enriched.append(event)
        if not dry_run:
            watch = dict(plan.exit_watch or {})
            watch["last_triggered_at"] = now.astimezone(timezone.utc).isoformat()
            plan_store.upsert(replace(plan, exit_watch=watch))
    return enriched
