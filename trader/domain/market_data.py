"""Neutral market-data primitives shared across packages."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Bar:
    ts: str  # ISO 8601
    open: float
    high: float
    low: float
    close: float
    volume: float


class MarketError(Exception):
    """Erreur d'accès marché. code machine-readable + contexte."""

    def __init__(self, code: str, context: str):
        self.code = code
        self.context = context
        super().__init__(f"{code}: {context}")
