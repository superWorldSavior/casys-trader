"""Frontière I/O Yahoo Finance pour les données marché."""

from __future__ import annotations

import math
from dataclasses import dataclass

from trader.domain.market_data import Bar, MarketError


@dataclass(frozen=True)
class Quote:
    symbol: str
    price: float
    ts: str


def get_bars(symbol: str, lookback: str = "5d", interval: str = "1h") -> list[Bar]:
    """Barres OHLCV. lookback ex: '1d','5d','1mo'; interval ex: '1h','1d'.

    Lève MarketError(code='no_data'|'fetch_failed') en cas d'échec — jamais de
    retour silencieux vide ambigu.
    """
    import yfinance as yf

    source_interval = "1h" if interval == "4h" else interval

    try:
        df = yf.Ticker(symbol).history(period=lookback, interval=source_interval, auto_adjust=False)
    except Exception as e:  # noqa: BLE001 — frontière externe
        raise MarketError("fetch_failed", f"{symbol}: {e}") from e

    if df is None or df.empty:
        raise MarketError("no_data", f"{symbol} (lookback={lookback}, interval={interval})")

    bars: list[Bar] = []
    for idx, row in df.iterrows():
        close = float(row["Close"])
        # Yahoo renvoie par moments des barres à close 0/NaN (titres peu
        # liquides, intraday). Ce n'est PAS un prix : on la jette ici, à la
        # source, sinon elle empoisonne valorisation/sizing/décision (falaise
        # d'équité STMN.SW 30/06). La fraîcheur ne checke que l'âge, pas la valeur.
        if not math.isfinite(close) or close <= 0.0:
            continue
        bars.append(
            Bar(
                ts=idx.isoformat(),
                open=float(row["Open"]),
                high=float(row["High"]),
                low=float(row["Low"]),
                close=close,
                volume=float(row["Volume"]),
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
