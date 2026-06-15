"""Couche data daily du radar : fetch injecte et controle de couverture."""

from __future__ import annotations

from collections.abc import Callable


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
