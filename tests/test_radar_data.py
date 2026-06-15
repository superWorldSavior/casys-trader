import pytest

from trader.radar_data import CoverageError, fetch_daily


def _fake_fetch(symbols: list[str]) -> dict[str, list[object]]:
    return {
        symbol: ([] if symbol == "DEAD" else [object(), object(), object()])
        for symbol in symbols
    }


def test_fetch_daily_returns_only_covered_symbols() -> None:
    bars = fetch_daily(["SPY", "DEAD"], fetch_fn=_fake_fetch, min_coverage=0.0)

    assert "SPY" in bars
    assert "DEAD" not in bars


def test_fetch_daily_coverage_below_threshold_raises() -> None:
    with pytest.raises(CoverageError):
        fetch_daily(["SPY", "DEAD"], fetch_fn=_fake_fetch, min_coverage=0.9)
