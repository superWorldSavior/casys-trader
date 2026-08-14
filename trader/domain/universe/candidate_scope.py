"""Pure candidate-pool policies for one venue universe scope."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

RADAR_CANDIDATES_TOP = 40


def compose_candidate_pool(
    venue_ranked: list[dict[str, Any]],
    news_challengers: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Compose top radar candidates plus every radar-eligible news challenger."""

    ranked_by_symbol = {
        str(item.get("symbol") or "").strip(): item
        for item in venue_ranked
        if isinstance(item, dict) and item.get("symbol")
    }
    candidates: list[dict[str, Any]] = []
    index_by_symbol: dict[str, int] = {}

    def radar_candidate(item: dict[str, Any], *, source: str) -> dict[str, Any]:
        return {
            "symbol": item["symbol"],
            "attractiveness": item["attractiveness"],
            "bias": item.get("bias", "long"),
            "candidate_source": source,
            "candidate_sources": [source],
        }

    for item in venue_ranked[:RADAR_CANDIDATES_TOP]:
        symbol = str(item.get("symbol") or "").strip()
        if not symbol or symbol in index_by_symbol:
            continue
        index_by_symbol[symbol] = len(candidates)
        candidates.append(radar_candidate(item, source="radar"))

    for challenger in news_challengers:
        if not isinstance(challenger, dict):
            continue
        symbol = str(challenger.get("symbol") or "").strip()
        ranked_item = ranked_by_symbol.get(symbol)
        if ranked_item is None:
            continue
        provenance = {
            key: value
            for key, value in challenger.items()
            if key not in {"symbol", "attractiveness", "bias", "candidate_sources"}
        }
        if symbol in index_by_symbol:
            existing = candidates[index_by_symbol[symbol]]
            primary_source = existing.get("candidate_source") or "fresh_news"
            sources = list(existing.get("candidate_sources") or [existing.get("candidate_source")])
            if "fresh_news" not in sources:
                sources.append("fresh_news")
            existing.update(provenance)
            existing["candidate_source"] = primary_source
            existing["candidate_sources"] = [source for source in sources if source]
            continue
        candidate = radar_candidate(ranked_item, source="fresh_news")
        candidate.update(provenance)
        candidate["candidate_source"] = "fresh_news"
        candidate["candidate_sources"] = ["fresh_news"]
        index_by_symbol[symbol] = len(candidates)
        candidates.append(candidate)

    return candidates


def candidate_run_ids(
    candidates: list[dict[str, Any]],
    observed_run_ids: list[str],
) -> list[str]:
    """Collect stable run references carried by one candidate scope."""

    values = {str(run_id).strip() for run_id in observed_run_ids if str(run_id).strip()}
    values.update(
        {
            str(metadata.get("candidate_run_id") or "").strip()
            for candidate in candidates
            for metadata in [candidate.get("metadata")]
            if isinstance(metadata, dict) and str(metadata.get("candidate_run_id") or "").strip()
        }
    )
    return sorted(values)


def merge_news_challengers(
    current: list[dict[str, Any]],
    retained: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Keep one fresh-news challenger per symbol without regressing evidence.

    The scout deliberately skips references already injected into the latest
    macro brief.  When the bounded macro catalog causes it to fall back to an
    older article for the same symbol, that fallback must not replace a newer
    still-valid challenger retained from the prior scope.  Otherwise only the
    ``source_refs`` churn, but it still produces a new candidate scope and an
    unnecessary macro-analysis run.
    """

    merged: list[dict[str, Any]] = []
    index_by_symbol: dict[str, int] = {}
    for challenger in [*current, *retained]:
        if not isinstance(challenger, dict):
            continue
        symbol = str(challenger.get("symbol") or "").strip()
        if not symbol:
            continue
        existing_index = index_by_symbol.get(symbol)
        if existing_index is None:
            index_by_symbol[symbol] = len(merged)
            merged.append(challenger)
            continue
        if _is_newer_challenger(challenger, merged[existing_index]):
            merged[existing_index] = challenger
    return merged


def _is_newer_challenger(candidate: dict[str, Any], existing: dict[str, Any]) -> bool:
    """Return whether ``candidate`` has strictly newer usable news evidence.

    Unknown timestamps retain the historical first-wins behavior.  This keeps
    malformed advisory payloads from displacing a valid retained challenger.
    """

    candidate_at = _challenger_latest_published_at(candidate)
    existing_at = _challenger_latest_published_at(existing)
    return candidate_at is not None and existing_at is not None and candidate_at > existing_at


def _challenger_latest_published_at(challenger: dict[str, Any]) -> datetime | None:
    fresh_news = challenger.get("fresh_news")
    if not isinstance(fresh_news, dict):
        return None
    return _parse_utc_datetime(fresh_news.get("latest_published_at"))


def retained_news_challengers(
    previous: dict[str, Any],
    venue_ranked: list[dict[str, Any]],
    *,
    as_of: Any,
) -> list[dict[str, Any]]:
    """Retain unexpired prior challengers while they remain radar-eligible."""

    moment = _parse_utc_datetime(as_of)
    if moment is None:
        return []
    eligible_symbols = {
        str(item.get("symbol") or "").strip() for item in venue_ranked if isinstance(item, dict) and item.get("symbol")
    }
    retained: list[dict[str, Any]] = []
    for candidate in previous.get("candidates", []) if isinstance(previous, dict) else []:
        if not isinstance(candidate, dict):
            continue
        sources = set(candidate.get("candidate_sources") or ())
        if candidate.get("candidate_source"):
            sources.add(candidate["candidate_source"])
        if "fresh_news" not in sources:
            continue
        symbol = str(candidate.get("symbol") or "").strip()
        if not symbol or symbol not in eligible_symbols:
            continue
        fresh_news = candidate.get("fresh_news")
        if not isinstance(fresh_news, dict):
            continue
        valid_until = _parse_utc_datetime(fresh_news.get("valid_until"))
        if valid_until is None or valid_until <= moment:
            continue
        retained.append(candidate)
    return retained


def _parse_utc_datetime(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)
