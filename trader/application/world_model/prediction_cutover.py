"""Cutoff and report contracts for the offline World prediction cutover.

This module has no I/O.  Persistence, leases and SQLite live in infrastructure.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime, timezone

CUTOVER_REPORT_SCHEMA = "world_prediction_cutover_report.v1"
CUTOVER_JOURNAL_SCHEMA = "world_prediction_cutover_journal.v1"


def aware_utc_datetime(value: object, *, field: str = "cutover clock") -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must return a timezone-aware UTC datetime")
    return value.astimezone(timezone.utc)


def resolve_cutover_clock(clock: Callable[[], datetime] | datetime | None) -> datetime:
    if clock is None:
        return datetime.now(timezone.utc)
    if isinstance(clock, datetime):
        return aware_utc_datetime(clock, field="cutover clock")
    if callable(clock):
        return aware_utc_datetime(clock(), field="cutover clock")
    raise TypeError("cutover clock must be a timezone-aware datetime or callable")


def utc_cutover_today(clock: Callable[[], datetime] | datetime | None) -> date:
    return resolve_cutover_clock(clock).date()


def reject_open_or_future_cutoff(before: date, *, today: date) -> None:
    if not isinstance(before, date) or isinstance(before, datetime):
        raise TypeError("before must be a date")
    if before > today:
        raise ValueError(
            f"before {before.isoformat()} includes the open UTC day {today.isoformat()} or a future day"
        )


__all__ = [
    "CUTOVER_JOURNAL_SCHEMA",
    "CUTOVER_REPORT_SCHEMA",
    "aware_utc_datetime",
    "reject_open_or_future_cutoff",
    "resolve_cutover_clock",
    "utc_cutover_today",
]
