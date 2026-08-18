from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from trader.reporting.read_models.intelligence_briefing import (
    load_daily_briefing,
    load_news_feed,
    load_upcoming_earnings,
)


NOW = datetime(2026, 8, 17, 12, tzinfo=UTC)


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _write_jsonl(path: Path, *rows: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row) + "\n" for row in rows),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# load_news_feed — window
# ---------------------------------------------------------------------------


def test_news_feed_includes_items_within_window_and_excludes_outside(
    tmp_path: Path,
) -> None:
    inside_day = (NOW - timedelta(days=2)).date().isoformat()
    outside_day = (NOW - timedelta(days=4)).date().isoformat()

    _write_jsonl(
        tmp_path / "news_items" / f"{inside_day}.jsonl",
        {
            "uuid": "in-1",
            "symbol": "AAPL",
            "title": "Inside window",
            "publisher": "Reuters",
            "published_at": (NOW - timedelta(days=2)).isoformat(),
            "link": "https://example.com/in",
        },
    )
    _write_jsonl(
        tmp_path / "news_items" / f"{outside_day}.jsonl",
        {
            "uuid": "out-1",
            "symbol": "AAPL",
            "title": "Outside window",
            "publisher": "Reuters",
            "published_at": (NOW - timedelta(days=4)).isoformat(),
            "link": "https://example.com/out",
        },
    )

    payload = load_news_feed(tmp_path, now=NOW, window_days=3)
    ids = {item["id"] for item in payload["items"]}

    assert "in-1" in ids
    assert "out-1" not in ids


def test_news_feed_windows_on_fetched_at_for_resurfaced_articles(
    tmp_path: Path,
) -> None:
    """Providers resurface old articles: an item fetched today must stay in
    the feed even when its published_at is months old."""
    today = NOW.date().isoformat()
    _write_jsonl(
        tmp_path / "news_items" / f"{today}.jsonl",
        {
            "uuid": "old-pub-1",
            "symbol": "AAPL",
            "title": "Resurfaced deep dive",
            "publisher": "Reuters",
            "published_at": (NOW - timedelta(days=180)).isoformat(),
            "fetched_at": (NOW - timedelta(hours=2)).isoformat(),
            "link": "https://example.com/old",
        },
        {
            "uuid": "fresh-1",
            "symbol": "AAPL",
            "title": "Fresh headline",
            "publisher": "Reuters",
            "published_at": (NOW - timedelta(hours=1)).isoformat(),
            "fetched_at": (NOW - timedelta(minutes=30)).isoformat(),
            "link": "https://example.com/fresh",
        },
    )

    payload = load_news_feed(tmp_path, now=NOW, window_days=3)
    ids = [item["id"] for item in payload["items"]]

    assert "old-pub-1" in ids
    # Display order still favours recent publication.
    assert ids.index("fresh-1") < ids.index("old-pub-1")


# ---------------------------------------------------------------------------
# load_news_feed — uuid dedup
# ---------------------------------------------------------------------------


def test_news_feed_deduplicates_by_uuid(tmp_path: Path) -> None:
    day = NOW.date().isoformat()
    item = {
        "uuid": "dup-uuid",
        "symbol": "MSFT",
        "title": "Duplicated by uuid",
        "publisher": "Bloomberg",
        "published_at": NOW.isoformat(),
    }
    _write_jsonl(tmp_path / "news_items" / f"{day}.jsonl", item, item)

    payload = load_news_feed(tmp_path, now=NOW)
    matching = [i for i in payload["items"] if i["id"] == "dup-uuid"]

    assert len(matching) == 1


# ---------------------------------------------------------------------------
# load_news_feed — fallback fetched_at
# ---------------------------------------------------------------------------


def test_news_feed_uses_fetched_at_when_no_published_at(tmp_path: Path) -> None:
    day = NOW.date().isoformat()
    _write_jsonl(
        tmp_path / "news_items" / f"{day}.jsonl",
        {
            "uuid": "fallback-ts",
            "symbol": "TSLA",
            "title": "Fallback timestamp item",
            "publisher": "AP",
            "fetched_at": (NOW - timedelta(hours=1)).isoformat(),
        },
    )

    payload = load_news_feed(tmp_path, now=NOW)
    item = next(i for i in payload["items"] if i["id"] == "fallback-ts")

    assert item["published_at"] is not None


# ---------------------------------------------------------------------------
# load_news_feed — symbol filter
# ---------------------------------------------------------------------------


def test_news_feed_filters_by_symbol(tmp_path: Path) -> None:
    day = NOW.date().isoformat()
    _write_jsonl(
        tmp_path / "news_items" / f"{day}.jsonl",
        {
            "uuid": "wanted",
            "symbol": "NVDA",
            "title": "Wanted item",
            "publisher": "FT",
            "published_at": NOW.isoformat(),
        },
        {
            "uuid": "unwanted",
            "symbol": "AAPL",
            "title": "Unwanted item",
            "publisher": "FT",
            "published_at": NOW.isoformat(),
        },
    )

    payload = load_news_feed(tmp_path, now=NOW, symbol="NVDA")
    ids = {item["id"] for item in payload["items"]}

    assert "wanted" in ids
    assert "unwanted" not in ids


# ---------------------------------------------------------------------------
# load_news_feed — geo excluded when symbol filter is active
# ---------------------------------------------------------------------------


def test_news_feed_excludes_geo_when_symbol_filter_active(tmp_path: Path) -> None:
    day = NOW.date().isoformat()
    _write_jsonl(
        tmp_path / "news_items" / f"{day}.jsonl",
        {
            "uuid": "company-1",
            "symbol": "NVDA",
            "title": "Company item",
            "publisher": "Reuters",
            "published_at": NOW.isoformat(),
        },
    )
    _write_jsonl(
        tmp_path / "gdelt" / "events.jsonl",
        {
            "url": "https://geo.example.com/1",
            "title": "Geo event",
            "domain": "geo.example.com",
            "sourcecountry": "US",
            "seendate": NOW.isoformat(),
        },
    )

    payload = load_news_feed(tmp_path, now=NOW, symbol="NVDA")
    kinds = {item["kind"] for item in payload["items"]}

    assert "geo" not in kinds
    assert "company" in kinds


# ---------------------------------------------------------------------------
# load_news_feed — geo included without filter
# ---------------------------------------------------------------------------


def test_news_feed_includes_geo_without_filter(tmp_path: Path) -> None:
    _write_jsonl(
        tmp_path / "gdelt" / "events.jsonl",
        {
            "url": "https://geo.example.com/2",
            "title": "Geo event visible",
            "domain": "geo.example.com",
            "sourcecountry": "DE",
            "seendate": NOW.isoformat(),
        },
    )

    payload = load_news_feed(tmp_path, now=NOW)
    kinds = {item["kind"] for item in payload["items"]}

    assert "geo" in kinds


# ---------------------------------------------------------------------------
# load_daily_briefing — severity sort of top_stories
# ---------------------------------------------------------------------------


def test_briefing_top_stories_sorted_by_severity_then_recency(
    tmp_path: Path,
) -> None:
    # Use distinct story texts so editorial dedup does not merge them.
    digest = {
        "as_of": (NOW - timedelta(hours=1)).isoformat(),
        "regime": "risk-off",
        "rates_bias": "hawkish",
        "usd_bias": "strong",
        "points": [
            {"point": "Unemployment rate in the US rose unexpectedly to five percent.", "severity": "low"},
            {"point": "Federal Reserve signals emergency rate hike to counter inflation spike.", "severity": "high"},
            {"point": "ECB holds deposit rate steady amid eurozone growth concerns.", "severity": "medium"},
        ],
    }
    _write_json(
        tmp_path / "global_situation_digests" / "current.json",
        digest,
    )

    payload = load_daily_briefing(tmp_path, now=NOW)
    stories = payload["top_stories"]

    assert stories[0]["severity"] == "high"
    assert stories[1]["severity"] == "medium"
    assert stories[2]["severity"] == "low"


# ---------------------------------------------------------------------------
# load_daily_briefing — dedup between digest points and brief alerts
# ---------------------------------------------------------------------------


def test_briefing_deduplicates_identical_story_text_across_sources(
    tmp_path: Path,
) -> None:
    shared_text = "Fed holds rates steady"
    digest = {
        "as_of": NOW.isoformat(),
        "points": [{"point": shared_text, "severity": "high"}],
    }
    _write_json(
        tmp_path / "global_situation_digests" / "current.json",
        digest,
    )
    _write_jsonl(
        tmp_path / "news_briefs" / "latest-GLOBAL.jsonl",
        {
            "brief_id": "brief-1",
            "venue": "GLOBAL",
            "as_of": NOW.isoformat(),
            # Same text with different casing / extra space
            "alerts": [{"point": "Fed Holds Rates  Steady", "severity": "high"}],
        },
    )

    payload = load_daily_briefing(tmp_path, now=NOW)
    matching = [s for s in payload["top_stories"] if "fed" in s["point"].lower()]

    assert len(matching) == 1


# ---------------------------------------------------------------------------
# load_daily_briefing — news_brief venue mapping (GLOBAL → null)
# ---------------------------------------------------------------------------


def test_briefing_global_brief_venue_mapped_to_null(tmp_path: Path) -> None:
    _write_jsonl(
        tmp_path / "news_briefs" / "latest-GLOBAL.jsonl",
        {
            "brief_id": "brief-global",
            "venue": "GLOBAL",
            "as_of": NOW.isoformat(),
            "alerts": [{"point": "Global macro alert", "severity": "medium"}],
        },
    )
    _write_jsonl(
        tmp_path / "news_briefs" / "latest-TW.jsonl",
        {
            "brief_id": "brief-tw",
            "venue": "TW",
            "as_of": NOW.isoformat(),
            "alerts": [{"point": "TW specific alert", "severity": "medium"}],
        },
    )

    payload = load_daily_briefing(tmp_path, now=NOW)
    global_story = next(s for s in payload["top_stories"] if "global" in s["point"].lower())
    tw_story = next(s for s in payload["top_stories"] if "tw" in s["point"].lower())

    assert global_story["venue"] is None
    assert tw_story["venue"] == "TW"


# ---------------------------------------------------------------------------
# load_daily_briefing — indicators delta/history
# ---------------------------------------------------------------------------


def test_briefing_indicators_compute_delta_and_bounded_history(
    tmp_path: Path,
) -> None:
    series_dir = tmp_path / "macro_series"
    series_dir.mkdir(parents=True)
    rows = [
        {"ts_collected": "2026-01-01T00:00:00Z", "series_id": "fed_funds_effective", "period": f"2026-{m:02d}", "value": float(m)}
        for m in range(1, 35)  # 34 periods → history capped at 30
    ]
    _write_jsonl(series_dir / "fed_funds_effective.jsonl", *rows)

    payload = load_daily_briefing(tmp_path, now=NOW)
    ind = next(i for i in payload["indicators"] if i["series_id"] == "fed_funds_effective")

    assert ind["value"] == 34.0
    assert ind["previous_value"] == 33.0
    assert abs(ind["delta"] - 1.0) < 1e-6
    assert len(ind["history"]) == 30
    assert ind["label"] == "Fed Funds Rate"
    assert ind["unit"] == "%"


# ---------------------------------------------------------------------------
# load_daily_briefing — period dedup keeps latest ts_collected
# ---------------------------------------------------------------------------


def test_briefing_indicators_period_dedup_keeps_latest_ts_collected(
    tmp_path: Path,
) -> None:
    series_dir = tmp_path / "macro_series"
    series_dir.mkdir(parents=True)
    _write_jsonl(
        series_dir / "gold_usd.jsonl",
        # Two rows for the same period: keep the later ts_collected value
        {"ts_collected": "2026-08-01T00:00:00Z", "period": "2026-08", "value": 1900.0},
        {"ts_collected": "2026-08-10T00:00:00Z", "period": "2026-08", "value": 1950.0},
        {"ts_collected": "2026-08-01T00:00:00Z", "period": "2026-07", "value": 1850.0},
    )

    payload = load_daily_briefing(tmp_path, now=NOW)
    ind = next(i for i in payload["indicators"] if i["series_id"] == "gold_usd")

    assert ind["value"] == 1950.0
    assert ind["previous_value"] == 1850.0


# ---------------------------------------------------------------------------
# load_daily_briefing — corrupt JSON line tolerated
# ---------------------------------------------------------------------------


def test_briefing_tolerates_corrupt_json_lines(tmp_path: Path) -> None:
    series_dir = tmp_path / "macro_series"
    series_dir.mkdir(parents=True)
    path = series_dir / "brent_crude_usd.jsonl"
    path.write_text(
        '{"ts_collected":"2026-08-01T00:00:00Z","period":"2026-08","value":85.0}\n'
        "{broken json\n"
        '{"ts_collected":"2026-08-02T00:00:00Z","period":"2026-07","value":82.0}\n',
        encoding="utf-8",
    )

    payload = load_daily_briefing(tmp_path, now=NOW)
    ind = next(
        (i for i in payload["indicators"] if i["series_id"] == "brent_crude_usd"),
        None,
    )

    assert ind is not None
    assert ind["value"] == 85.0


# ---------------------------------------------------------------------------
# load_daily_briefing — empty state returns valid, empty payload
# ---------------------------------------------------------------------------


def test_briefing_on_empty_state_returns_valid_schema(tmp_path: Path) -> None:
    payload = load_daily_briefing(tmp_path, now=NOW)

    assert "generated_at" in payload
    assert "headline" in payload
    assert "top_stories" in payload
    assert "indicators" in payload
    assert "news" in payload
    assert "counts" in payload
    assert payload["top_stories"] == []
    assert payload["indicators"] == []
    assert payload["news"] == []
    assert payload["headline"]["posture"] is None
    assert payload["headline"]["regime"] is None


# ---------------------------------------------------------------------------
# load_daily_briefing — headline lead = posture rationale
# ---------------------------------------------------------------------------


def test_briefing_headline_lead_equals_posture_rationale(tmp_path: Path) -> None:
    _write_json(
        tmp_path / "global_universe_postures" / "current.json",
        {
            "posture_id": "p-1",
            "as_of": NOW.isoformat(),
            "gross_mode": "cautious",
            "net_bias": "neutral",
            "rationale": "Risk environment elevated.",
            "venue_posture": {"TW": "selective"},
            "family_priority": {"favored": [], "deprioritized": []},
        },
    )

    payload = load_daily_briefing(tmp_path, now=NOW)

    assert payload["headline"]["lead"] == "Risk environment elevated."
    assert payload["headline"]["posture"] is not None
    assert payload["headline"]["posture"]["posture_id"] == "p-1"


# ---------------------------------------------------------------------------
# load_upcoming_earnings — future date within horizon
# ---------------------------------------------------------------------------


def test_earnings_future_date_within_horizon_included(tmp_path: Path) -> None:
    future = (NOW.date() + timedelta(days=30)).isoformat()
    _write_jsonl(
        tmp_path / "fundamental_items" / "AAPL.jsonl",
        {
            "kind": "earnings_calendar",
            "symbol": "AAPL",
            "as_of": NOW.isoformat(),
            "payload": {
                "Earnings Date": [future],
                "Earnings Average": 1.23,
                "Revenue Average": 90_000_000_000.0,
            },
        },
    )

    results = load_upcoming_earnings(tmp_path, now=NOW)
    syms = [r["symbol"] for r in results]

    assert "AAPL" in syms
    r = next(r for r in results if r["symbol"] == "AAPL")
    assert r["earnings_date"] == future
    assert abs(r["eps_consensus"] - 1.23) < 1e-9
    assert not r["stale"]


# ---------------------------------------------------------------------------
# load_upcoming_earnings — past date excluded
# ---------------------------------------------------------------------------


def test_earnings_past_date_excluded(tmp_path: Path) -> None:
    past = (NOW.date() - timedelta(days=5)).isoformat()
    _write_jsonl(
        tmp_path / "fundamental_items" / "MSFT.jsonl",
        {
            "kind": "earnings_calendar",
            "symbol": "MSFT",
            "as_of": NOW.isoformat(),
            "payload": {"Earnings Date": [past], "Earnings Average": None, "Revenue Average": None},
        },
    )

    results = load_upcoming_earnings(tmp_path, now=NOW)
    syms = [r["symbol"] for r in results]

    assert "MSFT" not in syms


# ---------------------------------------------------------------------------
# load_upcoming_earnings — beyond horizon excluded
# ---------------------------------------------------------------------------


def test_earnings_beyond_horizon_excluded(tmp_path: Path) -> None:
    far_future = (NOW.date() + timedelta(days=200)).isoformat()
    _write_jsonl(
        tmp_path / "fundamental_items" / "GOOG.jsonl",
        {
            "kind": "earnings_calendar",
            "symbol": "GOOG",
            "as_of": NOW.isoformat(),
            "payload": {"Earnings Date": [far_future], "Earnings Average": None, "Revenue Average": None},
        },
    )

    results = load_upcoming_earnings(tmp_path, now=NOW, horizon_days=90)
    syms = [r["symbol"] for r in results]

    assert "GOOG" not in syms


# ---------------------------------------------------------------------------
# load_upcoming_earnings — picks first future date, not [0]
# ---------------------------------------------------------------------------


def test_earnings_picks_first_future_date_not_first_element(tmp_path: Path) -> None:
    past = (NOW.date() - timedelta(days=10)).isoformat()
    future = (NOW.date() + timedelta(days=20)).isoformat()
    _write_jsonl(
        tmp_path / "fundamental_items" / "TSLA.jsonl",
        {
            "kind": "earnings_calendar",
            "symbol": "TSLA",
            "as_of": NOW.isoformat(),
            "payload": {
                "Earnings Date": [past, future],  # first element is past
                "Earnings Average": 0.5,
                "Revenue Average": None,
            },
        },
    )

    results = load_upcoming_earnings(tmp_path, now=NOW)
    r = next((r for r in results if r["symbol"] == "TSLA"), None)

    assert r is not None, "TSLA should appear (future date exists)"
    assert r["earnings_date"] == future, "Must pick the first FUTURE date, not list[0]"


# ---------------------------------------------------------------------------
# load_upcoming_earnings — empty date list excluded
# ---------------------------------------------------------------------------


def test_earnings_empty_date_list_excluded(tmp_path: Path) -> None:
    _write_jsonl(
        tmp_path / "fundamental_items" / "NVDA.jsonl",
        {
            "kind": "earnings_calendar",
            "symbol": "NVDA",
            "as_of": NOW.isoformat(),
            "payload": {"Earnings Date": [], "Earnings Average": None, "Revenue Average": None},
        },
    )

    results = load_upcoming_earnings(tmp_path, now=NOW)
    syms = [r["symbol"] for r in results]

    assert "NVDA" not in syms


# ---------------------------------------------------------------------------
# load_upcoming_earnings — stale flag when as_of is old
# ---------------------------------------------------------------------------


def test_earnings_stale_flag_when_as_of_old(tmp_path: Path) -> None:
    future = (NOW.date() + timedelta(days=15)).isoformat()
    old_as_of = (NOW - timedelta(days=20)).isoformat()  # older than default stale_days=14
    _write_jsonl(
        tmp_path / "fundamental_items" / "META.jsonl",
        {
            "kind": "earnings_calendar",
            "symbol": "META",
            "as_of": old_as_of,
            "payload": {"Earnings Date": [future], "Earnings Average": None, "Revenue Average": None},
        },
    )

    results = load_upcoming_earnings(tmp_path, now=NOW)
    r = next((r for r in results if r["symbol"] == "META"), None)

    assert r is not None
    assert r["stale"] is True


# ---------------------------------------------------------------------------
# load_upcoming_earnings — not stale when as_of is recent
# ---------------------------------------------------------------------------


def test_earnings_not_stale_when_as_of_recent(tmp_path: Path) -> None:
    future = (NOW.date() + timedelta(days=15)).isoformat()
    recent_as_of = (NOW - timedelta(days=3)).isoformat()
    _write_jsonl(
        tmp_path / "fundamental_items" / "AMZN.jsonl",
        {
            "kind": "earnings_calendar",
            "symbol": "AMZN",
            "as_of": recent_as_of,
            "payload": {"Earnings Date": [future], "Earnings Average": None, "Revenue Average": None},
        },
    )

    results = load_upcoming_earnings(tmp_path, now=NOW)
    r = next((r for r in results if r["symbol"] == "AMZN"), None)

    assert r is not None
    assert r["stale"] is False


# ---------------------------------------------------------------------------
# load_upcoming_earnings — uses max(as_of) per symbol across multiple entries
# ---------------------------------------------------------------------------


def test_earnings_uses_latest_as_of_per_symbol(tmp_path: Path) -> None:
    future1 = (NOW.date() + timedelta(days=10)).isoformat()
    future2 = (NOW.date() + timedelta(days=25)).isoformat()
    old_as_of = (NOW - timedelta(days=5)).isoformat()
    new_as_of = (NOW - timedelta(days=1)).isoformat()
    _write_jsonl(
        tmp_path / "fundamental_items" / "IBM.jsonl",
        # Older entry has date1, newer entry has date2 — must use newer.
        {
            "kind": "earnings_calendar",
            "symbol": "IBM",
            "as_of": old_as_of,
            "payload": {"Earnings Date": [future1], "Earnings Average": None, "Revenue Average": None},
        },
        {
            "kind": "earnings_calendar",
            "symbol": "IBM",
            "as_of": new_as_of,
            "payload": {"Earnings Date": [future2], "Earnings Average": None, "Revenue Average": None},
        },
    )

    results = load_upcoming_earnings(tmp_path, now=NOW)
    r = next((r for r in results if r["symbol"] == "IBM"), None)

    assert r is not None
    assert r["earnings_date"] == future2, "Should use the entry with the most recent as_of"


# ---------------------------------------------------------------------------
# load_upcoming_earnings — sorted ascending by earnings_date
# ---------------------------------------------------------------------------


def test_earnings_sorted_ascending_by_earnings_date(tmp_path: Path) -> None:
    d1 = (NOW.date() + timedelta(days=5)).isoformat()
    d2 = (NOW.date() + timedelta(days=30)).isoformat()
    d3 = (NOW.date() + timedelta(days=15)).isoformat()
    for sym, d in [("SYM_A", d2), ("SYM_B", d3), ("SYM_C", d1)]:
        _write_jsonl(
            tmp_path / "fundamental_items" / f"{sym}.jsonl",
            {
                "kind": "earnings_calendar",
                "symbol": sym,
                "as_of": NOW.isoformat(),
                "payload": {"Earnings Date": [d], "Earnings Average": None, "Revenue Average": None},
            },
        )

    results = load_upcoming_earnings(tmp_path, now=NOW)
    dates = [r["earnings_date"] for r in results]

    assert dates == sorted(dates), "Results must be sorted ascending by earnings_date"


# ---------------------------------------------------------------------------
# Editorial dedup — first 8 words match
# ---------------------------------------------------------------------------


def test_briefing_editorial_dedup_first_8_words(tmp_path: Path) -> None:
    """Two alerts sharing the first 8 words must be deduplicated."""
    common_prefix = "European energy sector faces prolonged uncertainty as regulation tightens"
    _write_jsonl(
        tmp_path / "news_briefs" / "latest-EU.jsonl",
        {
            "as_of": NOW.isoformat(),
            "alerts": [
                {
                    "point": f"{common_prefix} across member states.",
                    "severity": "watch",
                },
                {
                    "point": f"{common_prefix} over the coming quarter.",
                    "severity": "info",
                },
            ],
        },
    )

    payload = load_daily_briefing(tmp_path, now=NOW)
    matching = [s for s in payload["top_stories"] if "european energy sector" in s["point"].lower()]

    assert len(matching) == 1, "Quasi-duplicate (first 8 words) should be collapsed to one story"


# ---------------------------------------------------------------------------
# Editorial dedup — Jaccard ≥ 0.6 keeps more severe
# ---------------------------------------------------------------------------


def test_briefing_editorial_dedup_jaccard_keeps_more_severe(tmp_path: Path) -> None:
    """When two stories are Jaccard quasi-duplicates (bigrams ≥ 0.6), the more severe one is kept.

    Both stories share the same opening clause; S2 appends a brief recession qualifier.
    Bigram Jaccard ≈ 0.73 — well above the 0.6 threshold.
    """
    _write_jsonl(
        tmp_path / "news_briefs" / "latest-GLOBAL.jsonl",
        {
            "as_of": NOW.isoformat(),
            "alerts": [
                # severity=info comes first in the raw feed
                {
                    "point": (
                        "Federal Reserve prolonged tightening cycle squeezing corporate credit globally."
                    ),
                    "severity": "info",
                },
                # severity=risk comes second but is more severe
                {
                    "point": (
                        "Federal Reserve prolonged tightening cycle squeezing corporate credit "
                        "globally with recession risk."
                    ),
                    "severity": "risk",
                },
            ],
        },
    )

    payload = load_daily_briefing(tmp_path, now=NOW)
    # After severity sort + dedup, only the risk one should remain.
    fed_stories = [s for s in payload["top_stories"] if "federal reserve" in s["point"].lower()]

    assert len(fed_stories) == 1
    assert fed_stories[0]["severity"] == "risk", "More severe story must win the dedup"


# ---------------------------------------------------------------------------
# is_operational filter
# ---------------------------------------------------------------------------


def test_briefing_excludes_is_operational_stories(tmp_path: Path) -> None:
    _write_jsonl(
        tmp_path / "news_briefs" / "latest-GLOBAL.jsonl",
        {
            "as_of": NOW.isoformat(),
            "alerts": [
                {
                    "point": "Market data pipeline stalled on bridge process restart.",
                    "severity": "watch",
                    "is_operational": True,
                },
                {
                    "point": "Equity volatility expectations rise ahead of CPI release.",
                    "severity": "watch",
                    "is_operational": False,
                },
            ],
        },
    )

    payload = load_daily_briefing(tmp_path, now=NOW)
    points = [s["point"] for s in payload["top_stories"]]

    assert not any("pipeline" in p for p in points), "is_operational story must be excluded"
    assert any("volatility" in p for p in points), "Non-operational story must be included"


# ---------------------------------------------------------------------------
# posture_history — 30-day window
# ---------------------------------------------------------------------------


def test_posture_history_30d_window(tmp_path: Path) -> None:
    inside_date = (NOW - timedelta(days=20)).date().isoformat()
    outside_date = (NOW - timedelta(days=40)).date().isoformat()

    for d in [inside_date, outside_date]:
        _write_jsonl(
            tmp_path / "global_universe_postures" / f"{d}.jsonl",
            {"as_of": f"{d}T12:00:00+00:00", "gross_mode": "cautious", "net_bias": "neutral"},
        )

    payload = load_daily_briefing(tmp_path, now=NOW)
    history = payload["posture_history"]
    as_ofs = [p["as_of"] for p in history]

    assert any(inside_date in a for a in as_ofs), "Inside-window date must be included"
    assert not any(outside_date in a for a in as_ofs), "Outside-window date must be excluded"


# ---------------------------------------------------------------------------
# posture_history — joins regime from matching digest
# ---------------------------------------------------------------------------


def test_posture_history_joins_regime_from_digest(tmp_path: Path) -> None:
    d = (NOW - timedelta(days=5)).date().isoformat()
    _write_jsonl(
        tmp_path / "global_universe_postures" / f"{d}.jsonl",
        {"as_of": f"{d}T12:00:00+00:00", "gross_mode": "favor", "net_bias": "long"},
    )
    _write_jsonl(
        tmp_path / "global_situation_digests" / f"{d}.jsonl",
        {"as_of": f"{d}T12:00:00+00:00", "regime": "risk_on", "rates_bias": "dovish"},
    )

    payload = load_daily_briefing(tmp_path, now=NOW)
    history = payload["posture_history"]
    entry = next((p for p in history if "favor" == p.get("gross_mode")), None)

    assert entry is not None
    assert entry["regime"] == "risk_on"
    assert entry["net_bias"] == "long"


# ---------------------------------------------------------------------------
# posture_history — sorted ascending
# ---------------------------------------------------------------------------


def test_posture_history_sorted_ascending(tmp_path: Path) -> None:
    dates = [(NOW - timedelta(days=d)).date().isoformat() for d in [10, 5, 15, 2]]
    for d in dates:
        _write_jsonl(
            tmp_path / "global_universe_postures" / f"{d}.jsonl",
            {"as_of": f"{d}T12:00:00+00:00", "gross_mode": "cautious", "net_bias": "neutral"},
        )

    payload = load_daily_briefing(tmp_path, now=NOW)
    history = payload["posture_history"]
    as_ofs = [p["as_of"] for p in history]

    assert as_ofs == sorted(as_ofs), "posture_history must be sorted ascending by as_of"


# ---------------------------------------------------------------------------
# load_daily_briefing — earnings and posture_history in payload
# ---------------------------------------------------------------------------


def test_briefing_includes_earnings_and_posture_history_keys(tmp_path: Path) -> None:
    """load_daily_briefing must always include the earnings and posture_history keys."""
    payload = load_daily_briefing(tmp_path, now=NOW)

    assert "earnings" in payload, "earnings key must be present"
    assert "posture_history" in payload, "posture_history key must be present"
    assert isinstance(payload["earnings"], list)
    assert isinstance(payload["posture_history"], list)


def test_briefing_earnings_populated_from_fundamental_items(tmp_path: Path) -> None:
    future = (NOW.date() + timedelta(days=45)).isoformat()
    _write_jsonl(
        tmp_path / "fundamental_items" / "SNAP.jsonl",
        {
            "kind": "earnings_calendar",
            "symbol": "SNAP",
            "as_of": NOW.isoformat(),
            "payload": {
                "Earnings Date": [future],
                "Earnings Average": 0.05,
                "Revenue Average": 1_200_000_000.0,
            },
        },
    )

    payload = load_daily_briefing(tmp_path, now=NOW)
    syms = [e["symbol"] for e in payload["earnings"]]

    assert "SNAP" in syms


# ---------------------------------------------------------------------------
# Earnings — inclure la date du JOUR (d >= today)
# ---------------------------------------------------------------------------


def test_earnings_includes_today(tmp_path: Path) -> None:
    """Une date d'earnings égale à aujourd'hui doit être incluse (>= today)."""
    today = NOW.date().isoformat()
    _write_jsonl(
        tmp_path / "fundamental_items" / "ORCL.jsonl",
        {
            "kind": "earnings_calendar",
            "symbol": "ORCL",
            "as_of": NOW.isoformat(),
            "payload": {"Earnings Date": [today], "Earnings Average": None, "Revenue Average": None},
        },
    )

    results = load_upcoming_earnings(tmp_path, now=NOW)
    syms = [r["symbol"] for r in results]

    assert "ORCL" in syms, "La date du jour doit être incluse dans Coming up"


def test_earnings_picks_min_of_multiple_candidates(tmp_path: Path) -> None:
    """Quand la liste contient plusieurs dates futures dans la fenêtre (non triées),
    on prend la plus proche (min), pas la première de la liste."""
    further = (NOW.date() + timedelta(days=40)).isoformat()
    closer = (NOW.date() + timedelta(days=10)).isoformat()
    _write_jsonl(
        tmp_path / "fundamental_items" / "COST.jsonl",
        {
            "kind": "earnings_calendar",
            "symbol": "COST",
            "as_of": NOW.isoformat(),
            "payload": {
                # further vient en premier — on veut quand même closer
                "Earnings Date": [further, closer],
                "Earnings Average": None,
                "Revenue Average": None,
            },
        },
    )

    results = load_upcoming_earnings(tmp_path, now=NOW)
    r = next((r for r in results if r["symbol"] == "COST"), None)

    assert r is not None
    assert r["earnings_date"] == closer, "Doit retourner la date minimale (plus proche), pas la première"


# ---------------------------------------------------------------------------
# Editorial dedup — bigrammes : les antonymes rise/fall ne fusionnent PAS
# ---------------------------------------------------------------------------


def test_editorial_dedup_antonyms_not_merged(tmp_path: Path) -> None:
    """'Oil prices rise as OPEC holds output steady' et '...fall as...' partagent
    beaucoup de tokens unigrams mais diffèrent sur les bigrammes autour de l'antonyme.

    Bigram Jaccard ≈ 0.56 < 0.6 → les deux phrases NE doivent PAS être fusionnées.

    Note: les phrases doivent rester courtes pour que l'antonyme représente une
    fraction significative des bigrammes. Des phrases trop longues dilueraient
    l'effet et feraient remonter le Jaccard au-dessus du seuil.
    """
    _write_jsonl(
        tmp_path / "news_briefs" / "latest-GLOBAL.jsonl",
        {
            "as_of": NOW.isoformat(),
            "alerts": [
                {
                    "point": "Oil prices rise as OPEC holds production output steady.",
                    "severity": "watch",
                },
                {
                    "point": "Oil prices fall as OPEC holds production output steady.",
                    "severity": "info",
                },
            ],
        },
    )

    payload = load_daily_briefing(tmp_path, now=NOW)
    oil_stories = [s for s in payload["top_stories"] if "oil prices" in s["point"].lower()]

    assert len(oil_stories) == 2, (
        "Les deux phrases (rise vs fall) sont des antonymes — bigramme Jaccard < 0.6 "
        "→ ne doivent PAS être fusionnées"
    )


def test_editorial_dedup_near_identical_still_merged(tmp_path: Path) -> None:
    """Deux reformulations quasi-identiques (un mot inséré) fusionnent toujours.

    Bigram Jaccard ≈ 0.67 > 0.6 → fusionné, la plus sévère est conservée.
    """
    _write_jsonl(
        tmp_path / "news_briefs" / "latest-GLOBAL.jsonl",
        {
            "as_of": NOW.isoformat(),
            "alerts": [
                {
                    "point": "Global semiconductor supply chain tightening amid geopolitical tensions.",
                    "severity": "watch",
                },
                {
                    "point": "Global semiconductor supply chain tightening amid rising geopolitical tensions.",
                    "severity": "info",
                },
            ],
        },
    )

    payload = load_daily_briefing(tmp_path, now=NOW)
    semi_stories = [s for s in payload["top_stories"] if "semiconductor" in s["point"].lower()]

    assert len(semi_stories) == 1, (
        "Les quasi-reformulations (bigramme Jaccard > 0.6) doivent toujours être fusionnées"
    )


# ---------------------------------------------------------------------------
# Indicators — superseded series (cpi_us_all_items) not displayed
# ---------------------------------------------------------------------------


def test_briefing_superseded_series_not_in_indicators(tmp_path: Path) -> None:
    """L'ancienne série cpi_us_all_items (superseded) ne doit plus apparaître dans
    les indicators — même si le fichier historique est encore présent sur disque."""
    series_dir = tmp_path / "macro_series"
    series_dir.mkdir(parents=True)
    # Ancienne série encore sur disque
    _write_jsonl(
        series_dir / "cpi_us_all_items.jsonl",
        {"ts_collected": "2025-01-01T00:00:00Z", "series_id": "BLS/cu/CUSR0000SA0",
         "period": "2025-01", "value": 319.0},
    )
    # Nouvelle série (IMF)
    _write_jsonl(
        series_dir / "cpi_us_imf.jsonl",
        {"ts_collected": "2026-08-01T00:00:00Z", "series_id": "IMF/CPI/M.US.PCPI_IX",
         "period": "2026-07", "value": 148.3},
    )

    payload = load_daily_briefing(tmp_path, now=NOW)
    series_ids = [i["series_id"] for i in payload["indicators"]]

    assert "cpi_us_all_items" not in series_ids, "La série superseded ne doit plus apparaître"
    assert "cpi_us_imf" in series_ids, "La nouvelle série IMF doit apparaître"
