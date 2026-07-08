"""Execution protocols implemented by broker adapters."""

from __future__ import annotations

from typing import Protocol

from trader.execution.contracts import Commission, Fill, Order, Position

__all__ = ["Broker", "CommissionModel"]


class CommissionModel(Protocol):
    def calculate(self, order: Order, price: float) -> Commission: ...


class Broker(Protocol):
    def submit(self, order: Order, price: float, ts: str, dry_run: bool = True, fx_rate: float = 1.0) -> Fill | None: ...
    def positions(self) -> dict[str, Position]: ...
    def cash(self) -> float: ...
