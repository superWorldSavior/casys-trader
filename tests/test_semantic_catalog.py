from trader.market.features import _candlestick_signal, _chart_breakout
import trader.semantic.catalog as catalog
from trader.semantic.catalog import (
    describe_semantic_layer,
    family_for_symbol,
    find_indicators,
    list_indicators,
    normalize_temporal_query,
)
from trader.tools.market import Bar


def test_describe_semantic_layer_expose_niveaux_et_familles() -> None:
    layer = describe_semantic_layer()

    assert layer["levels"] == ["market", "family", "symbol", "timeframe", "lookback", "window", "as_of", "indicator"]
    assert "indices" in layer["families"]
    assert "energy" in layer["families"]
    assert "4h" in layer["timeframes"]
    assert layer["timeframes"]["4h"]["source_interval"] == "1h"
    assert 48 in layer["windows"]
    assert layer["families"]["forex_majors"] == [
        "EURUSD=X",
        "GBPUSD=X",
        "USDJPY=X",
        "USDCHF=X",
        "USDCAD=X",
        "AUDUSD=X",
        "NZDUSD=X",
        "EURJPY=X",
    ]
    assert layer["families"]["europe_indices"] == ["^FCHI"]
    assert layer["families"]["commodities_futures"] == ["CL=F", "BZ=F", "NG=F"]


def test_list_indicators_expose_indicateurs_gouvernes() -> None:
    indicators = list_indicators()
    names = {item["name"] for item in indicators}

    assert {"return", "volatility", "z_score", "efficiency_ratio", "autocorrelation", "spread_zscore"} <= names
    assert {"candlestick_signal", "chart_breakout", "trend_slope", "range_position"} <= names
    assert all("description" in item for item in indicators)


def test_label_to_value_expose_les_labels_categoriels_watchables() -> None:
    expected = {
        "chart_breakout": {"breakout_up": 1.0, "breakout_down": -1.0},
        "candlestick_signal": {
            "bullish_engulfing": 1.0,
            "bull_engulf": 1.0,
            "bearish_engulfing": -1.0,
            "bear_engulf": -1.0,
            "hammer": 0.5,
            "shooting_star": -0.5,
        },
    }

    assert hasattr(catalog, "INDICATOR_LABEL_VALUES")
    assert hasattr(catalog, "label_to_value")
    assert catalog.INDICATOR_LABEL_VALUES == expected
    for indicator, labels in expected.items():
        for label, value in labels.items():
            assert catalog.label_to_value(indicator, label) == value
    assert catalog.label_to_value("candlestick_signal", "doji") is None
    assert catalog.label_to_value("z_score", "breakout_up") is None


def test_label_values_coincident_avec_les_sorties_reelles_des_features() -> None:
    assert hasattr(catalog, "INDICATOR_LABEL_VALUES")
    candle = catalog.INDICATOR_LABEL_VALUES["candlestick_signal"]
    breakout = catalog.INDICATOR_LABEL_VALUES["chart_breakout"]

    assert _candlestick_signal([
        Bar(ts="t1", open=100.0, high=101.0, low=94.0, close=95.0, volume=1000.0),
        Bar(ts="t2", open=94.0, high=102.5, low=92.5, close=102.0, volume=1000.0),
    ]) == candle["bullish_engulfing"]
    assert _candlestick_signal([
        Bar(ts="t1", open=100.0, high=106.0, low=99.0, close=105.0, volume=1000.0),
        Bar(ts="t2", open=106.0, high=107.0, low=98.0, close=99.0, volume=1000.0),
    ]) == candle["bearish_engulfing"]
    assert _candlestick_signal([
        Bar(ts="t1", open=100.0, high=101.5, low=97.5, close=101.0, volume=1000.0),
    ]) == candle["hammer"]
    assert _candlestick_signal([
        Bar(ts="t1", open=100.0, high=102.5, low=98.5, close=99.0, volume=1000.0),
    ]) == candle["shooting_star"]

    assert _chart_breakout([
        Bar(ts="t1", open=100.0, high=101.0, low=99.0, close=100.0, volume=1000.0),
        Bar(ts="t2", open=102.0, high=103.0, low=100.0, close=102.0, volume=1000.0),
        Bar(ts="t3", open=107.0, high=109.0, low=106.0, close=108.0, volume=1000.0),
    ]) == breakout["breakout_up"]
    assert _chart_breakout([
        Bar(ts="t1", open=100.0, high=101.0, low=99.0, close=100.0, volume=1000.0),
        Bar(ts="t2", open=102.0, high=103.0, low=100.0, close=102.0, volume=1000.0),
        Bar(ts="t3", open=96.0, high=97.0, low=94.0, close=95.0, volume=1000.0),
    ]) == breakout["breakout_down"]


def test_find_indicators_recherche_par_concept() -> None:
    hits = find_indicators("regime")
    names = {item["name"] for item in hits}

    assert "efficiency_ratio" in names
    assert "autocorrelation" in names


def test_find_indicators_recherche_chandeliers_et_chartisme() -> None:
    candles = {item["name"] for item in find_indicators("candlestick")}
    chart = {item["name"] for item in find_indicators("chart")}

    assert "candlestick_signal" in candles
    assert "chart_breakout" in chart


def test_family_for_symbol_reconnait_forex_et_cac40() -> None:
    assert family_for_symbol("EURUSD=X") == "forex_majors"
    assert family_for_symbol("^FCHI") == "europe_indices"
    assert family_for_symbol("CL=F") == "commodities_futures"


def test_normalize_temporal_query_valide_timeframe_lookback_window_et_4h() -> None:
    query = normalize_temporal_query(timeframe="4h", lookback="1mo", window=999, as_of="latest")

    assert query == {
        "timeframe": "4h",
        "source_interval": "1h",
        "lookback": "1mo",
        "window": 240,
        "as_of": "latest",
    }


def test_normalize_temporal_query_retourne_defauts_si_valeurs_inconnues() -> None:
    query = normalize_temporal_query(timeframe="2h", lookback="bad", window=1, as_of="future")

    assert query["timeframe"] == "1h"
    assert query["lookback"] == "5d"
    assert query["window"] == 2
    assert query["as_of"] == "latest"
