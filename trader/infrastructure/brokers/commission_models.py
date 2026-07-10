"""Commission adapters used to approximate external broker pricing."""

from __future__ import annotations

from dataclasses import dataclass

from trader.domain.contracts import Commission, CommissionModelName, Order


class NoCommissionModel:
    def calculate(self, order: Order, price: float) -> Commission:
        return Commission(amount=0.0)


@dataclass(frozen=True)
class IbkrCommissionModel:
    """Approximate IBKR Pro pricing for paper trading."""

    us_stock_per_share: float = 0.0035
    us_stock_min_per_order: float = 0.35
    us_stock_max_trade_value_pct: float = 0.01
    fx_bps: float = 0.20
    fx_min_per_order: float = 2.0
    index_cfd_rate: float = 0.0001
    france40_cfd_min_per_order: float = 1.0
    europe_stock_rate: float = 0.0005
    europe_stock_min_per_order: float = 1.25
    taiwan_stock_rate: float = 0.0008
    taiwan_stock_min_per_order: float = 80.0

    def calculate(self, order: Order, price: float) -> Commission:
        symbol = order.symbol.upper()
        quantity = abs(float(order.quantity))
        notional = abs(quantity * float(price))
        if quantity <= 0.0 or price <= 0.0:
            return Commission(amount=0.0, model="ibkr_invalid_order")

        if symbol.endswith("=X"):
            return Commission(
                amount=max(notional * (self.fx_bps * 0.0001), self.fx_min_per_order),
                currency="USD",
                model="ibkr_spot_fx_tiered",
            )
        if symbol == "^FCHI":
            return Commission(
                amount=max(notional * self.index_cfd_rate, self.france40_cfd_min_per_order),
                currency="EUR",
                model="ibkr_france40_cfd",
            )
        if symbol.startswith("^"):
            return Commission(
                amount=max(notional * self.index_cfd_rate, 1.0),
                currency="USD",
                model="ibkr_index_cfd",
            )
        if symbol.endswith((".PA", ".DE")):
            return Commission(
                amount=max(notional * self.europe_stock_rate, self.europe_stock_min_per_order),
                currency="EUR",
                model="ibkr_europe_stock_tiered",
            )
        if symbol.endswith(".TW"):
            return Commission(
                amount=max(notional * self.taiwan_stock_rate, self.taiwan_stock_min_per_order),
                currency="TWD",
                model="ibkr_taiwan_stock_tiered",
            )
        if _looks_like_us_stock_or_etf(symbol):
            commission = max(quantity * self.us_stock_per_share, self.us_stock_min_per_order)
            max_commission = notional * self.us_stock_max_trade_value_pct
            return Commission(
                amount=min(commission, max_commission),
                currency="USD",
                model="ibkr_us_stock_tiered",
            )
        return Commission(amount=0.0, currency="USD", model="ibkr_unknown")


def commission_model_from_name(
    name: CommissionModelName | str | None,
) -> IbkrCommissionModel | NoCommissionModel | None:
    normalized = (name or "none").strip().lower()
    if normalized in {"", "none", "off", "false", "0"}:
        return None
    if normalized == "ibkr":
        return IbkrCommissionModel()
    raise ValueError(f"commission model inconnu: {name}")


def _looks_like_us_stock_or_etf(symbol: str) -> bool:
    return symbol.replace(".", "").isalnum() and "-" not in symbol and "=" not in symbol


__all__ = [
    "IbkrCommissionModel",
    "NoCommissionModel",
    "commission_model_from_name",
]
