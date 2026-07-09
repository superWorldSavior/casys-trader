"""Protocols for reporting read-model collaborators."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol

__all__ = ["DecisionQualityScorer"]


class DecisionQualityScorer(Protocol):
    def __call__(
        self,
        ledger: str | Path,
        *,
        band: float,
        days_buffer: int,
    ) -> dict[str, Any]:
        ...
