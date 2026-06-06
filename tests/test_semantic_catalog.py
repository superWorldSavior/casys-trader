from trader.semantic.catalog import (
    describe_semantic_layer,
    family_for_symbol,
    find_indicators,
    list_indicators,
    normalize_temporal_query,
)


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
