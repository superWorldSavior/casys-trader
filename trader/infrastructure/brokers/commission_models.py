"""Commission adapters used to approximate external broker pricing."""

from __future__ import annotations

from dataclasses import dataclass

from trader.domain.contracts import Commission, CommissionModelName, Order
from trader.domain.market import fx


# First IBKR Pro bracket used by the paper adapter.  These are broker
# commissions only: exchange, regulatory, transaction-tax and other third-party
# pass-through fees are deliberately not represented by this approximation.
_EUR_TIERED_SUFFIXES = frozenset(
    {
        ".AS",  # Netherlands
        ".BR",  # Belgium
        ".DE",  # Germany
        ".HE",  # Finland
        ".LS",  # Portugal
        ".MI",  # Italy
        ".PA",  # France
        ".VI",  # Austria
    }
)
_NORDIC_TIERED_SUFFIXES = frozenset({".CO", ".OL", ".ST"})
_TAIWAN_TIERED_SUFFIXES = frozenset({".T", ".TW", ".TWO"})
_UNAVAILABLE_MODEL = "ibkr_unpriced_venue"


class NoCommissionModel:
    def calculate(self, order: Order, price: float) -> Commission:
        return Commission(amount=0.0)


@dataclass(frozen=True)
class IbkrCommissionModel:
    """Approximate first-bracket IBKR Pro pricing for paper trading.

    The adapter models broker commission only.  It does not claim to include
    venue/regulatory pass-through fees or taxes (notably Taiwan sell tax).
    """

    us_stock_per_share: float = 0.0035
    us_stock_min_per_order: float = 0.35
    us_stock_max_trade_value_pct: float = 0.01
    fx_bps: float = 0.20
    fx_min_per_order: float = 2.0
    index_cfd_rate: float = 0.0001
    france40_cfd_min_per_order: float = 1.0
    europe_stock_rate: float = 0.0005
    europe_stock_min_per_order: float = 1.25
    europe_stock_max_per_order: float = 29.0
    spain_stock_min_per_order: float = 3.0
    uk_stock_min_per_order: float = 1.0
    switzerland_stock_min_per_order: float = 1.5
    switzerland_stock_max_per_order: float = 49.0
    nordic_stock_min_per_order: float = 10.0
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
        if symbol in fx.SYMBOL_CCY:
            return Commission(
                amount=0.0,
                currency=fx.currency_for(symbol),
                model=_UNAVAILABLE_MODEL,
            )
        if symbol.startswith("^"):
            return Commission(
                amount=max(notional * self.index_cfd_rate, 1.0),
                currency="USD",
                model="ibkr_index_cfd",
            )
        suffix = fx.mapped_suffix_for(symbol)
        if suffix in _EUR_TIERED_SUFFIXES:
            return Commission(
                amount=min(
                    max(
                        notional * self.europe_stock_rate,
                        self.europe_stock_min_per_order,
                    ),
                    self.europe_stock_max_per_order,
                ),
                currency=fx.currency_for(symbol),
                model="ibkr_europe_stock_tiered",
            )
        if suffix == ".MC":
            return Commission(
                amount=max(
                    notional * self.europe_stock_rate,
                    self.spain_stock_min_per_order,
                ),
                currency=fx.currency_for(symbol),
                model="ibkr_spain_stock_fixed_smartrouting",
            )
        if suffix == ".L":
            return Commission(
                amount=max(
                    notional * self.europe_stock_rate,
                    self.uk_stock_min_per_order,
                ),
                currency=fx.currency_for(symbol),
                model="ibkr_uk_stock_tiered",
            )
        if suffix == ".SW":
            return Commission(
                amount=min(
                    max(
                        notional * self.europe_stock_rate,
                        self.switzerland_stock_min_per_order,
                    ),
                    self.switzerland_stock_max_per_order,
                ),
                currency=fx.currency_for(symbol),
                model="ibkr_switzerland_stock_tiered",
            )
        if suffix in _NORDIC_TIERED_SUFFIXES:
            return Commission(
                amount=max(
                    notional * self.europe_stock_rate,
                    self.nordic_stock_min_per_order,
                ),
                currency=fx.currency_for(symbol),
                model="ibkr_nordic_stock_tiered",
            )
        if suffix in _TAIWAN_TIERED_SUFFIXES:
            return Commission(
                amount=max(notional * self.taiwan_stock_rate, self.taiwan_stock_min_per_order),
                currency=fx.currency_for(symbol),
                model="ibkr_taiwan_stock_tiered",
            )
        # A canonical non-US market must never fall through to the permissive
        # US ticker heuristic.  Missing pricing remains visible and downstream
        # economics fail closed instead of presenting a fabricated US fee.
        if suffix is not None or symbol in fx.SYMBOL_CCY:
            return Commission(
                amount=0.0,
                currency=fx.currency_for(symbol),
                model=_UNAVAILABLE_MODEL,
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
