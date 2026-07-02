"""rotation_wiring — helpers purs pour le câblage du radar de rotation.

Pas d'I/O réseau, pas d'état global. Entrées typées, sorties déterministes.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

from trader.features import compute_indicator_values

# ---------------------------------------------------------------------------
# venue_of
# ---------------------------------------------------------------------------

_EU_SUFFIXES = (
    ".PA",
    ".DE",
    ".AS",
    ".MI",
    ".SW",
    ".L",
    ".CO",
    ".ST",
    ".MC",
    ".BR",
    ".LS",
    ".HE",
    ".VI",
    ".OL",
)
_EU_SYMBOLS = frozenset({"^FCHI"})
_TW_SUFFIXES = (".TW", ".TWO")


def venue_of(symbol: str) -> str:
    """Heuristique place de cotation basée sur le suffixe yfinance.

    Returns:
        "EU" | "TW" | "FX" | "US"
    """
    if symbol in _EU_SYMBOLS:
        return "EU"
    if any(symbol.endswith(s) for s in _EU_SUFFIXES):
        return "EU"
    if any(symbol.endswith(s) for s in _TW_SUFFIXES):
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


# ---------------------------------------------------------------------------
# compute_gap_adverse
# ---------------------------------------------------------------------------

def compute_gap_adverse(
    bars_by_symbol: dict[str, list],
    ranked: list[dict],
    *,
    gap_threshold: float,
) -> frozenset[str]:
    """Symboles dont le gap d'ouverture est ADVERSE au biais déclaré.

    Pour chaque item de `ranked` ayant ≥ 2 barres :
        gap = (last.open - prev.close) / prev.close
    Adverse si :
        bias == "long"  ET gap <= -gap_threshold  (baisse contre biais haussier)
        bias == "short" ET gap >= +gap_threshold  (hausse contre biais baissier)

    Symbole absent de bars_by_symbol ou avec < 2 barres → ignoré (pas adverse).
    prev.close == 0 → ignoré (division par zéro).
    """
    adverse: set[str] = set()
    for item in ranked:
        symbol = item["symbol"]
        bias = item["bias"]
        bars = bars_by_symbol.get(symbol)
        if not bars or len(bars) < 2:
            continue
        prev = bars[-2]
        last = bars[-1]
        if prev.close == 0:
            continue
        gap = (last.open - prev.close) / prev.close
        if bias == "long" and gap <= -gap_threshold:
            adverse.add(symbol)
        elif bias == "short" and gap >= gap_threshold:
            adverse.add(symbol)
    return frozenset(adverse)


# ---------------------------------------------------------------------------
# build_rank_fn
# ---------------------------------------------------------------------------

def build_rank_fn(
    config_dir: str | Path,
    *,
    fetch_fn: Callable[[list[str]], dict[str, list]],
    as_of: str,
) -> Callable[[], dict]:
    """Construit une rank_fn clôture qui fetche, scanne et rankifie le pool.

    Args:
        config_dir: chemin vers le répertoire de config (pool.yaml, radar.yaml, conviction.yaml).
        fetch_fn: callable(symbols) -> {symbol: list[Bar]} — injectée pour le réseau.
        as_of: date de référence ISO 8601.

    Returns:
        rank_fn() -> {"ranked", "ineligible", "components_by_symbol"}
    """
    from trader.pool_config import load_pool
    from trader.radar_config import load_radar_params, load_conviction
    from trader.radar import scan_and_rank, daily_components
    from trader.radar_data import fetch_daily
    from trader.semantic.catalog import FAMILIES, family_for_symbol

    config_dir = Path(config_dir)
    pool = load_pool(config_dir)
    params = load_radar_params(config_dir)
    conviction = load_conviction(config_dir, known_families=set(FAMILIES))

    bench_syms = sorted(set(params.benchmarks.values()) | {params.default_benchmark})
    all_syms = list(dict.fromkeys(list(pool.symbols) + bench_syms))

    def rank_fn() -> dict:
        bars = fetch_daily(all_syms, fetch_fn=fetch_fn, min_coverage=params.min_coverage)
        # Nettoie les barres incomplètes (close NaN ou <= 0, ex barre du jour non
        # clôturée côté yahoo) AVANT tout calcul — sinon le benchmark hérite du NaN
        # et propage un score NaN à tout son marché (cf US/SPY). NaN != NaN.
        bars = {
            sym: clean
            for sym, bb in bars.items()
            if (clean := [b for b in bb if b.close == b.close and b.close > 0])
        }
        bench_ret = benchmark_ret_for(
            bars,
            benchmarks=params.benchmarks,
            default_benchmark=params.default_benchmark,
            score_window=params.score_window_bars,
        )
        components = {s: daily_components(s, b, params) for s, b in bars.items()}
        scan = scan_and_rank(
            bars,
            indicators_fn=lambda s, b: components[s],
            benchmark_ret_for=bench_ret,
            tilt_for=lambda s: conviction.get(family_for_symbol(s), 0.0),
            hard_exclusions=set(pool.hard_exclusions),
            atr_floor=params.atr_floor,
            amplitude_cap=params.amplitude_cap,
            w_trend=params.w_trend,
            w_rs=params.w_rs,
            w_amp=params.w_amp,
        )
        return {
            "ranked": scan["ranked"],
            "ineligible": scan["ineligible"],
            "components_by_symbol": components,
            "gap_adverse": compute_gap_adverse(
                bars, scan["ranked"], gap_threshold=params.gap_threshold
            ),
        }

    return rank_fn


# ---------------------------------------------------------------------------
# build_llm_override_fn
# ---------------------------------------------------------------------------

def build_llm_override_fn(
    *,
    acpx_bin: str = "acpx",
    spark_model: str | None = None,
    timeout_s: int = 120,
) -> Callable:
    """Construit une override_fn câblée sur le router LLM réel.

    Args:
        acpx_bin: chemin vers le binaire acpx.
        spark_model: modèle Spark (None → défaut du router).
        timeout_s: timeout transmis au LLM.

    Returns:
        override_fn(payload) -> {"add": [...], "remove": [...]}
    """
    from trader import llm
    from trader.rotation_override import make_llm_override_fn

    kw: dict = {"acpx_bin": acpx_bin}
    if spark_model is not None:
        kw["spark_model"] = spark_model

    router = llm.build_default_router_from_env(**kw)

    def _complete(prompt: str, *, timeout_s: int) -> str:
        result = router.complete(prompt, timeout_s=timeout_s)
        return getattr(result, "text", "")  # LlmFailure n'a pas .text -> ""

    return make_llm_override_fn(_complete, timeout_s=timeout_s)


# ---------------------------------------------------------------------------
# build_market_context_from_regime
# ---------------------------------------------------------------------------


def build_market_context_from_regime(
    family_bias: dict[str, dict] | None,
) -> dict[str, Any] | None:
    """Assemble le market_context v1 depuis family_regime.compute_family_bias.

    Args:
        family_bias: dict famille → {dir, frac, up, down, n} ou None.

    Returns:
        {"regime_families": family_bias} si family_bias non-vide, sinon None.
    """
    if not family_bias:
        return None
    return {"regime_families": dict(family_bias)}


# ---------------------------------------------------------------------------
# run_cli
# ---------------------------------------------------------------------------

def run_cli(
    config_dir: str | Path,
    state_dir: str | Path,
    *,
    fetch_fn: Callable[[list[str]], dict[str, list]] | None = None,
    override_fn: Callable[[Any], dict] | None = None,
    sticky_fn: Callable[[], set[str]] | None = None,
    as_of: str | None = None,
) -> dict:
    """Point d'entrée prod de la rotation : charge config, injecte les dépendances, exécute run().

    Args:
        config_dir: répertoire de config.
        state_dir: répertoire d'état.
        fetch_fn: callable réseau injecté (None → download_daily_batch).
        override_fn: callable override agent (None → default_override_fn).
        sticky_fn: callable sticky (None → sticky_collector depuis state_dir).
        as_of: date ISO 8601 (None → date.today()).

    Returns:
        dict run() : {"final_hot_set", "default_hot_set", "alerts", "written"}.
    """
    from trader.pool_config import load_pool
    from trader.radar_config import load_radar_params
    from trader.radar_data import download_daily_batch
    from trader.rotation import run
    from trader.rotation_collectors import (
        sticky_collector,
        build_positions_fn,
        build_plans_fn,
        default_override_fn,
    )

    config_dir = str(config_dir)
    state_dir = str(state_dir)

    if as_of is None:
        as_of = date.today().isoformat()

    if fetch_fn is None:
        cache_dir = Path(state_dir) / "radar_cache"
        def fetch_fn(syms):
            return download_daily_batch(syms, as_of=as_of, cache_dir=cache_dir)

    params = load_radar_params(Path(config_dir))
    pool = load_pool(Path(config_dir))
    rank_fn = build_rank_fn(config_dir, fetch_fn=fetch_fn, as_of=as_of)

    if sticky_fn is None:
        def sticky_fn():
            return sticky_collector(
                positions_fn=build_positions_fn(state_dir),
                plans_fn=build_plans_fn(state_dir),
            )

    if override_fn is None:
        if params.override_enabled:
            override_fn = build_llm_override_fn()
        else:
            override_fn = default_override_fn

    return run(
        config_dir=config_dir,
        state_dir=state_dir,
        as_of=as_of,
        rank_fn=rank_fn,
        sticky_fn=sticky_fn,
        override_fn=override_fn,
        pool=set(pool.symbols),
        cap_m=params.cap_m,
        delta=params.delta,
        dwell_days=params.dwell_days,
        emergency_floor=params.emergency_score,
    )
