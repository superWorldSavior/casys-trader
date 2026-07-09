"""Protocols for decision-bench reporting collaborators."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Protocol

__all__ = ["BenchHistory", "ModelBenchCompleter"]


class BenchHistory(Protocol):
    def bars_asof(self, symbol: str, ts: str, limit: int) -> Iterable[Any]:
        ...

    def price_asof(self, symbol: str, ts: str) -> float | None:
        ...


class ModelBenchCompleter(Protocol):
    def __call__(self, spec: Any, prompt: str, timeout_s: int) -> Any:
        ...
