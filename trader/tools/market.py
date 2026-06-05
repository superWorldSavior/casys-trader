"""market — lecture des données marché.

v1 : yfinance (gratuit, US listed). Derrière une interface stable pour pouvoir
swapper vers une autre source (IB, LEAN, Polygon) plus tard.

Contrat : entrées minimales (symbole, lookback, interval), sortie machine-readable.
Aucune décision ici — uniquement de la donnée brute.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Bar:
    ts: str  # ISO 8601
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True)
class Quote:
    symbol: str
    price: float
    ts: str


class MarketError(Exception):
    """Erreur d'accès marché. code machine-readable + contexte."""

    def __init__(self, code: str, context: str):
        self.code = code
        self.context = context
        super().__init__(f"{code}: {context}")


def get_bars(symbol: str, lookback: str = "5d", interval: str = "1h") -> list[Bar]:
    """Barres OHLCV. lookback ex: '1d','5d','1mo'; interval ex: '1h','1d'.

    Lève MarketError(code='no_data'|'fetch_failed') en cas d'échec — jamais de
    retour silencieux vide ambigu.
    """
    import yfinance as yf

    try:
        df = yf.Ticker(symbol).history(period=lookback, interval=interval, auto_adjust=False)
    except Exception as e:  # noqa: BLE001 — frontière externe
        raise MarketError("fetch_failed", f"{symbol}: {e}") from e

    if df is None or df.empty:
        raise MarketError("no_data", f"{symbol} (lookback={lookback}, interval={interval})")

    bars: list[Bar] = []
    for idx, row in df.iterrows():
        bars.append(
            Bar(
                ts=idx.isoformat(),
                open=float(row["Open"]),
                high=float(row["High"]),
                low=float(row["Low"]),
                close=float(row["Close"]),
                volume=float(row["Volume"]),
            )
        )
    return bars


def get_quote(symbol: str) -> Quote:
    """Dernier prix connu (close de la dernière barre intraday)."""
    bars = get_bars(symbol, lookback="1d", interval="1h")
    last = bars[-1]
    return Quote(symbol=symbol, price=last.close, ts=last.ts)
