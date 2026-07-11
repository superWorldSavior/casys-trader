"""Stable identity for durable decision records."""

from __future__ import annotations

__all__ = ["decision_id"]


def decision_id(cycle_ts: str, sequence: int, symbol: str) -> str:
    """Return the stable cross-store identity of one cycle decision."""

    return f"{cycle_ts}|{sequence}|{symbol}"
