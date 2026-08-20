"""Application services for indicator-watch scanning."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Callable, Protocol

from trader.domain.market import sessions as market
from trader.domain.market_data import MarketError
from trader.domain.planning.indicator_watch import evaluate_indicator_watches, watch_market_requests
from trader.domain.planning.protocols import SchedulerLike
from trader.market.protocols import DataSource

log = logging.getLogger("trader.application.watch_scanner")

MarketErrorClassifier = Callable[[MarketError], bool]
WarningLogger = Callable[[str, object, object, object], None]


class TradePlanLike(Protocol):
    id: str
    symbol: str
    exit_watch: dict | None

    def model_copy(self, *, update: dict) -> "TradePlanLike": ...


class TradePlanStoreLike(Protocol):
    def open_plans(self) -> list[TradePlanLike]: ...

    def upsert(self, plan: TradePlanLike) -> None: ...


def _default_connection_error(_: MarketError) -> bool:
    return False


def _completed_daily_bars(
    bars: list,
    *,
    symbol: str,
    now: datetime,
) -> list | None:
    """Drop the current incomplete daily session; reject malformed timestamps."""

    completed_session = market.last_completed_session_date(now, symbol=symbol)
    completed: list = []
    for bar in bars:
        try:
            timestamp = datetime.fromisoformat(str(bar.ts).replace("Z", "+00:00"))
        except (AttributeError, ValueError):
            return None
        if timestamp.date() <= completed_session:
            completed.append(bar)
    return completed


def _usable_watch_bars(
    bars: list,
    *,
    symbol: str,
    interval: str,
    now: datetime,
    kind: str,
) -> list | None:
    """Return bars usable by a watch, or None when stale.

    Daily watches follow completed-session semantics; intraday keeps its age budget.
    """
    if not bars:
        return bars
    usable = bars
    if interval.strip().lower() == "1d":
        usable = _completed_daily_bars(bars, symbol=symbol, now=now)
        if usable is None:
            log.debug("%s bars invalid timestamp %s/%s", kind, symbol, interval)
            return None
        freshness = market.assess_daily_freshness(usable, now=now, symbol=symbol)
    else:
        freshness = market.assess_freshness(
            usable,
            now=now,
            max_age_minutes=market.freshness_budget_minutes(interval),
        )
    if freshness.fresh:
        return usable
    age = None if freshness.age_minutes is None else round(freshness.age_minutes, 1)
    log.debug(
        "%s bars stale %s/%s reason=%s age=%s",
        kind,
        symbol,
        interval,
        freshness.reason,
        age,
    )
    return None


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
    sched: SchedulerLike,
    now: datetime,
    data_source: DataSource,
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
            bars = data_source.get_bars(symbol, lookback=lookback, interval=interval)
        except MarketError as exc:
            if is_connection_market_error(exc):
                raise
            warning("indicator_watch data unavailable %s/%s: %s", symbol, interval, exc.code)
            continue
        usable = _usable_watch_bars(
            bars,
            symbol=symbol,
            interval=interval,
            now=now,
            kind="indicator_watch",
        )
        if usable is None:
            continue
        bars_by_key[(symbol, interval)] = usable

    triggered = evaluate_indicator_watches(watches, bars_by_key, now=now)
    for event in triggered:
        if str(event.get("on_trigger")) != "EXECUTE_ORDER":
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
    data_source: DataSource,
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
    bars_by_key: dict[tuple[str, str], list] = {}
    for symbol, bars in bars_by_symbol.items():
        usable = bars
        if bars_interval.strip().lower() == "1d":
            usable = _usable_watch_bars(
                bars,
                symbol=symbol,
                interval=bars_interval,
                now=now,
                kind="exit_watch",
            )
        bars_by_key[(symbol, bars_interval)] = [] if usable is None else usable
    for symbol, interval, lookback in watch_market_requests(watches, universe_symbols=symbols):
        key = (symbol, interval)
        if key in bars_by_key:
            continue
        try:
            bars = data_source.get_bars(symbol, lookback=lookback, interval=interval)
        except MarketError as exc:
            if is_connection_market_error(exc):
                raise
            warning("exit_watch data unavailable %s/%s: %s", symbol, interval, exc.code)
            continue
        usable = _usable_watch_bars(
            bars,
            symbol=symbol,
            interval=interval,
            now=now,
            kind="exit_watch",
        )
        if usable is None:
            continue
        bars_by_key[key] = usable

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
            plan_store.upsert(plan.model_copy(update={"exit_watch": watch}))
    return enriched
