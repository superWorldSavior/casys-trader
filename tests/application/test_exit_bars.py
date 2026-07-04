from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from trader.market.market_data import Bar


class _PlanStore:
    def __init__(self, symbols: list[str]) -> None:
        self._plans = [SimpleNamespace(symbol=symbol) for symbol in symbols]

    def open_plans(self) -> list[SimpleNamespace]:
        return list(self._plans)


class _DataSource:
    def __init__(self, bars_by_symbol: dict[str, list[Bar]]) -> None:
        self.bars_by_symbol = bars_by_symbol
        self.calls: list[tuple[str, str, str]] = []

    def get_bars(self, symbol: str, lookback: str, interval: str) -> list[Bar]:
        self.calls.append((symbol, lookback, interval))
        return self.bars_by_symbol.get(symbol, [])


def test_fetch_exit_bars_uses_fresh_valid_5m_for_open_tradable_plans() -> None:
    from trader.application.exit_bars import fetch_exit_bars_for_open_plans

    now = datetime(2026, 6, 15, 14, 30, tzinfo=timezone.utc)
    fallback_bar = Bar(ts=now.isoformat(), open=99.0, high=101.0, low=98.0, close=100.0, volume=10.0)
    five_minute_bar = Bar(ts=now.isoformat(), open=100.0, high=102.0, low=99.0, close=101.0, volume=12.0)
    data_source = _DataSource({"SPY": [five_minute_bar]})

    bars, intervals = fetch_exit_bars_for_open_plans(
        plan_store=_PlanStore(["SPY", "QQQ"]),
        data_source=data_source,
        tradable_bars_by_symbol={"SPY": [fallback_bar]},
        tradable_prices={"SPY": 100.0},
        now=now,
        fallback_interval="15m",
    )

    assert data_source.calls == [("SPY", "1d", "5m")]
    assert bars["SPY"] == [five_minute_bar]
    assert intervals == {"SPY": "5m"}


def test_fetch_exit_bars_falls_back_when_5m_bars_are_invalid() -> None:
    from trader.application.exit_bars import fetch_exit_bars_for_open_plans

    now = datetime(2026, 6, 15, 14, 30, tzinfo=timezone.utc)
    fallback_bar = Bar(ts=now.isoformat(), open=99.0, high=101.0, low=98.0, close=100.0, volume=10.0)
    invalid_bar = Bar(ts=now.isoformat(), open=100.0, high=98.0, low=99.0, close=101.0, volume=12.0)

    bars, intervals = fetch_exit_bars_for_open_plans(
        plan_store=_PlanStore(["SPY"]),
        data_source=_DataSource({"SPY": [invalid_bar]}),
        tradable_bars_by_symbol={"SPY": [fallback_bar]},
        tradable_prices={"SPY": 100.0},
        now=now,
        fallback_interval="15m",
    )

    assert bars["SPY"] == [fallback_bar]
    assert intervals == {"SPY": "15m"}
