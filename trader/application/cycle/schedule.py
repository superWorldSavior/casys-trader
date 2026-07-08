"""Application scheduling policy for one daemon cycle.

The runtime daemon owns process composition and state files. This module owns the
small scheduling decisions that can be tested without booting the runtime loop.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Callable

from trader.domain.planning.scheduling import stale_backoff_wake_minutes
from trader.planning.scheduler import Scheduler

EventAppender = Callable[..., None]


def bounded_wake_minutes(value: float, *, minimum: float | None = None, maximum: float | None = None) -> float:
    """Return the agent-requested wake delay, bounded only by explicit flags."""
    result = float(value)
    if minimum is not None:
        result = max(result, minimum)
    if maximum is not None:
        result = min(result, maximum)
    return result


def ensure_default_wake(
    sched: Scheduler,
    *,
    now: datetime,
    default_wake_minutes: float,
) -> None:
    current = sched.next_wake()
    if current is None or current <= now:
        sched.set_next_wake_in(minutes=default_wake_minutes, now=now)


def select_due_symbols(
    symbols: list[str],
    *,
    sched: Scheduler,
    once: bool,
    bootstrap: bool,
    now: datetime | None = None,
) -> list[str]:
    if once or bootstrap:
        return symbols
    return sched.due_symbols(symbols, now=now)


def earliest_active_watch_expiry_iso(
    sched: Scheduler,
    sym: str,
    *,
    now: datetime,
) -> str | None:
    """Return the nearest active watch expiry for one symbol, as ISO string."""
    try:
        watches = sched.active_indicator_watches(now=now)
    except Exception:  # noqa: BLE001 - best-effort scheduling comfort
        return None
    expiries: list[datetime] = []
    for watch in watches:
        if str(watch.get("symbol")) != sym:
            continue
        raw = watch.get("expires_at")
        if not raw:
            continue
        try:
            dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except ValueError:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        if dt > now:
            expiries.append(dt)
    return min(expiries).isoformat() if expiries else None


def resolve_wake_event(
    event: str,
    sym: str,
    now: datetime,
    macro_next: list[dict],
    next_regular_session_open: Callable,
) -> str | None:
    """Resolve a calendar wake event to an absolute ISO timestamp."""
    try:
        if event == "session_open":
            dt = next_regular_session_open(now, symbol=sym)
            return dt.isoformat()
        if event == "macro_event":
            if macro_next:
                return macro_next[0]["at"]
            return None
        if event == "pre_earnings":
            return None
        return None
    except Exception:  # noqa: BLE001 - wake resolution must be fail-safe
        return None


def _noop_event_appender(_event: str, **_payload: object) -> None:
    return None


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
    if sched is None:
        return
    event_appender = append_event or _noop_event_appender
    watch_logger = logger or logging.getLogger("casys-trader")

    cancel_results: list[dict] = []
    for watch_id in cancel_watch_ids:
        watch_id = str(watch_id)
        if not watch_id.startswith(f"{sym}:"):
            event_appender(
                "watch_cancel_rejected",
                symbol=sym,
                watch_id=watch_id,
                reason="not_owned_by_symbol",
            )
            cancel_results.append({"watch_id": watch_id, "outcome": "not_owned"})
            continue
        sched.remove_indicator_watch(watch_id)
        event_appender("watch_cancelled_by_agent", symbol=sym, watch_id=watch_id)
        watch_logger.info("[watch] annulée par l'agent %s %s", sym, watch_id)
        cancel_results.append({"watch_id": watch_id, "outcome": "cancelled"})
    if cancel_results:
        entry["cancel_watch_results"] = cancel_results

    if pending_indicator_watch is not None:
        sched.set_symbol_indicator_watch(sym, pending_indicator_watch)
        event_appender(
            "armed_plan_created"
            if pending_indicator_watch.get("on_trigger") == "EXECUTE_ORDER"
            else "indicator_watch_created",
            symbol=sym,
            watch_id=pending_indicator_watch.get("id"),
            on_trigger=pending_indicator_watch.get("on_trigger"),
            expires_at=pending_indicator_watch.get("expires_at"),
        )
        watch_logger.info(
            "[watch] armée %s %s on_trigger=%s expire=%s",
            sym,
            pending_indicator_watch.get("id"),
            pending_indicator_watch.get("on_trigger"),
            pending_indicator_watch.get("expires_at"),
        )
        entry["indicator_watch_created"] = True
        entry["indicator_watch"] = {
            "id": pending_indicator_watch["id"],
            "expires_at": pending_indicator_watch["expires_at"],
            "logic": pending_indicator_watch["logic"],
            "on_trigger": pending_indicator_watch["on_trigger"],
            "conditions": pending_indicator_watch["conditions"],
        }
        if "order" in pending_indicator_watch:
            entry["indicator_watch"]["order"] = pending_indicator_watch["order"]

    if next_wake_iso is not None:
        sched.set_symbol_next_wake(sym, next_wake_iso)
    elif next_wake_in_minutes is not None:
        sched.set_symbol_next_wake_in(sym, minutes=next_wake_in_minutes, now=now)
    else:
        watch_wake_iso = earliest_active_watch_expiry_iso(sched, sym, now=now)
        if watch_wake_iso is not None:
            sched.set_symbol_next_wake(sym, watch_wake_iso)
        else:
            sched.clear_symbol_next_wake(sym)
