# trader/fx.py
"""fx — devise de cotation et conversion vers la base USD.

Module pur : pas d'I/O, pas de fetch. Reçoit le taux, l'applique.
`rate` = USD par unité de la devise native (USD→1.0).
"""
from __future__ import annotations

import math

BASE_CCY = "USD"

# Correspondances exactes — prioritaires sur la boucle suffixes.
SYMBOL_CCY: dict[str, str] = {
    "^FCHI": "EUR",
    "^TWII": "TWD",
}

SUFFIX_CCY: dict[str, str] = {
    ".TW": "TWD",
    ".TWO": "TWD",
    ".PA": "EUR",
    ".DE": "EUR",
    ".AS": "EUR",
    ".MI": "EUR",
    ".L": "GBP",
    ".SW": "CHF",
    # Places eurozone du pool (Bruxelles, Helsinki, Lisbonne, Madrid, Vienne).
    ".BR": "EUR",
    ".HE": "EUR",
    ".LS": "EUR",
    ".MC": "EUR",
    ".VI": "EUR",
    # Nordiques hors euro (Copenhague, Oslo, Stockholm).
    ".CO": "DKK",
    ".OL": "NOK",
    ".ST": "SEK",
    # .T : dans CE pool ce sont des titres taïwanais (variante du suffixe .TW),
    # PAS du Tokyo — confirmé par Erwan 2026-06-24 (« y a pas de Tokyo ici, c'est
    # que du Taïwan »). Donc TWD. Pas de collision avec .TW/.TWO (endswith distinct).
    ".T": "TWD",
}


def currency_for(symbol: str) -> str:
    """Devise de cotation : table exacte SYMBOL_CCY puis suffixe SUFFIX_CCY. Défaut USD."""
    sym = (symbol or "").strip().upper()
    if sym in SYMBOL_CCY:
        return SYMBOL_CCY[sym]
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
