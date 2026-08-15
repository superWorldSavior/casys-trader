"""Shared exponential failure-retry helpers for kernel analyst runtimes.

Callers keep their historical semantics via optional flags.  Do not silently
unify those modes: universe regional retries filter on status and understand
``prepared_write_error``; news/macro reads pre-lineage aliases.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from typing import Any

DEFAULT_FAILURE_BACKOFF_MINUTES = 30
DEFAULT_FAILURE_BACKOFF_MAX_MINUTES = 360


def ensure_utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc) if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def positive_int(raw: Any, *, default: int) -> int:
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return default


def failure_delay_seconds(
    attempt: int,
    *,
    error_code: Any = None,
    base_minutes: int = DEFAULT_FAILURE_BACKOFF_MINUTES,
    max_minutes: int = DEFAULT_FAILURE_BACKOFF_MAX_MINUTES,
    prepared_write_base_minutes: int | None = None,
    prepared_write_max_minutes: int | None = None,
) -> int:
    """Bounded exponential delay.  ``prepared_write_error`` uses a shorter ladder."""

    prepared_write = (
        error_code == "prepared_write_error" and prepared_write_base_minutes is not None
    )
    if prepared_write:
        base_minutes = prepared_write_base_minutes
        if prepared_write_max_minutes is not None:
            max_minutes = prepared_write_max_minutes
    return min(base_minutes * (2 ** max(0, attempt - 1)), max_minutes) * 60


def latest_failure(raw: Any, *, legacy_aliases: bool = False) -> Mapping[str, Any] | None:
    """Return the persisted failure record for one status payload.

    * ``legacy_aliases=False`` (universe): prefer nested ``latest_failure``, else
      the payload itself, and only if ``status`` is ``invalid`` or ``error``.
    * ``legacy_aliases=True`` (news/macro): return a copy of nested
      ``latest_failure`` with no status filter; otherwise reconstruct from
      root ``last_failure_at`` / ``last_error``.
    """

    if not isinstance(raw, Mapping):
        return None
    nested = raw.get("latest_failure")
    if legacy_aliases:
        if isinstance(nested, Mapping):
            return dict(nested)
        failed_at = raw.get("last_failure_at")
        error = raw.get("last_error")
        if not failed_at or not isinstance(error, Mapping):
            return None
        code = str(error.get("code") or "analysis_not_written")
        message = str(error.get("message") or "")
        return {
            "failed_at": str(failed_at),
            "error_code": code,
            "error_message": message,
            "error": {"code": code, "message": message},
        }
    candidate: Mapping[str, Any] = nested if isinstance(nested, Mapping) else raw
    return candidate if candidate.get("status") in {"invalid", "error"} else None


def failure_backoff(
    raw: Any,
    *,
    retry_lineage: str,
    now: datetime,
    legacy_aliases: bool = False,
    error_code: Any = None,
    base_minutes: int = DEFAULT_FAILURE_BACKOFF_MINUTES,
    max_minutes: int = DEFAULT_FAILURE_BACKOFF_MAX_MINUTES,
    prepared_write_base_minutes: int | None = None,
    prepared_write_max_minutes: int | None = None,
) -> dict[str, Any] | None:
    """Return the active persisted retry gate for one retry lineage, or None."""

    failure = latest_failure(raw, legacy_aliases=legacy_aliases)
    if failure is None or failure.get("retry_lineage") != retry_lineage:
        return None
    delay_kwargs = {
        "error_code": error_code,
        "base_minutes": base_minutes,
        "max_minutes": max_minutes,
        "prepared_write_base_minutes": prepared_write_base_minutes,
        "prepared_write_max_minutes": prepared_write_max_minutes,
    }
    if legacy_aliases:
        attempt = positive_int(failure.get("retry_attempt") or failure.get("attempt"), default=1)
        delay_seconds = positive_int(
            failure.get("retry_delay_seconds") or failure.get("delay_seconds"),
            default=failure_delay_seconds(attempt, **delay_kwargs),
        )
        retry_at = parse_datetime(failure.get("next_retry_at") or failure.get("next_at"))
        failed_at = parse_datetime(
            failure.get("failed_at") or failure.get("last_failure_at") or failure.get("at")
        )
    else:
        delay_kwargs["error_code"] = (
            failure.get("error_code") if error_code is None else error_code
        )
        attempt = max(1, int(failure.get("retry_attempt") or 1))
        delay_seconds = max(
            1,
            int(
                failure.get("retry_delay_seconds")
                or failure_delay_seconds(attempt, **delay_kwargs)
            ),
        )
        retry_at = parse_datetime(failure.get("next_retry_at"))
        failed_at = parse_datetime(failure.get("as_of"))
    if retry_at is None:
        if failed_at is None:
            return None
        retry_at = failed_at + timedelta(seconds=delay_seconds)
    if now >= retry_at:
        return None
    return {
        "attempt": attempt,
        "delay_seconds": delay_seconds,
        "next_retry_at": retry_at.isoformat(),
        "error": _failure_error_label(failure, legacy_aliases=legacy_aliases),
    }


def _failure_error_label(failure: Mapping[str, Any], *, legacy_aliases: bool) -> str:
    if legacy_aliases:
        error = failure.get("error")
        if isinstance(error, Mapping) and error.get("code"):
            return str(error["code"])
        return str(failure.get("error_code") or "unknown")
    return str(failure.get("error_code") or failure.get("status") or "unknown")
