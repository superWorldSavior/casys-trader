"""Small builders for durable trade entry context snapshots."""

from __future__ import annotations


def build_trade_entry_context(
    *,
    price: float,
    runtime_interval: str,
    data_age_minutes: float | None,
    session_open: bool,
    daily_as_of: object | None,
) -> dict[str, object]:
    """Build the stable context persisted on TradePlan entries."""
    return {
        "price": price,
        "runtime_interval": runtime_interval,
        "data_age_m": None if data_age_minutes is None else int(round(data_age_minutes)),
        "session_open": bool(session_open),
        "daily_as_of": daily_as_of,
    }
