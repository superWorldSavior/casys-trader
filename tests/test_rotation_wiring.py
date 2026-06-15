"""Tests for trader/rotation_wiring.py — pure helpers, no I/O."""

from __future__ import annotations

import pytest

from trader.tools.market import Bar
from trader.rotation_wiring import (
    venue_of,
    benchmark_ret_for,
    resolve_as_of,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _bar(close: float, ts: str = "2024-01-02", open_: float | None = None) -> Bar:
    """Minimal Bar with a known close (and ts)."""
    o = open_ if open_ is not None else close
    return Bar(ts=ts, open=o, high=close, low=close, close=close, volume=0.0)


def _bars_with_return(start: float, end: float, ts: str = "2024-01-05") -> list[Bar]:
    """Two bars giving a known window_return = end/start - 1."""
    return [
        _bar(start, ts="2024-01-01"),
        _bar(end, ts=ts),
    ]


# ---------------------------------------------------------------------------
# venue_of
# ---------------------------------------------------------------------------

class TestVenueOf:
    def test_spy_us(self):
        assert venue_of("SPY") == "US"

    def test_nvda_us(self):
        assert venue_of("NVDA") == "US"

    def test_ho_pa_eu(self):
        assert venue_of("HO.PA") == "EU"

    def test_rhm_de_eu(self):
        assert venue_of("RHM.DE") == "EU"

    def test_as_suffix_eu(self):
        assert venue_of("PHIA.AS") == "EU"

    def test_mi_suffix_eu(self):
        assert venue_of("ENI.MI") == "EU"

    def test_fchi_eu(self):
        assert venue_of("^FCHI") == "EU"

    def test_tw_suffix_tw(self):
        assert venue_of("2330.TW") == "TW"

    def test_fx_suffix_fx(self):
        assert venue_of("EURUSD=X") == "FX"

    def test_unknown_suffix_us(self):
        assert venue_of("ABC.ZZ") == "US"


# ---------------------------------------------------------------------------
# benchmark_ret_for
# ---------------------------------------------------------------------------

class TestBenchmarkRetFor:
    BENCHMARKS = {"US": "SPY", "EU": "^FCHI", "TW": "^TWII"}
    DEFAULT_BENCH = "SPY"

    def _call(self, bars_by_symbol, *, score_window=2):
        return benchmark_ret_for(
            bars_by_symbol,
            benchmarks=self.BENCHMARKS,
            default_benchmark=self.DEFAULT_BENCH,
            score_window=score_window,
        )

    def test_us_symbol_uses_spy_return(self):
        """SPY present → benchmark_ret for NVDA == SPY's window return."""
        spy_bars = _bars_with_return(100.0, 110.0)  # return = 0.10
        nvda_bars = _bars_with_return(200.0, 220.0)
        result = self._call({"NVDA": nvda_bars, "SPY": spy_bars})
        assert "NVDA" in result
        assert abs(result["NVDA"] - 0.10) < 1e-6

    def test_eu_symbol_uses_fchi(self):
        """^FCHI present → benchmark_ret for HO.PA == ^FCHI window return."""
        fchi_bars = _bars_with_return(7000.0, 7070.0)  # return = 0.01
        hopa_bars = _bars_with_return(50.0, 55.0)
        result = self._call({"HO.PA": hopa_bars, "^FCHI": fchi_bars})
        assert abs(result["HO.PA"] - 0.01) < 1e-6

    def test_missing_benchmark_gives_zero(self):
        """If the benchmark symbol is NOT in bars_by_symbol → 0.0."""
        # TW symbol but ^TWII is absent
        tw_bars = _bars_with_return(100.0, 105.0)
        result = self._call({"2330.TW": tw_bars})  # no ^TWII
        assert result["2330.TW"] == 0.0

    def test_benchmark_none_value_gives_zero(self):
        """compute_indicator_values returns None for return when <2 bars → 0.0."""
        spy_bars = [_bar(100.0)]  # only 1 bar → return = None
        nvda_bars = _bars_with_return(200.0, 220.0)
        result = self._call({"NVDA": nvda_bars, "SPY": spy_bars})
        assert result["NVDA"] == 0.0

    def test_fx_symbol_uses_default_benchmark_when_absent(self):
        """FX → bench = SPY via benchmarks; if SPY absent → 0.0."""
        fx_bars = _bars_with_return(1.0, 1.05)
        # SPY not in dict
        result = self._call({"EURUSD=X": fx_bars})
        assert result["EURUSD=X"] == 0.0

    def test_all_symbols_returned(self):
        """Result keys == bars_by_symbol keys."""
        spy_bars = _bars_with_return(100.0, 110.0)
        nvda_bars = _bars_with_return(200.0, 220.0)
        msft_bars = _bars_with_return(300.0, 330.0)
        result = self._call({"NVDA": nvda_bars, "MSFT": msft_bars, "SPY": spy_bars})
        assert set(result.keys()) == {"NVDA", "MSFT", "SPY"}


# ---------------------------------------------------------------------------
# resolve_as_of
# ---------------------------------------------------------------------------

class TestResolveAsOf:
    def test_empty_dict_returns_empty_string(self):
        assert resolve_as_of({}) == ""

    def test_single_symbol(self):
        bars = [_bar(100.0, ts="2024-03-01")]
        assert resolve_as_of({"SPY": bars}) == "2024-03-01"

    def test_returns_most_recent_last_bar_ts(self):
        bars_a = [_bar(100.0, ts="2024-01-01"), _bar(101.0, ts="2024-01-05")]
        bars_b = [_bar(200.0, ts="2024-01-01"), _bar(202.0, ts="2024-01-03")]
        result = resolve_as_of({"A": bars_a, "B": bars_b})
        assert result == "2024-01-05"

    def test_symbol_with_no_bars_is_ignored(self):
        bars_a = [_bar(100.0, ts="2024-02-10")]
        result = resolve_as_of({"SPY": bars_a, "EMPTY": []})
        assert result == "2024-02-10"

    def test_all_empty_bars_returns_empty_string(self):
        result = resolve_as_of({"SPY": [], "NVDA": []})
        assert result == ""

    def test_lexicographic_ts_comparison(self):
        """ISO 8601 dates sort lexicographically — validate the correct one wins."""
        bars_a = [_bar(1.0, ts="2024-12-31")]
        bars_b = [_bar(2.0, ts="2025-01-01")]
        assert resolve_as_of({"A": bars_a, "B": bars_b}) == "2025-01-01"
