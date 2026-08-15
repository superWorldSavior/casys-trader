from datetime import datetime, timedelta, timezone

from trader.runtime._retry_policy import (
    failure_backoff,
    failure_delay_seconds,
    latest_failure,
    parse_datetime,
)


NOW = datetime(2026, 7, 10, 12, 0, tzinfo=timezone.utc)


def test_failure_delay_seconds_standard_ladder_and_prepared_write() -> None:
    assert [failure_delay_seconds(attempt) for attempt in range(1, 7)] == [
        30 * 60,
        60 * 60,
        120 * 60,
        240 * 60,
        360 * 60,
        360 * 60,
    ]
    assert failure_delay_seconds(
        1,
        error_code="prepared_write_error",
        prepared_write_base_minutes=1,
        prepared_write_max_minutes=30,
    ) == 60
    assert failure_delay_seconds(
        6,
        error_code="prepared_write_error",
        prepared_write_base_minutes=1,
        prepared_write_max_minutes=30,
    ) == 30 * 60
    assert failure_delay_seconds(1, error_code="prepared_write_error") == 30 * 60


def test_latest_failure_universe_mode_filters_status() -> None:
    nested = {
        "latest_failure": {
            "status": "error",
            "retry_lineage": "scope-a",
            "error_code": "TimeoutError",
        }
    }
    assert latest_failure(nested) == nested["latest_failure"]
    assert latest_failure({"status": "error", "retry_lineage": "scope-a"})["retry_lineage"] == "scope-a"
    assert latest_failure({"latest_failure": {"retry_lineage": "scope-a"}}) is None
    assert latest_failure(
        {
            "last_failure_at": "2026-07-09T09:00:00+00:00",
            "last_error": {"code": "TimeoutError", "message": "down"},
        }
    ) is None


def test_latest_failure_legacy_aliases_reconstruct_root_fields() -> None:
    nested = {"latest_failure": {"failed_at": "x", "error_code": "TimeoutError"}}
    assert latest_failure(nested, legacy_aliases=True) == nested["latest_failure"]
    assert latest_failure(
        {
            "status": "success",
            "last_failure_at": "2026-07-09T09:00:00+00:00",
            "last_error": {"code": "TimeoutError", "message": "provider unavailable"},
        },
        legacy_aliases=True,
    ) == {
        "failed_at": "2026-07-09T09:00:00+00:00",
        "error_code": "TimeoutError",
        "error_message": "provider unavailable",
        "error": {"code": "TimeoutError", "message": "provider unavailable"},
    }


def test_failure_backoff_universe_mode_uses_as_of_and_prepared_write() -> None:
    lineage = "scope-a"
    raw = {
        "status": "error",
        "retry_lineage": lineage,
        "retry_attempt": 1,
        "error_code": "prepared_write_error",
        "as_of": NOW.isoformat(),
    }
    assert failure_backoff(raw, retry_lineage=lineage, now=NOW + timedelta(seconds=30)) is not None
    open_gate = failure_backoff(
        raw,
        retry_lineage=lineage,
        now=NOW + timedelta(minutes=2),
        prepared_write_base_minutes=1,
        prepared_write_max_minutes=30,
    )
    assert open_gate is None
    still_closed = failure_backoff(
        raw,
        retry_lineage=lineage,
        now=NOW + timedelta(minutes=2),
    )
    assert still_closed is not None
    assert still_closed["error"] == "prepared_write_error"


def test_failure_backoff_legacy_aliases_read_next_at_and_failed_at() -> None:
    lineage = "macro:EU"
    raw = {
        "latest_failure": {
            "retry_lineage": lineage,
            "attempt": 2,
            "delay_seconds": 3600,
            "next_at": (NOW + timedelta(hours=1)).isoformat(),
            "failed_at": NOW.isoformat(),
            "error": {"code": "TimeoutError", "message": "down"},
        }
    }
    closed = failure_backoff(raw, retry_lineage=lineage, now=NOW + timedelta(minutes=10), legacy_aliases=True)
    assert closed is not None
    assert closed["attempt"] == 2
    assert closed["delay_seconds"] == 3600
    assert closed["error"] == "TimeoutError"
    assert failure_backoff(
        raw,
        retry_lineage=lineage,
        now=NOW + timedelta(hours=1),
        legacy_aliases=True,
    ) is None
    assert failure_backoff(raw, retry_lineage="other", now=NOW, legacy_aliases=True) is None


def test_parse_datetime_accepts_zulu_and_rejects_empty() -> None:
    parsed = parse_datetime("2026-07-09T09:00:00Z")
    assert parsed is not None
    assert parsed.tzinfo is not None
    assert parse_datetime("") is None
    assert parse_datetime(None) is None
