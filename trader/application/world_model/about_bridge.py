"""Pure situation/company briefs → ABOUT artifacts + relations builders.

News side consumes ``situation_notes`` rows (one dated analyst assertion per
row): one artifact per note, one ABOUT relation per mapped listing.
Symbol-less ``macro`` notes resolve to the finest unambiguous scope for their
brief venue (venue → country → region → world, derived from the mapping;
``GLOBAL`` always resolves to the world node). Company
side consumes the issuer registry + verified briefs: one artifact per brief,
ABOUT to the company plus ABOUT to the bound listing. Every registry entry is
a bound listing by construction (mapping-only included: the mapping anchor is
the listing authority), so the instrument relation is never gated on the
brief exchange hint. Validity windows travel with the artifacts; expiry is enforced
at query time per cutoff, never by a build-time clock — backfilled history
stays joinable at its contemporary cutoffs.

Operational noise points are rejected when the flag exists; the
``situation_notes`` corpus does not persist it (documented limitation).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from trader.domain.company.intelligence import CompanyIntelligenceBrief
from trader.domain.situation.brief import SituationPoint
from trader.domain.world_company import DriverCompanyBundle, company_signal_from_brief
from trader.domain.world_context import EntityRef, KnowledgeArtifact
from trader.domain.world_episode import parse_utc_timestamp
from trader.application.world_model.issuer_registry import index_briefs_by_symbol
from trader.domain.world_family_catalog import FamilyCatalog
from trader.domain.world_graph import KnowledgeArtifactRef, KnowledgeWorldRelation, WorldEntityRef
from trader.domain.world_ontology_lifecycle import (
    MARKET_ONTOLOGY_REVISION_FAMILY,
    admits_market_ontology_family,
)
from trader.domain.world_issuer_registry import IssuerRegistry
from trader.domain.world_knowledge import (
    knowledge_artifact_content_sha256,
    knowledge_artifact_id,
    knowledge_artifact_row,
)
from trader.domain.world_news import DriverNewsBundle, news_signal_from_point
from trader.domain.world_scope import WorldScopeMapping

NEWS_BRIDGE_VERSION = "world_news_bridge.v1"
COMPANY_BRIDGE_VERSION = "world_company_bridge.v1"
NEWS_BRIEF_SCHEMA = "news_macro_brief.v1"
COMPANY_BRIEF_SCHEMA = "company_micro_brief.v1"
COMPANY_BRIEF_TTL = timedelta(days=30)

NEWS_EXCLUSION_REASONS = frozenset(
    {
        "empty_point",
        "no_symbols",
        "unmapped_symbols",
        "missing_venue",
        "unmapped_venue",
        "missing_clocks",
        "invalid_window",
        "operational",
        "missing_event_class",
        "invalid_event_class",
    }
)
COMPANY_EXCLUSION_REASONS = frozenset(
    {"missing_brief", "brief_unreadable", "no_sector", "signal_failed", "missing_clocks"}
)


def _required_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a non-empty string")
    text = value.strip()
    if not text:
        raise ValueError(f"{field_name} must be a non-empty string")
    return text


def _string_list(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, (str, bytes, bytearray)):
        return ()
    if not isinstance(value, Sequence):
        return ()
    return tuple(str(item).strip() for item in value if str(item).strip())


@dataclass(frozen=True)
class AboutExclusion:
    """One refused bridge input with its reason. Counted, never silent."""

    subject_key: str
    reason: str
    detail: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "subject_key", _required_text(self.subject_key, "subject_key"))
        reason = _required_text(self.reason, "reason")
        if reason not in NEWS_EXCLUSION_REASONS and reason not in COMPANY_EXCLUSION_REASONS:
            raise ValueError(f"exclusion reason is unknown: {reason}")
        object.__setattr__(self, "reason", reason)
        detail = self.detail if isinstance(self.detail, str) else ""
        object.__setattr__(self, "detail", detail.strip())


@dataclass(frozen=True)
class AboutArtifactDraft:
    """One storable artifact plus its ABOUT relations (revision pinned)."""

    envelope: KnowledgeArtifact
    signal: DriverNewsBundle | DriverCompanyBundle
    relations: tuple[KnowledgeWorldRelation, ...]
    bridge_version: str


@dataclass(frozen=True)
class AboutBridgeBuild:
    """Deterministic drafts plus the exclusions that shaped them."""

    drafts: tuple[AboutArtifactDraft, ...]
    excluded: tuple[AboutExclusion, ...]

    @property
    def exclusion_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for item in self.excluded:
            counts[item.reason] = counts.get(item.reason, 0) + 1
        return counts


def _listing_index(mapping: WorldScopeMapping) -> dict[str, list[WorldEntityRef]]:
    # Join keys are case-folded on both sides: ticker identity is
    # case-insensitive, while emitted targets stay mapping-canonical.
    index: dict[str, list[WorldEntityRef]] = {}
    for entry in mapping.entries:
        ref = WorldEntityRef(
            kind="instrument", entity_id=f"{entry.venue.entity_id}:symbol:{entry.anchor.instrument}"
        )
        bucket = index.setdefault(entry.anchor.instrument.strip().upper(), [])
        if ref not in bucket:
            bucket.append(ref)
    return index


def _venue_scope_index(mapping: WorldScopeMapping) -> dict[str, WorldEntityRef]:
    """Finest unambiguous canonical scope per logical brief venue.

    Derived from the mapping rows alone, no gazetteer: one distinct venue →
    that venue, else one distinct country → that country, else one distinct
    region → that region, else the world node. ``GLOBAL`` never reaches this
    index (world by rule).
    """

    buckets: dict[str, list[tuple[str, str, str, str]]] = {}
    for entry in mapping.entries:
        key = entry.anchor.market_venue.strip().upper()
        buckets.setdefault(key, []).append(entry.output_key())
    index: dict[str, WorldEntityRef] = {}
    for venue_key, outputs in buckets.items():
        distinct = sorted(set(outputs))
        venues = sorted({output[0] for output in distinct})
        countries = sorted({output[1] for output in distinct})
        regions = sorted({output[2] for output in distinct})
        if len(venues) == 1:
            index[venue_key] = WorldEntityRef(kind="venue", entity_id=venues[0])
        elif len(countries) == 1:
            index[venue_key] = WorldEntityRef(kind="country", entity_id=countries[0])
        elif len(regions) == 1:
            index[venue_key] = WorldEntityRef(kind="region", entity_id=regions[0])
        else:
            index[venue_key] = WorldEntityRef(kind="world", entity_id="market")
    return index


def _admitted_revision(ontology_revision: str) -> str:
    revision = _required_text(ontology_revision, "ontology_revision")
    if not admits_market_ontology_family(revision):
        raise ValueError("ontology_revision is not an admitted market ontology revision")
    # Fraicheur prouvee ; stamp = famille stable.
    return MARKET_ONTOLOGY_REVISION_FAMILY


def _artifact_envelope(
    *,
    kind: str,
    artifact_id: str,
    subjects: Sequence[EntityRef],
    schema_version: str,
    signal_dict: Mapping[str, Any],
    ready_at: datetime,
    valid_until: datetime | None,
    source_refs: Sequence[str],
) -> KnowledgeArtifact:
    placeholder = KnowledgeArtifact(
        kind=kind,
        artifact_id=artifact_id,
        subjects=tuple(subjects),
        schema_version=schema_version,
        content_sha256="00" * 32,
        ready_at=ready_at,
        valid_until=valid_until,
        source_refs=tuple(source_refs),
    )
    content = knowledge_artifact_content_sha256(knowledge_artifact_row(placeholder, signal_dict))
    return KnowledgeArtifact(
        kind=kind,
        artifact_id=artifact_id,
        subjects=tuple(subjects),
        schema_version=schema_version,
        content_sha256=content,
        ready_at=ready_at,
        valid_until=valid_until,
        source_refs=tuple(source_refs),
    )


def _about_relation(
    *,
    ref: KnowledgeArtifactRef,
    target: WorldEntityRef,
    ready_at: datetime,
    valid_until: datetime | None,
    ontology_revision: str,
    source_refs: Sequence[str],
) -> KnowledgeWorldRelation:
    return KnowledgeWorldRelation(
        kind="ABOUT",
        source=ref,
        target=target,
        effective_from=ready_at,
        effective_until=valid_until,
        ontology_revision=ontology_revision,
        source_refs=tuple(source_refs),
    )


def _note_point(note: Mapping[str, Any]) -> SituationPoint | None:
    return SituationPoint.from_mapping(
        {
            "point": note.get("point"),
            "symbols": _string_list(note.get("symbols")),
            "sources": _string_list(note.get("source_names")),
            "source_refs": _string_list(note.get("source_uuids")),
            "severity": note.get("severity"),
            "signal": note.get("signal"),
            "horizon": note.get("horizon"),
            "direction": note.get("direction"),
            "event_class": note.get("event_class"),
            "is_operational": note.get("is_operational"),
        }
    )


def build_news_about(
    notes: Sequence[Mapping[str, Any]],
    mapping: WorldScopeMapping,
    *,
    ontology_revision: str,
) -> AboutBridgeBuild:
    """Bridge situation notes to news artifacts + ABOUT relations. Pure."""

    if not isinstance(notes, Sequence) or isinstance(notes, (str, bytes, bytearray)):
        raise TypeError("notes must be a sequence of mappings")
    if not isinstance(mapping, WorldScopeMapping):
        raise TypeError("mapping must be WorldScopeMapping")
    revision = _admitted_revision(ontology_revision)
    index = _listing_index(mapping)
    venue_scopes = _venue_scope_index(mapping)
    drafts: list[AboutArtifactDraft] = []
    excluded: list[AboutExclusion] = []
    for position, note in enumerate(notes):
        if not isinstance(note, Mapping):
            raise TypeError("notes must be a sequence of mappings")
        note_key = str(note.get("note_key") or f"note:{position}").strip() or f"note:{position}"
        try:
            point = _note_point(note)
        except ValueError:
            excluded.append(AboutExclusion(subject_key=note_key, reason="invalid_event_class"))
            continue
        if point is None:
            excluded.append(AboutExclusion(subject_key=note_key, reason="empty_point"))
            continue
        if point.is_operational:
            excluded.append(AboutExclusion(subject_key=note_key, reason="operational"))
            continue
        if not point.event_class:
            excluded.append(AboutExclusion(subject_key=note_key, reason="missing_event_class"))
            continue
        scope_target: WorldEntityRef | None = None
        if not point.symbols:
            if point.event_class != "macro":
                excluded.append(AboutExclusion(subject_key=note_key, reason="no_symbols"))
                continue
            raw_venue = note.get("venue")
            venue_key = raw_venue.strip().upper() if isinstance(raw_venue, str) else ""
            if not venue_key:
                excluded.append(AboutExclusion(subject_key=note_key, reason="missing_venue"))
                continue
            if venue_key == "GLOBAL":
                scope_target = WorldEntityRef(kind="world", entity_id="market")
            else:
                scope_target = venue_scopes.get(venue_key)
                if scope_target is None:
                    excluded.append(
                        AboutExclusion(subject_key=note_key, reason="unmapped_venue", detail=venue_key)
                    )
                    continue
        try:
            ready_at = parse_utc_timestamp(note.get("valid_from") or note.get("as_of"), "ready_at")
        except (TypeError, ValueError):
            excluded.append(AboutExclusion(subject_key=note_key, reason="missing_clocks"))
            continue
        raw_until = note.get("valid_until")
        try:
            valid_until = None if raw_until is None else parse_utc_timestamp(raw_until, "valid_until")
        except (TypeError, ValueError):
            excluded.append(AboutExclusion(subject_key=note_key, reason="missing_clocks"))
            continue
        if valid_until is not None and valid_until <= ready_at:
            excluded.append(AboutExclusion(subject_key=note_key, reason="invalid_window"))
            continue
        targets: list[WorldEntityRef] = [scope_target] if scope_target is not None else []
        if scope_target is None:
            for symbol in point.symbols:
                targets.extend(index.get(symbol.strip().upper(), ()))
        if not targets:
            excluded.append(
                AboutExclusion(
                    subject_key=note_key, reason="unmapped_symbols", detail=",".join(point.symbols)
                )
            )
            continue
        try:
            signal = news_signal_from_point(point)
        except (TypeError, ValueError) as exc:
            excluded.append(AboutExclusion(subject_key=note_key, reason="empty_point", detail=str(exc)))
            continue
        artifact_id = knowledge_artifact_id("news_macro", note_key)
        subjects = tuple(EntityRef(kind=item.kind, entity_id=item.entity_id) for item in targets)
        envelope = _artifact_envelope(
            kind="news_macro",
            artifact_id=artifact_id,
            subjects=subjects,
            schema_version=NEWS_BRIEF_SCHEMA,
            signal_dict=signal.to_dict(),
            ready_at=ready_at,
            valid_until=valid_until,
            source_refs=(f"situation_note:{note_key}", *point.source_refs),
        )
        ref = KnowledgeArtifactRef(artifact_id=artifact_id, content_sha256=envelope.content_sha256)
        relations = tuple(
            _about_relation(
                ref=ref,
                target=item,
                ready_at=ready_at,
                valid_until=valid_until,
                ontology_revision=revision,
                source_refs=(f"situation_note:{note_key}",),
            )
            for item in targets
        )
        drafts.append(
            AboutArtifactDraft(
                envelope=envelope, signal=signal, relations=relations, bridge_version=NEWS_BRIDGE_VERSION
            )
        )
    return AboutBridgeBuild(drafts=tuple(drafts), excluded=tuple(excluded))


def build_company_about(
    registry: IssuerRegistry,
    briefs: Mapping[str, CompanyIntelligenceBrief | Mapping[str, Any]],
    catalog: FamilyCatalog,
    *,
    ontology_revision: str,
) -> AboutBridgeBuild:
    """Bridge verified company briefs to artifacts + ABOUT relations. Pure."""

    if not isinstance(registry, IssuerRegistry):
        raise TypeError("registry must be IssuerRegistry")
    if not isinstance(briefs, Mapping):
        raise TypeError("briefs must be a mapping of symbol to brief")
    if not isinstance(catalog, FamilyCatalog):
        raise TypeError("catalog must be FamilyCatalog")
    revision = _admitted_revision(ontology_revision)
    indexed = index_briefs_by_symbol(briefs)
    drafts: list[AboutArtifactDraft] = []
    excluded: list[AboutExclusion] = []
    for node_id in sorted(registry.entries):
        entry = registry.entries[node_id]
        raw = indexed.get(entry.symbol.strip().upper())
        if raw is None:
            excluded.append(AboutExclusion(subject_key=node_id, reason="missing_brief"))
            continue
        if isinstance(raw, CompanyIntelligenceBrief):
            brief: CompanyIntelligenceBrief | None = raw
        elif isinstance(raw, Mapping):
            try:
                brief = CompanyIntelligenceBrief.from_mapping(raw)
            except (TypeError, ValueError):
                brief = None
        else:
            raise TypeError("briefs must map symbols to CompanyIntelligenceBrief or mappings")
        if brief is None:
            excluded.append(AboutExclusion(subject_key=node_id, reason="brief_unreadable"))
            continue
        sector = catalog.family_for_symbol(entry.symbol)
        if sector is None:
            excluded.append(AboutExclusion(subject_key=node_id, reason="no_sector"))
            continue
        try:
            signal = company_signal_from_brief(brief, sector=sector)
        except (TypeError, ValueError) as exc:
            excluded.append(AboutExclusion(subject_key=node_id, reason="signal_failed", detail=str(exc)))
            continue
        try:
            ready_at = parse_utc_timestamp(brief.as_of, "ready_at")
        except (TypeError, ValueError):
            excluded.append(AboutExclusion(subject_key=node_id, reason="missing_clocks"))
            continue
        valid_until = ready_at + COMPANY_BRIEF_TTL
        artifact_id = knowledge_artifact_id("company_intelligence", brief.brief_id, brief.as_of)
        company_ref = WorldEntityRef(kind="company", entity_id=entry.issuer_entity_id)
        instrument_ref = WorldEntityRef(
            kind="instrument", entity_id=f"mic:{entry.mic}:symbol:{entry.symbol}"
        )
        subjects = [
            EntityRef(kind="company", entity_id=company_ref.entity_id),
            EntityRef(kind="instrument", entity_id=instrument_ref.entity_id),
        ]
        targets = [company_ref, instrument_ref]
        envelope = _artifact_envelope(
            kind="company_intelligence",
            artifact_id=artifact_id,
            subjects=subjects,
            schema_version=COMPANY_BRIEF_SCHEMA,
            signal_dict=signal.to_dict(),
            ready_at=ready_at,
            valid_until=valid_until,
            source_refs=(f"company_brief:{brief.brief_id}", *brief.source_refs),
        )
        ref = KnowledgeArtifactRef(artifact_id=artifact_id, content_sha256=envelope.content_sha256)
        relations = tuple(
            _about_relation(
                ref=ref,
                target=item,
                ready_at=ready_at,
                valid_until=valid_until,
                ontology_revision=revision,
                source_refs=(f"company_brief:{brief.brief_id}",),
            )
            for item in targets
        )
        drafts.append(
            AboutArtifactDraft(
                envelope=envelope,
                signal=signal,
                relations=relations,
                bridge_version=COMPANY_BRIDGE_VERSION,
            )
        )
    return AboutBridgeBuild(drafts=tuple(drafts), excluded=tuple(excluded))


__all__ = [
    "COMPANY_BRIDGE_VERSION",
    "COMPANY_BRIEF_SCHEMA",
    "COMPANY_BRIEF_TTL",
    "COMPANY_EXCLUSION_REASONS",
    "NEWS_BRIDGE_VERSION",
    "NEWS_BRIEF_SCHEMA",
    "NEWS_EXCLUSION_REASONS",
    "AboutArtifactDraft",
    "AboutBridgeBuild",
    "AboutExclusion",
    "build_company_about",
    "build_news_about",
]
