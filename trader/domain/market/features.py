"""Deterministic indicator calculations for the trading semantic layer."""

from __future__ import annotations

import math
from statistics import mean, pstdev
from typing import Iterable

from trader.domain.semantic.catalog import INDICATOR_LABEL_VALUES, family_for_symbol

_CANDLESTICK_VALUES = INDICATOR_LABEL_VALUES["candlestick_signal"]
_CHART_BREAKOUT_VALUES = INDICATOR_LABEL_VALUES["chart_breakout"]

DEFAULT_INDICATORS = [
    "return",
    "volatility",
    "ohlc_volatility",
    "atr_pct",
    "relative_volume",
    "z_score",
    "efficiency_ratio",
    "autocorrelation",
    "relative_strength",
    "spread_zscore",
    "candlestick_signal",
    "candle_body_ratio",
    "candle_wick_skew",
    "chart_breakout",
    "trend_slope",
    "range_position",
]


def _attr(obj: object, name: str) -> float:
    if isinstance(obj, dict):
        return float(obj[name])
    return float(getattr(obj, name))


def _closes(bars: list[object]) -> list[float]:
    return [_attr(bar, "close") for bar in bars]


def _opens(bars: list[object]) -> list[float]:
    return [_attr(bar, "open") for bar in bars]


def _highs(bars: list[object]) -> list[float]:
    return [_attr(bar, "high") for bar in bars]


def _lows(bars: list[object]) -> list[float]:
    return [_attr(bar, "low") for bar in bars]


def _window(bars: Iterable[object], window: int) -> list[object]:
    selected = list(bars)
    return selected[-window:] if window > 0 else selected


def swing_low(bars: Iterable[object], window: int) -> float | None:
    selected = _window(bars, window)
    if not selected:
        return None
    return min(_lows(selected))


def swing_high(bars: Iterable[object], window: int) -> float | None:
    selected = _window(bars, window)
    if not selected:
        return None
    return max(_highs(selected))


def vwap(bars: Iterable[object], window: int) -> float | None:
    selected = _window(bars, window)
    if not selected:
        return None

    weighted_sum = 0.0
    volume_sum = 0.0
    for bar in selected:
        volume = _attr(bar, "volume")
        typical = (
            _attr(bar, "high") + _attr(bar, "low") + _attr(bar, "close")
        ) / 3.0
        weighted_sum += typical * volume
        volume_sum += volume

    if volume_sum <= 0:
        return None
    return weighted_sum / volume_sum


def _returns(closes: list[float]) -> list[float]:
    return [
        closes[i] / closes[i - 1] - 1.0
        for i in range(1, len(closes))
        if closes[i - 1] != 0
    ]


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, 6)


def _window_return(closes: list[float]) -> float | None:
    if len(closes) < 2 or closes[0] == 0:
        return None
    return closes[-1] / closes[0] - 1.0


def _volatility(closes: list[float]) -> float | None:
    returns = _returns(closes)
    if len(returns) < 2:
        return None
    return pstdev(returns)


def _z_score(closes: list[float]) -> float | None:
    if len(closes) < 2:
        return None
    sigma = pstdev(closes)
    if sigma == 0:
        return None
    return (closes[-1] - mean(closes)) / sigma


def _efficiency_ratio(closes: list[float]) -> float | None:
    if len(closes) < 2:
        return None
    path = sum(abs(closes[i] - closes[i - 1]) for i in range(1, len(closes)))
    if path == 0:
        return None
    return abs(closes[-1] - closes[0]) / path


def _autocorrelation(closes: list[float]) -> float | None:
    returns = _returns(closes)
    if len(returns) < 3:
        return None
    xs = returns[:-1]
    ys = returns[1:]
    mx = mean(xs)
    my = mean(ys)
    denom_x = math.sqrt(sum((x - mx) ** 2 for x in xs))
    denom_y = math.sqrt(sum((y - my) ** 2 for y in ys))
    if denom_x == 0 or denom_y == 0:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (denom_x * denom_y)


def _ohlc_volatility(bars: list[object]) -> float | None:
    if not bars:
        return None
    estimates: list[float] = []
    for bar in bars:
        open_ = _attr(bar, "open")
        high = _attr(bar, "high")
        low = _attr(bar, "low")
        close = _attr(bar, "close")
        if min(open_, high, low, close) <= 0:
            continue
        hl = math.log(high / low)
        co = math.log(close / open_)
        estimate = 0.5 * hl * hl - (2.0 * math.log(2.0) - 1.0) * co * co
        estimates.append(max(0.0, estimate))
    if not estimates:
        return None
    return math.sqrt(mean(estimates))


def _atr_pct(bars: list[object]) -> float | None:
    """Average true range normalized by the latest close.

    Normalizing makes the value comparable across currencies and price scales.
    The first bar uses its own high-low range; subsequent bars also include gaps
    from the previous close.
    """
    if not bars:
        return None
    true_ranges: list[float] = []
    previous_close: float | None = None
    for bar in bars:
        high = _attr(bar, "high")
        low = _attr(bar, "low")
        close = _attr(bar, "close")
        if not all(math.isfinite(value) for value in (high, low, close)) or high < low:
            previous_close = close if math.isfinite(close) else previous_close
            continue
        candidates = [high - low]
        if previous_close is not None and math.isfinite(previous_close):
            candidates.extend((abs(high - previous_close), abs(low - previous_close)))
        true_ranges.append(max(candidates))
        previous_close = close
    latest_close = _attr(bars[-1], "close")
    if not true_ranges or not math.isfinite(latest_close) or latest_close <= 0:
        return None
    return mean(true_ranges) / latest_close


def _relative_volume(bars: list[object]) -> float | None:
    """Latest bar volume divided by the positive rolling baseline before it."""
    if len(bars) < 2:
        return None
    latest = _attr(bars[-1], "volume")
    if not math.isfinite(latest) or latest < 0:
        return None
    baseline = [
        volume
        for bar in bars[:-1]
        if math.isfinite(volume := _attr(bar, "volume")) and volume > 0
    ]
    if not baseline:
        return None
    return latest / mean(baseline)


def _candle_body_ratio(bars: list[object]) -> float | None:
    if not bars:
        return None
    bar = bars[-1]
    open_ = _attr(bar, "open")
    high = _attr(bar, "high")
    low = _attr(bar, "low")
    close = _attr(bar, "close")
    candle_range = high - low
    if candle_range <= 0:
        return None
    return abs(close - open_) / candle_range


def _candle_wick_skew(bars: list[object]) -> float | None:
    if not bars:
        return None
    bar = bars[-1]
    open_ = _attr(bar, "open")
    high = _attr(bar, "high")
    low = _attr(bar, "low")
    close = _attr(bar, "close")
    candle_range = high - low
    if candle_range <= 0:
        return None
    upper = high - max(open_, close)
    lower = min(open_, close) - low
    return (lower - upper) / candle_range


def _candlestick_signal(bars: list[object]) -> float | None:
    if not bars:
        return None
    current = bars[-1]
    open_ = _attr(current, "open")
    high = _attr(current, "high")
    low = _attr(current, "low")
    close = _attr(current, "close")
    body = abs(close - open_)
    candle_range = high - low
    if candle_range <= 0:
        return None

    if len(bars) >= 2:
        previous = bars[-2]
        prev_open = _attr(previous, "open")
        prev_close = _attr(previous, "close")
        bullish_engulfing = (
            prev_close < prev_open
            and close > open_
            and open_ <= prev_close
            and close >= prev_open
        )
        bearish_engulfing = (
            prev_close > prev_open
            and close < open_
            and open_ >= prev_close
            and close <= prev_open
        )
        if bullish_engulfing:
            return _CANDLESTICK_VALUES["bullish_engulfing"]
        if bearish_engulfing:
            return _CANDLESTICK_VALUES["bearish_engulfing"]

    upper = high - max(open_, close)
    lower = min(open_, close) - low
    if body > 0 and lower >= 2.0 * body and upper <= body:
        return _CANDLESTICK_VALUES["hammer"]
    if body > 0 and upper >= 2.0 * body and lower <= body:
        return _CANDLESTICK_VALUES["shooting_star"]
    return 0.0


def _chart_breakout(bars: list[object]) -> float | None:
    if len(bars) < 2:
        return None
    prior = bars[:-1]
    last_close = _attr(bars[-1], "close")
    prior_high = max(_highs(prior))
    prior_low = min(_lows(prior))
    if last_close > prior_high:
        return _CHART_BREAKOUT_VALUES["breakout_up"]
    if last_close < prior_low:
        return _CHART_BREAKOUT_VALUES["breakout_down"]
    return 0.0


def _trend_slope(closes: list[float]) -> float | None:
    if len(closes) < 2:
        return None
    n = len(closes)
    xs = list(range(n))
    mx = mean(xs)
    my = mean(closes)
    denom = sum((x - mx) ** 2 for x in xs)
    if denom == 0 or my == 0:
        return None
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, closes)) / denom
    return slope / abs(my)


def _range_position(bars: list[object]) -> float | None:
    if not bars:
        return None
    high = max(_highs(bars))
    low = min(_lows(bars))
    if high <= low:
        return None
    close = _attr(bars[-1], "close")
    return (close - low) / (high - low)


def _spread_zscore(
    symbol: str,
    bars_by_symbol: dict[str, list[object]],
    peer_symbols: list[str],
    window: int,
) -> float | None:
    symbol_closes = _closes(_window(bars_by_symbol.get(symbol, []), window))
    peer_closes = [
        _closes(_window(bars_by_symbol.get(peer, []), window))
        for peer in peer_symbols
        if bars_by_symbol.get(peer)
    ]
    if not symbol_closes or not peer_closes:
        return None

    min_len = min(len(symbol_closes), *(len(values) for values in peer_closes))
    if min_len < 2:
        return None

    symbol_series = symbol_closes[-min_len:]
    peer_series = [values[-min_len:] for values in peer_closes]
    spreads = [
        symbol_series[index] - mean(peer[index] for peer in peer_series)
        for index in range(min_len)
    ]
    sigma = pstdev(spreads)
    if sigma == 0:
        return None
    return (spreads[-1] - mean(spreads)) / sigma


def compute_indicator_values(
    bars: list[object],
    *,
    names: list[str],
    window: int,
    family_return: float | None = None,
    family_spread_zscore: float | None = None,
) -> dict[str, float | None]:
    selected = _window(bars, window)
    closes = _closes(selected)
    values: dict[str, float | None] = {}
    base_return = _window_return(closes)

    for name in names:
        if name == "return":
            values[name] = _round(base_return)
        elif name == "volatility":
            values[name] = _round(_volatility(closes))
        elif name == "ohlc_volatility":
            values[name] = _round(_ohlc_volatility(selected))
        elif name == "atr_pct":
            values[name] = _round(_atr_pct(selected))
        elif name == "relative_volume":
            values[name] = _round(_relative_volume(selected))
        elif name == "z_score":
            values[name] = _round(_z_score(closes))
        elif name == "efficiency_ratio":
            values[name] = _round(_efficiency_ratio(closes))
        elif name == "autocorrelation":
            values[name] = _round(_autocorrelation(closes))
        elif name == "relative_strength":
            values[name] = _round(None if base_return is None or family_return is None else base_return - family_return)
        elif name == "spread_zscore":
            values[name] = _round(family_spread_zscore)
        elif name == "candlestick_signal":
            values[name] = _round(_candlestick_signal(selected))
        elif name == "candle_body_ratio":
            values[name] = _round(_candle_body_ratio(selected))
        elif name == "candle_wick_skew":
            values[name] = _round(_candle_wick_skew(selected))
        elif name == "chart_breakout":
            values[name] = _round(_chart_breakout(selected))
        elif name == "trend_slope":
            values[name] = _round(_trend_slope(closes))
        elif name == "range_position":
            values[name] = _round(_range_position(selected))
        else:
            raise ValueError(f"unknown_indicator: {name}")
    return values


def build_indicator_snapshot(
    bars_by_symbol: dict[str, list[object]],
    *,
    symbols: list[str],
    names: list[str] | None = None,
    window: int = 48,
) -> dict[str, dict]:
    selected_names = names or DEFAULT_INDICATORS
    returns_by_symbol: dict[str, float | None] = {}
    for symbol in symbols:
        bars = bars_by_symbol.get(symbol, [])
        returns_by_symbol[symbol] = _window_return(_closes(_window(bars, window))) if bars else None

    family_returns: dict[str, float] = {}
    for symbol in symbols:
        family = family_for_symbol(symbol)
        if family is None:
            continue
        family_values = [
            ret
            for candidate, ret in returns_by_symbol.items()
            if family_for_symbol(candidate) == family and ret is not None
        ]
        if family_values:
            family_returns[family] = mean(family_values)

    spread_zscores: dict[str, float | None] = {}
    for symbol in symbols:
        family = family_for_symbol(symbol)
        if family is None:
            spread_zscores[symbol] = None
            continue
        peers = [
            candidate
            for candidate in symbols
            if candidate != symbol and family_for_symbol(candidate) == family
        ]
        spread_zscores[symbol] = _spread_zscore(symbol, bars_by_symbol, peers, window)

    snapshot: dict[str, dict] = {}
    for symbol in symbols:
        bars = bars_by_symbol.get(symbol, [])
        family = family_for_symbol(symbol)
        values = compute_indicator_values(
            bars,
            names=selected_names,
            window=window,
            family_return=family_returns.get(family or ""),
            family_spread_zscore=spread_zscores.get(symbol),
        ) if bars else {name: None for name in selected_names}
        snapshot[symbol] = {
            "family": family,
            "window": window,
            "indicators": values,
        }
    return snapshot
