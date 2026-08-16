"""Compatibility façade for raw-learning watermark selection."""

from trader.domain.learnings.selection import parse_ts, pending_raw_count, select_new_raw

_parse_ts = parse_ts

__all__ = ["pending_raw_count", "select_new_raw"]
