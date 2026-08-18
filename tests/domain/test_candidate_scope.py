from __future__ import annotations

from typing import Any

from trader.domain.universe.candidate_scope import merge_news_challengers


def _challenger(
    symbol: str,
    *,
    published_at: str | None,
    source_refs: list[str],
) -> dict[str, Any]:
    fresh_news: dict[str, Any] = {"source_refs": source_refs}
    if published_at is not None:
        fresh_news["latest_published_at"] = published_at
    return {
        "symbol": symbol,
        "candidate_source": "fresh_news",
        "fresh_news": fresh_news,
    }


def test_merge_keeps_retained_when_published_at_is_equal() -> None:
    current = [_challenger("NVDA", published_at="2026-08-18T10:00:00+00:00", source_refs=["ref-b"])]
    retained = [_challenger("NVDA", published_at="2026-08-18T10:00:00+00:00", source_refs=["ref-a"])]

    assert merge_news_challengers(current, retained) == retained


def test_merge_keeps_retained_when_both_timestamps_are_unknown() -> None:
    current = [_challenger("NVDA", published_at=None, source_refs=["ref-b"])]
    retained = [_challenger("NVDA", published_at=None, source_refs=["ref-a"])]

    assert merge_news_challengers(current, retained) == retained


def test_merge_replaces_retained_when_current_is_strictly_newer() -> None:
    current = [_challenger("NVDA", published_at="2026-08-18T11:00:00+00:00", source_refs=["ref-newer"])]
    retained = [_challenger("NVDA", published_at="2026-08-18T10:00:00+00:00", source_refs=["ref-older"])]

    assert merge_news_challengers(current, retained) == current
