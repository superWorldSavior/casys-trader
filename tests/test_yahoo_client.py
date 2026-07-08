"""Garde de la frontière Yahoo v8 : parsing pur + unique juge de validité.

Invariant central : `yahoo_client` TRADUIT (null→NaN) sans jamais filtrer une
barre ; `market_data_yf.get_bars` reste le SEUL endroit qui juge la validité
(close fini et > 0). Les deux responsabilités sont testées séparément.
"""

from __future__ import annotations

import json
import math

import pytest

from trader.domain.market_data import MarketError
from trader.infrastructure.market_sources import market_data_yf as mdy
from trader.infrastructure.market_sources import yahoo_client as yc


def _v8(timestamps, opens, highs, lows, closes, volumes, *, error=None, tz="Europe/Paris"):
    result = None if error else [{
        "timestamp": timestamps,
        "meta": {"exchangeTimezoneName": tz},
        "indicators": {"quote": [{
            "open": opens, "high": highs, "low": lows,
            "close": closes, "volume": volumes,
        }]},
    }]
    return json.dumps({"chart": {"error": error, "result": result}})


# --- yahoo_client : parsing pur, aucun jugement -----------------------------

def test_maps_parallel_arrays_in_chronological_order():
    body = _v8(
        [1704186000, 1704189600],
        [10.0, 11.0], [10.5, 11.5], [9.5, 10.5], [10.2, 11.2], [100, 200],
    )
    bars = yc.fetch_ohlc("ACA.PA", "5d", "1h", http_get=lambda url: body)
    assert [b.close for b in bars] == [10.2, 11.2]
    assert [b.open for b in bars] == [10.0, 11.0]
    assert bars[0].volume == 100.0


def test_exchange_timezone_applied_to_timestamps():
    # 1704186000 = 09:00 UTC = 10:00 Europe/Paris (UTC+1 en janvier) — parité yfinance.
    body = _v8([1704186000], [1], [1], [1], [1], [1], tz="Europe/Paris")
    (bar,) = yc.fetch_ohlc("ACA.PA", "1d", "1h", http_get=lambda url: body)
    assert bar.ts == "2024-01-02T10:00:00+01:00"


def test_dst_offset_is_resolved_per_bar_not_fixed():
    # Même place (Europe/Paris), deux dates de part et d'autre du changement
    # d'heure : l'offset suit le DST automatiquement (ZoneInfo/IANA), il n'est
    # PAS figé. 1704186000 = 02/01 (hiver, +01:00) ; 1719910800 = 02/07 (été, +02:00).
    winter = _v8([1704186000], [1], [1], [1], [1], [1], tz="Europe/Paris")
    summer = _v8([1719910800], [1], [1], [1], [1], [1], tz="Europe/Paris")
    (w,) = yc.fetch_ohlc("ACA.PA", "1d", "1h", http_get=lambda url: winter)
    (s,) = yc.fetch_ohlc("ACA.PA", "1d", "1h", http_get=lambda url: summer)
    assert w.ts.endswith("+01:00")
    assert s.ts.endswith("+02:00")


def test_null_in_the_middle_becomes_nan_and_bar_is_kept():
    # close null au MILIEU : la barre survit, close=NaN (traduction, pas jugement).
    body = _v8([1, 2, 3], [10, 11, 12], [10, 11, 12], [10, 11, 12], [10, None, 12], [1, 1, 1])
    bars = yc.fetch_ohlc("X", "5d", "1h", http_get=lambda url: body)
    assert len(bars) == 3
    assert math.isnan(bars[1].close)
    assert bars[2].close == 12.0


def test_short_parallel_array_yields_nan_not_indexerror():
    # volume plus court que timestamp : index manquant → NaN, jamais d'exception.
    body = _v8([1, 2], [10, 11], [10, 11], [10, 11], [10, 11], [100])
    bars = yc.fetch_ohlc("X", "5d", "1h", http_get=lambda url: body)
    assert math.isnan(bars[1].volume)


def test_chart_error_raises_fetch_failed():
    body = json.dumps({"chart": {"error": {"code": "Not Found"}, "result": None}})
    with pytest.raises(MarketError) as exc:
        yc.fetch_ohlc("NOPE", "5d", "1h", http_get=lambda url: body)
    assert exc.value.code == "fetch_failed"


def test_empty_result_raises_no_data():
    body = json.dumps({"chart": {"error": None, "result": []}})
    with pytest.raises(MarketError) as exc:
        yc.fetch_ohlc("X", "5d", "1h", http_get=lambda url: body)
    assert exc.value.code == "no_data"


def test_result_without_timestamps_raises_no_data():
    body = _v8([], [], [], [], [], [])
    with pytest.raises(MarketError) as exc:
        yc.fetch_ohlc("X", "5d", "1h", http_get=lambda url: body)
    assert exc.value.code == "no_data"


def test_transport_failure_raises_fetch_failed():
    def boom(url):
        raise OSError("connection reset")

    with pytest.raises(MarketError) as exc:
        yc.fetch_ohlc("X", "5d", "1h", http_get=boom)
    assert exc.value.code == "fetch_failed"


def test_unparseable_body_raises_fetch_failed():
    with pytest.raises(MarketError) as exc:
        yc.fetch_ohlc("X", "5d", "1h", http_get=lambda url: "<html>garbage")
    assert exc.value.code == "fetch_failed"


def test_read_interruption_raises_fetch_failed_not_raw():
    # Coupure réseau en cours de lecture : http.client.IncompleteRead n'hérite
    # PAS d'OSError → sans le filet « frontière externe » elle fuirait crue.
    import http.client

    def truncated(url):
        raise http.client.IncompleteRead(b"partial")

    with pytest.raises(MarketError) as exc:
        yc.fetch_ohlc("X", "5d", "1h", http_get=truncated)
    assert exc.value.code == "fetch_failed"


def test_malformed_json_shape_raises_fetch_failed_not_raw():
    # JSON valide mais `chart` n'est pas un dict → `.get` lèverait AttributeError
    # (hors du contrat) sans le filet. Doit devenir fetch_failed.
    body = json.dumps({"chart": ["unexpected"]})
    with pytest.raises(MarketError) as exc:
        yc.fetch_ohlc("X", "5d", "1h", http_get=lambda url: body)
    assert exc.value.code == "fetch_failed"


def test_quote_absent_with_timestamps_yields_all_nan_bars():
    # Contrat explicite : timestamps présents mais `indicators.quote` absent →
    # le client ne juge pas, il émet des barres all-NaN. C'est le métier qui tranche.
    body = json.dumps({"chart": {"error": None, "result": [{
        "timestamp": [1, 2],
        "meta": {"exchangeTimezoneName": "Europe/Paris"},
        "indicators": {},
    }]}})
    bars = yc.fetch_ohlc("X", "5d", "1h", http_get=lambda url: body)
    assert len(bars) == 2
    assert all(math.isnan(b.close) and math.isnan(b.open) for b in bars)


# --- get_bars : la garde métier reste l'UNIQUE juge de validité -------------

def _raw(closes):
    return [
        yc.RawBar(ts=f"2024-01-{i + 1:02d}T00:00:00+00:00", open=1.0, high=1.0, low=1.0, close=c, volume=1.0)
        for i, c in enumerate(closes)
    ]


def test_get_bars_drops_nonpositive_and_nan_close_only():
    raw = _raw([10.0, float("nan"), 0.0, -3.0, 12.0])
    bars = mdy.get_bars("X", "5d", "1d", fetch=lambda s, lb, iv: raw)
    assert [b.close for b in bars] == [10.0, 12.0]


def test_get_bars_all_invalid_raises_no_data():
    raw = _raw([0.0, float("nan")])
    with pytest.raises(MarketError) as exc:
        mdy.get_bars("X", "5d", "1d", fetch=lambda s, lb, iv: raw)
    assert exc.value.code == "no_data"


def test_get_bars_propagates_fetch_marketerror():
    def boom(symbol, lookback, interval):
        raise MarketError("fetch_failed", "boom")

    with pytest.raises(MarketError) as exc:
        mdy.get_bars("X", "5d", "1d", fetch=boom)
    assert exc.value.code == "fetch_failed"
