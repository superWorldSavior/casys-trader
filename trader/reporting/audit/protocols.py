"""Protocols for decision-audit reporting collaborators."""

from __future__ import annotations

from typing import Protocol

__all__ = ["PriceHistoryLoader"]


class PriceHistoryLoader(Protocol):
    def __call__(
        self,
        symbols: list[str],
        *,
        start: str,
        end: str,
        interval: str,
    ) -> dict[str, list[dict]]:
        ...
