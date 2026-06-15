import pytest

from trader.radar_data import CoverageError, download_daily_batch, fetch_daily


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
