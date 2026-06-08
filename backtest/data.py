"""data — historique OHLCV et accès as-of strict.

Le backtest ne doit jamais voir de barre postérieure à l'instant simulé. Toute
lecture passe donc par une recherche bornée sur les timestamps triés.
"""

from __future__ import annotations

import hashlib
import json
from bisect import bisect_right
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from trader.tools.market import Bar


class DataError(Exception):
    """Erreur d'accès historique. code machine-readable + contexte."""

    def __init__(self, code: str, context: str):
        self.code = code
        self.context = context
        super().__init__(f"{code}: {context}")


@dataclass(frozen=True)
class HistoryStore:
    """Stockage déterministe de barres OHLCV triées par symbole."""

    _bars_by_symbol: dict[str, tuple[Bar, ...]]

    @classmethod
    def from_bars(cls, bars_by_symbol: dict[str, list[Bar]]) -> "HistoryStore":
        """Construit un historique mémoire, sans accès réseau."""
        normalized: dict[str, tuple[Bar, ...]] = {}
        for symbol in sorted(bars_by_symbol):
            normalized[symbol] = tuple(sorted(bars_by_symbol[symbol], key=lambda bar: bar.ts))
        return cls(normalized)

    @classmethod
    def load(cls, symbols: list[str], start: str, end: str, interval: str = "1h") -> "HistoryStore":
        """Charge l'historique via yfinance avec cache disque déterministe."""
        cache_dir = Path("state/backtest_cache")
        cache_dir.mkdir(parents=True, exist_ok=True)

        loaded: dict[str, list[Bar]] = {}
        for symbol in sorted(symbols):
            cache_path = cache_dir / f"{_cache_key(symbol, start, end, interval)}.json"
            if cache_path.exists():
                loaded[symbol] = _read_cache(cache_path)
                if not loaded[symbol]:
                    raise DataError("no_data", f"{symbol} cache vide")
                continue

            bars = _fetch_symbol(symbol, start=start, end=end, interval=interval)
            if not bars:
                raise DataError("no_data", f"{symbol} (start={start}, end={end}, interval={interval})")
            cache_path.write_text(json.dumps([asdict(bar) for bar in bars], ensure_ascii=False, indent=2))
            loaded[symbol] = bars

        if not loaded or all(not bars for bars in loaded.values()):
            raise DataError("no_data", f"symbols={symbols}, start={start}, end={end}, interval={interval}")
        return cls.from_bars(loaded)

    def symbols(self) -> list[str]:
        """Symboles disponibles, triés pour rester déterministe."""
        return sorted(self._bars_by_symbol)

    def timeline(self) -> list[str]:
        """Union des timestamps ISO, triée croissante."""
        timestamps = {bar.ts for bars in self._bars_by_symbol.values() for bar in bars}
        return sorted(timestamps)

    def price_asof(self, symbol: str, ts: str) -> float | None:
        """Close de la dernière barre connue à `ts`, ou None si aucune."""
        bars = self._bars_by_symbol.get(symbol)
        if not bars:
            return None

        index = _asof_index(bars, ts)
        if index is None:
            return None
        return bars[index].close

    def price_after(self, symbol: str, ts: str, horizon: timedelta) -> float | None:
        """Close as-of à `ts+horizon`, si une barre nouvelle existe dans l'horizon.

        Retourne None quand aucune barre réellement postérieure à `ts` n'apparaît
        dans `(ts, ts+horizon]`, ce qui rend les gaps/week-ends non évaluables.
        """
        bars = self._bars_by_symbol.get(symbol)
        if not bars:
            return None

        i0 = _asof_index(bars, ts)
        target = (datetime.fromisoformat(ts) + horizon).isoformat()
        ih = _asof_index(bars, target)
        if i0 is None or ih is None or ih <= i0:
            return None
        return bars[ih].close

    def bars_asof(self, symbol: str, ts: str, lookback: int) -> list[Bar]:
        """Barres historiques jusqu'à `ts`, ordre croissant, sans fuite future."""
        if lookback <= 0:
            return []

        bars = self._bars_by_symbol.get(symbol)
        if not bars:
            return []

        index = _asof_index(bars, ts)
        if index is None:
            return []

        start = max(0, index + 1 - lookback)
        return list(bars[start : index + 1])


def _asof_index(bars: tuple[Bar, ...], ts: str) -> int | None:
    """Index de la dernière barre telle que barre.ts <= ts."""
    timestamps = [bar.ts for bar in bars]
    index = bisect_right(timestamps, ts) - 1
    if index < 0:
        return None
    return index


def _cache_key(symbol: str, start: str, end: str, interval: str) -> str:
    """Clé stable et sûre pour le système de fichiers."""
    raw = json.dumps(
        {"symbol": symbol, "start": start, "end": end, "interval": interval},
        ensure_ascii=False,
        sort_keys=True,
    )
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]
    safe_symbol = "".join(ch if ch.isalnum() else "_" for ch in symbol.upper())
    return f"{safe_symbol}_{digest}"


def _read_cache(cache_path: Path) -> list[Bar]:
    """Lit un cache JSON de barres."""
    try:
        raw = json.loads(cache_path.read_text())
        return [_bar_from_dict(item) for item in raw]
    except Exception as e:  # noqa: BLE001 — frontière disque
        raise DataError("fetch_failed", f"cache {cache_path}: {e}") from e


def _fetch_symbol(symbol: str, *, start: str, end: str, interval: str) -> list[Bar]:
    """Récupère un symbole via yfinance."""
    try:
        import yfinance as yf

        df = yf.Ticker(symbol).history(start=start, end=end, interval=interval, auto_adjust=False)
    except Exception as e:  # noqa: BLE001 — frontière externe
        raise DataError("fetch_failed", f"{symbol}: {e}") from e

    if df is None or df.empty:
        return []

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


def _bar_from_dict(data: dict[str, Any]) -> Bar:
    """Convertit une barre JSON en valeur typée."""
    return Bar(
        ts=str(data["ts"]),
        open=float(data["open"]),
        high=float(data["high"]),
        low=float(data["low"]),
        close=float(data["close"]),
        volume=float(data["volume"]),
    )
