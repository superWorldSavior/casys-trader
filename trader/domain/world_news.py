"""Closed news-signal vocabulary for ABOUT overlay hops. Stdlib/domain only.

``DriverNewsBundle`` is frozen onto ``DriverState`` for ``news_macro`` ABOUT
relations. It copies closed brief semantics (event class, direction, strength,
severity, horizon bucket, attribution quality), never point text, titles,
UUIDs, links, or symbols. Free-text brief fields enter only through versioned,
deterministic derivation rules; operational/infrastructure noise points are
rejected at the boundary.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from trader.domain.situation.brief import SITUATION_EVENT_CLASSES, SituationPoint

NEWS_PRODUCER_VERSION = "world_news_brief.v1"
NEWS_TRANSFORM_VERSION = "news_signal.v1"
DRIVER_NEWS_BUNDLE_SCHEMA = "driver_news_bundle.v2"
DRIVER_NEWS_BUNDLE_SCHEMA_V1 = "driver_news_bundle.v1"
NEWS_EVENT_CLASS_RULE_VERSION = "news_event_class.v1"
NEWS_HORIZON_RULE_VERSION = "news_horizon.v1"

# Canonical set lives with the analyst contract (situation/brief.py); the
# world-model name stays as an alias so existing readers do not churn.
NEWS_EVENT_CLASSES = SITUATION_EVENT_CLASSES
NEWS_EVENT_CLASS_SOURCES = frozenset({"analyst", "keywords"})
NEWS_DIRECTIONS = frozenset(
    {
        "bullish",
        "bearish",
        "risk_on",
        "risk_off",
        "neutral",
        "mixed",
        "unspecified",
    }
)
NEWS_STRENGTHS = frozenset({"event", "strong", "weak"})
NEWS_SEVERITIES = frozenset({"info", "watch", "risk"})
NEWS_HORIZON_BUCKETS = frozenset(
    {
        "reported",
        "days",
        "weeks",
        "months",
        "quarters",
        "years",
        "unspecified",
    }
)
NEWS_ATTRIBUTION_QUALITY = frozenset({"symbol_sourced", "symbol_only", "unattributed"})

# First match wins, in class order. Substring match on lowercased text.
# The corpus is bilingual analyst prose; absence assertions win over topics.
_EVENT_CLASS_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "no_news",
        (
            "no fresh",
            "no catalyst",
            "no news",
            "zero fresh",
            "signal fondamental absent",
            "aucune news",
            "aucun flux",
            "sans news",
            "sans catalyseur",
            "sans flux",
            "pas de catalyseur",
            "pas de catalyst",
            "background, not fresh",
            "older than as_of",
        ),
    ),
    (
        "earnings",
        (
            "earnings",
            "eps ",
            "quarterly results",
            "earnings call",
            "10-q",
            "10-k",
            "miss estimates",
            "beat estimates",
            "résultats",
            "results beat",
            "results miss",
            "bénéfice",
            "chiffre d'affaires",
        ),
    ),
    ("guidance", ("guidance", "outlook", "forecast", "full-year", "raises outlook", "lowers outlook")),
    (
        "corporate_action",
        (
            "merger",
            "fusion",
            "acquisition",
            "acquire",
            "takeover",
            "buyout",
            "joint venture",
            "spinoff",
            "spin-off",
            " ipo",
            "delist",
            "tender offer",
            "majority stake",
            "licensing deal",
        ),
    ),
    (
        "regulatory",
        (
            "regulat",
            "antitrust",
            "lawsuit",
            " sues",
            "court",
            " fine",
            "penalty",
            "investigation",
            "fraud",
            "sec ",
            "sec,",
        ),
    ),
    (
        "capital",
        (
            "dividend",
            "dividende",
            "buyback",
            "rachat",
            "share repurchase",
            "stock split",
            "reverse split",
            "capital raise",
            "debt offering",
            "secondary offering",
        ),
    ),
    (
        "analyst",
        (
            "upgrade",
            "downgrade",
            "price target",
            "objectif de cours",
            "overweight",
            "underweight",
            "outperform",
            "underperform",
            "buy rating",
            "sell rating",
            "recommandation",
            "analyst",
        ),
    ),
    (
        "operations",
        (
            "ceo",
            "cfo",
            "executive",
            "strike",
            "layoff",
            "plant",
            "factory",
            "product launch",
            "recall",
            "contract",
            "partnership",
            "expansion",
            "shutdown",
            "orders",
            "order book",
            "new order",
            "deliver",
            "backlog",
            "trial",
            "phase 3",
            "phase 2",
            "fda",
            "collaboration",
        ),
    ),
    (
        "macro",
        (
            "fed ",
            "ecb",
            "fomc",
            "boj",
            "rate cut",
            "rate hike",
            " rates",
            "taux",
            "inflation",
            " cpi",
            " gdp",
            "recession",
            "central bank",
            "tariff",
            "rotation",
            "risk-on",
            "risk-off",
            "risk on",
            "risk off",
        ),
    ),
)

# Checked first, in this order; reported markers only win when no rule matches.
_HORIZON_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("years", ("year", "12-month", "12-24", " fy", "fiscal")),
    ("quarters", ("quarter", "q1", "q2", "q3", "q4", "h1", "h2", "half-year", "half year")),
    ("months", ("month",)),
    ("weeks", ("week",)),
    ("days", ("day", "today", "tomorrow", "intraday", "48h")),
)
_REPORTED_MARKERS = ("reported", "announced", "yesterday")

_NEWS_BUNDLE_KEYS = frozenset(
    {
        "schema_version",
        "event_class",
        "event_class_source",
        "direction",
        "strength",
        "severity",
        "horizon_bucket",
        "attribution_quality",
    }
)
_FORBIDDEN_NEWS_KEYS = frozenset(
    {
        "point",
        "title",
        "uuid",
        "link",
        "symbol",
        "symbols",
        "brief_id",
        "sources",
        "source_refs",
        "text",
        "report_text",
        "raw_text",
        "summary",
    }
)


def _required_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a non-empty string")
    text = value.strip()
    if not text:
        raise ValueError(f"{field_name} must be a non-empty string")
    return text


def _closed_member(value: Any, field_name: str, allowed: frozenset[str]) -> str:
    text = _required_text(value, field_name)
    if text not in allowed:
        allowed_text = ", ".join(sorted(allowed))
        raise ValueError(f"{field_name} must be one of: {allowed_text}")
    return text


def classify_news_event_class(text: Any) -> str:
    """Deterministic free-text → closed event class (``news_event_class.v1``).

    Legacy keyword path: it classified the persisted v1 bundles, but new
    bundles read the analyst-stored field instead. Kept (and tested) to
    document v1 history semantics — never a fallback for new writes.
    """

    lowered = text.strip().lower() if isinstance(text, str) else ""
    if not lowered:
        return "unknown"
    for event_class, keywords in _EVENT_CLASS_KEYWORDS:
        if any(keyword in lowered for keyword in keywords):
            return event_class
    return "unknown"


def bucket_news_horizon(text: Any) -> str:
    """Deterministic free-text horizon → closed bucket (``news_horizon.v1``)."""

    lowered = text.strip().lower() if isinstance(text, str) else ""
    if not lowered:
        return "unspecified"
    for bucket, keywords in _HORIZON_RULES:
        if any(keyword in lowered for keyword in keywords):
            return bucket
    if any(marker in lowered for marker in _REPORTED_MARKERS):
        return "reported"
    return "unspecified"


def news_signal_from_point(point: SituationPoint | Mapping[str, Any]) -> DriverNewsBundle:
    """Pure brief point → closed news bundle. Rejects operational noise.

    The event class is the analyst-stored field, never re-derived: a point
    without one fails loud (callers count the exclusion).
    """

    if isinstance(point, Mapping) and not isinstance(point, SituationPoint):
        parsed = SituationPoint.from_mapping(point)
        if parsed is None:
            raise ValueError("news point has no usable text")
        point = parsed
    if not isinstance(point, SituationPoint):
        raise TypeError("news point must be SituationPoint or a mapping")
    if point.is_operational:
        raise ValueError("operational noise points cannot carry a news signal")
    if not point.event_class:
        raise ValueError("news point has no analyst event_class")
    symbols = tuple(point.symbols)
    source_refs = tuple(point.source_refs)
    if symbols and source_refs:
        attribution = "symbol_sourced"
    elif symbols:
        attribution = "symbol_only"
    else:
        attribution = "unattributed"
    return DriverNewsBundle(
        event_class=point.event_class,
        event_class_source="analyst",
        direction=point.direction or "unspecified",
        strength=point.signal,
        severity=point.severity,
        horizon_bucket=bucket_news_horizon(point.horizon),
        attribution_quality=attribution,
    )


@dataclass(frozen=True)
class DriverNewsBundle:
    """Closed news semantics for one ABOUT overlay hop. No raw text."""

    event_class: str
    event_class_source: str
    direction: str
    strength: str
    severity: str
    horizon_bucket: str
    attribution_quality: str
    schema_version: str = DRIVER_NEWS_BUNDLE_SCHEMA

    def __post_init__(self) -> None:
        schema_version = _required_text(self.schema_version, "schema_version")
        if schema_version not in {DRIVER_NEWS_BUNDLE_SCHEMA, DRIVER_NEWS_BUNDLE_SCHEMA_V1}:
            raise ValueError(
                f"schema_version must be {DRIVER_NEWS_BUNDLE_SCHEMA} or {DRIVER_NEWS_BUNDLE_SCHEMA_V1}"
            )
        object.__setattr__(self, "schema_version", schema_version)
        object.__setattr__(self, "event_class", _closed_member(self.event_class, "event_class", NEWS_EVENT_CLASSES))
        object.__setattr__(
            self,
            "event_class_source",
            _closed_member(self.event_class_source, "event_class_source", NEWS_EVENT_CLASS_SOURCES),
        )
        object.__setattr__(self, "direction", _closed_member(self.direction, "direction", NEWS_DIRECTIONS))
        object.__setattr__(self, "strength", _closed_member(self.strength, "strength", NEWS_STRENGTHS))
        object.__setattr__(self, "severity", _closed_member(self.severity, "severity", NEWS_SEVERITIES))
        object.__setattr__(
            self,
            "horizon_bucket",
            _closed_member(self.horizon_bucket, "horizon_bucket", NEWS_HORIZON_BUCKETS),
        )
        object.__setattr__(
            self,
            "attribution_quality",
            _closed_member(self.attribution_quality, "attribution_quality", NEWS_ATTRIBUTION_QUALITY),
        )

    def identity_tuple(self) -> tuple[str, str, str, str, str, str, str]:
        return (
            self.event_class,
            self.event_class_source,
            self.direction,
            self.strength,
            self.severity,
            self.horizon_bucket,
            self.attribution_quality,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "event_class": self.event_class,
            "event_class_source": self.event_class_source,
            "direction": self.direction,
            "strength": self.strength,
            "severity": self.severity,
            "horizon_bucket": self.horizon_bucket,
            "attribution_quality": self.attribution_quality,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | DriverNewsBundle) -> DriverNewsBundle:
        if isinstance(value, DriverNewsBundle):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("driver news bundle must be DriverNewsBundle or a mapping")
        leaked = sorted(str(key) for key in value if key in _FORBIDDEN_NEWS_KEYS)
        if leaked:
            raise ValueError(f"driver news bundle forbids raw fields: {', '.join(leaked)}")
        unknown = sorted(str(key) for key in value if key not in _NEWS_BUNDLE_KEYS)
        if unknown:
            raise ValueError(f"driver news bundle forbids unknown fields: {', '.join(unknown)}")
        raw_version = value.get("schema_version", DRIVER_NEWS_BUNDLE_SCHEMA)
        if raw_version == DRIVER_NEWS_BUNDLE_SCHEMA_V1:
            # Pre-source era: every persisted v1 bundle came from keyword
            # classification (the stored-field path did not exist). Historical
            # fact, not a guess — and v1 dicts never carry the key.
            if "event_class_source" in value:
                raise ValueError("driver_news_bundle.v1 forbids event_class_source")
            source: Any = "keywords"
        else:
            source = value.get("event_class_source")
        return cls(
            schema_version=raw_version,
            event_class=value.get("event_class"),
            event_class_source=source,
            direction=value.get("direction"),
            strength=value.get("strength"),
            severity=value.get("severity"),
            horizon_bucket=value.get("horizon_bucket"),
            attribution_quality=value.get("attribution_quality"),
        )


__all__ = [
    "DRIVER_NEWS_BUNDLE_SCHEMA",
    "DRIVER_NEWS_BUNDLE_SCHEMA_V1",
    "NEWS_ATTRIBUTION_QUALITY",
    "NEWS_DIRECTIONS",
    "NEWS_EVENT_CLASS_RULE_VERSION",
    "NEWS_EVENT_CLASS_SOURCES",
    "NEWS_EVENT_CLASSES",
    "NEWS_HORIZON_BUCKETS",
    "NEWS_HORIZON_RULE_VERSION",
    "NEWS_PRODUCER_VERSION",
    "NEWS_SEVERITIES",
    "NEWS_STRENGTHS",
    "NEWS_TRANSFORM_VERSION",
    "DriverNewsBundle",
    "bucket_news_horizon",
    "classify_news_event_class",
    "news_signal_from_point",
]
