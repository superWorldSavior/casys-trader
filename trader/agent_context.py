"""Compact context and bounded indicator research for the runtime agent."""

from __future__ import annotations

from typing import Iterable

from .features import DEFAULT_INDICATORS, build_indicator_snapshot
from .regime import classify_regime, multi_horizon_signals
from .semantic.catalog import family_for_symbol, normalize_temporal_query
from .tools import market

COCKPIT_INDICATORS = [
    "return",
    "volatility",
    "z_score",
    "efficiency_ratio",
    "autocorrelation",
    "relative_strength",
    "spread_zscore",
]

# Indicateurs supplémentaires nécessaires au classifieur de régime,
# calculés dans le snapshot mais non affichés comme colonnes numériques.
_REGIME_EXTRA_INDICATORS = [
    "trend_slope",
    "chart_breakout",
    "candlestick_signal",
]

_INDICATOR_COLUMNS = {
    "return": "r",
    "volatility": "vol",
    "ohlc_volatility": "ohv",
    "z_score": "z",
    "efficiency_ratio": "er",
    "autocorrelation": "ac",
    "relative_strength": "rs",
    "spread_zscore": "sz",
}

_FAMILY_CODES = {
    "indices": "idx",
    "countries": "cty",
    "europe_indices": "eu",
    "defense": "def",
    "energy": "en",
    "commodities_futures": "fut",
    "metals": "met",
    "nasdaq_single_names": "ndq",
    "crypto": "cry",
    "forex_majors": "fx",
}

_CROSS_ASSET_INDICATORS = {"relative_strength", "spread_zscore"}


def _compact_price(value: float | None) -> float | None:
    return None if value is None else round(float(value), 6)


def _family_code(value: str | None) -> str | None:
    return _FAMILY_CODES.get(value or "", value)


def build_market_cockpit(
    bars_by_symbol: dict[str, list[object]],
    *,
    symbols: list[str],
    prices: dict[str, float],
    window: int = 48,
    top_n: int = 5,
    daily_bars_by_symbol: dict[str, list[object]] | None = None,
) -> dict:
    """Build a compact, deterministic market dashboard with no raw bars.

    Colonnes :
        s, f, p : symbole, famille, prix
        r, vol, z, er, ac, rs, sz : indicateurs numériques (COCKPIT_INDICATORS)
        reg, vs, st, cndle : régime de marché (classify_regime)
        htf, aligned, sig : signaux multi-horizon pré-calculés (cp3)
    """
    # On calcule les indicateurs affichables + ceux nécessaires au classifieur.
    all_snapshot_names = COCKPIT_INDICATORS + _REGIME_EXTRA_INDICATORS
    snapshot = build_indicator_snapshot(
        bars_by_symbol,
        symbols=symbols,
        names=all_snapshot_names,
        window=window,
    )
    indicator_cols = [_INDICATOR_COLUMNS[name] for name in COCKPIT_INDICATORS]
    cols = ["s", "f", "p", *indicator_cols, "reg", "vs", "st", "cndle", "htf", "aligned", "sig"]
    rows: list[list] = []
    daily_bars_by_symbol = daily_bars_by_symbol or {}
    for symbol in symbols:
        item = snapshot.get(symbol, {"family": None, "indicators": {}})
        indicators = item["indicators"]
        regime = classify_regime(indicators)
        base_bars = bars_by_symbol.get(symbol, [])
        hourly_bars = market.aggregate_bars(base_bars, target_interval="1h") if base_bars else []
        four_hour_bars = market.aggregate_bars(hourly_bars, target_interval="4h") if hourly_bars else []
        bars_by_horizon = {
            "15m": base_bars,
            "1h": hourly_bars,
            "4h": four_hour_bars,
        }
        daily_bars = daily_bars_by_symbol.get(symbol)
        if daily_bars:
            bars_by_horizon["1d"] = daily_bars
        htf_signals = multi_horizon_signals(
            bars_by_horizon,
            window=window,
            precomputed_indicators_by_horizon={"15m": indicators},
        )
        rows.append(
            [
                symbol,
                _family_code(item["family"]),
                _compact_price(prices.get(symbol)),
                *[indicators.get(name) for name in COCKPIT_INDICATORS],
                regime.regime,
                regime.vol_state,
                regime.stretched,
                regime.candle,
                htf_signals["htf"],
                htf_signals["aligned"],
                htf_signals.get("sig"),
            ]
        )

    def rank_by_abs(indicator: str) -> list[dict]:
        index = COCKPIT_INDICATORS.index(indicator) + 3
        candidates = [
            [row[0], row[index]]
            for row in rows
            if row[index] is not None
        ]
        return sorted(candidates, key=lambda item: abs(float(item[1])), reverse=True)[:top_n]

    return {
        "v": "cp3",
        "window": window,
        "schema": (
            "cols: s=sym,f=family,p=price,"
            "r=ret,vol=stdev_ret,z=price_z,er=Kaufman,ac=lag1_ret_corr,rs=ret-fam_ret,sz=spread_z,"
            "reg=regime,vs=vol_state,st=stretched,cndle=candle_pattern,"
            "htf=highest_timeframe_regime,aligned=base_htf_trend_aligned,sig=notable_events"
        ),
        "cols": cols,
        "rows": rows,
        "highlights": {
            "abs_r": rank_by_abs("return"),
            "abs_z": rank_by_abs("z_score"),
            "abs_sz": rank_by_abs("spread_zscore"),
        },
    }


def resolve_indicator_requests(
    raw_requests: Iterable[object],
    bars_by_symbol: dict[str, list[object]],
    *,
    symbols: list[str],
    max_requests: int,
    max_indicators: int,
    default_window: int = 48,
    market_get_bars=None,
    cached_interval: str = "1h",
    cached_lookback: str = "5d",
) -> dict:
    """Resolve LLM-requested indicator lookups with hard bounds."""
    known = set(DEFAULT_INDICATORS)
    all_requests = list(raw_requests)
    limited_requests = all_requests[:max_requests]
    resolved: list[dict] = []
    get_bars = market_get_bars or market.get_bars

    for request in limited_requests:
        symbol = str(getattr(request, "symbol", ""))
        if symbol not in symbols:
            continue
        requested_names = [
            str(name)
            for name in getattr(request, "indicators", [])
            if str(name) in known
        ][:max_indicators]
        if not requested_names:
            requested_names = DEFAULT_INDICATORS[:max_indicators]
        temporal = normalize_temporal_query(
            timeframe=str(getattr(request, "timeframe", "1h") or "1h"),
            lookback=getattr(request, "lookback", None),
            window=int(getattr(request, "window", default_window) or default_window),
            as_of=str(getattr(request, "as_of", "latest") or "latest"),
        )
        symbol_bars = bars_by_symbol.get(symbol)
        if (
            symbol_bars is None
            or temporal["timeframe"] != cached_interval
            or temporal["lookback"] != cached_lookback
        ):
            try:
                symbol_bars = get_bars(
                    symbol,
                    lookback=temporal["lookback"],
                    interval=temporal["timeframe"],
                )
            except market.MarketError:
                continue
        local_bars_by_symbol = {**bars_by_symbol, symbol: symbol_bars}
        if any(name in _CROSS_ASSET_INDICATORS for name in requested_names):
            family = family_for_symbol(symbol)
            family_symbols = [
                candidate
                for candidate in symbols
                if candidate != symbol and family_for_symbol(candidate) == family
            ]
            for peer in family_symbols:
                peer_bars = bars_by_symbol.get(peer)
                if (
                    peer_bars is None
                    or temporal["timeframe"] != cached_interval
                    or temporal["lookback"] != cached_lookback
                ):
                    try:
                        peer_bars = get_bars(
                            peer,
                            lookback=temporal["lookback"],
                            interval=temporal["timeframe"],
                        )
                    except market.MarketError:
                        continue
                local_bars_by_symbol[peer] = peer_bars
        snapshot = build_indicator_snapshot(
            local_bars_by_symbol,
            symbols=symbols,
            names=requested_names,
            window=temporal["window"],
        )
        item = snapshot[symbol]
        resolved.append(
            {
                "symbol": symbol,
                "family": item["family"],
                "timeframe": temporal["timeframe"],
                "source_interval": temporal["source_interval"],
                "lookback": temporal["lookback"],
                "window": temporal["window"],
                "as_of": temporal["as_of"],
                "indicators": item["indicators"],
            }
        )

    return {
        "mode": "bounded_indicator_research",
        "max_requests": max_requests,
        "max_indicators_per_request": max_indicators,
        "truncated": len(all_requests) > max_requests,
        "requests": resolved,
    }
