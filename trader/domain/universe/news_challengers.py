"""Pure fresh-news challenger selection for an upstream candidate pool."""

from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

AttributionMethod = Literal["manual_alias", "legal_name", "auto_alias", "qualified_symbol"]

DEFAULT_MAX_AGE_HOURS = 72.0
DEFAULT_MIN_SCORE = 65
MAX_EVIDENCE_PER_SYMBOL = 3

_LEGAL_SUFFIXES = {
    "ab",
    "ag",
    "asa",
    "co",
    "company",
    "corp",
    "corporation",
    "group",
    "holding",
    "holdings",
    "inc",
    "incorporated",
    "limited",
    "ltd",
    "nv",
    "oyj",
    "plc",
    "sa",
    "se",
    "spa",
    "publ",
}
_LEGAL_SUFFIX_SEQUENCES = (
    ("s", "p", "a"),
    ("a", "s", "a"),
    ("p", "l", "c"),
    ("a", "b"),
    ("a", "g"),
    ("n", "v"),
    ("s", "a"),
    ("s", "e"),
)
_GENERIC_SINGLE_WORDS = {
    "american",
    "china",
    "company",
    "financial",
    "global",
    "group",
    "holding",
    "international",
    "national",
    "technology",
    "technologies",
}
_WIRE_PUBLISHERS = {"associated press", "bloomberg", "reuters"}
_RECOGNIZED_PUBLISHERS = {
    "barrons",
    "barrons com",
    "cnbc",
    "financial times",
    "marketwatch",
    "mt newswires",
    "nikkei asia",
    "the wall street journal",
    "wsj",
}
_LOW_SIGNAL_PUBLISHERS = {
    "24 7 wall st",
    "barchart",
    "guru focus",
    "gurufocus com",
    "insider monkey",
    "investorshub",
    "motley fool",
    "simply wall st",
    "stocktwits",
    "the motley fool",
    "zacks",
}
_LOW_SIGNAL_PHRASES = (
    "best stocks",
    "best qqq stocks",
    "discount zone",
    "estimated value",
    "great opportunity to buy",
    "should you buy",
    "should you still buy",
    "is it too late",
    "is now the time",
    "possibly undervalued",
    "priced below",
    "stocks to buy",
    "top dividend stocks",
    "undervalued",
    "valuation story",
    "why investors should",
)
_EXCHANGE_QUALIFIERS: dict[str, tuple[str, ...]] = {
    ".TWO": ("TPEX", "TWO"),
    ".TW": ("TPE", "TWSE"),
    ".PA": ("EPA", "PAR"),
    ".DE": ("ETR", "XETRA"),
    ".AS": ("AMS",),
    ".L": ("LON",),
    ".SW": ("SWX", "VTX"),
    ".MI": ("BIT",),
    ".MC": ("BME",),
    ".ST": ("STO",),
    ".CO": ("CPH",),
    ".HE": ("HEL",),
    ".OL": ("OSL",),
    ".BR": ("EBR",),
    ".VI": ("VIE",),
    ".LS": ("ELI",),
}
_US_EXCHANGE_QUALIFIERS = ("AMEX", "NASDAQ", "NYSE", "NYSEARCA")
_DOLLAR_TICKER_RE = re.compile(r"(?<![A-Z0-9])\$([A-Z][A-Z0-9.-]{0,9})(?![A-Z0-9])", re.IGNORECASE)
_EXCHANGE_REFERENCE_RE = re.compile(
    r"\b([A-Z]{2,10})\s*:\s*([A-Z0-9]+(?:[.-][A-Z0-9]+)?)\b",
    re.IGNORECASE,
)
_EVENT_PATTERNS: tuple[tuple[str, int, tuple[str, ...]], ...] = (
    (
        "critical_event",
        40,
        (
            "bankruptcy",
            "cyberattack",
            "default",
            "force majeure",
            "guidance cut",
            "guidance raise",
            "misses estimates",
            "outage",
            "profit warning",
            "recall",
            "regulatory approval",
            "sanction",
            "shutdown",
        ),
    ),
    (
        "earnings_guidance",
        25,
        (
            "earnings",
            "forecast",
            "guidance",
            "outlook",
            "profit",
            "quarterly results",
            "revenue",
            "sales",
        ),
    ),
    (
        "m_and_a",
        25,
        ("acquire", "acquisition", "bid for", "merger", "takeover"),
    ),
    (
        "regulatory_legal",
        25,
        ("antitrust", "approval", "investigation", "lawsuit", "regulator"),
    ),
    (
        "contract_product",
        25,
        ("contract", "launch", "order", "partnership", "trial"),
    ),
    (
        "capital_action",
        10,
        ("buyback", "dividend", "investment", "layoffs", "offering", "upgrade"),
    ),
)


@dataclass(frozen=True)
class NewsChallengerEvidence:
    source_ref: str
    title: str
    publisher: str
    published_at: str
    fetched_for: str
    attribution_method: AttributionMethod
    matched_alias: str
    event_type: str
    score: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "uuid": self.source_ref,
            "title": self.title,
            "publisher": self.publisher,
            "published_at": self.published_at,
            "fetched_for": self.fetched_for,
            "attribution": {
                "method": self.attribution_method,
                "matched": self.matched_alias,
                "directness": "direct",
            },
            "event_type": self.event_type,
            "score": self.score,
        }


@dataclass(frozen=True)
class NewsChallenger:
    symbol: str
    score: int
    latest_published_at: str
    valid_until: str
    event_types: tuple[str, ...]
    publishers: tuple[str, ...]
    source_refs: tuple[str, ...]
    evidence_count: int
    evidence: tuple[NewsChallengerEvidence, ...]

    def to_candidate(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "attractiveness": 0.0,
            "bias": "neutral",
            "candidate_source": "fresh_news",
            "fresh_news": {
                "score": self.score,
                "latest_published_at": self.latest_published_at,
                "valid_until": self.valid_until,
                "event_types": list(self.event_types),
                "publishers": list(self.publishers),
                "source_refs": list(self.source_refs),
                "evidence_count": self.evidence_count,
                "evidence": [item.to_dict() for item in self.evidence],
            },
        }


@dataclass(frozen=True)
class NewsChallengerSelection:
    challengers: tuple[NewsChallenger, ...]
    items_read: int
    eligible_items: int
    rejection_counts: dict[str, int]


@dataclass(frozen=True)
class _AliasMatch:
    symbol: str
    alias: str
    method: AttributionMethod
    points: int


@dataclass(frozen=True)
class _AliasOccurrence:
    match: _AliasMatch
    start: int
    end: int


@dataclass(frozen=True)
class _PhraseEntry:
    tokens: tuple[str, ...]
    matches: tuple[_AliasMatch, ...]


@dataclass(frozen=True)
class _AttributionCatalog:
    phrases_by_first_token: dict[str, tuple[_PhraseEntry, ...]]
    dollar_tickers: dict[str, tuple[_AliasMatch, ...]]
    exchange_specs: dict[str, tuple[str, frozenset[str]]]


@dataclass(frozen=True)
class _AttributedEntity:
    match: _AliasMatch
    occurrences: tuple[_AliasOccurrence, ...]


def select_news_challengers(
    news_items: Iterable[Mapping[str, Any]],
    *,
    symbol_names: Mapping[str, str],
    manual_aliases: Mapping[str, Iterable[str]] | None,
    eligible_symbols: set[str],
    radar_symbols: set[str],
    seen_source_refs: set[str],
    as_of: datetime,
    max_age_hours: float = DEFAULT_MAX_AGE_HOURS,
    min_score: int = DEFAULT_MIN_SCORE,
) -> NewsChallengerSelection:
    """Select every directly attributed, fresh, material symbol outside radar."""

    now = _ensure_utc(as_of)
    attribution_catalog = _build_attribution_catalog(symbol_names, manual_aliases or {})
    by_symbol: dict[str, list[NewsChallengerEvidence]] = defaultdict(list)
    rejected: dict[str, int] = defaultdict(int)
    items_read = 0
    eligible_items = 0
    processed_source_refs = set(seen_source_refs)
    corroborating_score_floor = max(0, min_score - 10)

    for item in news_items:
        items_read += 1
        source_ref = str(item.get("uuid") or "").strip()
        title = str(item.get("title") or "").strip()
        published_at = _parse_datetime(item.get("published_at"))
        if not source_ref or not title or published_at is None:
            rejected["missing_contract"] += 1
            continue
        if source_ref in processed_source_refs:
            reason = "already_seen" if source_ref in seen_source_refs else "duplicate_source_ref"
            rejected[reason] += 1
            continue
        processed_source_refs.add(source_ref)
        if published_at > now + timedelta(minutes=5):
            rejected["future"] += 1
            continue
        age_hours = (now - published_at).total_seconds() / 3600.0
        if age_hours < 0 or age_hours > max_age_hours:
            rejected["stale"] += 1
            continue

        attribution = _attribute_title(title, attribution_catalog)
        if attribution is None:
            rejected["no_direct_entity"] += 1
            continue
        if _is_analyst_source_only(title, attribution):
            rejected["analyst_source_only"] += 1
            continue
        attributed_match = attribution.match
        if attributed_match.symbol not in eligible_symbols:
            rejected["out_of_venue"] += 1
            continue
        if attributed_match.symbol in radar_symbols:
            rejected["already_radar"] += 1
            continue

        event_type, event_points = _event_signal(title)
        publisher = str(item.get("publisher") or "").strip()
        score = min(
            100,
            attributed_match.points
            + _freshness_points(age_hours)
            + event_points
            + _publisher_points(publisher)
            + _headline_penalty(title),
        )
        if score < corroborating_score_floor:
            rejected["low_score"] += 1
            continue

        eligible_items += 1
        by_symbol[attributed_match.symbol].append(
            NewsChallengerEvidence(
                source_ref=source_ref,
                title=title,
                publisher=publisher,
                published_at=published_at.isoformat(),
                fetched_for=str(item.get("symbol") or "").strip(),
                attribution_method=attributed_match.method,
                matched_alias=attributed_match.alias,
                event_type=event_type,
                score=score,
            )
        )

    challengers: list[NewsChallenger] = []
    for symbol, evidence in by_symbol.items():
        challenger = _aggregate_challenger(symbol, evidence, max_age_hours=max_age_hours)
        if challenger.score < min_score:
            rejected["low_symbol_score"] += 1
            continue
        challengers.append(challenger)
    challengers.sort(key=_challenger_sort_key)
    return NewsChallengerSelection(
        challengers=tuple(challengers),
        items_read=items_read,
        eligible_items=eligible_items,
        rejection_counts=dict(sorted(rejected.items())),
    )


def _aggregate_challenger(
    symbol: str,
    evidence: list[NewsChallengerEvidence],
    *,
    max_age_hours: float,
) -> NewsChallenger:
    ordered = sorted(evidence, key=_evidence_sort_key)
    leading_event = ordered[0].event_type
    corroborating_publishers = {
        _normalize_text(item.publisher)
        for item in ordered
        if item.publisher and item.event_type == leading_event
    }
    corroboration = min(10, max(0, len(corroborating_publishers) - 1) * 5)
    score = min(100, ordered[0].score + corroboration)
    latest_dt = max(_required_datetime(item.published_at) for item in ordered)
    latest = latest_dt.isoformat()
    publishers = _unique_publishers(ordered)
    return NewsChallenger(
        symbol=symbol,
        score=score,
        latest_published_at=latest,
        valid_until=(latest_dt + timedelta(hours=max_age_hours)).isoformat(),
        event_types=tuple(sorted({item.event_type for item in ordered})),
        publishers=publishers,
        source_refs=tuple(dict.fromkeys(item.source_ref for item in ordered)),
        evidence_count=len(ordered),
        evidence=tuple(ordered[:MAX_EVIDENCE_PER_SYMBOL]),
    )


def _challenger_sort_key(item: NewsChallenger) -> tuple[int, float, str]:
    published_at = _required_datetime(item.latest_published_at)
    return (-item.score, -published_at.timestamp(), item.symbol)


def _evidence_sort_key(item: NewsChallengerEvidence) -> tuple[int, float, str]:
    published_at = _required_datetime(item.published_at)
    return (-item.score, -published_at.timestamp(), item.source_ref)


def _unique_publishers(evidence: Iterable[NewsChallengerEvidence]) -> tuple[str, ...]:
    by_normalized_name: dict[str, str] = {}
    for item in evidence:
        normalized = _normalize_text(item.publisher)
        if normalized and normalized not in by_normalized_name:
            by_normalized_name[normalized] = item.publisher
    return tuple(by_normalized_name[key] for key in sorted(by_normalized_name))


def _build_attribution_catalog(
    symbol_names: Mapping[str, str],
    manual_aliases: Mapping[str, Iterable[str]],
) -> _AttributionCatalog:
    phrase_matches: dict[str, list[_AliasMatch]] = defaultdict(list)
    dollar_tickers: dict[str, list[_AliasMatch]] = defaultdict(list)
    exchange_specs: dict[str, tuple[str, frozenset[str]]] = {}

    for symbol, name in symbol_names.items():
        symbol = str(symbol)
        legal_aliases = _legal_name_aliases(str(name))
        for alias, method, points in legal_aliases:
            phrase_matches[alias].append(_AliasMatch(symbol=symbol, alias=alias, method=method, points=points))
        raw_manual_aliases = manual_aliases.get(symbol, ())
        if isinstance(raw_manual_aliases, str):
            raw_manual_aliases = (raw_manual_aliases,)
        for raw_alias in raw_manual_aliases:
            alias = _normalize_text(raw_alias)
            if alias:
                phrase_matches[alias].append(
                    _AliasMatch(symbol=symbol, alias=alias, method="manual_alias", points=20)
                )

        base, qualifiers = _symbol_exchange_spec(symbol)
        normalized_base = _normalize_text(base)
        exchange_specs[symbol] = (normalized_base, frozenset(qualifier.upper() for qualifier in qualifiers))
        qualified_match = _AliasMatch(symbol=symbol, alias=symbol, method="qualified_symbol", points=20)
        if _symbol_suffix(symbol) or "." in symbol:
            phrase_matches[_normalize_text(symbol)].append(qualified_match)
        for qualifier in qualifiers:
            phrase_matches[_normalize_text(f"{qualifier} {base}")].append(qualified_match)
        if not _symbol_suffix(symbol):
            dollar_tickers[base.upper()].append(
                _AliasMatch(symbol=symbol, alias=f"${base}", method="qualified_symbol", points=20)
            )

    phrases_by_first_token: dict[str, list[_PhraseEntry]] = defaultdict(list)
    for phrase, matches in phrase_matches.items():
        tokens = tuple(phrase.split())
        if not tokens:
            continue
        phrases_by_first_token[tokens[0]].append(
            _PhraseEntry(tokens=tokens, matches=_dedupe_matches(matches))
        )
    return _AttributionCatalog(
        phrases_by_first_token={
            token: tuple(sorted(entries, key=lambda entry: (-len(entry.tokens), entry.tokens)))
            for token, entries in phrases_by_first_token.items()
        },
        dollar_tickers={ticker: _dedupe_matches(matches) for ticker, matches in dollar_tickers.items()},
        exchange_specs=exchange_specs,
    )


def _dedupe_matches(matches: Iterable[_AliasMatch]) -> tuple[_AliasMatch, ...]:
    unique = {
        (item.symbol, item.alias, item.method, item.points): item
        for item in matches
    }
    return tuple(unique[key] for key in sorted(unique))


def _legal_name_aliases(name: str) -> set[tuple[str, AttributionMethod, int]]:
    normalized = _normalize_text(name)
    words = normalized.split()
    words = _strip_legal_suffixes(words)
    if words and words[0] == "the":
        words = words[1:]
    aliases: set[tuple[str, AttributionMethod, int]] = set()
    if words:
        full = " ".join(words)
        aliases.add((full, "legal_name", 18))
        if len(words) == 1 and len(words[0]) >= 4 and words[0] not in _GENERIC_SINGLE_WORDS:
            aliases.add((words[0], "auto_alias", 15))
    return aliases


def _strip_legal_suffixes(words: list[str]) -> list[str]:
    stripped = list(words)
    while stripped:
        if stripped[-1] in _LEGAL_SUFFIXES:
            stripped.pop()
            continue
        sequence = next(
            (suffix for suffix in _LEGAL_SUFFIX_SEQUENCES if len(stripped) >= len(suffix) and tuple(stripped[-len(suffix) :]) == suffix),
            None,
        )
        if sequence is None:
            break
        del stripped[-len(sequence) :]
    return stripped


def _attribute_title(
    title: str,
    catalog: _AttributionCatalog,
) -> _AttributedEntity | None:
    occurrences = _phrase_occurrences(_normalize_text(title), catalog)

    longest_occurrences = [
        occurrence
        for occurrence in occurrences
        if not any(
            other.start <= occurrence.start
            and other.end >= occurrence.end
            and (other.end - other.start) > (occurrence.end - occurrence.start)
            for other in occurrences
        )
    ]
    matches = [occurrence.match for occurrence in longest_occurrences]
    for found in _DOLLAR_TICKER_RE.finditer(title):
        matches.extend(catalog.dollar_tickers.get(found.group(1).upper(), ()))
    symbols = {match.symbol for match in matches}
    if len(symbols) != 1:
        return None
    selected = max(matches, key=lambda match: (match.points, len(match.alias), match.method))
    if _has_exchange_conflict(title, selected.symbol, catalog):
        return None
    selected_occurrences = tuple(
        occurrence
        for occurrence in longest_occurrences
        if occurrence.match.symbol == selected.symbol
    )
    return _AttributedEntity(match=selected, occurrences=selected_occurrences)


def _is_analyst_source_only(title: str, attribution: _AttributedEntity) -> bool:
    if not attribution.occurrences:
        return False
    title_tokens = _normalize_text(title).split()
    return all(
        _occurrence_is_analyst_source(title_tokens, occurrence)
        for occurrence in attribution.occurrences
    )


def _occurrence_is_analyst_source(
    title_tokens: list[str],
    occurrence: _AliasOccurrence,
) -> bool:
    prefix = title_tokens[: occurrence.start]
    suffix = title_tokens[occurrence.end :]
    prefix_tail = tuple(prefix[-3:])
    suffix_head = tuple(suffix[:3])

    if len(prefix_tail) >= 2 and prefix_tail[-2:] == ("according", "to"):
        return True
    if prefix_tail and prefix_tail[-1] == "by":
        return True
    if len(prefix_tail) >= 2 and prefix_tail[-2] in {"analyst", "analysts"} and prefix_tail[-1] == "at":
        return True
    if len(suffix_head) >= 2 and suffix_head[:2] in {
        ("remains", "bullish"),
        ("remains", "bearish"),
    }:
        return True

    analyst_terms = set(title_tokens)
    if {"price", "target"}.issubset(analyst_terms) or "rating" in analyst_terms:
        return True
    if suffix and suffix[0] in {"upgrades", "downgrades"}:
        return True

    source_verbs = {"expects", "forecasts", "says", "sees"}
    if suffix and suffix[0] in source_verbs:
        _, prefix_event_points = _event_signal(" ".join(prefix))
        return prefix_event_points > 0
    return False


def _phrase_occurrences(normalized_title: str, catalog: _AttributionCatalog) -> list[_AliasOccurrence]:
    title_tokens = normalized_title.split()
    occurrences: list[_AliasOccurrence] = []
    for start, first_token in enumerate(title_tokens):
        for entry in catalog.phrases_by_first_token.get(first_token, ()):
            end = start + len(entry.tokens)
            if end > len(title_tokens) or tuple(title_tokens[start:end]) != entry.tokens:
                continue
            occurrences.extend(
                _AliasOccurrence(match=match, start=start, end=end)
                for match in entry.matches
            )
    return occurrences


def _has_exchange_conflict(title: str, symbol: str, catalog: _AttributionCatalog) -> bool:
    exchange_spec = catalog.exchange_specs.get(symbol)
    if exchange_spec is None:
        return False
    normalized_base, allowed_qualifiers = exchange_spec
    return any(
        _normalize_text(found.group(2)) == normalized_base
        and found.group(1).upper() not in allowed_qualifiers
        for found in _EXCHANGE_REFERENCE_RE.finditer(title)
    )


def _symbol_exchange_spec(symbol: str) -> tuple[str, tuple[str, ...]]:
    suffix = _symbol_suffix(symbol)
    if suffix:
        return symbol[: -len(suffix)], _EXCHANGE_QUALIFIERS[suffix]
    return symbol, _US_EXCHANGE_QUALIFIERS


def _symbol_suffix(symbol: str) -> str | None:
    upper = symbol.upper()
    return next((suffix for suffix in _EXCHANGE_QUALIFIERS if upper.endswith(suffix)), None)


def _event_signal(title: str) -> tuple[str, int]:
    normalized = _normalize_text(title)
    best = ("ordinary_news", 0)
    for event_type, points, phrases in _EVENT_PATTERNS:
        if points <= best[1]:
            continue
        if any(phrase in normalized for phrase in phrases):
            best = (event_type, points)
    return best


def _freshness_points(age_hours: float) -> int:
    if age_hours <= 6:
        return 25
    if age_hours <= 24:
        return 20
    if age_hours <= 48:
        return 12
    return 5


def _publisher_points(publisher: str) -> int:
    normalized = _normalize_text(publisher)
    if _matches_publisher(normalized, _WIRE_PUBLISHERS):
        return 15
    if _matches_publisher(normalized, _RECOGNIZED_PUBLISHERS):
        return 10
    if _matches_publisher(normalized, _LOW_SIGNAL_PUBLISHERS):
        return 0
    return 5 if normalized else 0


def _matches_publisher(publisher: str, catalog: set[str]) -> bool:
    return publisher in catalog or any(publisher.startswith(f"{known} ") for known in catalog)


def _headline_penalty(title: str) -> int:
    normalized = _normalize_text(title)
    return -25 if any(phrase in normalized for phrase in _LOW_SIGNAL_PHRASES) else 0


def _parse_datetime(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return _ensure_utc(parsed)


def _required_datetime(value: Any) -> datetime:
    parsed = _parse_datetime(value)
    if parsed is None:
        raise ValueError(f"invalid datetime: {value!r}")
    return parsed


def _ensure_utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc) if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _normalize_text(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    ascii_text = "".join(char for char in text if not unicodedata.combining(char))
    return re.sub(r"[^a-z0-9]+", " ", ascii_text.lower()).strip()
