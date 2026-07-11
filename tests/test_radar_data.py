from __future__ import annotations

import threading

import pytest

from trader.infrastructure.market_sources import radar_data as radar_data_impl
from trader.infrastructure.market_sources.yahoo_client import RawBar
from trader.market.radar_data import CoverageError, download_daily_batch, fetch_daily


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


def _rows(symbol: str) -> list[dict]:
    return [
        {
            "ts": "2026-06-14T00:00:00+00:00",
            "open": 100.0,
            "high": 103.0,
            "low": 99.0,
            "close": 102.0,
            "volume": 1_000.0,
        },
        {
            "ts": "2026-06-15T00:00:00+00:00",
            "open": 102.0,
            "high": 106.0,
            "low": 101.0,
            "close": 105.0 if symbol == "SPY" else 210.0,
            "volume": 2_000.0,
        },
    ]


def test_download_daily_batch_converts_injected_rows_to_bars(tmp_path) -> None:
    def fake_download(symbols: list[str]) -> dict[str, list[dict]]:
        return {symbol: _rows(symbol) for symbol in symbols}

    bars_by_symbol = download_daily_batch(
        ["SPY", "QQQ"],
        as_of="2026-06-15",
        cache_dir=tmp_path,
        download_fn=fake_download,
    )

    spy_bars = bars_by_symbol["SPY"]
    assert set(bars_by_symbol) == {"SPY", "QQQ"}
    assert spy_bars[0].ts == "2026-06-14T00:00:00+00:00"
    assert spy_bars[0].open == 100.0
    assert spy_bars[1].close == 105.0
    assert spy_bars[1].volume == 2_000.0


def test_download_daily_batch_reads_cache_without_redownloading(tmp_path) -> None:
    calls = 0

    def fake_download(symbols: list[str]) -> dict[str, list[dict]]:
        nonlocal calls
        calls += 1
        return {symbol: _rows(symbol) for symbol in symbols}

    first = download_daily_batch(
        ["SPY"],
        as_of="2026-06-15",
        cache_dir=tmp_path,
        download_fn=fake_download,
    )
    second = download_daily_batch(
        ["SPY"],
        as_of="2026-06-15",
        cache_dir=tmp_path,
        download_fn=fake_download,
    )

    assert calls == 1
    assert second["SPY"][0] == first["SPY"][0]


def test_download_daily_batch_chunks_symbols(tmp_path) -> None:
    chunks: list[list[str]] = []

    def fake_download(symbols: list[str]) -> dict[str, list[dict]]:
        chunks.append(list(symbols))
        return {symbol: _rows(symbol) for symbol in symbols}

    symbols = [f"S{i:03d}" for i in range(250)]

    download_daily_batch(
        symbols,
        as_of="2026-06-15",
        cache_dir=tmp_path,
        download_fn=fake_download,
        chunk_size=100,
    )

    assert [len(chunk) for chunk in chunks] == [100, 100, 50]


def test_download_daily_batch_retries_transient_failure(tmp_path) -> None:
    calls = 0

    def flaky_download(symbols: list[str]) -> dict[str, list[dict]]:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("temporary")
        return {symbol: _rows(symbol) for symbol in symbols}

    bars_by_symbol = download_daily_batch(
        ["SPY"],
        as_of="2026-06-15",
        cache_dir=tmp_path,
        download_fn=flaky_download,
        max_retries=3,
    )

    assert calls == 2
    assert "SPY" in bars_by_symbol


def _raw_daily(symbol: str) -> list[RawBar]:
    close = 105.0 if symbol == "SPY" else 210.0
    return [
        RawBar(
            ts="2026-06-15T09:30:00-04:00",
            open=100.0,
            high=106.0,
            low=99.0,
            close=close,
            volume=2_000.0,
        )
    ]


def test_direct_yahoo_download_uses_adjusted_daily_contract_and_normalises_timestamp() -> None:
    calls: list[tuple[str, str, str, bool]] = []

    def fake_fetch(symbol: str, lookback: str, interval: str, *, auto_adjust: bool):
        calls.append((symbol, lookback, interval, auto_adjust))
        return _raw_daily(symbol)

    result = radar_data_impl._download_yahoo_direct(
        ["SPY", "QQQ"],
        fetch_fn=fake_fetch,
        max_retries=1,
        max_workers=2,
    )

    assert sorted(calls) == [
        ("QQQ", "1mo", "1d", True),
        ("SPY", "1mo", "1d", True),
    ]
    assert list(result) == ["SPY", "QQQ"]
    assert result["SPY"][0].ts == "2026-06-15T00:00:00"
    assert result["SPY"][0].close == 105.0


def test_direct_yahoo_download_retries_and_isolates_per_symbol_failures() -> None:
    attempts: dict[str, int] = {}

    def fake_fetch(symbol: str, _lookback: str, _interval: str, *, auto_adjust: bool):
        assert auto_adjust is True
        attempts[symbol] = attempts.get(symbol, 0) + 1
        if symbol == "DEAD" or (symbol == "FLAKY" and attempts[symbol] == 1):
            raise RuntimeError("temporary")
        return _raw_daily(symbol)

    result = radar_data_impl._download_yahoo_direct(
        ["OK", "FLAKY", "DEAD"],
        fetch_fn=fake_fetch,
        max_retries=2,
        max_workers=3,
    )

    assert list(result) == ["OK", "FLAKY"]
    assert attempts == {"OK": 1, "FLAKY": 2, "DEAD": 2}


def test_direct_yahoo_download_bounds_concurrency() -> None:
    barrier = threading.Barrier(2, timeout=1)
    lock = threading.Lock()
    active = 0
    peak = 0

    def fake_fetch(symbol: str, _lookback: str, _interval: str, *, auto_adjust: bool):
        nonlocal active, peak
        assert auto_adjust is True
        with lock:
            active += 1
            peak = max(peak, active)
        try:
            if symbol in {"A", "B"}:
                barrier.wait()
            return _raw_daily(symbol)
        finally:
            with lock:
                active -= 1

    result = radar_data_impl._download_yahoo_direct(
        ["A", "B", "C"],
        fetch_fn=fake_fetch,
        max_retries=1,
        max_workers=2,
    )

    assert list(result) == ["A", "B", "C"]
    assert peak == 2


def test_download_daily_batch_uses_direct_yahoo_by_default_and_keeps_cache_api(
    tmp_path,
    monkeypatch,
) -> None:
    calls: list[tuple[list[str], int, int]] = []

    def fake_direct(symbols: list[str], *, max_retries: int, max_workers: int):
        calls.append((list(symbols), max_retries, max_workers))
        return {symbol: _raw_daily(symbol) for symbol in symbols}

    monkeypatch.setattr(radar_data_impl, "_download_yahoo_direct", fake_direct)

    first = download_daily_batch(
        ["SPY", "QQQ"],
        as_of="2026-06-15",
        cache_dir=tmp_path,
        max_retries=2,
        max_workers=3,
    )
    second = download_daily_batch(
        ["SPY", "QQQ"],
        as_of="2026-06-15",
        cache_dir=tmp_path,
        max_retries=2,
        max_workers=3,
    )

    assert calls == [(["SPY", "QQQ"], 2, 3)]
    assert second == first
    assert (tmp_path / "2026-06-15.json").exists()


def test_download_daily_batch_rejects_nonpositive_worker_count(tmp_path) -> None:
    with pytest.raises(ValueError, match="max_workers_must_be_positive"):
        download_daily_batch(
            ["SPY"],
            as_of="2026-06-15",
            cache_dir=tmp_path,
            max_workers=0,
        )
