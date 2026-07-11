"""Pure bounded aggregation of regional macro/news briefs."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, cast

from trader.domain.situation.brief import MAX_POINT_CHARS, NewsMacroBrief, SituationPoint

DEFAULT_MAX_DIGEST_POINTS = 5
_MAX_DIGEST_SOURCE_REFS = 8

Regime = Literal["risk_on", "risk_off", "mixed", "unknown"]
RatesBias = Literal["hawkish", "dovish", "neutral", "unknown"]
UsdBias = Literal["strong", "weak", "neutral", "unknown"]

_VALID_REGIMES = {"risk_on", "risk_off", "mixed", "unknown"}
_VALID_RATES_BIASES = {"hawkish", "dovish", "neutral", "unknown"}
_VALID_USD_BIASES = {"strong", "weak", "neutral", "unknown"}


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def _clean_string_tuple(value: Any, *, limit: int) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    items: list[str] = []
    seen: set[str] = set()
    for raw in value:
        item = _clean_text(raw)
        if not item or item in seen:
            continue
        items.append(item)
        seen.add(item)
        if len(items) >= limit:
            break
    return tuple(items)


def _normalise_enum(value: Any, *, allowed: set[str]) -> str:
    candidate = _clean_text(value).lower()
    return candidate if candidate in allowed else "unknown"


def _normalise_points(value: Any, *, limit: int) -> tuple[SituationPoint, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    points: list[SituationPoint] = []
    for raw in value:
        if isinstance(raw, SituationPoint):
            raw = raw.to_dict()
        if not isinstance(raw, Mapping):
            continue
        point = SituationPoint.from_mapping(raw)
        if point is None or len(point.point) > MAX_POINT_CHARS:
            continue
        points.append(point)
        if len(points) >= limit:
            break
    return tuple(points)


def _normalise_coverage(value: Any, *, point_count: int) -> dict[str, Any]:
    raw = value if isinstance(value, Mapping) else {}
    return {
        "venues_seen": _clean_string_tuple(raw.get("venues_seen"), limit=_MAX_DIGEST_SOURCE_REFS),
        "point_count": point_count,
    }


@dataclass(frozen=True)
class GlobalSituationDigest:
    """Compact, sourced global macro view built from regional briefs."""

    as_of: str
    regime: str = "unknown"
    rates_bias: str = "unknown"
    usd_bias: str = "unknown"
    points: tuple[SituationPoint, ...] = ()
    source_refs: tuple[str, ...] = ()
    coverage: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        points = _normalise_points(self.points, limit=DEFAULT_MAX_DIGEST_POINTS)
        object.__setattr__(self, "as_of", _clean_text(self.as_of))
        object.__setattr__(
            self,
            "regime",
            _normalise_enum(self.regime, allowed=_VALID_REGIMES),
        )
        object.__setattr__(
            self,
            "rates_bias",
            _normalise_enum(self.rates_bias, allowed=_VALID_RATES_BIASES),
        )
        object.__setattr__(
            self,
            "usd_bias",
            _normalise_enum(self.usd_bias, allowed=_VALID_USD_BIASES),
        )
        object.__setattr__(self, "points", points)
        object.__setattr__(
            self,
            "source_refs",
            _clean_string_tuple(self.source_refs, limit=_MAX_DIGEST_SOURCE_REFS),
        )
        object.__setattr__(
            self,
            "coverage",
            _normalise_coverage(self.coverage, point_count=len(points)),
        )

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> "GlobalSituationDigest":
        points = _normalise_points(
            payload.get("points"),
            limit=DEFAULT_MAX_DIGEST_POINTS,
        )
        return cls(
            as_of=_clean_text(payload.get("as_of")),
            regime=_normalise_enum(payload.get("regime"), allowed=_VALID_REGIMES),
            rates_bias=_normalise_enum(
                payload.get("rates_bias"),
                allowed=_VALID_RATES_BIASES,
            ),
            usd_bias=_normalise_enum(payload.get("usd_bias"), allowed=_VALID_USD_BIASES),
            points=points,
            source_refs=_clean_string_tuple(
                payload.get("source_refs"),
                limit=_MAX_DIGEST_SOURCE_REFS,
            ),
            coverage=_normalise_coverage(payload.get("coverage"), point_count=len(points)),
        )

    def to_dict(self) -> dict[str, Any]:
        coverage = _normalise_coverage(self.coverage, point_count=len(self.points))
        return {
            "as_of": self.as_of,
            "regime": self.regime,
            "rates_bias": self.rates_bias,
            "usd_bias": self.usd_bias,
            "points": [point.to_dict() for point in self.points],
            "source_refs": list(self.source_refs),
            "coverage": {
                "venues_seen": list(coverage["venues_seen"]),
                "point_count": coverage["point_count"],
            },
        }


@dataclass(frozen=True)
class _PointEntry:
    venue: str
    section_type: str
    section_name: str | None
    section_order: int
    point_order: int
    point: SituationPoint
    is_global: bool = False

    @property
    def priority(self) -> tuple[int, int, int, str, int, int, str, str]:
        severity_score = {"risk": 300, "watch": 100, "info": 0}.get(
            self.point.severity,
            0,
        )
        signal_score = {"event": 200, "strong": 100, "weak": 0}.get(
            self.point.signal,
            0,
        )
        section_score = {"alerts": 40, "symbols": 30, "families": 20, "zones": 10}.get(
            self.section_type,
            0,
        )
        return (
            0 if self.is_global else 1,
            -(severity_score + signal_score),
            -section_score,
            self.venue,
            self.section_order,
            self.point_order,
            self.section_name or "",
            self.point.point,
        )


def _section_entries(
    *,
    venue: str,
    section_type: str,
    sections: Iterable[Any],
) -> list[_PointEntry]:
    entries: list[_PointEntry] = []
    for section_order, section in enumerate(sections):
        name = _clean_text(getattr(section, "name", None))
        points = getattr(section, "points", ())
        if not name or not isinstance(points, tuple):
            continue
        for point_order, point in enumerate(points):
            if isinstance(point, SituationPoint):
                entries.append(
                    _PointEntry(
                        venue=venue,
                        section_type=section_type,
                        section_name=name,
                        section_order=section_order,
                        point_order=point_order,
                        point=point,
                    )
                )
    return entries


def _brief_entries(venue: str, brief: NewsMacroBrief) -> list[_PointEntry]:
    entries = _section_entries(venue=venue, section_type="zones", sections=brief.zones)
    entries.extend(_section_entries(venue=venue, section_type="families", sections=brief.families))
    entries.extend(_section_entries(venue=venue, section_type="symbols", sections=brief.symbols))
    entries.extend(
        _PointEntry(
            venue=venue,
            section_type="alerts",
            section_name=None,
            section_order=0,
            point_order=index,
            point=point,
        )
        for index, point in enumerate(brief.alerts)
        if isinstance(point, SituationPoint)
    )
    return entries


def _point_weight(point: SituationPoint) -> int:
    severity = {"info": 1, "watch": 2, "risk": 3}.get(point.severity, 1)
    signal = {"weak": 1, "strong": 2, "event": 3}.get(point.signal, 1)
    return severity * signal


def _normalise_for_match(point: SituationPoint) -> str:
    return " ".join(point.point.lower().replace("-", " ").split())


def _resolve_bias(
    positive: int,
    negative: int,
    *,
    positive_label: str,
    negative_label: str,
    neutral_label: str,
    unknown_label: str,
) -> str:
    if positive == negative:
        return unknown_label if positive == 0 else neutral_label
    return positive_label if positive > negative else negative_label


def _derive_regime(points: Iterable[SituationPoint]) -> Regime:
    risk_on = 0
    risk_off = 0
    for point in points:
        direction = point.direction
        text = _normalise_for_match(point)
        weight = _point_weight(point)
        if direction in {"risk_on", "bullish"} or "risk on" in text:
            risk_on += weight
        elif direction in {"risk_off", "bearish"} or "risk off" in text:
            risk_off += weight
    result = _resolve_bias(
        risk_on,
        risk_off,
        positive_label="risk_on",
        negative_label="risk_off",
        neutral_label="mixed",
        unknown_label="unknown",
    )
    return cast(Regime, result)


def _derive_rates_bias(points: Iterable[SituationPoint]) -> RatesBias:
    hawkish = 0
    dovish = 0
    hawkish_markers = (
        "hawkish",
        "rate hike",
        "higher rates",
        "yields rise",
        "yield rises",
        "tightening",
    )
    dovish_markers = (
        "dovish",
        "rate cut",
        "lower rates",
        "yields fall",
        "yield falls",
        "easing",
    )
    for point in points:
        text = _normalise_for_match(point)
        weight = _point_weight(point)
        has_hawkish = any(marker in text for marker in hawkish_markers)
        has_dovish = any(marker in text for marker in dovish_markers)
        if has_hawkish and not has_dovish:
            hawkish += weight
        elif has_dovish and not has_hawkish:
            dovish += weight
    result = _resolve_bias(
        hawkish,
        dovish,
        positive_label="hawkish",
        negative_label="dovish",
        neutral_label="neutral",
        unknown_label="unknown",
    )
    return cast(RatesBias, result)


def _derive_usd_bias(points: Iterable[SituationPoint]) -> UsdBias:
    strong = 0
    weak = 0
    strong_markers = (
        "strong dollar",
        "strong usd",
        "dollar is strong",
        "usd is strong",
        "usd strengthens",
        "dollar strengthens",
        "dxy rises",
    )
    weak_markers = (
        "weak dollar",
        "weak usd",
        "dollar is weak",
        "usd is weak",
        "usd weakens",
        "dollar weakens",
        "dxy falls",
    )
    for point in points:
        text = _normalise_for_match(point)
        weight = _point_weight(point)
        has_strong = any(marker in text for marker in strong_markers)
        has_weak = any(marker in text for marker in weak_markers)
        if has_strong and not has_weak:
            strong += weight
        elif has_weak and not has_strong:
            weak += weight
    result = _resolve_bias(
        strong,
        weak,
        positive_label="strong",
        negative_label="weak",
        neutral_label="neutral",
        unknown_label="unknown",
    )
    return cast(UsdBias, result)


def build_global_situation_digest(
    regional_briefs: Mapping[str, NewsMacroBrief],
    *,
    as_of: str,
    max_points: int = DEFAULT_MAX_DIGEST_POINTS,
    global_brief: NewsMacroBrief | None = None,
) -> GlobalSituationDigest:
    """Aggregate salient, sourced regional observations into one global digest.

    When *global_brief* is provided its points are ranked first (regardless of
    severity/signal), filling the cap before any regional point is considered.
    Regional briefs complement the remaining slots.
    """

    point_limit = min(DEFAULT_MAX_DIGEST_POINTS, max(0, int(max_points)))
    brief_items = [
        (_clean_text(venue), brief)
        for venue, brief in regional_briefs.items()
        if _clean_text(venue) and isinstance(brief, NewsMacroBrief)
    ]
    entries: list[_PointEntry] = []
    venues_seen: list[str] = []
    for venue, brief in sorted(
        brief_items,
        key=lambda item: (item[0], item[1].venue, item[1].brief_id),
    ):
        if venue not in venues_seen:
            venues_seen.append(venue)
        entries.extend(_brief_entries(venue, brief))

    if global_brief is not None:
        global_venue = _clean_text(global_brief.venue) or "GLOBAL"
        global_entries = [
            _PointEntry(
                venue=e.venue,
                section_type=e.section_type,
                section_name=e.section_name,
                section_order=e.section_order,
                point_order=e.point_order,
                point=e.point,
                is_global=True,
            )
            for e in _brief_entries(global_venue, global_brief)
        ]
        entries = global_entries + entries

    selected: list[SituationPoint] = []
    for entry in sorted(entries, key=lambda item: item.priority):
        if len(selected) >= point_limit:
            break
        point = _normalise_points((entry.point,), limit=1)
        if not point or not (point[0].sources or point[0].source_refs):
            continue
        selected.append(point[0])

    source_refs = _clean_string_tuple(
        [ref for point in selected for ref in point.source_refs],
        limit=_MAX_DIGEST_SOURCE_REFS,
    )
    points = tuple(selected)
    return GlobalSituationDigest(
        as_of=as_of,
        regime=_derive_regime(points),
        rates_bias=_derive_rates_bias(points),
        usd_bias=_derive_usd_bias(points),
        points=points,
        source_refs=source_refs,
        coverage={"venues_seen": tuple(venues_seen), "point_count": len(points)},
    )


__all__ = [
    "DEFAULT_MAX_DIGEST_POINTS",
    "GlobalSituationDigest",
    "build_global_situation_digest",
]
