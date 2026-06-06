from trader.features import build_indicator_snapshot, compute_indicator_values
from trader.tools.market import Bar


def _bar(ts: str, close: float, high: float | None = None, low: float | None = None) -> Bar:
    return Bar(
        ts=ts,
        open=close - 0.5,
        high=high if high is not None else close + 1.0,
        low=low if low is not None else close - 1.0,
        close=close,
        volume=1000.0,
    )


def test_compute_indicator_values_calcule_indicateurs_deterministes() -> None:
    bars = [
        _bar("t1", 100.0),
        _bar("t2", 102.0),
        _bar("t3", 101.0),
        _bar("t4", 105.0),
        _bar("t5", 110.0),
    ]

    values = compute_indicator_values(
        bars,
        names=["return", "volatility", "z_score", "efficiency_ratio", "autocorrelation"],
        window=5,
    )

    assert values["return"] == 0.1
    assert round(values["efficiency_ratio"], 6) == round(10.0 / 12.0, 6)
    assert values["volatility"] > 0
    assert values["z_score"] > 0
    assert values["autocorrelation"] is not None


def test_build_indicator_snapshot_compare_les_familles() -> None:
    bars_by_symbol = {
        "SPY": [_bar("t1", 100.0), _bar("t2", 102.0), _bar("t3", 104.0)],
        "QQQ": [_bar("t1", 100.0), _bar("t2", 101.0), _bar("t3", 103.0)],
        "CL=F": [_bar("t1", 50.0), _bar("t2", 52.0), _bar("t3", 53.0)],
    }

    snapshot = build_indicator_snapshot(
        bars_by_symbol,
        symbols=["SPY", "QQQ", "CL=F"],
        names=["return", "relative_strength"],
        window=3,
    )

    assert snapshot["SPY"]["indicators"]["return"] == 0.04
    assert snapshot["SPY"]["family"] == "indices"
    assert snapshot["CL=F"]["family"] == "commodities_futures"
    assert snapshot["SPY"]["indicators"]["relative_strength"] > snapshot["QQQ"]["indicators"]["relative_strength"]


def test_build_indicator_snapshot_calcule_spread_zscore_famille() -> None:
    bars_by_symbol = {
        "SPY": [_bar("t1", 100.0), _bar("t2", 104.0), _bar("t3", 109.0), _bar("t4", 111.0)],
        "QQQ": [_bar("t1", 100.0), _bar("t2", 101.0), _bar("t3", 102.0), _bar("t4", 103.0)],
        "DIA": [_bar("t1", 100.0), _bar("t2", 101.0), _bar("t3", 102.0), _bar("t4", 103.0)],
    }

    snapshot = build_indicator_snapshot(
        bars_by_symbol,
        symbols=["SPY", "QQQ", "DIA"],
        names=["spread_zscore"],
        window=4,
    )

    assert snapshot["SPY"]["indicators"]["spread_zscore"] > 0


def test_compute_indicator_values_detecte_chandeliers_japonais() -> None:
    bars = [
        Bar(ts="t1", open=100.0, high=101.0, low=94.0, close=95.0, volume=1000.0),
        Bar(ts="t2", open=94.0, high=102.5, low=92.5, close=102.0, volume=1000.0),
    ]

    values = compute_indicator_values(
        bars,
        names=["candlestick_signal", "candle_body_ratio", "candle_wick_skew"],
        window=2,
    )

    assert values["candlestick_signal"] == 1.0
    assert values["candle_body_ratio"] == 0.8
    assert values["candle_wick_skew"] > 0


def test_compute_indicator_values_calcule_signaux_chartistes() -> None:
    bars = [
        _bar("t1", 100.0, high=101.0, low=99.0),
        _bar("t2", 102.0, high=103.0, low=100.0),
        _bar("t3", 104.0, high=105.0, low=102.0),
        _bar("t4", 108.0, high=109.0, low=106.0),
    ]

    values = compute_indicator_values(
        bars,
        names=["chart_breakout", "trend_slope", "range_position"],
        window=4,
    )

    assert values["chart_breakout"] == 1.0
    assert values["trend_slope"] > 0
    assert values["range_position"] > 0.8
