"""rotation_wiring — helpers purs pour le câblage du radar de rotation.

Pas d'I/O réseau, pas d'état global. Entrées typées, sorties déterministes.
"""

from __future__ import annotations

from trader.features import compute_indicator_values

# ---------------------------------------------------------------------------
# venue_of
# ---------------------------------------------------------------------------

_EU_SUFFIXES = (".PA", ".DE", ".AS", ".MI")
_EU_SYMBOLS = frozenset({"^FCHI"})


def venue_of(symbol: str) -> str:
    """Heuristique place de cotation basée sur le suffixe yfinance.

    Returns:
        "EU" | "TW" | "FX" | "US"
    """
    if symbol in _EU_SYMBOLS:
        return "EU"
    if any(symbol.endswith(s) for s in _EU_SUFFIXES):
        return "EU"
    if symbol.endswith(".TW"):
        return "TW"
    if symbol.endswith("=X"):
        return "FX"
    return "US"


# ---------------------------------------------------------------------------
# benchmark_ret_for
# ---------------------------------------------------------------------------

def benchmark_ret_for(
    bars_by_symbol: dict[str, list],
    *,
    benchmarks: dict[str, str],
    default_benchmark: str,
    score_window: int,
) -> dict[str, float]:
    """Retourne le rendement benchmark pour chaque symbole du pool.

    Args:
        bars_by_symbol: {symbol: list[Bar]}
        benchmarks: {venue: benchmark_symbol}  e.g. {"US": "SPY", "EU": "^FCHI"}
        default_benchmark: symbole de repli si venue absente de benchmarks
        score_window: fenêtre passée à compute_indicator_values

    Returns:
        {symbol: benchmark_return}  — 0.0 si benchmark absent ou rendement None
    """
    # Cache des rendements benchmark déjà calculés pour éviter les recalculs
    _bench_cache: dict[str, float] = {}

    def _bench_return(bench_symbol: str) -> float:
        if bench_symbol in _bench_cache:
            return _bench_cache[bench_symbol]
        bars = bars_by_symbol.get(bench_symbol)
        if not bars:
            _bench_cache[bench_symbol] = 0.0
            return 0.0
        raw = compute_indicator_values(bars, names=["return"], window=score_window)["return"]
        value = raw if raw is not None else 0.0
        _bench_cache[bench_symbol] = value
        return value

    result: dict[str, float] = {}
    for symbol in bars_by_symbol:
        venue = venue_of(symbol)
        bench = benchmarks.get(venue, default_benchmark)
        result[symbol] = _bench_return(bench)
    return result


# ---------------------------------------------------------------------------
# resolve_as_of
# ---------------------------------------------------------------------------

def resolve_as_of(bars_by_symbol: dict[str, list]) -> str:
    """Retourne le ts de la dernière barre disponible parmi tous les symboles.

    Prend le max de `bars[-1].ts` pour chaque symbole ayant des barres.
    Les ts ISO 8601 (YYYY-MM-DD ou datetime ISO) se comparent correctement en
    ordre lexicographique.

    Returns:
        str ts ou "" si aucun symbole n'a de barres.
    """
    best: str = ""
    for bars in bars_by_symbol.values():
        if not bars:
            continue
        ts = str(bars[-1].ts)
        if not best or ts > best:
            best = ts
    return best
