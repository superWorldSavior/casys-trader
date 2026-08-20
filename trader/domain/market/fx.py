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


def mapped_suffix_for(symbol: str) -> str | None:
    """Return the canonical listed-market suffix, when one is configured.

    This is deliberately narrower than ``str.rsplit(".", 1)``: an unknown
    dotted symbol is not silently promoted to a supported venue.  Consumers
    such as commission adapters can therefore distinguish a known non-US
    listing from the historical USD default used by :func:`currency_for`.
    """

    sym = (symbol or "").strip().upper()
    return next(
        (
            suffix
            for suffix in sorted(SUFFIX_CCY, key=len, reverse=True)
            if sym.endswith(suffix)
        ),
        None,
    )


def known_currency_for(symbol: str) -> str | None:
    """Return the quote currency only when the exact market is mapped."""

    sym = (symbol or "").strip().upper()
    if sym in SYMBOL_CCY:
        return SYMBOL_CCY[sym]
    suffix = mapped_suffix_for(sym)
    return SUFFIX_CCY.get(suffix) if suffix is not None else None


def currency_for(symbol: str) -> str:
    """Devise de cotation : mapping canonique, puis défaut USD historique."""

    return known_currency_for(symbol) or BASE_CCY


def to_usd(amount: float, ccy: str, rate: float) -> float:
    """Convertit `amount` (en `ccy`) vers USD via `rate` (USD/unité ccy)."""
    if ccy == BASE_CCY:
        return amount
    if not math.isfinite(rate) or rate <= 0.0:
        raise ValueError(f"fx rate invalide pour {ccy}: {rate!r}")
    return amount * rate
