"""Pure contracts for macro/news situation briefs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Mapping

Severity = Literal["info", "watch", "risk"]
SignalStrength = Literal["weak", "strong", "event"]
Direction = Literal["bullish", "bearish", "risk_on", "risk_off", "neutral", "mixed"]

MAX_POINT_CHARS = 200
MAX_SOURCES = 8
MAX_SYMBOLS = 12
MAX_ZONES = 8
MAX_FAMILIES = 20
MAX_SYMBOL_SECTIONS = 80
MAX_ZONE_POINTS = 5
MAX_FAMILY_POINTS = 3
MAX_SYMBOL_POINTS = 3
MAX_ALERTS = 8

_VALID_SEVERITIES = {"info", "watch", "risk"}
_VALID_SIGNALS = {"weak", "strong", "event"}
_VALID_DIRECTIONS = {"bullish", "bearish", "risk_on", "risk_off", "neutral", "mixed"}


def _clean_text(value: Any, *, max_chars: int | None = None) -> str:
    text = str(value or "").strip()
    if max_chars is not None and len(text) > max_chars:
        suffix = "..."
        text = text[: max_chars - len(suffix)].rstrip() + suffix
    return text


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


def _clean_sources(item: Mapping[str, Any]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Return readable source names and stable audit references.

    Legacy briefs stored UUIDs directly in ``sources``. When ``source_refs`` is
    absent, preserve those values as references so old JSONL archives remain
    joinable without presenting UUIDs as human-readable publisher names.
    """

    raw_refs = item.get("source_refs")
    if isinstance(raw_refs, (list, tuple)):
        refs = _clean_string_tuple(raw_refs, limit=MAX_SOURCES)
        names = _clean_string_tuple(
            item.get("sources") or item.get("source_names"),
            limit=MAX_SOURCES,
        )
        return names, refs
    return (
        _clean_string_tuple(item.get("source_names"), limit=MAX_SOURCES),
        _clean_string_tuple(item.get("sources"), limit=MAX_SOURCES),
    )


@dataclass(frozen=True)
class SituationPoint:
    """One bounded, sourced macro/news observation."""

    point: str
    # Human-readable publishers/data-source names for operators.
    sources: tuple[str, ...] = ()
    # Stable UUIDs or deterministic local refs used for audit joins.
    source_refs: tuple[str, ...] = ()
    symbols: tuple[str, ...] = ()
    severity: Severity = "info"
    signal: SignalStrength = "weak"
    horizon: str | None = None
    direction: Direction | None = None
    # Marks operational/infrastructure noise (pipeline stalls, data gaps…).
    # Filtered out by read-models before display; never a default True.
    is_operational: bool = False

    @classmethod
    def from_mapping(cls, item: Mapping[str, Any]) -> "SituationPoint | None":
        point = _clean_text(item.get("point"), max_chars=MAX_POINT_CHARS)
        if not point:
            return None
        severity = _clean_text(item.get("severity")) or "info"
        if severity not in _VALID_SEVERITIES:
            severity = "info"
        signal = _clean_text(item.get("signal") or item.get("signal_strength")) or "weak"
        if signal not in _VALID_SIGNALS:
            signal = "weak"
        horizon = _clean_text(item.get("horizon")) or None
        direction = _clean_text(item.get("direction")) or None
        if direction not in _VALID_DIRECTIONS:
            direction = None
        sources, source_refs = _clean_sources(item)
        # Strict bool: only Python True, "true", or "True" map to True.
        raw_op = item.get("is_operational")
        is_operational = raw_op is True or raw_op in ("true", "True")
        return cls(
            point=point,
            sources=sources,
            source_refs=source_refs,
            symbols=_clean_string_tuple(item.get("symbols"), limit=MAX_SYMBOLS),
            severity=severity,  # type: ignore[arg-type]
            signal=signal,  # type: ignore[arg-type]
            horizon=horizon,
            direction=direction,  # type: ignore[arg-type]
            is_operational=is_operational,
        )

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "point": self.point,
            "sources": list(self.sources),
            "source_refs": list(self.source_refs),
            "symbols": list(self.symbols),
            "severity": self.severity,
            "signal": self.signal,
        }
        if self.horizon:
            payload["horizon"] = self.horizon
        if self.direction:
            payload["direction"] = self.direction
        # is_operational only emitted when True — keeps historical payloads compact.
        if self.is_operational:
            payload["is_operational"] = True
        return payload


@dataclass(frozen=True)
class SituationSection:
    """Named section of situation points, for a zone, family, or symbol."""

    name: str
    points: tuple[SituationPoint, ...]

    def to_list(self) -> list[dict[str, Any]]:
        return [point.to_dict() for point in self.points]


def _sections_from_mapping(
    raw: Any,
    *,
    max_sections: int,
    max_points: int,
) -> tuple[SituationSection, ...]:
    if not isinstance(raw, Mapping):
        return ()
    sections: list[SituationSection] = []
    for raw_name, raw_points in raw.items():
        name = _clean_text(raw_name)
        if not name or not isinstance(raw_points, list):
            continue
        points: list[SituationPoint] = []
        for raw_point in raw_points:
            if not isinstance(raw_point, Mapping):
                continue
            point = SituationPoint.from_mapping(raw_point)
            if point is None:
                continue
            points.append(point)
            if len(points) >= max_points:
                break
        if points:
            sections.append(SituationSection(name=name, points=tuple(points)))
        if len(sections) >= max_sections:
            break
    return tuple(sections)


def _points_from_list(raw: Any, *, max_points: int) -> tuple[SituationPoint, ...]:
    if not isinstance(raw, list):
        return ()
    points: list[SituationPoint] = []
    for raw_point in raw:
        if not isinstance(raw_point, Mapping):
            continue
        point = SituationPoint.from_mapping(raw_point)
        if point is None:
            continue
        points.append(point)
        if len(points) >= max_points:
            break
    return tuple(points)


@dataclass(frozen=True)
class NewsMacroBrief:
    """Bounded daily situation brief produced by the macro/news analyst."""

    brief_id: str
    venue: str
    as_of: str
    valid_until: str
    input_refs: dict[str, Any] | None = None
    zones: tuple[SituationSection, ...] = ()
    families: tuple[SituationSection, ...] = ()
    symbols: tuple[SituationSection, ...] = ()
    alerts: tuple[SituationPoint, ...] = ()

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> "NewsMacroBrief | None":
        as_of = _clean_text(payload.get("as_of"))
        valid_until = _clean_text(payload.get("valid_until"))
        if not as_of or not valid_until:
            return None
        venue = _clean_text(payload.get("venue")) or "GLOBAL"
        brief_id = _clean_text(payload.get("brief_id")) or f"{as_of}|{venue}"
        input_refs = payload.get("input_refs")
        if not isinstance(input_refs, dict):
            input_refs = None
        return cls(
            brief_id=brief_id,
            venue=venue,
            as_of=as_of,
            valid_until=valid_until,
            input_refs=dict(input_refs) if input_refs else None,
            zones=_sections_from_mapping(
                payload.get("zones"),
                max_sections=MAX_ZONES,
                max_points=MAX_ZONE_POINTS,
            ),
            families=_sections_from_mapping(
                payload.get("families"),
                max_sections=MAX_FAMILIES,
                max_points=MAX_FAMILY_POINTS,
            ),
            symbols=_sections_from_mapping(
                payload.get("symbols"),
                max_sections=MAX_SYMBOL_SECTIONS,
                max_points=MAX_SYMBOL_POINTS,
            ),
            alerts=_points_from_list(payload.get("alerts"), max_points=MAX_ALERTS),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "brief_id": self.brief_id,
            "venue": self.venue,
            "as_of": self.as_of,
            "valid_until": self.valid_until,
            "input_refs": self.input_refs or {},
            "zones": {section.name: section.to_list() for section in self.zones},
            "families": {section.name: section.to_list() for section in self.families},
            "symbols": {section.name: section.to_list() for section in self.symbols},
            "alerts": [point.to_dict() for point in self.alerts],
        }

    def ref(self, *, date: str) -> dict[str, str]:
        return {
            "date": date,
            "venue": self.venue,
            "brief_id": self.brief_id,
            "as_of": self.as_of,
        }
