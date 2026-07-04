from trader.agent.context import resolve_indicator_requests
from trader.agent.client import IndicatorRequest
from trader.market.market_data import Bar


def _bar(index: int, close: float) -> Bar:
    return Bar(
        ts=f"t{index}",
        open=close - 0.5,
        high=close + 1.0,
        low=close - 1.0,
        close=close,
        volume=100.0,
    )


def test_ne_reutilise_le_cache_que_si_lintervalle_matche() -> None:
    # Le cache contient des barres 15m. Une requête 1h ne doit PAS les réutiliser :
    # elle doit refetch en 1h (sinon on lit du 15m en croyant que c'est du 1h).
    calls: list[tuple[str, str, str]] = []

    def get_bars(symbol: str, lookback: str, interval: str) -> list[Bar]:
        calls.append((symbol, lookback, interval))
        return [_bar(index, 100.0 + index) for index in range(12)]

    cached_15m = {"SPY": [_bar(i, 200.0 + i) for i in range(12)]}

    result = resolve_indicator_requests(
        [IndicatorRequest(symbol="SPY", indicators=["return"], timeframe="1h", lookback="5d", window=24)],
        cached_15m,
        symbols=["SPY"],
        max_requests=2,
        max_indicators=4,
        market_get_bars=get_bars,
        cached_interval="15m",
        cached_lookback="5d",
    )

    assert calls == [("SPY", "5d", "1h")]  # a bien refetch en 1h, pas réutilisé le 15m
    assert result["requests"][0]["timeframe"] == "1h"


def test_resolve_indicator_requests_normalise_et_charge_timeframe_4h() -> None:
    calls: list[tuple[str, str, str]] = []

    def get_bars(symbol: str, lookback: str, interval: str) -> list[Bar]:
        calls.append((symbol, lookback, interval))
        return [_bar(index, 100.0 + index) for index in range(12)]

    result = resolve_indicator_requests(
        [
            IndicatorRequest(
                symbol="SPY",
                indicators=["return"],
                timeframe="4h",
                lookback="1mo",
                window=96,
            )
        ],
        {},
        symbols=["SPY"],
        max_requests=2,
        max_indicators=4,
        market_get_bars=get_bars,
    )

    assert calls == [("SPY", "1mo", "4h")]
    assert result["requests"][0]["timeframe"] == "4h"
    assert result["requests"][0]["source_interval"] == "1h"
    assert result["requests"][0]["lookback"] == "1mo"
    assert result["requests"][0]["window"] == 96
    assert result["requests"][0]["indicators"]["return"] is not None


def test_resolve_indicator_requests_accepte_les_abreviations_du_cockpit() -> None:
    bars = [_bar(index, close) for index, close in enumerate([100.0, 101.0, 100.0, 102.0, 101.0, 103.0])]

    result = resolve_indicator_requests(
        [IndicatorRequest(symbol="SPY", indicators=["er", "ac"], timeframe="1h", lookback="5d", window=6)],
        {"SPY": bars},
        symbols=["SPY"],
        max_requests=1,
        max_indicators=2,
    )

    resolved_names = set(result["requests"][0]["indicators"])
    assert resolved_names == {"efficiency_ratio", "autocorrelation"}


def test_resolve_indicator_requests_charge_les_pairs_pour_relative_strength_4h() -> None:
    calls: list[tuple[str, str, str]] = []

    def get_bars(symbol: str, lookback: str, interval: str) -> list[Bar]:
        calls.append((symbol, lookback, interval))
        base = {"SPY": 100.0, "QQQ": 200.0, "DIA": 300.0}[symbol]
        closes = [base, base, base, base, base * 1.1] if symbol == "SPY" else [base] * 5
        return [
            Bar(ts=f"t{index}", open=close, high=close + 1.0, low=close - 1.0, close=close, volume=100.0)
            for index, close in enumerate(closes)
        ]

    result = resolve_indicator_requests(
        [
            IndicatorRequest(
                symbol="SPY",
                indicators=["relative_strength"],
                timeframe="4h",
                lookback="1mo",
                window=5,
            )
        ],
        {},
        symbols=["SPY", "QQQ", "DIA"],
        max_requests=1,
        max_indicators=1,
        market_get_bars=get_bars,
    )

    assert calls == [("SPY", "1mo", "4h"), ("QQQ", "1mo", "4h"), ("DIA", "1mo", "4h")]
    assert result["requests"][0]["indicators"]["relative_strength"] > 0.04
