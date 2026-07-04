"""Runtime scheduling adapters for daemon wake and watch side effects."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Callable

from trader.application import cycle_schedule, watch_scanner
from trader.market import market_data as market
from trader.planning.scheduler import Scheduler

EventAppender = Callable[..., None]
LogCallable = Callable[..., None]
MarketErrorClassifier = Callable[[market.MarketError], bool]


def _noop_event(_event: str, **_payload: object) -> None:
    return None


def _noop_log(*_args: object, **_kwargs: object) -> None:
    return None


def bounded_wake_minutes(value: float, *, minimum: float | None = None, maximum: float | None = None) -> float:
    return cycle_schedule.bounded_wake_minutes(value, minimum=minimum, maximum=maximum)


def stale_backoff_wake_minutes(streak: int, *, default_wake_minutes: float) -> float:
    return cycle_schedule.stale_backoff_wake_minutes(streak, default_wake_minutes=default_wake_minutes)


def ensure_default_wake(
    sched: Scheduler,
    *,
    now: datetime,
    default_wake_minutes: float,
) -> None:
    cycle_schedule.ensure_default_wake(sched, now=now, default_wake_minutes=default_wake_minutes)


def select_due_symbols(
    symbols: list[str],
    *,
    sched: Scheduler,
    once: bool,
    bootstrap: bool,
    now: datetime | None = None,
) -> list[str]:
    return cycle_schedule.select_due_symbols(symbols, sched=sched, once=once, bootstrap=bootstrap, now=now)


def exit_watch_cooldown_elapsed(watch: dict, *, now: datetime) -> bool:
    return watch_scanner.exit_watch_cooldown_elapsed(watch, now=now)


def scan_exit_watches(
    *,
    plan_store: watch_scanner.TradePlanStoreLike,
    bars_by_symbol: dict[str, list],
    symbols: list[str],
    now: datetime,
    dry_run: bool,
    bars_interval: str,
    data_source: object,
    is_connection_market_error: MarketErrorClassifier,
    log_warning: LogCallable | None = None,
    append_event: EventAppender | None = None,
    log_cycle_progress: LogCallable | None = None,
) -> list[dict]:
    enriched = watch_scanner.scan_exit_watches(
        plan_store=plan_store,
        bars_by_symbol=bars_by_symbol,
        symbols=symbols,
        now=now,
        dry_run=dry_run,
        bars_interval=bars_interval,
        data_source=data_source,
        is_connection_market_error=is_connection_market_error,
        log_warning=log_warning,
    )
    event_appender = append_event or _noop_event
    progress_logger = log_cycle_progress or _noop_log
    for event in enriched:
        event_appender(
            "exit_watch_triggered",
            symbol=event["symbol"],
            plan_id=event["plan_id"],
            watch_id=event["watch_id"],
        )
    if enriched:
        progress_logger(
            "[exit_watch] triggered=%d symbols=%s",
            len(enriched),
            [item["symbol"] for item in enriched],
        )
    return enriched


def scan_indicator_watches(
    symbols: list[str],
    *,
    sched: Scheduler,
    now: datetime,
    data_source: object,
    is_connection_market_error: MarketErrorClassifier,
    log_warning: LogCallable | None = None,
    append_event: EventAppender | None = None,
    log_cycle_progress: LogCallable | None = None,
) -> list[dict]:
    triggered = watch_scanner.scan_indicator_watches(
        symbols,
        sched=sched,
        now=now,
        data_source=data_source,
        is_connection_market_error=is_connection_market_error,
        log_warning=log_warning,
    )
    event_appender = append_event or _noop_event
    progress_logger = log_cycle_progress or _noop_log
    for event in triggered:
        event_appender(
            "indicator_watch_triggered",
            symbol=event["symbol"],
            watch_id=event["watch_id"],
            on_trigger=event.get("on_trigger"),
        )
    if triggered:
        progress_logger(
            "[indicator_watch] triggered=%d symbols=%s",
            len(triggered),
            [item["symbol"] for item in triggered],
        )
    return triggered


def earliest_active_watch_expiry_iso(sched: Scheduler, sym: str, *, now: datetime) -> str | None:
    return cycle_schedule.earliest_active_watch_expiry_iso(sched, sym, now=now)


def resolve_wake_event(
    event: str,
    sym: str,
    now: datetime,
    macro_next: list[dict],
    next_regular_session_open: Callable,
) -> str | None:
    return cycle_schedule.resolve_wake_event(event, sym, now, macro_next, next_regular_session_open)


def apply_decision_schedule(
    *,
    sched: Scheduler | None,
    sym: str,
    now: datetime,
    next_wake_in_minutes: float | None,
    next_wake_iso: str | None = None,
    cancel_watch_ids: list[str],
    pending_indicator_watch: dict | None,
    entry: dict,
    append_event: EventAppender | None = None,
    logger: logging.Logger | None = None,
) -> None:
    cycle_schedule.apply_decision_schedule(
        sched=sched,
        sym=sym,
        now=now,
        next_wake_in_minutes=next_wake_in_minutes,
        next_wake_iso=next_wake_iso,
        cancel_watch_ids=cancel_watch_ids,
        pending_indicator_watch=pending_indicator_watch,
        entry=entry,
        append_event=append_event,
        logger=logger,
    )


def expire_indicator_watches(
    sched: Scheduler,
    *,
    now: datetime,
    append_event: EventAppender | None = None,
    log_info: LogCallable | None = None,
) -> list[dict]:
    expired = sched.pop_expired_indicator_watches(now=now)
    event_appender = append_event or _noop_event
    info_logger = log_info or _noop_log
    for item in expired:
        symbol = str(item.get("symbol"))
        watch_id = str(item.get("id") or "")
        on_trigger = item.get("on_trigger")
        event_appender(
            "armed_plan_expired" if on_trigger == "EXECUTE_ORDER" else "indicator_watch_expired",
            symbol=symbol,
            watch_id=watch_id,
            on_trigger=on_trigger,
            expires_at=item.get("expires_at"),
        )
        info_logger(
            "[watch] expirée %s %s on_trigger=%s (TTL atteint → réveil déjà calé, l'agent re-décide)",
            symbol,
            watch_id,
            on_trigger,
        )
    return expired
