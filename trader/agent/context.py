"""Compact context and bounded indicator research for the runtime agent."""

from __future__ import annotations

import math
from typing import Callable, Iterable

from trader.market import fx as _fx
from trader.market import market_data as market
from trader.market.features import (
    DEFAULT_INDICATORS,
    build_indicator_snapshot,
    compute_indicator_values,
    swing_high,
    swing_low,
)
from trader.market.regime import classify_regime, multi_horizon_signals
from trader.domain.semantic.catalog import (
    INDICATOR_ALIASES,
    INDICATOR_COLUMNS,
    family_for_symbol,
    normalize_temporal_query,
)
COCKPIT_INDICATORS = [
    "return",
    "volatility",
    "z_score",
    "efficiency_ratio",
    "autocorrelation",
    "relative_strength",
    "spread_zscore",
]
COCKPIT_DAILY_WINDOW = 15

# Indicateurs supplémentaires nécessaires au classifieur de régime,
# calculés dans le snapshot mais non affichés comme colonnes numériques.
_REGIME_EXTRA_INDICATORS = [
    "trend_slope",
    "chart_breakout",
    "candlestick_signal",
]

_DAILY_INDICATOR_COLUMNS = {
    name: f"{column}_d"
    for name, column in INDICATOR_COLUMNS.items()
    if name in COCKPIT_INDICATORS
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


# Fenêtres de swing exposées au LLM. Elles correspondent aux `window` que
# l'agent emploie réellement pour ses hard_stop structurels (audit decisions :
# 24 et 48). Donner la distance résolue ICI évite que l'agent borne min/max_pct
# à l'aveugle et génère des warnings hard_stop_*_pct inutiles.
_SWING_WINDOWS = (24, 48)
_STRUCTURE_ATR_WINDOW = 14
_STRUCTURE_VOLUME_WINDOW = 20


def _compact_price(value: float | None) -> float | None:
    return None if value is None else round(float(value), 6)


def _bar_value(bar: object, name: str) -> object | None:
    if isinstance(bar, dict):
        return bar.get(name)
    return getattr(bar, name, None)


def build_symbol_structure(
    bars: Iterable[object],
    *,
    price: float | None = None,
    timeframe: str | None = None,
) -> dict:
    """Compact, exact structure facts for one symbol.

    This is pushed only for symbols being decided and reused by indicator-tool
    results. It gives the agent exact anchors for stop/R/Fibonacci arithmetic
    without inflating every row of the global cockpit.
    """
    selected = list(bars)
    if not selected:
        return {}
    last = selected[-1]
    last_close = _bar_value(last, "close")
    try:
        resolved_price = float(price if price is not None else last_close)
    except (TypeError, ValueError):
        resolved_price = None
    if resolved_price is not None and (not math.isfinite(resolved_price) or resolved_price <= 0):
        resolved_price = None
    atr = compute_indicator_values(
        selected,
        names=["atr_pct"],
        window=_STRUCTURE_ATR_WINDOW,
    )["atr_pct"]
    relative_volume = compute_indicator_values(
        selected,
        names=["relative_volume"],
        window=_STRUCTURE_VOLUME_WINDOW,
    )["relative_volume"]
    structure = {
        "timeframe": timeframe,
        "bar_as_of": None if _bar_value(last, "ts") is None else str(_bar_value(last, "ts")),
        "bars_available": len(selected),
        "price": _compact_price(resolved_price),
        "atr_pct_14": atr,
        "relative_volume_20": relative_volume,
    }
    for window in _SWING_WINDOWS:
        structure[f"swing_low_{window}"] = _compact_price(swing_low(selected, window))
        structure[f"swing_high_{window}"] = _compact_price(swing_high(selected, window))
    return structure


def _swing_distances_pct(
    bars: list[object], price: float | None, window: int
) -> tuple[float | None, float | None]:
    """Distance signée (fraction du prix) du prix aux swing_low / swing_high.

    Retourne ``(low_dist, high_dist)`` avec, sur la même fenêtre `window` et les
    mêmes barres que `resolve_exit_plan` côté daemon :
        low_dist  = (price - swing_low) / price   → >0 si swing_low SOUS le prix
        high_dist = (swing_high - price) / price  → >0 si swing_high AU-DESSUS

    L'unité (fraction) est identique à `min_pct`/`max_pct` du hard_stop, donc
    l'agent compare directement. ``None`` si prix invalide ou barres absentes.
    """
    if not bars or price is None:
        return None, None
    try:
        p = float(price)
    except (TypeError, ValueError):
        return None, None
    if not math.isfinite(p) or p <= 0:
        return None, None
    low = swing_low(bars, window)
    high = swing_high(bars, window)
    low_dist = round((p - low) / p, 5) if low is not None else None
    high_dist = round((high - p) / p, 5) if high is not None else None
    return low_dist, high_dist


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
    fee_estimator: Callable[[str, float], dict | None] | None = None,
    fee_ref_notional: float | None = None,
    fx_rate_by_ccy: dict[str, float] | None = None,
    equity_usd: float = 0.0,
    risk_pct: float = 0.0,
    max_order_value: float = 0.0,
) -> dict:
    """Build a compact, deterministic market dashboard with no raw bars.

    Colonnes :
        s, f, p : symbole, famille, prix
        r, vol, z, er, ac, rs, sz : indicateurs numériques (COCKPIT_INDICATORS)
        reg, vs, st, cndle : régime de marché (classify_regime)
        htf, aligned, sig : signaux multi-horizon pré-calculés (cp3)
        be, fee : break-even et coût aller-retour modélisés — seulement si
                  `fee_estimator` est fourni (sinon colonnes absentes)

    `fee_estimator(symbol, price)` doit aussi déclarer `cost_scope` et
    `cost_estimate_is_all_in`. Les champs historiques de ligne restent stables,
    mais le cockpit expose la complétude au niveau racine pour que l'agent ne
    confonde jamais une commission broker avec un coût de transaction all-in.
    """
    # On calcule les indicateurs affichables + ceux nécessaires au classifieur.
    daily_bars_by_symbol = daily_bars_by_symbol or {}
    all_snapshot_names = COCKPIT_INDICATORS + _REGIME_EXTRA_INDICATORS
    snapshot = build_indicator_snapshot(
        bars_by_symbol,
        symbols=symbols,
        names=all_snapshot_names,
        window=window,
    )
    daily_snapshot = (
        build_indicator_snapshot(
            daily_bars_by_symbol,
            symbols=symbols,
            names=COCKPIT_INDICATORS,
            window=COCKPIT_DAILY_WINDOW,
        )
        if daily_bars_by_symbol
        else {}
    )
    indicator_cols = [INDICATOR_COLUMNS[name] for name in COCKPIT_INDICATORS]
    daily_indicator_cols = [_DAILY_INDICATOR_COLUMNS[name] for name in COCKPIT_INDICATORS]
    swing_cols = [name for w in _SWING_WINDOWS for name in (f"sl{w}", f"sh{w}")]
    cols = [
        "s",
        "f",
        "p",
        *indicator_cols,
        *daily_indicator_cols,
        "reg",
        "vs",
        "st",
        "cndle",
        "htf",
        "aligned",
        "sig",
        *swing_cols,
    ]
    if fee_estimator is not None:
        cols = [*cols, "be_ref_bps", "fee", "fee_ccy"]
    cols = [*cols, "ccy", "fx_usd", "risk_budget_native", "max_order_native"]
    rows: list[list] = []
    fee_scopes: set[str] = set()
    fee_all_in_flags: list[bool] = []
    for symbol in symbols:
        item = snapshot.get(symbol, {"family": None, "indicators": {}})
        indicators = item["indicators"]
        daily_item = daily_snapshot.get(symbol, {"indicators": {}})
        daily_indicators = daily_item["indicators"]
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
        row = [
            symbol,
            _family_code(item["family"]),
            _compact_price(prices.get(symbol)),
            *[indicators.get(name) for name in COCKPIT_INDICATORS],
            *[daily_indicators.get(name) for name in COCKPIT_INDICATORS],
            regime.regime,
            regime.vol_state,
            regime.stretched,
            regime.candle,
            htf_signals["htf"],
            htf_signals["aligned"],
            htf_signals.get("sig"),
        ]
        for swing_window in _SWING_WINDOWS:
            low_dist, high_dist = _swing_distances_pct(
                base_bars, prices.get(symbol), swing_window
            )
            row += [low_dist, high_dist]
        if fee_estimator is not None:
            cost = fee_estimator(symbol, prices.get(symbol))
            if cost is None:
                row += [None, None, None]
            else:
                # Valeurs numériques (parsing agent) + devise séparée. be_ref_bps =
                # break-even au notionnel de référence, PAS au sizing réel de l'ordre.
                row += [cost["be_bps"], cost["fee_rt"], cost["currency"]]
                fee_scopes.add(
                    str(cost.get("cost_scope") or "commission_model_only")
                )
                fee_all_in_flags.append(
                    cost.get("cost_estimate_is_all_in", False) is True
                )
        # Estampillage devise : ccy, taux FX informatif, budgets natifs pré-calculés.
        # Les valeurs d'analyse (p, indicateurs, swings) restent en devise native —
        # seuls risk_budget_native et max_order_native sont des montants convertis.
        ccy = _fx.currency_for(symbol)
        if ccy == _fx.BASE_CCY:
            rate: float | None = (fx_rate_by_ccy or {}).get(ccy, 1.0)
        else:
            rate = (fx_rate_by_ccy or {}).get(ccy)
        row += [
            ccy,
            rate,
            (risk_pct * equity_usd / rate) if rate else 0.0,
            (max_order_value / rate) if rate else 0.0,
        ]
        rows.append(row)

    def rank_by_abs(indicator: str) -> list[dict]:
        index = COCKPIT_INDICATORS.index(indicator) + 3
        candidates = [
            [row[0], row[index]]
            for row in rows
            if row[index] is not None
        ]
        return sorted(candidates, key=lambda item: abs(float(item[1])), reverse=True)[:top_n]

    schema = (
        "cols: s=sym,f=family,p=price,"
        "r=ret_short,vol=stdev_ret_short,z=price_z_short,er=Kaufman_short,"
        "ac=lag1_ret_corr_short,rs=force relative courte,sz=spread_z_short,"
        "r_d=ret_daily,vol_d=stdev_ret_daily,z_d=price_z_daily,er_d=Kaufman_daily,"
        "ac_d=lag1_ret_corr_daily,rs_d=force relative daily,sz_d=spread_z_daily,"
        "reg=regime,vs=vol_state,st=stretched,cndle=candle_pattern,"
        "htf=highest_timeframe_regime,aligned=base_htf_trend_aligned,sig=notable_events,"
        "sl24/sl48=dist_signee_au_swing_low_(frac_du_prix)_fenetres_24/48,"
        "sh24/sh48=dist_signee_au_swing_high_(frac_du_prix)_fenetres_24/48"
    )
    result = {
        "v": "cp3",
        "window": window,
        "daily_window": COCKPIT_DAILY_WINDOW,
        "schema": schema,
        "cols": cols,
        "rows": rows,
        "highlights": {
            "abs_r": rank_by_abs("return"),
            "abs_z": rank_by_abs("z_score"),
            "abs_sz": rank_by_abs("spread_zscore"),
        },
    }
    if fee_estimator is not None:
        # be_ref_bps=break-even aller-retour modélisé en bps POUR UN ORDRE DE
        # fee_ref_notional. La complétude est séparée des valeurs numériques afin
        # de préserver le contrat des lignes existantes.
        result["schema"] = (
            schema
            + ",be_ref_bps=modeled_break_even_roundtrip_bps_at_fee_ref_notional,"
            "fee=modeled_roundtrip_cost,fee_ccy=currency"
        )
        result["fee_ref_notional"] = fee_ref_notional
        result["fee_scope"] = (
            next(iter(fee_scopes))
            if len(fee_scopes) == 1
            else ("mixed" if fee_scopes else "unknown")
        )
        result["fee_estimate_is_all_in"] = bool(fee_all_in_flags) and all(
            fee_all_in_flags
        )
    return result


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
    """Resolve indicator lookups with request-count bounds.

    The governed indicator catalog is finite. By default callers pass its full
    size as ``max_indicators`` so every valid requested indicator is returned.
    An explicit lower operator cap remains supported and is reported honestly
    instead of silently dropping indicators.
    """
    known = set(DEFAULT_INDICATORS)
    all_requests = list(raw_requests)
    limited_requests = all_requests[:max_requests]
    resolved: list[dict] = []
    indicators_truncated = False
    get_bars = market_get_bars or market.get_bars

    for request in limited_requests:
        symbol = str(getattr(request, "symbol", ""))
        if symbol not in symbols:
            continue
        requested_names = []
        seen_names: set[str] = set()
        for name in getattr(request, "indicators", []):
            canonical_name = INDICATOR_ALIASES.get(str(name), str(name))
            if canonical_name in known and canonical_name not in seen_names:
                requested_names.append(canonical_name)
                seen_names.add(canonical_name)
        requested_indicator_count = len(requested_names)
        request_indicators_truncated = requested_indicator_count > max_indicators
        indicators_truncated = indicators_truncated or request_indicators_truncated
        requested_names = requested_names[:max_indicators]
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
        structure = build_symbol_structure(
            symbol_bars,
            timeframe=temporal["timeframe"],
        )
        resolved.append(
            {
                "symbol": symbol,
                "family": item["family"],
                "timeframe": temporal["timeframe"],
                "source_interval": temporal["source_interval"],
                "lookback": temporal["lookback"],
                "window": temporal["window"],
                "as_of": temporal["as_of"],
                "requested_indicator_count": requested_indicator_count,
                "returned_indicator_count": len(requested_names),
                "indicators_truncated": request_indicators_truncated,
                "indicators": item["indicators"],
                "structure": structure,
            }
        )

    return {
        "mode": "bounded_indicator_research",
        "max_requests": max_requests,
        "max_indicators_per_request": max_indicators,
        "truncated": len(all_requests) > max_requests or indicators_truncated,
        "requests": resolved,
    }


def build_indicator_resolver(
    *,
    symbols: list[str],
    get_bars: Callable[..., list[object]],
    max_requests: int,
    max_indicators: int,
    default_window: int = 48,
) -> Callable[[Iterable[object]], dict]:
    """Fabrique un resolver d'indicateurs *fetch-first* pour le worker de file.

    Contrat étroit (AX #7 Explicit, #9 Narrow) : UN seul comportement — chaque
    requête est résolue en fetchant les barres via ``get_bars`` (aucun cache de
    cycle, à la différence du mode batch qui réutilise ``tradable_bars_by_symbol``).
    Le mode queue n'a jamais de cache pré-chargé, donc ``bars_by_symbol`` n'est
    pas exposé et il n'y a pas de « second régime » implicite.

    Le resolver retourné a la signature ``(requests) -> ToolPayload`` exigée par
    le Protocol ``IndicatorResolver`` (``agent/tools/core.py``) : il se branche
    tel quel dans ``ToolContext.indicator_resolver``.

    Parameters
    ----------
    symbols:
        Univers du cycle (requis) — filtre dur + calcul des paires cross-asset.
    get_bars:
        Fetcher de barres (requis, pas de défaut) — typiquement
        ``data_source.get_bars`` enveloppé thread-safe. Requis explicitement :
        sans lui, aucune barre ne serait résolue (footgun AX évité).
    max_requests, max_indicators, default_window:
        Bornes du resolve (bootables, mêmes valeurs que REQUEST_CONTEXT batch).
    """
    if get_bars is None:
        # Sans ce guard, resolve_indicator_requests retomberait en silence sur
        # market.get_bars — exactement le footgun que le contrat veut interdire (review R5).
        raise ValueError("get_bars requis (fetch-first) : pas de fallback implicite (AX #7)")

    def _resolver(requests: Iterable[object]) -> dict:
        return resolve_indicator_requests(
            requests,
            {},
            symbols=symbols,
            max_requests=max_requests,
            max_indicators=max_indicators,
            default_window=default_window,
            market_get_bars=get_bars,
        )

    return _resolver
