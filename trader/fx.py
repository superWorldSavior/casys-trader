# trader/fx.py
"""fx — devise de cotation et conversion vers la base USD.

Module pur : pas d'I/O, pas de fetch. Reçoit le taux, l'applique.
`rate` = USD par unité de la devise native (USD→1.0).
"""
from __future__ import annotations

import math

BASE_CCY = "USD"

SUFFIX_CCY: dict[str, str] = {
    ".TW": "TWD",
    ".TWO": "TWD",
    ".PA": "EUR",
    ".DE": "EUR",
    ".AS": "EUR",
    ".MI": "EUR",
}


def currency_for(symbol: str) -> str:
    """Devise de cotation dérivée du suffixe. Défaut USD."""
    sym = (symbol or "").strip().upper()
    for suffix, ccy in SUFFIX_CCY.items():
        if sym.endswith(suffix):
            return ccy
    return BASE_CCY


def to_usd(amount: float, ccy: str, rate: float) -> float:
    """Convertit `amount` (en `ccy`) vers USD via `rate` (USD/unité ccy)."""
    if ccy == BASE_CCY:
        return amount
    if not math.isfinite(rate) or rate <= 0.0:
        raise ValueError(f"fx rate invalide pour {ccy}: {rate!r}")
    return amount * rate
