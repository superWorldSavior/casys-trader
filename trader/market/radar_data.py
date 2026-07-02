"""Couche data daily du radar : fetch injecte et controle de couverture."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from pathlib import Path

from trader.domain.market_data import Bar


class CoverageError(RuntimeError):
    pass


def fetch_daily(
    symbols: list[str],
    *,
    fetch_fn: Callable[[list[str]], dict[str, list]],
    min_coverage: float = 0.8,
) -> dict[str, list]:
    """Retourne les barres des symboles couverts, ou leve sous le seuil."""
    raw = fetch_fn(symbols)
    covered = {symbol: bars for symbol, bars in raw.items() if bars}
    if symbols and len(covered) / len(symbols) < min_coverage:
        raise CoverageError(f"coverage {len(covered)}/{len(symbols)} < {min_coverage}")
    return covered


def download_daily_batch(
    symbols: list[str],
    *,
    as_of: str,
    cache_dir: Path,
    download_fn: Callable[[list[str]], object] | None = None,
    chunk_size: int = 100,
    max_retries: int = 3,
) -> dict[str, list[Bar]]:
    """Telecharge les barres daily par chunks, puis cache le resultat par as_of."""
    cache_dir = Path(cache_dir)
    cache_path = cache_dir / f"{as_of}.json"
    if cache_path.exists():
        return _decode_cache(json.loads(cache_path.read_text(encoding="utf-8")))

    if chunk_size <= 0:
        raise ValueError("chunk_size_must_be_positive")
    if max_retries <= 0:
        raise ValueError("max_retries_must_be_positive")

    downloader = download_fn or _download_yfinance
    bars_by_symbol: dict[str, list[Bar]] = {}
    for start in range(0, len(symbols), chunk_size):
        chunk = symbols[start : start + chunk_size]
        raw = _download_chunk_with_retry(
            chunk,
            download_fn=downloader,
            max_retries=max_retries,
        )
        for symbol, rows in _normalise_download_output(raw, chunk).items():
            bars = [_row_to_bar(row) for row in rows]
            if bars:
                bars_by_symbol[symbol] = bars

    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(
        json.dumps(_encode_cache(bars_by_symbol), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return bars_by_symbol


def _download_chunk_with_retry(
    symbols: list[str],
    *,
    download_fn: Callable[[list[str]], object],
    max_retries: int,
) -> object:
    last_exc: Exception | None = None
    for attempt in range(max_retries):
        try:
            return download_fn(symbols)
        except Exception as exc:  # noqa: BLE001 - frontiere externe injectee
            last_exc = exc
            if attempt + 1 == max_retries:
                break
            time.sleep(min(0.25, 0.05 * (2**attempt)))
    assert last_exc is not None
    raise last_exc


def _download_yfinance(symbols: list[str]) -> object:
    import yfinance as yf

    return yf.download(
        symbols,
        interval="1d",
        auto_adjust=True,
        group_by="ticker",
        threads=True,
    )


def _normalise_download_output(raw: object, symbols: list[str]) -> dict[str, object]:
    if isinstance(raw, dict):
        return raw
    if len(symbols) == 1:
        return {symbols[0]: _rows_from_dataframe(raw)}

    out: dict[str, object] = {}
    for symbol in symbols:
        try:
            frame = raw[symbol]  # type: ignore[index]
        except Exception:
            continue
        out[symbol] = _rows_from_dataframe(frame)
    return out


def _rows_from_dataframe(frame: object) -> list[dict]:
    empty = getattr(frame, "empty", False)
    if empty:
        return []
    iterrows = getattr(frame, "iterrows", None)
    if not callable(iterrows):
        return list(frame or [])  # type: ignore[arg-type]
    rows: list[dict] = []
    for idx, row in iterrows():
        rows.append(
            {
                "ts": idx.isoformat(),
                "open": row["Open"],
                "high": row["High"],
                "low": row["Low"],
                "close": row["Close"],
                "volume": row["Volume"],
            }
        )
    return rows


def _row_to_bar(row: object) -> Bar:
    if isinstance(row, Bar):
        return row
    return Bar(
        ts=str(_row_get(row, "ts", "Date")),
        open=float(_row_get(row, "open", "Open")),
        high=float(_row_get(row, "high", "High")),
        low=float(_row_get(row, "low", "Low")),
        close=float(_row_get(row, "close", "Close")),
        volume=float(_row_get(row, "volume", "Volume")),
    )


def _row_get(row: object, *names: str) -> object:
    for name in names:
        if isinstance(row, dict) and name in row:
            return row[name]
        if hasattr(row, name):
            return getattr(row, name)
        try:
            return row[name]  # type: ignore[index]
        except Exception:
            pass
    raise KeyError(names[0])


def _encode_cache(bars_by_symbol: dict[str, list[Bar]]) -> dict[str, list[dict]]:
    return {
        symbol: [
            {
                "ts": bar.ts,
                "open": bar.open,
                "high": bar.high,
                "low": bar.low,
                "close": bar.close,
                "volume": bar.volume,
            }
            for bar in bars
        ]
        for symbol, bars in bars_by_symbol.items()
    }


def _decode_cache(payload: dict[str, list[dict]]) -> dict[str, list[Bar]]:
    return {
        symbol: [_row_to_bar(row) for row in rows]
        for symbol, rows in payload.items()
    }
