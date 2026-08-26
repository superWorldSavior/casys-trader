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
    source_interval: str | None = None,
    now: datetime,
    kind: str,
) -> list | None:
    """Return bars usable by a watch, or None when stale.

    Daily watches follow completed-session semantics.  Intraday watches first
    prove that each source bar is closed, then aggregate complete source groups
    when their semantic target differs (the governed 4h = 4x1h case).
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
        usable = market.completed_intraday_bars(
            usable,
            now=now,
            target_interval=interval,
            source_interval=source_interval,
        )
        if usable is None:
            log.debug("%s bars invalid timestamp or interval %s/%s", kind, symbol, interval)
            return None
        freshness = market.assess_completed_intraday_freshness(
            usable,
            now=now,
            target_interval=interval,
            source_interval=source_interval,
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


def _load_watch_bars(
    watches: list[dict],
    *,
    symbols: list[str],
    now: datetime,
    data_source: DataSource,
    kind: str,
    is_connection_market_error: MarketErrorClassifier,
    warning: WarningLogger,
    existing_bars_by_source: dict[tuple[str, str], list] | None = None,
) -> dict[tuple[str, str], list]:
    """Fetch source series once, then materialize closed target-timeframe bars.

    A watch's target is intentionally separate from the provider request: 4h
    conditions read 1h source rows and aggregate only after current source bars
    have been excluded.  This is what makes the partial trailing 4h bucket
    unobservable to the evaluator.
    """

    raw_cache: dict[tuple[str, str, str], list] = {}
    results: dict[tuple[str, str], list] = {}
    existing = existing_bars_by_source or {}
    for request in watch_market_requests(watches, universe_symbols=symbols):
        source_key = (request.symbol, request.source_interval)
        if source_key in existing:
            bars = existing[source_key]
        else:
            cache_key = (*source_key, request.lookback)
            if cache_key not in raw_cache:
                try:
                    raw_cache[cache_key] = data_source.get_bars(
                        request.symbol,
                        lookback=request.lookback,
                        interval=request.source_interval,
                    )
                except MarketError as exc:
                    if is_connection_market_error(exc):
                        raise
                    warning(
                        f"{kind} data unavailable %s/%s: %s",
                        request.symbol,
                        request.source_interval,
                        exc.code,
                    )
                    continue
            bars = raw_cache[cache_key]
        usable = _usable_watch_bars(
            bars,
            symbol=request.symbol,
            interval=request.interval,
            source_interval=request.source_interval,
            now=now,
            kind=kind,
        )
        if usable is not None:
            results[(request.symbol, request.interval)] = usable
    return results


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
    bars_by_key = _load_watch_bars(
        watches,
        symbols=symbols,
        now=now,
        data_source=data_source,
        kind="indicator_watch",
        is_connection_market_error=is_connection_market_error,
        warning=warning,
    )

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
    bars_by_key = _load_watch_bars(
        watches,
        symbols=symbols,
        now=now,
        data_source=data_source,
        kind="exit_watch",
        is_connection_market_error=is_connection_market_error,
        warning=warning,
        existing_bars_by_source={(symbol, bars_interval): bars for symbol, bars in bars_by_symbol.items()},
    )

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
