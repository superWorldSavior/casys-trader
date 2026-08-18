"""Read-only intelligence briefing and news feed for the browser desk.

Two projections:
- load_news_feed      : bounded news feed (company items + optional geo).
- load_daily_briefing : headline posture/digest + top stories + macro-series
                        indicators + recent news.

Never writes.  All sources are append-only JSONL ledgers or static JSON files.
Tolerates missing directories, corrupt JSON lines, and absent state gracefully.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from trader.infrastructure.state_db._jsonl_store import (
    read_json_object,
    read_jsonl_objects,
)


# ---------------------------------------------------------------------------
# Local helpers (mirror the style of intelligence_timeline.py)
# ---------------------------------------------------------------------------


def _dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, (list, tuple)) else []


def _text(value: Any) -> str:
    return str(value or "").strip()


def _parse_ts(value: Any) -> datetime | None:
    text = _text(value)
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


# ---------------------------------------------------------------------------
# Macro-series label/unit mapping
# Values sourced from the actual DBnomics series:
# - rate series (fed_funds, ecb, unemployment) are percentages.
# - CPI / HICP are price-level *indices*, not year-over-year changes.
# - commodity prices are USD per unit.
# ---------------------------------------------------------------------------

_SERIES_META: dict[str, tuple[str, str | None]] = {
    "fed_funds_effective":  ("Fed Funds Rate",        "%"),
    "ecb_deposit_rate":     ("ECB Deposit Rate",      "%"),
    "cpi_us_all_items":     ("US CPI",                "index"),  # legacy label kept for mapping
    "cpi_us_imf":           ("US CPI",                "index"),
    "hicp_euro_area":       ("Euro Area HICP",        "index"),
    "brent_crude_usd":      ("Brent Crude",           "USD/bbl"),
    "gold_usd":             ("Gold",                  "USD/oz"),
    "unemployment_rate_us": ("US Unemployment Rate",  "%"),
}

# Series replaced by a newer entry — still on disk but no longer collected.
# Skip them in the indicators projection to avoid stale/duplicate display.
_SUPERSEDED_SERIES: set[str] = {"cpi_us_all_items"}

# ---------------------------------------------------------------------------
# Severity ordering.  The daemon vocabulary is risk > watch > info
# (see state/global_situation_digests and state/news_briefs alerts);
# high/medium/low kept as synonyms in case a producer emits them.
# ---------------------------------------------------------------------------

_SEVERITY_ORDER: dict[str, int] = {
    "risk": 0,
    "high": 0,
    "watch": 1,
    "medium": 1,
    "info": 2,
    "low": 2,
}


def _severity_key(row: Mapping[str, Any]) -> int:
    return _SEVERITY_ORDER.get(_text(row.get("severity")).lower(), 3)


# ---------------------------------------------------------------------------
# News feed
# ---------------------------------------------------------------------------


def load_news_feed(
    state_dir: str | Path,
    *,
    now: datetime,
    venue: str | None = None,
    symbol: str | None = None,
    limit: int = 60,
    window_days: int = 3,
    company_names: dict[str, str] | None = None,
    symbol_venues: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Return a bounded, deduplicated news feed.

    Company items come from state/news_items/YYYY-MM-DD.jsonl files within
    [now - window_days, now].  Geo items come from state/gdelt/events.jsonl
    and are included only when no venue/symbol filter is active.
    """

    root = Path(state_dir)
    company_names = company_names or {}
    symbol_venues = symbol_venues or {}
    now_utc = now.astimezone(UTC)
    threshold = now_utc - timedelta(days=max(0, window_days))

    wanted_venue = _text(venue).upper() or None
    wanted_symbol = _text(symbol).upper() or None

    # ------------------------------------------------------------------ company
    company_items: list[dict[str, Any]] = []
    seen_uuid: set[str] = set()
    seen_title_pub: set[tuple[str, str]] = set()

    for offset in range(max(0, window_days) + 1):
        day = (now_utc - timedelta(days=offset)).date().isoformat()
        path = root / "news_items" / f"{day}.jsonl"
        for row in read_jsonl_objects(path):
            sym = _text(row.get("symbol")).upper()
            if wanted_symbol and sym != wanted_symbol:
                continue
            item_venue = symbol_venues.get(sym) or None
            if wanted_venue and item_venue != wanted_venue:
                continue
            # Display timestamp: published_at first, fallback fetched_at.
            ts = _parse_ts(row.get("published_at")) or _parse_ts(row.get("fetched_at"))
            # Window on collection time: providers resurface old articles,
            # so an item fetched today stays in the feed even when its
            # published_at is months old (it just sorts lower).
            window_ts = _parse_ts(row.get("fetched_at")) or ts
            if window_ts and window_ts > now_utc:
                continue
            if window_ts and window_ts < threshold:
                continue
            # Dedup: first by uuid, then by (title, publisher)
            uid = _text(row.get("uuid"))
            title = _text(row.get("title"))
            publisher = _text(row.get("publisher"))
            tp_key = (title.casefold(), publisher.casefold())
            if uid and uid in seen_uuid:
                continue
            if tp_key in seen_title_pub:
                continue
            if uid:
                seen_uuid.add(uid)
            seen_title_pub.add(tp_key)
            company_items.append(
                {
                    "id": uid or f"{sym}:{title[:40]}",
                    "kind": "company",
                    "title": title,
                    "source": publisher,
                    "published_at": _iso(ts) if ts else None,
                    "url": _text(row.get("link")) or None,
                    "symbol": sym or None,
                    "name": company_names.get(sym) or None,
                    "venue": item_venue,
                    "country": None,
                }
            )

    # ------------------------------------------------------------------ geo (only without venue/symbol filter)
    geo_items: list[dict[str, Any]] = []
    if not wanted_venue and not wanted_symbol:
        seen_url: set[str] = set()
        for row in read_jsonl_objects(root / "gdelt" / "events.jsonl"):
            ts = _parse_ts(row.get("seendate")) or _parse_ts(row.get("ts_collected"))
            if ts and not (threshold <= ts <= now_utc):
                continue
            url = _text(row.get("url"))
            if url in seen_url:
                continue
            seen_url.add(url)
            geo_items.append(
                {
                    "id": url or _text(row.get("ts_collected")),
                    "kind": "geo",
                    "title": _text(row.get("title")),
                    "source": _text(row.get("domain")),
                    "published_at": _iso(ts) if ts else None,
                    "url": url or None,
                    "symbol": None,
                    "name": None,
                    "venue": None,
                    "country": _text(row.get("sourcecountry")) or None,
                }
            )

    # ------------------------------------------------------------------ merge + sort + limit

    def _sort_key(item: dict[str, Any]) -> float:
        ts = _parse_ts(item.get("published_at"))
        return ts.timestamp() if ts else 0.0

    all_items = sorted(company_items + geo_items, key=_sort_key, reverse=True)
    limited = all_items[: max(1, limit)]

    counts: dict[str, int] = {
        "company": sum(1 for i in limited if i["kind"] == "company"),
        "geo": sum(1 for i in limited if i["kind"] == "geo"),
        "total": len(limited),
    }
    return {
        "generated_at": _iso(now_utc),
        "window_days": window_days,
        "items": limited,
        "counts": counts,
    }


# ---------------------------------------------------------------------------
# Upcoming earnings
# ---------------------------------------------------------------------------


def load_upcoming_earnings(
    state_dir: str | Path,
    *,
    now: datetime,
    horizon_days: int = 90,
    limit: int = 40,
    stale_days: int = 14,
    company_names: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Return earnings events from fundamental_items, within the given horizon.

    Algorithm
    ---------
    - Scans state/fundamental_items/*.jsonl (read-only).
    - Per symbol, keeps only the entry with the most recent as_of (max).
    - For each kept entry, iterates payload['Earnings Date'] (a list of
      'YYYY-MM-DD') and picks the first date strictly after today that is
      also ≤ today + horizon_days — never [0] blindly.
    - Marks the entry stale when its as_of is older than stale_days.
    - Returns UpcomingEarning-compatible dicts sorted by earnings_date asc,
      capped at limit.
    """
    root = Path(state_dir)
    now_utc = now.astimezone(UTC)
    today = now_utc.date()
    horizon = today + timedelta(days=horizon_days)
    stale_threshold = now_utc - timedelta(days=stale_days)
    company_names = company_names or {}

    # Per-symbol: keep the row with the latest as_of
    best_by_symbol: dict[str, dict[str, Any]] = {}
    items_dir = root / "fundamental_items"
    if items_dir.exists():
        for path in items_dir.glob("*.jsonl"):
            for row in read_jsonl_objects(path):
                if _text(row.get("kind")) != "earnings_calendar":
                    continue
                sym = _text(row.get("symbol")).upper()
                if not sym:
                    continue
                row_as_of = _text(row.get("as_of"))
                existing = best_by_symbol.get(sym)
                if existing is None or row_as_of > _text(existing.get("as_of")):
                    best_by_symbol[sym] = row

    results: list[dict[str, Any]] = []
    for sym, row in best_by_symbol.items():
        payload = _dict(row.get("payload"))
        date_list = _list(payload.get("Earnings Date"))

        # Collect all candidate dates in ]yesterday, horizon] then take the min.
        # Using >= today (open-exclusive on yesterday) instead of > today to include
        # same-day earnings — most relevant for "Coming up" display.
        # min() rather than break-on-first is safe regardless of date_list order.
        candidates: list[date] = []
        for d_str in date_list:
            d_text = _text(d_str)
            if not d_text:
                continue
            try:
                d = date.fromisoformat(d_text)
            except (TypeError, ValueError):
                continue
            if d >= today and d <= horizon:
                candidates.append(d)
        earnings_date_str: str | None = min(candidates).isoformat() if candidates else None

        if earnings_date_str is None:
            continue

        as_of_text = _text(row.get("as_of"))
        as_of_dt = _parse_ts(as_of_text)
        stale = bool(as_of_dt and as_of_dt < stale_threshold)

        eps_raw = payload.get("Earnings Average")
        try:
            eps_consensus: float | None = float(eps_raw)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            eps_consensus = None

        rev_raw = payload.get("Revenue Average")
        try:
            revenue_consensus: float | None = float(rev_raw)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            revenue_consensus = None

        results.append(
            {
                "symbol": sym,
                "name": company_names.get(sym) or None,
                "earnings_date": earnings_date_str,
                "eps_consensus": eps_consensus,
                "revenue_consensus": revenue_consensus,
                "as_of": as_of_text or None,
                "stale": stale,
            }
        )

    results.sort(key=lambda r: r["earnings_date"])
    return results[: max(1, limit)] if results else []


# ---------------------------------------------------------------------------
# Posture history (private helper)
# ---------------------------------------------------------------------------


def _load_posture_history(
    root: Path,
    *,
    now_utc: datetime,
    window_days: int = 30,
) -> list[dict[str, Any]]:
    """Load gross_mode/net_bias/regime history for the last window_days days.

    Sources
    -------
    - state/global_universe_postures/YYYY-MM-DD.jsonl → last entry per file
    - state/global_situation_digests/YYYY-MM-DD.jsonl → last entry per file
      (joined by date stem to provide regime)

    Returns a list of PostureHistoryPoint-compatible dicts sorted by as_of asc.
    """
    threshold = now_utc - timedelta(days=window_days)

    postures_dir = root / "global_universe_postures"
    digests_dir = root / "global_situation_digests"

    # Last entry per dated posture file
    posture_by_date: dict[str, dict[str, Any]] = {}
    if postures_dir.exists():
        for path in postures_dir.glob("????-??-??.jsonl"):
            rows = read_jsonl_objects(path)
            if rows:
                posture_by_date[path.stem] = rows[-1]

    # Last entry per dated digest file → regime
    digest_by_date: dict[str, dict[str, Any]] = {}
    if digests_dir.exists():
        for path in digests_dir.glob("????-??-??.jsonl"):
            rows = read_jsonl_objects(path)
            if rows:
                digest_by_date[path.stem] = rows[-1]

    results: list[dict[str, Any]] = []
    for date_str, posture in posture_by_date.items():
        as_of = _text(posture.get("as_of"))
        as_of_dt = _parse_ts(as_of)
        # Apply 30-day window based on file date (fallback: as_of)
        ref_dt = as_of_dt or _parse_ts(date_str + "T00:00:00+00:00")
        if ref_dt and (ref_dt < threshold or ref_dt > now_utc):
            continue
        digest = digest_by_date.get(date_str, {})
        regime = _text(digest.get("regime")) or None
        results.append(
            {
                "as_of": as_of or date_str,
                "gross_mode": _text(posture.get("gross_mode")) or None,
                "net_bias": _text(posture.get("net_bias")) or None,
                "regime": regime,
            }
        )

    results.sort(key=lambda r: r["as_of"])
    return results


# ---------------------------------------------------------------------------
# Editorial dedup (private helper)
# ---------------------------------------------------------------------------


def _editorial_dedup(stories: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Remove quasi-duplicates from a severity-sorted story list.

    Two stories are quasi-duplicates when:
    - their first 8 normalised words (casefold, whitespace-split) are
      identical, OR
    - the Jaccard index of their consecutive BIGRAMS built from tokens longer
      than 2 characters is ≥ 0.6.

    Using bigrams instead of unigrams avoids merging antonyms that share many
    individual words but differ in consecutive pairs (e.g. "rise" vs "fall"
    alters the bigrams around it, keeping Jaccard low).

    Since the list is expected to be sorted severity-first (best first), the
    greedy first-seen wins rule keeps the most severe, then most recent story
    from each duplicate cluster.
    """

    def _clean(text: str) -> str:
        """Lower-case, replace punctuation with spaces for reliable tokenisation."""
        return "".join(c if c.isalnum() or c == "'" else " " for c in text.casefold())

    def _norm_words(text: str) -> list[str]:
        return _clean(text).split()

    def _long_token_list(text: str) -> list[str]:
        """Ordered list of tokens longer than 2 chars — preserves position for bigrams."""
        return [w for w in _clean(text).split() if len(w) > 2]

    def _bigrams(tokens: list[str]) -> set[tuple[str, str]]:
        if len(tokens) < 2:
            return set()
        return {(tokens[i], tokens[i + 1]) for i in range(len(tokens) - 1)}

    def _jaccard_bigrams(text_a: str, text_b: str) -> float:
        a = _bigrams(_long_token_list(text_a))
        b = _bigrams(_long_token_list(text_b))
        union = a | b
        if not union:
            return 0.0
        return len(a & b) / len(union)

    def _quasi_dup(text_a: str, text_b: str) -> bool:
        wa = _norm_words(text_a)
        wb = _norm_words(text_b)
        prefix_a = wa[:8]
        prefix_b = wb[:8]
        if prefix_a and prefix_a == prefix_b:
            return True
        return _jaccard_bigrams(text_a, text_b) >= 0.6

    kept: list[dict[str, Any]] = []
    kept_texts: list[str] = []

    for story in stories:
        point = _text(story.get("point"))
        if any(_quasi_dup(point, kept_text) for kept_text in kept_texts):
            continue
        kept.append(story)
        kept_texts.append(point)

    return kept


# ---------------------------------------------------------------------------
# Daily briefing
# ---------------------------------------------------------------------------


def load_daily_briefing(
    state_dir: str | Path,
    *,
    now: datetime,
    limit_stories: int = 12,
    limit_news: int = 14,
    company_names: dict[str, str] | None = None,
    symbol_venues: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Return a structured daily briefing.

    Sources
    -------
    - state/global_universe_postures/current.json  → headline.posture
    - state/global_situation_digests/current.json  → headline.regime/rates_bias/usd_bias + top_stories
    - state/news_briefs/latest-*.jsonl             → top_stories (alerts)
    - state/macro_series/*.jsonl                   → indicators
    - state/news_items/ + state/gdelt/             → news (via load_news_feed)

    The calendar key is intentionally absent: the handler adds it via
    trader.market.macro_calendar.macro_next so that the read model stays pure.
    """

    root = Path(state_dir)
    now_utc = now.astimezone(UTC)

    # ------------------------------------------------------------------ posture
    posture = read_json_object(root / "global_universe_postures" / "current.json")
    posture_d = _dict(posture)

    # ------------------------------------------------------------------ digest
    digest = read_json_object(root / "global_situation_digests" / "current.json")
    digest_d = _dict(digest)

    # ------------------------------------------------------------------ headline
    headline: dict[str, Any] = {
        "posture": posture_d or None,
        "regime": _text(digest_d.get("regime")) or None,
        "rates_bias": _text(digest_d.get("rates_bias")) or None,
        "usd_bias": _text(digest_d.get("usd_bias")) or None,
        "digest_as_of": _text(digest_d.get("as_of")) or None,
        "lead": _text(posture_d.get("rationale")) or None,
    }

    # ------------------------------------------------------------------ top_stories

    def _normalise(text: str) -> str:
        return " ".join(text.casefold().split())

    raw_stories: list[dict[str, Any]] = []
    seen_text: set[str] = set()

    # Points from global digest
    for point_row in _list(digest_d.get("points")):
        p = _dict(point_row)
        # is_operational points are operational noise, not editorial content.
        if p.get("is_operational"):
            continue
        point_text = _text(p.get("point"))
        if not point_text:
            continue
        key = _normalise(point_text)
        if key in seen_text:
            continue
        seen_text.add(key)
        raw_stories.append(
            {
                "point": point_text,
                "direction": _text(p.get("direction")) or None,
                "severity": _text(p.get("severity")) or None,
                "signal": _text(p.get("signal")) or None,
                "horizon": _text(p.get("horizon")) or None,
                "venue": None,
                "symbols": _list(p.get("symbols")),
                "sources": _list(p.get("sources")),
                "source_refs": _list(p.get("source_refs")),
                "as_of": _text(digest_d.get("as_of")) or None,
                "origin": "global_digest",
            }
        )

    # Alerts from news_briefs latest-*.jsonl
    briefs_dir = root / "news_briefs"
    if briefs_dir.exists():
        for path in sorted(briefs_dir.glob("latest-*.jsonl")):
            brief_venue_raw = path.name.removeprefix("latest-").removesuffix(".jsonl")
            rows = read_jsonl_objects(path)
            if not rows:
                continue
            brief = rows[-1]
            brief_as_of = _text(brief.get("as_of")) or None
            venue_key: str | None = brief_venue_raw if brief_venue_raw != "GLOBAL" else None
            for alert in _list(brief.get("alerts")):
                a = _dict(alert)
                # is_operational alerts are operational noise, skip.
                if a.get("is_operational"):
                    continue
                point_text = _text(a.get("point"))
                if not point_text:
                    continue
                key = _normalise(point_text)
                if key in seen_text:
                    continue
                seen_text.add(key)
                raw_stories.append(
                    {
                        "point": point_text,
                        "direction": _text(a.get("direction")) or None,
                        "severity": _text(a.get("severity")) or None,
                        "signal": _text(a.get("signal")) or None,
                        "horizon": _text(a.get("horizon")) or None,
                        "venue": venue_key,
                        "symbols": _list(a.get("symbols")),
                        "sources": _list(a.get("sources")),
                        "source_refs": _list(a.get("source_refs")),
                        "as_of": brief_as_of,
                        "origin": "news_brief",
                    }
                )

    # Sort: severity asc (0=high), then as_of desc
    def _story_sort_key(s: dict[str, Any]) -> tuple[int, float]:
        sev = _severity_key(s)
        ts = _parse_ts(s.get("as_of"))
        return (sev, -(ts.timestamp() if ts else 0.0))

    raw_stories.sort(key=_story_sort_key)

    # Editorial dedup: remove quasi-duplicates after severity sort.
    # The greedy first-wins rule keeps the most severe / most recent story.
    deduped_stories = _editorial_dedup(raw_stories)
    top_stories = deduped_stories[: max(1, limit_stories)]

    # ------------------------------------------------------------------ indicators
    indicators: list[dict[str, Any]] = []
    macro_dir = root / "macro_series"
    if macro_dir.exists():
        for path in sorted(macro_dir.glob("*.jsonl")):
            series_id = path.stem
            if series_id in _SUPERSEDED_SERIES:
                continue
            label, unit = _SERIES_META.get(series_id, (series_id, None))
            # Dedup by period — keep row with latest ts_collected
            rows_by_period: dict[str, dict[str, Any]] = {}
            for row in read_jsonl_objects(path):
                period = _text(row.get("period"))
                if not period:
                    continue
                existing = rows_by_period.get(period)
                if existing is None:
                    rows_by_period[period] = row
                elif _text(row.get("ts_collected")) > _text(existing.get("ts_collected")):
                    rows_by_period[period] = row
            sorted_rows = sorted(
                rows_by_period.values(), key=lambda r: _text(r.get("period"))
            )
            if not sorted_rows:
                continue
            last = sorted_rows[-1]
            prev = sorted_rows[-2] if len(sorted_rows) >= 2 else None
            try:
                value = float(last.get("value"))  # type: ignore[arg-type]
            except (TypeError, ValueError):
                continue
            prev_value: float | None = None
            delta: float | None = None
            if prev is not None:
                try:
                    prev_value = float(prev.get("value"))  # type: ignore[arg-type]
                    delta = round(value - prev_value, 6)
                except (TypeError, ValueError):
                    pass
            history = [
                {"period": _text(r.get("period")), "value": r.get("value")}
                for r in sorted_rows[-30:]
            ]
            indicators.append(
                {
                    "series_id": series_id,
                    "label": label,
                    "unit": unit,
                    "value": value,
                    "period": _text(last.get("period")),
                    "previous_value": prev_value,
                    "delta": delta,
                    "history": history,
                }
            )

    # ------------------------------------------------------------------ news
    news_feed = load_news_feed(
        root,
        now=now_utc,
        limit=limit_news,
        company_names=company_names,
        symbol_venues=symbol_venues,
    )
    news = news_feed.get("items", [])

    # ------------------------------------------------------------------ earnings
    earnings = load_upcoming_earnings(
        root,
        now=now_utc,
        company_names=company_names,
    )

    # ------------------------------------------------------------------ posture_history
    posture_history = _load_posture_history(root, now_utc=now_utc)

    # ------------------------------------------------------------------ as_of
    as_of_candidates: list[str] = [
        v
        for v in (_text(posture_d.get("as_of")), _text(digest_d.get("as_of")))
        if v
    ]

    return {
        "generated_at": _iso(now_utc),
        "as_of": max(as_of_candidates, default=None),
        "headline": headline,
        "top_stories": top_stories,
        "indicators": indicators,
        "news": news,
        "earnings": earnings,
        "posture_history": posture_history,
        "counts": {
            "stories": len(top_stories),
            "indicators": len(indicators),
            "news": len(news),
        },
    }


__all__ = [
    "load_daily_briefing",
    "load_news_feed",
    "load_upcoming_earnings",
]
