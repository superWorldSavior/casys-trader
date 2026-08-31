"""Application scheduling policy for one daemon cycle.

The runtime daemon owns process composition and state files. This module owns the
small scheduling decisions that can be tested without booting the runtime loop.
"""

from __future__ import annotations

import copy
import logging
from datetime import datetime, timedelta, timezone
from typing import Callable

from trader.domain.planning.scheduling import (
    stale_backoff_wake_minutes as stale_backoff_wake_minutes,
)
from trader.domain.planning.protocols import SchedulerLike
from trader.domain.planning.relevance_gate import (
    CALM_REVIEW_MAX_HOURS,
    HOT_REVIEW_MAX_HOURS,
)

EventAppender = Callable[..., None]
_UNSET = object()


def bounded_wake_minutes(value: float, *, minimum: float | None = None, maximum: float | None = None) -> float:
    """Return the agent-requested wake delay, bounded only by explicit flags."""
    result = float(value)
    if minimum is not None:
        result = max(result, minimum)
    if maximum is not None:
        result = min(result, maximum)
    return result


def ensure_default_wake(
    sched: SchedulerLike,
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
    sched: SchedulerLike,
    once: bool,
    bootstrap: bool,
    now: datetime | None = None,
) -> list[str]:
    if once or bootstrap:
        return symbols
    return sched.due_symbols(symbols, now=now)


def earliest_active_watch_expiry_iso(
    sched: SchedulerLike,
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


def has_active_watch(
    sched: SchedulerLike,
    sym: str,
    *,
    now: datetime,
) -> bool:
    """Return whether one non-expired indicator watch belongs to ``sym``."""
    try:
        return any(
            str(watch.get("symbol") or "") == sym
            for watch in sched.active_indicator_watches(now=now)
        )
    except Exception:  # noqa: BLE001 - cadence comfort must stay best-effort
        return False


def _wake_datetime(raw: str) -> datetime:
    parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


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


def read_schedule_effect(
    sched: SchedulerLike | None,
    *,
    sym: str,
    now: datetime,
    expected_next_wake: str | None | object = _UNSET,
    expected_active_watch_ids: set[str] | None = None,
    expected_absent_watch_ids: set[str] | None = None,
) -> dict:
    """Read and compare authoritative scheduler state after a mutation."""
    if sched is None:
        return {"status": "not_applicable", "reason": "scheduler_unavailable"}
    try:
        persisted_wake = sched.next_wake(sym)
        persisted_watches = [
            watch for watch in sched.active_indicator_watches(now=now) if str(watch.get("symbol") or "") == sym
        ]
        has_symbol_wake = getattr(sched, "has_symbol_wake", None)
        persisted_symbol_wake = bool(has_symbol_wake(sym)) if callable(has_symbol_wake) else None
    except Exception as exc:  # noqa: BLE001 - missing proof must stay explicit
        return {"status": "unavailable", "error": type(exc).__name__}
    effect = {
        "status": "unverified",
        "next_wake": persisted_wake.isoformat() if persisted_wake is not None else None,
        "active_watch_ids": sorted(str(watch.get("id")) for watch in persisted_watches if watch.get("id")),
    }
    if expected_next_wake is _UNSET:
        return effect

    actual_wake = effect["next_wake"]
    expected_wake = _canonical_wake(expected_next_wake)
    expected_ids = expected_active_watch_ids or set()
    absent_ids = expected_absent_watch_ids or set()
    observed_ids = set(effect["active_watch_ids"])
    wake_matches = (
        persisted_symbol_wake is False
        if expected_wake is None
        else persisted_symbol_wake is True and actual_wake == expected_wake
    )
    matches = wake_matches and expected_ids <= observed_ids and absent_ids.isdisjoint(observed_ids)
    if matches:
        effect["status"] = "verified"
        return effect
    effect["status"] = "mismatch"
    effect["reason"] = "schedule_receipt_mismatch"
    return effect


def _canonical_wake(value: str | None | object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError("expected_next_wake must be an ISO string or None")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def apply_decision_schedule(
    *,
    sched: SchedulerLike | None,
    sym: str,
    now: datetime,
    next_wake_in_minutes: float | None,
    next_wake_iso: str | None = None,
    cancel_watch_ids: list[str],
    pending_indicator_watch: dict | None,
    entry: dict,
    has_position: bool = False,
    session_open: bool = False,
    append_event: EventAppender | None = None,
    logger: logging.Logger | None = None,
) -> None:
    if sched is None:
        entry["schedule_effect"] = read_schedule_effect(
            sched,
            sym=sym,
            now=now,
            expected_next_wake=None,
        )
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
        superseded_watch_ids = (
            sched.set_symbol_indicator_watch(sym, pending_indicator_watch) or []
        )
        for superseded_watch_id in superseded_watch_ids:
            event_appender(
                "indicator_watch_superseded",
                symbol=sym,
                watch_id=superseded_watch_id,
                superseded_by_watch_id=pending_indicator_watch.get("id"),
            )
        if superseded_watch_ids:
            entry["indicator_watch_superseded_ids"] = list(superseded_watch_ids)
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
        # The decision ledger is also the continuity input for the next model
        # call.  Keep the exact normalized watch instead of rebuilding a lossy
        # projection that drops its creation time, rationale or future fields.
        entry["indicator_watch"] = copy.deepcopy(pending_indicator_watch)

    candidates: list[tuple[str, datetime]] = []
    if next_wake_iso is not None:
        candidates.append(("agent", _wake_datetime(next_wake_iso)))
    elif next_wake_in_minutes is not None:
        candidates.append(
            (
                "agent",
                (now + timedelta(minutes=float(next_wake_in_minutes))).astimezone(
                    timezone.utc
                ),
            )
        )

    watch_wake_iso = earliest_active_watch_expiry_iso(sched, sym, now=now)
    if watch_wake_iso is not None:
        candidates.append(("watch_expiry", _wake_datetime(watch_wake_iso)))

    hot = session_open and (has_position or has_active_watch(sched, sym, now=now))
    if hot:
        hot_deadline = (now + timedelta(hours=HOT_REVIEW_MAX_HOURS)).astimezone(
            timezone.utc
        )
        candidates.append(("hot_review", hot_deadline))
        entry["hot_review_deadline"] = hot_deadline.isoformat()
    else:
        calm_deadline = (now + timedelta(hours=CALM_REVIEW_MAX_HOURS)).astimezone(
            timezone.utc
        )
        candidates.append(("calm_review", calm_deadline))
        entry["calm_review_deadline"] = calm_deadline.isoformat()

    if candidates:
        wake_source, wake_at = min(candidates, key=lambda item: item[1])
        expected_next_wake = wake_at.isoformat()
        sched.set_symbol_next_wake(sym, expected_next_wake)
        entry["schedule_wake_source"] = wake_source
    else:
        sched.clear_symbol_next_wake(sym)
        expected_next_wake = None

    entry["schedule_effect"] = read_schedule_effect(
        sched,
        sym=sym,
        now=now,
        expected_next_wake=expected_next_wake,
        expected_active_watch_ids=(
            {str(pending_indicator_watch["id"])} if pending_indicator_watch is not None else set()
        ),
        expected_absent_watch_ids={result["watch_id"] for result in cancel_results if result["outcome"] == "cancelled"},
    )
