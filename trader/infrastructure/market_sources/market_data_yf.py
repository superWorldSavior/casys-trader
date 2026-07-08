"""Frontière I/O Yahoo Finance pour les données marché.

L'accès réseau brut vit dans `yahoo_client` (endpoint public v8 chart, urllib) ;
ce module garde la seule logique métier : rejet des barres non exploitables et
agrégation 4h. `yahoo_client` traduit `null`→NaN sans jamais juger une barre, si
bien que « qu'est-ce qu'un prix valide » reste décidé ici, à un seul endroit.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable, Sequence

from trader.domain.market_data import Bar, MarketError
from trader.infrastructure.market_sources.yahoo_client import RawBar, fetch_ohlc


@dataclass(frozen=True)
class Quote:
    symbol: str
    price: float
    ts: str


def get_bars(
    symbol: str,
    lookback: str = "5d",
    interval: str = "1h",
    *,
    fetch: Callable[[str, str, str], Sequence[RawBar]] = fetch_ohlc,
) -> list[Bar]:
    """Barres OHLCV. lookback ex: '1d','5d','1mo'; interval ex: '1h','1d'.

    `fetch` (transport injectable, défaut = Yahoo v8) lève lui-même
    MarketError('fetch_failed'|'no_data') — jamais de retour silencieux vide.
    """
    source_interval = "1h" if interval == "4h" else interval

    raw = fetch(symbol, lookback, source_interval)

    bars: list[Bar] = []
    for rb in raw:
        close = rb.close
        # Yahoo renvoie par moments des barres à close 0/NaN (titres peu
        # liquides, intraday). Ce n'est PAS un prix : on la jette ici, à la
        # source, sinon elle empoisonne valorisation/sizing/décision (falaise
        # d'équité STMN.SW 30/06). La fraîcheur ne checke que l'âge, pas la valeur.
        if not math.isfinite(close) or close <= 0.0:
            continue
        bars.append(
            Bar(
                ts=rb.ts,
                open=rb.open,
                high=rb.high,
                low=rb.low,
                close=close,
                volume=rb.volume,
            )
        )
    if not bars:
        raise MarketError("no_data", f"{symbol}: toutes les barres invalides (close 0/NaN)")
    if source_interval == interval:
        return bars

    from trader.market import market_data as market

    return market.aggregate_bars(bars, target_interval=interval, source_interval=source_interval)


def get_quote(symbol: str) -> Quote:
    """Dernier prix connu (close de la dernière barre intraday)."""
    bars = get_bars(symbol, lookback="1d", interval="1h")
    last = bars[-1]
    return Quote(symbol=symbol, price=last.close, ts=last.ts)
