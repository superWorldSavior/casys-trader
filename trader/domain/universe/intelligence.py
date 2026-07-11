"""Pure contracts for the universe-intelligence pass.

The module deliberately contains no storage, runtime, or LLM concerns.  It
projects a canonical macro/news brief into a bounded decision context and
builds stable identifiers/snapshots for a venue candidate scope.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

from trader.domain.company import CompanyIntelligenceBrief
from trader.domain.semantic.catalog import family_for_symbol
from trader.domain.situation import NewsMacroBrief, SituationPoint

DEFAULT_MAX_SITUATION_POINTS = 48
DEFAULT_MAX_SITUATION_TEXT_CHARS = 6_000
# Keep these domain defaults aligned with config/company_intelligence.yaml.
DEFAULT_SUMMARY_MAX_CHARS = 240
DEFAULT_MAX_POINTS = 5
UNCLASSIFIED_FAMILY = "unclassified"

SituationStatus = Literal["active", "not_available", "inactive", "venue_mismatch"]
CompanyContextStatus = Literal[
    "fresh",
    "partial",
    "stale",
    "missing",
    "unsupported",
    "identity_mismatch",
]


@dataclass(frozen=True)
class UniverseCompanyContext:
    """Bounded per-candidate projection of durable company research."""

    mode: str
    symbols: Mapping[str, Mapping[str, Any]]
    coverage: Mapping[str, int]

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "symbols": _json_copy(dict(self.symbols)),
            "coverage": dict(self.coverage),
        }


def project_company_briefs_to_universe_context(
    briefs_by_symbol: Mapping[str, CompanyIntelligenceBrief | None],
    *,
    candidate_symbols: Iterable[str],
    active_at: datetime | str,
    mode: str = "active",
) -> UniverseCompanyContext:
    """Keep one compact entry for every candidate, including missing briefs."""

    normalized_mode = str(mode or "observe").strip().lower()
    if normalized_mode not in {"observe", "active"}:
        normalized_mode = "observe"
    entries: dict[str, Mapping[str, Any]] = {}
    counts = {
        "fresh": 0,
        "partial": 0,
        "stale": 0,
        "missing": 0,
        "unsupported": 0,
        "identity_mismatch": 0,
    }
    for raw_symbol in candidate_symbols:
        symbol = str(raw_symbol or "").strip()
        if not symbol or symbol in entries:
            continue
        brief = briefs_by_symbol.get(symbol)
        entry = _project_company_brief(symbol, brief, active_at=active_at)
        status = str(entry["status"])
        counts[status] += 1
        entries[symbol] = entry
    return UniverseCompanyContext(mode=normalized_mode, symbols=entries, coverage=counts)


def _project_company_brief(
    symbol: str,
    brief: CompanyIntelligenceBrief | None,
    *,
    active_at: datetime | str,
) -> Mapping[str, Any]:
    if brief is None:
        return {"status": "missing", "brief_ref": None}
    if brief.symbol != symbol:
        return {"status": "identity_mismatch", "brief_ref": None}
    coverage_status = str(brief.coverage.get("status") or "partial")
    freshness = brief.freshness_status(active_at)
    if coverage_status == "unsupported":
        status: CompanyContextStatus = "unsupported"
    elif freshness == "stale":
        status = "stale"
    elif freshness in {"mixed", "missing", "unknown"} or coverage_status != "full":
        status = "partial"
    else:
        status = "fresh"

    drivers = [point.point for point in brief.company_thesis.pillars[:DEFAULT_MAX_POINTS]]
    summary = brief.company_thesis.summary or brief.business.summary
    return {
        "status": status,
        "brief_ref": brief.ref(),
        "as_of": brief.as_of,
        "input_signature": brief.input_signature,
        "freshness": freshness,
        "company_thesis_status": brief.company_thesis.status,
        "selection_view": brief.selection_view.to_dict(),
        "security_readiness": brief.security_readiness,
        "summary": summary[:DEFAULT_SUMMARY_MAX_CHARS],
        "drivers": drivers,
        "catalysts": [point.point for point in brief.catalysts[:DEFAULT_MAX_POINTS]],
        "risks": [point.point for point in brief.risks[:DEFAULT_MAX_POINTS]],
        "coverage": dict(brief.coverage),
        "source_refs": list(brief.source_refs[:DEFAULT_MAX_POINTS]),
    }


@dataclass(frozen=True)
class UniverseSituationContext:
    """Bounded, prompt-safe projection of one venue's active situation brief."""

    status: SituationStatus
    brief_ref: Mapping[str, str] | None = None
    metadata: Mapping[str, str] | None = None
    coverage: Mapping[str, Any] = field(default_factory=dict)
    zones: Mapping[str, list[dict[str, Any]]] = field(default_factory=dict)
    families: Mapping[str, list[dict[str, Any]]] = field(default_factory=dict)
    symbols: Mapping[str, list[dict[str, Any]]] = field(default_factory=dict)
    alerts: list[dict[str, Any]] = field(default_factory=list)
    truncated: bool = False
    point_count: int = 0
    text_chars: int = 0

    @classmethod
    def not_available(cls, *, candidate_count: int = 0) -> "UniverseSituationContext":
        return cls(
            status="not_available",
            coverage=_missing_coverage(candidate_count=max(0, int(candidate_count))),
        )

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "status": self.status,
            "coverage": _json_copy(dict(self.coverage)),
        }
        if self.status != "active":
            return payload
        payload.update(
            {
                "brief_ref": _json_copy(dict(self.brief_ref or {})),
                "metadata": _json_copy(dict(self.metadata or {})),
                "zones": _json_copy(dict(self.zones)),
                "families": _json_copy(dict(self.families)),
                "symbols": _json_copy(dict(self.symbols)),
                "alerts": _json_copy(list(self.alerts)),
                "truncated": bool(self.truncated),
                "point_count": int(self.point_count),
                "text_chars": int(self.text_chars),
            }
        )
        return payload


@dataclass(frozen=True)
class _PointEntry:
    section_type: str
    section_name: str | None
    section_order: int
    point_order: int
    point: SituationPoint

    @property
    def priority(self) -> tuple[int, int, int, int, str]:
        severity_score = {"risk": 300, "watch": 100, "info": 0}.get(self.point.severity, 0)
        signal_score = {"event": 200, "strong": 100, "weak": 0}.get(self.point.signal, 0)
        section_score = {"alerts": 40, "symbols": 30, "families": 20, "zones": 10}.get(
            self.section_type,
            0,
        )
        return (
            -(severity_score + signal_score),
            -section_score,
            self.section_order,
            self.point_order,
            self.section_name or "",
        )


def project_brief_to_universe_context(
    brief: NewsMacroBrief | None,
    *,
    venue: str,
    candidate_symbols: Iterable[str],
    active_at: datetime | str,
    coverage_metadata: Mapping[str, Any] | None = None,
    max_points: int = DEFAULT_MAX_SITUATION_POINTS,
    max_text_chars: int = DEFAULT_MAX_SITUATION_TEXT_CHARS,
) -> UniverseSituationContext:
    """Project one active brief without exposing ``input_refs`` or raw news.

    Points are selected globally by risk/event/strong priority. Family section
    names are always retained; symbol sections are intersected with the current
    candidate pool.  Coverage is a deliberately small derived envelope rather
    than a copy of the analyst's raw input references.
    """

    pool = {str(symbol).strip() for symbol in candidate_symbols if str(symbol).strip()}
    requested_venue = str(venue or "").strip()
    if brief is None:
        return UniverseSituationContext.not_available(candidate_count=len(pool))
    if brief.venue != requested_venue:
        return UniverseSituationContext(
            status="venue_mismatch",
            coverage=_missing_coverage(candidate_count=len(pool)),
        )
    if not _brief_is_active(brief, active_at):
        return UniverseSituationContext(
            status="inactive",
            coverage=_missing_coverage(candidate_count=len(pool)),
        )

    point_limit = max(0, int(max_points))
    text_limit = max(0, int(max_text_chars))
    zone_names = [section.name for section in brief.zones]
    family_names = [section.name for section in brief.families]
    symbol_names = [section.name for section in brief.symbols if section.name in pool]
    zones: dict[str, list[dict[str, Any]]] = {name: [] for name in zone_names}
    families: dict[str, list[dict[str, Any]]] = {name: [] for name in family_names}
    symbols: dict[str, list[dict[str, Any]]] = {name: [] for name in symbol_names}
    alerts: list[dict[str, Any]] = []

    entries: list[_PointEntry] = []
    entries.extend(_section_entries("zones", brief.zones))
    entries.extend(_section_entries("families", brief.families))
    entries.extend(
        entry
        for entry in _section_entries("symbols", brief.symbols)
        if entry.section_name in pool
    )
    entries.extend(
        _PointEntry(
            section_type="alerts",
            section_name=None,
            section_order=0,
            point_order=index,
            point=point,
        )
        for index, point in enumerate(brief.alerts)
    )

    selected = 0
    text_chars = 0
    text_truncated = False
    for entry in sorted(entries, key=lambda item: item.priority):
        if selected >= point_limit or text_chars >= text_limit:
            break
        remaining = text_limit - text_chars
        payload, was_truncated = _project_point(entry.point, pool=pool, max_text_chars=remaining)
        if payload is None:
            continue
        if entry.section_type == "alerts":
            alerts.append(payload)
        elif entry.section_type == "zones" and entry.section_name is not None:
            zones[entry.section_name].append(payload)
        elif entry.section_type == "families" and entry.section_name is not None:
            families[entry.section_name].append(payload)
        elif entry.section_type == "symbols" and entry.section_name is not None:
            symbols[entry.section_name].append(payload)
        selected += 1
        text_chars += len(payload["point"])
        text_truncated = text_truncated or was_truncated

    input_refs = brief.input_refs if isinstance(brief.input_refs, Mapping) else {}
    coverage = _coverage_from_refs(
        input_refs,
        pool_size=len(pool),
        metadata=coverage_metadata or {},
    )
    return UniverseSituationContext(
        status="active",
        brief_ref=brief.ref(date=_date_from_iso(brief.as_of)),
        metadata={
            "brief_id": brief.brief_id,
            "venue": brief.venue,
            "as_of": brief.as_of,
            "valid_until": brief.valid_until,
        },
        coverage=coverage,
        zones=zones,
        families=families,
        symbols=symbols,
        alerts=alerts,
        truncated=selected < len(entries) or text_truncated,
        point_count=selected,
        text_chars=text_chars,
    )


def enrich_candidates_with_family(
    candidates: Iterable[Mapping[str, Any]],
) -> tuple[dict[str, Any], ...]:
    """Copy candidate records and attach a deterministic semantic family."""

    enriched: list[dict[str, Any]] = []
    for raw in candidates:
        candidate = dict(raw)
        symbol = str(candidate.get("symbol") or "").strip()
        if not symbol:
            continue
        candidate["symbol"] = symbol
        family = str(candidate.get("family") or "").strip()
        candidate["family"] = family or family_for_symbol(symbol) or UNCLASSIFIED_FAMILY
        enriched.append(candidate)
    return tuple(enriched)


def build_family_snapshot(
    candidates: Iterable[Mapping[str, Any]],
    baseline: Iterable[str],
    sticky: Iterable[str],
) -> dict[str, dict[str, Any]]:
    """Aggregate candidate, baseline and sticky membership by semantic family."""

    enriched = enrich_candidates_with_family(candidates)
    baseline_symbols = _unique_symbols(baseline)
    sticky_symbols = _unique_symbols(sticky)
    buckets: dict[str, dict[str, Any]] = {}

    def bucket(family: str) -> dict[str, Any]:
        return buckets.setdefault(
            family,
            {
                "symbols": set(),
                "candidate_count": 0,
                "baseline_symbols": set(),
                "sticky_symbols": set(),
                "radar_symbols": set(),
                "challenger_symbols": set(),
                "bias_counts": Counter(),
                "attractiveness": [],
            },
        )

    family_by_symbol: dict[str, str] = {}
    for candidate in enriched:
        symbol = candidate["symbol"]
        family = candidate["family"]
        family_by_symbol[symbol] = family
        current = bucket(family)
        current["symbols"].add(symbol)
        current["candidate_count"] += 1
        provenance = str(
            candidate.get("candidate_source") or candidate.get("provenance") or "radar"
        ).strip()
        if provenance == "fresh_news":
            current["challenger_symbols"].add(symbol)
        else:
            current["radar_symbols"].add(symbol)
        bias = str(candidate.get("bias") or "unknown").strip() or "unknown"
        current["bias_counts"][bias] += 1
        attractiveness = _finite_float(candidate.get("attractiveness"))
        if attractiveness is not None:
            current["attractiveness"].append(attractiveness)

    for symbol in baseline_symbols:
        family = family_by_symbol.get(symbol) or family_for_symbol(symbol) or UNCLASSIFIED_FAMILY
        bucket(family)["baseline_symbols"].add(symbol)
    for symbol in sticky_symbols:
        family = family_by_symbol.get(symbol) or family_for_symbol(symbol) or UNCLASSIFIED_FAMILY
        bucket(family)["sticky_symbols"].add(symbol)

    snapshot: dict[str, dict[str, Any]] = {}
    for family in sorted(buckets):
        raw = buckets[family]
        attractiveness = raw.pop("attractiveness")
        snapshot[family] = {
            "symbols": sorted(raw["symbols"]),
            "candidate_count": int(raw["candidate_count"]),
            "baseline_symbols": sorted(raw["baseline_symbols"]),
            "sticky_symbols": sorted(raw["sticky_symbols"]),
            "radar_symbols": sorted(raw["radar_symbols"]),
            "challenger_symbols": sorted(raw["challenger_symbols"]),
            "bias_counts": dict(sorted(raw["bias_counts"].items())),
            "average_attractiveness": (
                round(sum(attractiveness) / len(attractiveness), 8) if attractiveness else None
            ),
        }
    return snapshot


def candidate_scope_id(
    venue: str,
    candidates: Iterable[Mapping[str, Any]],
    default_hotlist: Iterable[str],
    as_of: str,
) -> str:
    """Return a stable hash for one prepared venue scope.

    Candidate order is normalized, while the deterministic baseline order is
    intentionally significant.  The close/as-of timestamp makes a new daily
    preparation distinct even when scores happen to be identical.
    """

    normalized_candidates = sorted(
        (_candidate_identity(candidate) for candidate in candidates),
        key=lambda item: (item["symbol"], json.dumps(item, sort_keys=True, separators=(",", ":"))),
    )
    normalized_venue = str(venue or "").strip().upper()
    envelope = {
        "version": 1,
        "venue": normalized_venue,
        "as_of": str(as_of or "").strip(),
        "default_hotlist": _unique_symbols(default_hotlist),
        "candidates": normalized_candidates,
    }
    encoded = json.dumps(envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    digest = hashlib.sha256(encoded).hexdigest()
    return f"candidate_scope:v1:{normalized_venue}:{digest}"


def _candidate_identity(candidate: Mapping[str, Any]) -> dict[str, Any]:
    fresh_news = candidate.get("fresh_news")
    refs: list[str] = []
    if isinstance(fresh_news, Mapping):
        raw_refs = fresh_news.get("source_refs")
        if isinstance(raw_refs, (list, tuple, set)):
            refs = sorted({str(ref).strip() for ref in raw_refs if str(ref).strip()})
    raw_sources = candidate.get("candidate_sources")
    if isinstance(raw_sources, (list, tuple, set)):
        provenance = sorted({str(value).strip() for value in raw_sources if str(value).strip()})
    else:
        source = str(
            candidate.get("candidate_source") or candidate.get("provenance") or "radar"
        ).strip()
        provenance = [source or "radar"]
    return {
        "symbol": str(candidate.get("symbol") or "").strip(),
        "attractiveness": _finite_float(candidate.get("attractiveness")),
        "bias": str(candidate.get("bias") or "").strip(),
        "provenance": provenance,
        "fresh_source_refs": refs,
    }


def _section_entries(section_type: str, sections: Iterable[Any]) -> list[_PointEntry]:
    entries: list[_PointEntry] = []
    for section_order, section in enumerate(sections):
        for point_order, point in enumerate(section.points):
            entries.append(
                _PointEntry(
                    section_type=section_type,
                    section_name=section.name,
                    section_order=section_order,
                    point_order=point_order,
                    point=point,
                )
            )
    return entries


def _project_point(
    point: SituationPoint,
    *,
    pool: set[str],
    max_text_chars: int,
) -> tuple[dict[str, Any] | None, bool]:
    if max_text_chars <= 0:
        return None, False
    text = point.point
    was_truncated = False
    if len(text) > max_text_chars:
        if max_text_chars < 4:
            return None, False
        text = text[: max_text_chars - 3].rstrip() + "..."
        was_truncated = True
    payload = point.to_dict()
    payload["point"] = text
    payload["symbols"] = [symbol for symbol in payload.get("symbols", []) if symbol in pool]
    return payload, was_truncated


def _coverage_from_refs(
    input_refs: Mapping[str, Any],
    *,
    pool_size: int,
    metadata: Mapping[str, Any],
) -> dict[str, Any]:
    raw_candidates = input_refs.get("candidate_symbols")
    candidate_count = _nonnegative_int(metadata.get("candidate_count"))
    if candidate_count is None and isinstance(raw_candidates, (list, tuple, set)):
        candidate_count = len({str(value).strip() for value in raw_candidates if str(value).strip()})
    if candidate_count is None:
        candidate_count = pool_size

    news_items = _nonnegative_int(metadata.get("news_items_injected"))
    if news_items is None:
        news_items = _nonnegative_int(input_refs.get("news_item_count"))

    global_count = _nonnegative_int(input_refs.get("global_news_count"))
    global_status = _coverage_status(
        metadata.get("global_headlines", metadata.get("global_headlines_status"))
    )
    if global_status == "unknown" and global_count is not None:
        global_status = "present" if global_count > 0 else "missing"

    macro_status = _coverage_status(
        metadata.get("macro_series", metadata.get("macro_series_status")),
        allow_stale=True,
    )
    labels = input_refs.get("macro_series_labels")
    if macro_status == "unknown" and isinstance(labels, (list, tuple, set)):
        macro_status = "present" if any(str(value).strip() for value in labels) else "missing"

    candidates_with_news: Any = _nonnegative_int(metadata.get("candidates_with_news"))
    if candidates_with_news is None:
        candidates_with_news = "unknown"
    cap_hit: Any
    if isinstance(metadata.get("news_cap_hit"), bool):
        cap_hit = metadata["news_cap_hit"]
    else:
        cap_hit = "unknown"

    return {
        "status": "partial",
        "candidate_count": candidate_count,
        "candidates_with_news": candidates_with_news,
        "news_items_injected": news_items if news_items is not None else "unknown",
        "news_cap_hit": cap_hit,
        "global_headlines": global_status,
        "macro_series": macro_status,
    }


def _missing_coverage(*, candidate_count: int) -> dict[str, Any]:
    return {
        "status": "missing",
        "candidate_count": candidate_count,
        "candidates_with_news": "unknown",
        "news_items_injected": "unknown",
        "news_cap_hit": "unknown",
        "global_headlines": "unknown",
        "macro_series": "unknown",
    }


def _coverage_status(value: Any, *, allow_stale: bool = False) -> str:
    allowed = {"present", "missing", "unknown"}
    if allow_stale:
        allowed.add("stale")
    cleaned = str(value or "").strip().lower()
    return cleaned if cleaned in allowed else "unknown"


def _brief_is_active(brief: NewsMacroBrief, at: datetime | str) -> bool:
    moment = _parse_datetime(at)
    start = _parse_datetime(brief.as_of)
    end = _parse_datetime(brief.valid_until)
    if moment is None or start is None or end is None:
        return False
    return start <= moment < end


def _parse_datetime(value: datetime | str) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _date_from_iso(value: str) -> str:
    return value[:10] if len(value) >= 10 else value


def _finite_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def _nonnegative_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def _unique_symbols(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        symbol = str(value or "").strip()
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        result.append(symbol)
    return result


def _json_copy(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False))
