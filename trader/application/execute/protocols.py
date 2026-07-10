"""Ports required by execution application services."""

from __future__ import annotations

from typing import Protocol

from trader.domain.contracts import Commission, Fill, Order, Position


class CommissionModel(Protocol):
    def calculate(self, order: Order, price: float) -> Commission: ...


class Broker(Protocol):
    def submit(
        self,
        order: Order,
        price: float,
        ts: str,
        dry_run: bool = True,
        fx_rate: float = 1.0,
    ) -> Fill | None: ...

    def positions(self) -> dict[str, Position]: ...

    def cash(self) -> float: ...


__all__ = ["Broker", "CommissionModel"]
