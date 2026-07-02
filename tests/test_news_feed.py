from __future__ import annotations

import sys
import types
from datetime import datetime, timedelta, timezone

import pytest

from trader.tools import news_feed as nf

UTC = timezone.utc
NOW = datetime(2026, 6, 23, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _clear_news_cache():
    """Vide le cache module-level avant chaque test (isolation)."""
    nf.reset_cache()
    yield
    nf.reset_cache()


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


# ---------------------------------------------------------------------------
# Task 3 : cache TTL déterministe basé sur `now`
# ---------------------------------------------------------------------------


class _CountingFetcher:
    def __init__(self, raw):
        self.raw = raw
        self.calls = 0

    def __call__(self, symbol, *, now):
        self.calls += 1
        return self.raw


def test_cache_hit_within_ttl_does_not_refetch():
    nf.reset_cache()
    fetcher = _CountingFetcher(nf.RawNews(mapped=True, earnings_dates=(), news_count=1))
    nf.news_snapshot("ACA.PA", now=NOW, fetcher=fetcher)
    nf.news_snapshot("ACA.PA", now=NOW + timedelta(minutes=10), fetcher=fetcher)
    assert fetcher.calls == 1


def test_cache_expires_after_ttl():
    nf.reset_cache()
    fetcher = _CountingFetcher(nf.RawNews(mapped=True, earnings_dates=(), news_count=1))
    nf.news_snapshot("ACA.PA", now=NOW, fetcher=fetcher)
    nf.news_snapshot("ACA.PA", now=NOW + timedelta(minutes=31), fetcher=fetcher)
    assert fetcher.calls == 2


def test_error_snapshot_not_cached():
    nf.reset_cache()
    calls = {"n": 0}

    def flaky(symbol, *, now):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("down")
        return nf.RawNews(mapped=True, earnings_dates=(), news_count=2)

    first = nf.news_snapshot("ACA.PA", now=NOW, fetcher=flaky)
    second = nf.news_snapshot("ACA.PA", now=NOW + timedelta(minutes=1), fetcher=flaky)
    assert first["news_coverage"] == "error"
    assert second["news_coverage"] == "ok"  # pas servi depuis le cache
    assert calls["n"] == 2


# ---------------------------------------------------------------------------
# Task 4 : _yahoo_fetch — frontière impure (yfinance mocké)
# ---------------------------------------------------------------------------


def _install_fake_yfinance(monkeypatch, *, last_price, earnings_index, news):
    """Installe un mock yfinance réaliste.

    `_FastInfo` expose l'accès par index `["key"]` (comme yf.FastInfo en prod)
    MAIS ne supporte PAS `.get()` — appeler `.get()` lève AttributeError pour
    coller au comportement yfinance réel.
    """
    fake = types.ModuleType("yfinance")

    class _FastInfo:
        """Simule yf.FastInfo : index OK, .get() cassé comme en production."""
        def __init__(self, data):
            self._data = data

        def __getitem__(self, key):
            return self._data[key]

        def get(self, key, default=None):
            raise AttributeError(
                "_FastInfo.get() not supported — use index access"
            )

    class _Ticker:
        def __init__(self, symbol):
            self.symbol = symbol

        @property
        def fast_info(self):
            return _FastInfo({"last_price": last_price})

        def get_earnings_dates(self, limit=12):
            class _DF:
                def __init__(self, idx):
                    self.index = idx
            return _DF(earnings_index)

        @property
        def news(self):
            return news

    fake.Ticker = _Ticker
    monkeypatch.setitem(sys.modules, "yfinance", fake)


def test_yahoo_fetch_maps_and_counts(monkeypatch):
    import pandas as pd
    idx = [pd.Timestamp("2026-06-24T08:00:00Z")]
    news = [
        {"providerPublishTime": int((NOW - timedelta(days=1)).timestamp())},  # dans la fenêtre
        {"providerPublishTime": int((NOW - timedelta(days=30)).timestamp())},  # hors fenêtre
    ]
    _install_fake_yfinance(monkeypatch, last_price=12.3, earnings_index=idx, news=news)
    raw = nf._yahoo_fetch("ACA.PA", now=NOW)
    assert raw.mapped is True
    assert raw.news_count == 1
    assert len(raw.earnings_dates) == 1


def test_yahoo_fetch_unmapped_when_no_price(monkeypatch):
    _install_fake_yfinance(monkeypatch, last_price=None, earnings_index=[], news=[])
    raw = nf._yahoo_fetch("ZZZZ.XX", now=NOW)
    assert raw.mapped is False


# ---------------------------------------------------------------------------
# Bug regression : fast_info.get() cassé en prod → faux-négatif unmapped
# ---------------------------------------------------------------------------


def test_yahoo_fetch_mapped_when_news_but_no_price(monkeypatch):
    """Régression : un symbole avec des news mais sans prix doit être mapped=True.

    Avant le fix : le code utilisait fast_info.get("last_price") qui lève
    AttributeError sur le vrai yf.FastInfo → mapped restait False → coverage
    "unmapped" au lieu de "ok". Le mock reflète désormais ce comportement cassé.
    """
    news = [
        {"providerPublishTime": int((NOW - timedelta(days=1)).timestamp())},
        {"providerPublishTime": int((NOW - timedelta(days=2)).timestamp())},
    ]
    _install_fake_yfinance(monkeypatch, last_price=None, earnings_index=[], news=news)
    raw = nf._yahoo_fetch("ACA.PA", now=NOW)
    assert raw.mapped is True, "un symbole avec des news doit être mapped=True"
    assert raw.news_count == 2
    snap = nf.news_snapshot("ACA.PA", now=NOW)
    assert snap["news_coverage"] == "ok"


def test_yahoo_fetch_mapped_when_earnings_but_no_price_no_news(monkeypatch):
    """Un symbole avec earnings mais sans prix ni news doit être mapped=True (empty)."""
    import pandas as pd
    idx = [pd.Timestamp("2026-07-01T08:00:00Z")]
    _install_fake_yfinance(monkeypatch, last_price=None, earnings_index=idx, news=[])
    raw = nf._yahoo_fetch("MSFT", now=NOW)
    assert raw.mapped is True, "un symbole avec earnings doit être mapped=True"
    assert raw.news_count == 0
    snap = nf.news_snapshot("MSFT", now=NOW)
    assert snap["news_coverage"] == "empty"


# ---------------------------------------------------------------------------
# Fix A : _news_published_at — parsing des dates de news yfinance ≥1.4
# ---------------------------------------------------------------------------


def test_news_published_at_reads_content_pubdate_iso_with_z():
    """yfinance ≥1.4 : content.pubDate au format ISO avec 'Z' → datetime UTC."""
    item = {"content": {"pubDate": "2026-06-22T10:30:00Z"}}
    dt = nf._news_published_at(item)
    assert dt is not None
    assert dt.tzinfo is not None
    assert dt.year == 2026 and dt.month == 6 and dt.day == 22
    assert dt.hour == 10 and dt.minute == 30


def test_news_published_at_reads_content_pubdate_iso_with_offset():
    """yfinance ≥1.4 : content.pubDate avec offset explicite → datetime tz-aware."""
    item = {"content": {"pubDate": "2026-06-22T10:30:00+02:00"}}
    dt = nf._news_published_at(item)
    assert dt is not None
    assert dt.tzinfo is not None
    # L'instant doit être identique quel que soit la représentation de l'offset :
    # 2026-06-22T10:30:00+02:00 == 2026-06-22T08:30:00Z
    from datetime import timezone
    dt_utc = dt.astimezone(timezone.utc)
    assert dt_utc.hour == 8 and dt_utc.minute == 30


def test_news_published_at_fallback_legacy_epoch():
    """Legacy : providerPublishTime (epoch int) → datetime UTC."""
    epoch = int(NOW.timestamp())
    item = {"providerPublishTime": epoch}
    dt = nf._news_published_at(item)
    assert dt is not None
    assert dt.tzinfo is not None
    assert abs((dt - NOW).total_seconds()) < 1


def test_news_published_at_returns_none_when_no_date():
    """Aucune date exploitable → None."""
    item = {"title": "no date here"}
    dt = nf._news_published_at(item)
    assert dt is None


def test_news_published_at_content_not_dict_falls_to_legacy():
    """content est une string (mauvais format) → pas d'erreur, fallback epoch."""
    epoch = int(NOW.timestamp())
    item = {"content": "some string", "providerPublishTime": epoch}
    dt = nf._news_published_at(item)
    assert dt is not None


def test_yahoo_fetch_window_filter_with_content_pubdate(monkeypatch):
    """Intégration : item avec content.pubDate récent compté, vieux exclu.

    Un item dans la fenêtre 7j + un item hors fenêtre → news_count == 1.
    """
    recent = NOW - timedelta(days=1)
    old = NOW - timedelta(days=30)
    news = [
        {"content": {"pubDate": recent.isoformat().replace("+00:00", "Z")}},  # dans la fenêtre
        {"content": {"pubDate": old.isoformat().replace("+00:00", "Z")}},      # hors fenêtre
    ]
    _install_fake_yfinance(monkeypatch, last_price=12.3, earnings_index=[], news=news)
    raw = nf._yahoo_fetch("ACA.PA", now=NOW)
    assert raw.news_count == 1, f"attendu 1, obtenu {raw.news_count}"


def test_yahoo_fetch_window_filter_legacy_epoch_still_works(monkeypatch):
    """Régression : le format legacy epoch continue de fonctionner après le fix."""
    news = [
        {"providerPublishTime": int((NOW - timedelta(days=1)).timestamp())},   # dans la fenêtre
        {"providerPublishTime": int((NOW - timedelta(days=30)).timestamp())},   # hors fenêtre
    ]
    _install_fake_yfinance(monkeypatch, last_price=12.3, earnings_index=[], news=news)
    raw = nf._yahoo_fetch("ACA.PA", now=NOW)
    assert raw.news_count == 1
