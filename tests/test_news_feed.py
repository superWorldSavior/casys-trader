from __future__ import annotations

from datetime import datetime, timedelta, timezone

from trader.tools import news_feed as nf

UTC = timezone.utc
NOW = datetime(2026, 6, 23, 12, 0, tzinfo=UTC)


def test_next_future_earnings_picks_soonest_future():
    dates = (
        NOW - timedelta(days=1),       # passée → ignorée
        NOW + timedelta(hours=10),     # future la plus proche
        NOW + timedelta(days=5),
    )
    assert nf._next_future_earnings(NOW, dates) == NOW + timedelta(hours=10)


def test_next_future_earnings_none_when_all_past():
    dates = (NOW - timedelta(days=1), NOW - timedelta(hours=2))
    assert nf._next_future_earnings(NOW, dates) is None


def test_earnings_in_h_rounds_to_two_decimals():
    assert nf._earnings_in_h(NOW, NOW + timedelta(hours=10, minutes=30)) == 10.5


def test_earnings_in_h_none_when_no_date():
    assert nf._earnings_in_h(NOW, None) is None


def test_classify_coverage_unmapped_when_not_mapped():
    raw = nf.RawNews(mapped=False, earnings_dates=(), news_count=0)
    assert nf._classify_coverage(raw) == nf.COVERAGE_UNMAPPED


def test_classify_coverage_empty_when_mapped_no_news():
    raw = nf.RawNews(mapped=True, earnings_dates=(), news_count=0)
    assert nf._classify_coverage(raw) == nf.COVERAGE_EMPTY


def test_classify_coverage_ok_when_mapped_with_news():
    raw = nf.RawNews(mapped=True, earnings_dates=(), news_count=3)
    assert nf._classify_coverage(raw) == nf.COVERAGE_OK


def test_build_snapshot_shape():
    raw = nf.RawNews(
        mapped=True,
        earnings_dates=(NOW + timedelta(hours=24),),
        news_count=2,
    )
    snap = nf.build_snapshot("ACA.PA", now=NOW, raw=raw, source="yahoo")
    assert snap == {
        "earnings_in_h": 24.0,
        "news_coverage": "ok",
        "news_count": 2,
        "source": "yahoo",
        "asof": "2026-06-23T12:00:00+00:00",
    }


# ---------------------------------------------------------------------------
# Task 2 : news_snapshot — orchestration avec fetcher injectable
# ---------------------------------------------------------------------------


def _fake_fetcher(raw=None, exc=None):
    def _f(symbol, *, now):
        if exc is not None:
            raise exc
        return raw
    return _f


def test_news_snapshot_ok_path():
    raw = nf.RawNews(mapped=True, earnings_dates=(), news_count=1)
    snap = nf.news_snapshot("ACA.PA", now=NOW, fetcher=_fake_fetcher(raw=raw))
    assert snap["news_coverage"] == "ok"
    assert snap["source"] == "yahoo"


def test_news_snapshot_empty_distinct_from_unmapped():
    empty = nf.news_snapshot(
        "ACA.PA", now=NOW,
        fetcher=_fake_fetcher(raw=nf.RawNews(mapped=True, earnings_dates=(), news_count=0)),
    )
    unmapped = nf.news_snapshot(
        "ZZZZ.XX", now=NOW,
        fetcher=_fake_fetcher(raw=nf.RawNews(mapped=False, earnings_dates=(), news_count=0)),
    )
    assert empty["news_coverage"] == "empty"
    assert unmapped["news_coverage"] == "unmapped"


def test_news_snapshot_never_raises_on_fetcher_error():
    snap = nf.news_snapshot(
        "ACA.PA", now=NOW, fetcher=_fake_fetcher(exc=RuntimeError("yahoo down")),
    )
    assert snap["news_coverage"] == "error"
    assert snap["source"] == "none"
    assert snap["news_count"] == 0
    assert snap["earnings_in_h"] is None
    assert snap["asof"] == "2026-06-23T12:00:00+00:00"
