"""Protocols for decision-ledger reporting collaborators."""

from __future__ import annotations

from typing import Protocol

__all__ = ["DecisionLedgerAppender", "DecisionLedgerReader"]


class DecisionLedgerReader(Protocol):
    def read_all(self, *, symbol: str | None = None, limit: int | None = None) -> list[dict]:
        ...


class DecisionLedgerAppender(Protocol):
    def append(self, row: dict) -> object:
        ...
