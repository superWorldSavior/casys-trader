"""Execution/planning eligibility for symbols in one cycle."""

from __future__ import annotations

from datetime import datetime

from trader.domain.market import sessions as market


def build_execution_eligibility(
    symbols: list[str],
    *,
    stale_market_data: dict[str, dict],
    prices: dict[str, float],
    daily_bars_by_symbol: dict[str, list],
    data_age_by_symbol: dict[str, float],
    now: datetime,
    runtime_interval: str,
) -> dict[str, dict]:
    """Classify symbols into execution and planning contexts."""
    eligibility: dict[str, dict] = {}
    for sym in symbols:
        daily_bars = daily_bars_by_symbol.get(sym)
        stale = stale_market_data.get(sym) or {}
        eligibility[sym] = market.classify_symbol_context(
            runtime_interval=runtime_interval,
            has_runtime_price=sym in prices,
            is_runtime_stale=sym in stale_market_data,
            session_open=bool(market.session_snapshot(sym, now=now).get("open")),
            daily_fresh=bool(daily_bars),
            last_runtime_bar_ts=stale.get("last_bar_ts"),
            data_age_minutes=data_age_by_symbol.get(sym),
            daily_as_of=str(daily_bars[-1].ts) if daily_bars else None,
            next_session_open=market.next_regular_session_open(now, symbol=sym).isoformat(),
        )
    return eligibility


def execution_blocked_reason(
    execution_eligibility: dict[str, dict],
    symbol: str,
    *,
    fail_closed: bool = False,
) -> str | None:
    """Return deterministic execution block reason for one symbol, if any."""
    ctx = (execution_eligibility.get(symbol) or {}).get("execution")
    if ctx is None:
        return "execution:unclassified" if fail_closed else None
    if ctx.get("enabled"):
        return None
    return f"execution:{ctx.get('reason') or 'disabled'}"


__all__ = [
    "annotations",
    "datetime",
    "market",
    "build_execution_eligibility",
    "execution_blocked_reason",
]
