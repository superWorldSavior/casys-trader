from dataclasses import fields
from datetime import datetime, timezone

import pytest

from trader.application.cycle.market_snapshot import (
    MarketSnapshot,
    MissingFxRate,
    build_market_snapshot,
)
from trader.market.market_data import Bar, MarketError


NOW = datetime(2026, 7, 2, 10, 0, tzinfo=timezone.utc)


def _bar(ts: str, close: float = 100.0) -> Bar:
    return Bar(ts=ts, open=close, high=close, low=close, close=close, volume=1.0)


class FakeSource:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []
        self.sources: dict[str, str | None] = {}

    def get_bars(self, symbol: str, lookback: str, interval: str) -> list[Bar]:
        self.calls.append((symbol, lookback, interval))
        if interval == "15m":
            self.sources[symbol] = "runtime"
            return [_bar("2026-07-02T09:45:00+00:00")]
        if interval == "1d":
            self.sources[symbol] = "daily"
            return [_bar("2026-07-01T20:00:00+00:00")]
        return [_bar("2026-07-02T09:55:00+00:00")]

    def last_source(self, symbol: str) -> str | None:
        return self.sources.get(symbol)


class FakeScheduler:
    def __init__(self, streaks: dict[str, int]) -> None:
        self.streaks = dict(streaks)
        self.reset: list[str] = []

    def get_stale_streak(self, symbol: str) -> int:
        return self.streaks.get(symbol, 0)

    def reset_stale_streak(self, symbol: str) -> None:
        self.reset.append(symbol)


def _fx_rate_provider(_symbols, *, data_source) -> dict[str, float]:
    del data_source
    return {"USD": 1.0}


def test_market_snapshot_returns_prices_and_sources():
    source = FakeSource()

    snapshot = build_market_snapshot(
        symbols=["SPY"],
        data_source=source,
        now=NOW,
        max_market_data_age_minutes=40.0,
        runtime_interval="15m",
        runtime_lookback="5d",
        fx_rate_provider=_fx_rate_provider,
        plan_store=None,
    )

    assert isinstance(snapshot, MarketSnapshot)
    assert snapshot.prices == {"SPY": 100.0}
    assert snapshot.bars_by_symbol["SPY"][-1].close == 100.0
    assert snapshot.runtime_data_source_by_symbol == {"SPY": "runtime"}


def test_market_snapshot_marks_stale_runtime_data():
    class StaleSource(FakeSource):
        def get_bars(self, symbol: str, lookback: str, interval: str) -> list[Bar]:
            self.calls.append((symbol, lookback, interval))
            if interval == "15m":
                self.sources[symbol] = "runtime"
                return [_bar("2026-07-01T00:00:00+00:00")]
            return super().get_bars(symbol, lookback, interval)

    snapshot = build_market_snapshot(
        symbols=["SPY"],
        data_source=StaleSource(),
        now=NOW,
        max_market_data_age_minutes=40.0,
        runtime_interval="15m",
        runtime_lookback="5d",
        fx_rate_provider=_fx_rate_provider,
        plan_store=None,
    )

    assert "SPY" in snapshot.stale_market_data
    assert snapshot.data_age_by_symbol["SPY"] > 40.0
    assert snapshot.tradable_prices == {}
    assert snapshot.tradable_symbols == []


def test_market_snapshot_resets_scheduler_only_for_fresh_symbols():
    class MixedSource(FakeSource):
        def get_bars(self, symbol: str, lookback: str, interval: str) -> list[Bar]:
            self.calls.append((symbol, lookback, interval))
            if interval == "15m":
                self.sources[symbol] = "runtime"
                ts = "2026-07-01T00:00:00+00:00" if symbol == "STALE" else "2026-07-02T09:45:00+00:00"
                return [_bar(ts)]
            return super().get_bars(symbol, lookback, interval)

    sched = FakeScheduler({"FRESH": 2, "STALE": 3})

    build_market_snapshot(
        symbols=["FRESH", "STALE"],
        data_source=MixedSource(),
        now=NOW,
        max_market_data_age_minutes=40.0,
        runtime_interval="15m",
        runtime_lookback="5d",
        fx_rate_provider=_fx_rate_provider,
        plan_store=None,
        scheduler=sched,
    )

    assert sched.reset == ["FRESH"]


def test_market_snapshot_captures_runtime_source_and_requests_fx_rates():
    source = FakeSource()
    fx_calls: list[tuple[list[str], object]] = []

    def fx_rate_provider(symbols, *, data_source) -> dict[str, float]:
        fx_calls.append((list(symbols), data_source))
        return {"USD": 1.0, "TWD": 0.031}

    snapshot = build_market_snapshot(
        symbols=["2330.TW"],
        data_source=source,
        now=NOW,
        max_market_data_age_minutes=40.0,
        runtime_interval="15m",
        runtime_lookback="5d",
        fx_rate_provider=fx_rate_provider,
        plan_store=None,
    )

    assert snapshot.runtime_data_source_by_symbol == {"2330.TW": "runtime"}
    assert snapshot.fx_rate_by_ccy["TWD"] == 0.031
    assert snapshot.rate_for_symbol("2330.TW") == 0.031
    assert fx_calls == [(["2330.TW"], source)]
    assert source.calls[0] == ("2330.TW", "5d", "15m")
    assert ("2330.TW", "1y", "1d") in source.calls
    assert ("TWD=X", "2d", "1d") not in source.calls


@pytest.mark.parametrize(
    ("symbol", "rates"),
    [
        ("SPY", {"USD": 1.0}),
        ("2330.TW", {"USD": 1.0, "TWD": 0.031}),
    ],
)
@pytest.mark.parametrize(
    ("failure", "expected_reason"),
    [
        ("exception", "all_sources_failed"),
        ("empty", "no_data"),
    ],
)
def test_market_snapshot_runtime_unavailable_is_stale_and_not_tradable(
    symbol: str,
    rates: dict[str, float],
    failure: str,
    expected_reason: str,
) -> None:
    class RuntimeUnavailableSource(FakeSource):
        def get_bars(self, requested_symbol: str, lookback: str, interval: str) -> list[Bar]:
            if interval == "15m":
                self.calls.append((requested_symbol, lookback, interval))
                if failure == "exception":
                    raise MarketError("all_sources_failed", f"{requested_symbol}: indisponible")
                return []
            return super().get_bars(requested_symbol, lookback, interval)

    source = RuntimeUnavailableSource()
    fx_calls: list[list[str]] = []

    def fx_rate_provider(symbols, *, data_source) -> dict[str, float]:
        assert data_source is source
        fx_calls.append(list(symbols))
        return rates

    snapshot = build_market_snapshot(
        symbols=[symbol],
        data_source=source,
        now=NOW,
        max_market_data_age_minutes=40.0,
        runtime_interval="15m",
        runtime_lookback="5d",
        fx_rate_provider=fx_rate_provider,
        plan_store=None,
    )

    assert snapshot.stale_market_data[symbol] == {
        "last_bar_ts": None,
        "stale_reason": expected_reason,
        "data_age_minutes": None,
    }
    assert fx_calls == [[symbol]]
    assert symbol not in snapshot.prices
    assert symbol not in snapshot.bars_by_symbol
    assert symbol not in snapshot.tradable_prices
    assert symbol not in snapshot.tradable_symbols
    assert symbol not in snapshot.tradable_bars_by_symbol
    if symbol == "2330.TW":
        assert snapshot.rate_for_symbol(symbol) == 0.031


def test_market_snapshot_rate_lookup_is_derived_method_not_captured_field() -> None:
    field_names = {field.name for field in fields(MarketSnapshot)}

    assert "rate_for_symbol" not in field_names


def test_market_snapshot_missing_twd_rate_is_not_tradable_and_raises() -> None:
    """2330.TW sans taux TWD : non-tradable (missing_fx_rate) et rate_for_symbol lève."""

    def usd_only_provider(_symbols, *, data_source) -> dict[str, float]:
        del data_source
        return {"USD": 1.0}

    snapshot = build_market_snapshot(
        symbols=["2330.TW", "SPY"],
        data_source=FakeSource(),
        now=NOW,
        max_market_data_age_minutes=40.0,
        runtime_interval="15m",
        runtime_lookback="5d",
        fx_rate_provider=usd_only_provider,
        plan_store=None,
    )

    assert "2330.TW" in snapshot.stale_market_data
    assert snapshot.stale_market_data["2330.TW"]["stale_reason"] == "missing_fx_rate"
    assert "2330.TW" not in snapshot.tradable_symbols
    assert "2330.TW" not in snapshot.tradable_prices
    assert "2330.TW" not in snapshot.tradable_bars_by_symbol
    assert "SPY" in snapshot.tradable_symbols
    assert snapshot.rate_for_symbol("SPY") == 1.0
    with pytest.raises(MissingFxRate) as excinfo:
        snapshot.rate_for_symbol("2330.TW")
    assert excinfo.value.symbol == "2330.TW"
    assert excinfo.value.ccy == "TWD"


def test_market_snapshot_provider_exception_marks_non_usd_missing_fx() -> None:
    def boom(_symbols, *, data_source) -> dict[str, float]:
        del data_source
        raise RuntimeError("fx provider down")

    snapshot = build_market_snapshot(
        symbols=["2330.TW"],
        data_source=FakeSource(),
        now=NOW,
        max_market_data_age_minutes=40.0,
        runtime_interval="15m",
        runtime_lookback="5d",
        fx_rate_provider=boom,
        plan_store=None,
    )

    assert snapshot.fx_rate_by_ccy == {"USD": 1.0}
    assert snapshot.stale_market_data["2330.TW"]["stale_reason"] == "missing_fx_rate"
    assert snapshot.tradable_symbols == []
    with pytest.raises(MissingFxRate):
        snapshot.rate_for_symbol("2330.TW")


def test_market_snapshot_rate_for_symbol_rejects_non_finite_or_non_positive() -> None:
    def bad_twd(_symbols, *, data_source) -> dict[str, float]:
        del data_source
        return {"USD": 1.0, "TWD": 0.0}

    snapshot = build_market_snapshot(
        symbols=["2330.TW"],
        data_source=FakeSource(),
        now=NOW,
        max_market_data_age_minutes=40.0,
        runtime_interval="15m",
        runtime_lookback="5d",
        fx_rate_provider=bad_twd,
        plan_store=None,
    )

    with pytest.raises(MissingFxRate) as excinfo:
        snapshot.rate_for_symbol("2330.TW")
    assert excinfo.value.ccy == "TWD"


def test_try_rate_for_symbol_returns_none_instead_of_raising() -> None:
    def usd_only_provider(_symbols, *, data_source) -> dict[str, float]:
        del data_source
        return {"USD": 1.0}

    snapshot = build_market_snapshot(
        symbols=["2330.TW", "SPY"],
        data_source=FakeSource(),
        now=NOW,
        max_market_data_age_minutes=40.0,
        runtime_interval="15m",
        runtime_lookback="5d",
        fx_rate_provider=usd_only_provider,
        plan_store=None,
    )

    assert snapshot.try_rate_for_symbol("SPY") == 1.0
    assert snapshot.try_rate_for_symbol("2330.TW") is None
