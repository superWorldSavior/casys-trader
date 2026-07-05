"""T1a — factory `build_indicator_resolver` (spec queue tool-round §6.1).

Contrat étroit (AX #7 Explicit, #9 Narrow) : resolver *fetch-first* pour le
worker de file. UN seul comportement — chaque requête fetch via `get_bars`,
aucun cache de cycle (pas de `bars_by_symbol` exposé). Le filtre d'univers
(`symbols`) reste respecté.
"""
import pytest

from trader.agent.client import IndicatorRequest
from trader.agent.context import build_indicator_resolver
from trader.market.market_data import Bar


def _bar(index: int, close: float) -> Bar:
    return Bar(ts=f"t{index}", open=close - 0.5, high=close + 1.0, low=close - 1.0, close=close, volume=100.0)


def test_fetch_first_via_get_bars() -> None:
    # Aucune barre pré-chargée : le resolver DOIT fetcher via get_bars (mode queue).
    calls: list[tuple[str, str, str]] = []

    def get_bars(symbol: str, lookback: str, interval: str) -> list[Bar]:
        calls.append((symbol, lookback, interval))
        return [_bar(i, 100.0 + i) for i in range(12)]

    resolver = build_indicator_resolver(symbols=["SPY"], get_bars=get_bars, max_requests=2, max_indicators=4)
    result = resolver([IndicatorRequest(symbol="SPY", indicators=["return"], timeframe="1h", lookback="5d", window=24)])

    assert calls == [("SPY", "5d", "1h")]
    assert result["requests"][0]["timeframe"] == "1h"


def test_filtre_les_symboles_hors_univers() -> None:
    # Un symbole absent de `symbols` est ignoré : ni résolu, ni fetché.
    calls: list[str] = []

    def get_bars(symbol: str, lookback: str, interval: str) -> list[Bar]:
        calls.append(symbol)
        return [_bar(i, 100.0 + i) for i in range(12)]

    resolver = build_indicator_resolver(symbols=["SPY"], get_bars=get_bars, max_requests=2, max_indicators=4)
    result = resolver([IndicatorRequest(symbol="QQQ", indicators=["return"], timeframe="1h", lookback="5d", window=24)])

    assert calls == []
    assert result["requests"] == []


def test_get_bars_none_leve_valueerror() -> None:
    # Review R5 : get_bars=None ne doit PAS retomber en silence sur market.get_bars.
    with pytest.raises(ValueError):
        build_indicator_resolver(symbols=["SPY"], get_bars=None, max_requests=2, max_indicators=4)
