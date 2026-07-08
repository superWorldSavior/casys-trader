"""Commission models and pure paper-fill accounting helpers."""

from __future__ import annotations

import math
from dataclasses import dataclass

from trader.execution.contracts import Commission, CommissionModelName, Order
from trader.execution.ports import CommissionModel
from trader.market import fx

POSITION_EPSILON = 1e-9


class NoCommissionModel:
    def calculate(self, order: Order, price: float) -> Commission:
        return Commission(amount=0.0)


@dataclass(frozen=True)
class IbkrCommissionModel:
    """Approximation IBKR Pro pour le paper trading.

    Les taux changent par pays/compte/venue. On encode les paliers bas utiles à
    l'univers courant pour éviter de surévaluer les scalps papier.
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


def _looks_like_us_stock_or_etf(symbol: str) -> bool:
    return symbol.replace(".", "").isalnum() and "-" not in symbol and "=" not in symbol


def round_trip_cost(
    model: CommissionModel | None,
    symbol: str,
    price: float | None,
    ref_notional: float,
) -> dict | None:
    """Coût aller-retour d'un trade au prix courant, pour un notionnel de référence.

    Le modèle de commission ne dépend pas du sens (BUY/SELL) : au même prix et
    même quantité, entrée et sortie coûtent l'identique, donc l'aller-retour vaut
    2x la commission d'une jambe. On expose deux signaux décisionnels :
        - `fee_rt`  : coût aller-retour en devise du symbole
        - `be_bps`  : seuil de rentabilité en points de base (mouvement minimal
                      du prix pour couvrir les frais), = fee_rt / notional x 1e4

    Renvoie None si le coût n'est pas estimable (pas de modèle, prix/notionnel
    invalides) - l'appelant n'affiche alors aucune colonne frais.
    """
    if (
        model is None
        or price is None
        or not math.isfinite(price)
        or price <= 0.0
        or not math.isfinite(ref_notional)
        or ref_notional <= 0.0
    ):
        return None
    quantity = ref_notional / price
    if quantity <= 0.0:
        return None
    one_way = model.calculate(Order(symbol=symbol, side="BUY", quantity=quantity), price)
    if one_way.model in {"ibkr_unknown", "ibkr_invalid_order"}:
        # Coût non modélisé pour ce symbole : ne pas l'afficher comme gratuit.
        return None
    fee_rt = one_way.amount * 2.0
    be_bps = (fee_rt / ref_notional) * 10_000.0
    return {"fee_rt": round(fee_rt, 2), "currency": one_way.currency, "be_bps": round(be_bps, 2)}


def commission_model_from_name(name: CommissionModelName | str | None) -> CommissionModel | None:
    normalized = (name or "none").strip().lower()
    if normalized in {"", "none", "off", "false", "0"}:
        return None
    if normalized == "ibkr":
        return IbkrCommissionModel()
    raise ValueError(f"commission model inconnu: {name}")


def compute_fill_effect(
    *,
    old_quantity: float,
    old_avg_price: float,
    order: Order,
    price: float,
    fx_rate: float,
    commission: Commission,
) -> tuple[float, float, float]:
    """Retourne (new_quantity, new_avg_price, cash_delta_total_usd) - logique paper.

    cash_delta_total_usd est le montant à soustraire du cash (USD) après le fill.

    Logique avg_price :
      - new_qty == 0                            -> avg_price = 0 (position clôturée)
      - old_qty == 0 ou même sens               -> moyenne pondérée
      - retournement (old_qty * new_qty < 0)    -> prix d'exécution
      - réduction sans retournement             -> avg_price inchangé
    """
    if abs(old_quantity) <= POSITION_EPSILON:
        old_quantity = 0.0
        old_avg_price = 0.0

    signed = order.quantity if order.side == "BUY" else -order.quantity
    new_qty = old_quantity + signed
    if abs(new_qty) <= POSITION_EPSILON:
        new_qty = 0.0

    if new_qty == 0:
        new_avg_price = 0.0
    elif old_quantity == 0 or old_quantity * signed > 0:
        total = old_avg_price * abs(old_quantity) + price * abs(signed)
        new_avg_price = total / abs(new_qty)
    elif old_quantity * new_qty < 0:
        new_avg_price = price
    else:
        new_avg_price = old_avg_price

    ccy = fx.currency_for(order.symbol)
    cash_delta = fx.to_usd(signed * price, ccy, fx_rate)
    fee_usd = fx.to_usd(commission.amount, commission.currency, fx_rate)
    return new_qty, new_avg_price, cash_delta + fee_usd
