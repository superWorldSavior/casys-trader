from datetime import datetime, timedelta, timezone

import pytest

from trader.runtime.ib_attach import IBAttachBackoff


def test_backoff_due_windows_and_success_disables_reprobe() -> None:
    t0 = datetime(2026, 6, 15, 8, 0, tzinfo=timezone.utc)
    retry_after = timedelta(minutes=5)
    epsilon = timedelta(microseconds=1)

    backoff = IBAttachBackoff(retry_after=retry_after)

    assert backoff.due(t0) is True

    backoff.record_failure(t0)

    assert backoff.due(t0) is False
    assert backoff.due(t0 + retry_after - epsilon) is False
    assert backoff.due(t0 + retry_after) is True

    backoff.record_success()

    assert backoff.attached is True
    assert backoff.due(t0 + retry_after) is False
    assert backoff.due(t0 + timedelta(days=1)) is False


def test_backoff_rejette_retry_after_nul_ou_negatif() -> None:
    with pytest.raises(ValueError):
        IBAttachBackoff(retry_after=timedelta(0))

    with pytest.raises(ValueError):
        IBAttachBackoff(retry_after=-timedelta(seconds=1))
