"""Application commands and distinct structural/knowledge point-in-time resolvers."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from trader.application.world_model.cohort_ports import WorldOntologyHeadsProof
from trader.application.world_model.graph_ports import (
    WorldEntityEventEnvelope,
    WorldEntityEventId,
    WorldEntityIdentityEventEnvelope,
    WorldEntityIdentityEventId,
    WorldGraphLedger,
    WorldOntologyRevisionEventEnvelope,
    WorldOntologyRevisionEventId,
    WorldRelationEventEnvelope,
    WorldRelationEventId,
)
from trader.domain.world_availability import (
    AvailabilityEvidence,
    PersistedWorldRef,
    PointInTimeEligibilityPolicy,
)
from trader.domain.world_episode import canonical_sha256, parse_utc_timestamp
from trader.domain.world_graph import (
    KnowledgeWorldRelation,
    KnowledgeWorldRelationAsserted,
    KnowledgeWorldRelationEvent,
    KnowledgeWorldRelationRetired,
    StructuralWorldRelation,
    StructuralWorldRelationAsserted,
    StructuralWorldRelationEvent,
    StructuralWorldRelationRetired,
    WorldEntityAsserted,
    WorldEntityEvent,
    WorldEntityIdentityEvent,
    WorldEntityIdentityLink,
    WorldEntityIdentityLinked,
    WorldEntityIdentityLinkSuperseded,
    WorldEntityIdentityMap,
    WorldEntityIdentityUnlinked,
    WorldEntityRef,
    WorldEntityRetired,
    WorldEntityRevisionRef,
    WorldEntitySuperseded,
    WorldOntologyRevision,
    WorldOntologyRevisionEvent,
    WorldOntologyRevisionPublished,
    WorldOntologyRevisionSuperseded,
    WorldStructuralRelationRef,
    admits_macro_observes_relation,
    evaluate_world_graph_point_in_time,
    fold_knowledge_relation_events_at_cutoff,
    fold_structural_relation_events_at_cutoff,
    reconcile_world_ontology_revision,
)


@dataclass(frozen=True)
class AssertWorldEntity:
    entity: WorldEntityRef | Mapping[str, Any]
    source_refs: Sequence[str]
    effective_from: datetime | str
    effective_until: datetime | str | None = None


@dataclass(frozen=True)
class RetireWorldEntity:
    entity: WorldEntityRef | Mapping[str, Any]
    retired_at: datetime | str
    source_refs: Sequence[str]


@dataclass(frozen=True)
class SupersedeWorldEntity:
    entity: WorldEntityRef | Mapping[str, Any]
    successor: WorldEntityRef | Mapping[str, Any]
    superseded_at: datetime | str
    source_refs: Sequence[str]


@dataclass(frozen=True)
class LinkWorldEntityIdentity:
    link: WorldEntityIdentityLink | Mapping[str, Any]


@dataclass(frozen=True)
class UnlinkWorldEntityIdentity:
    link_id: str
    retired_at: datetime | str
    source_refs: Sequence[str]


@dataclass(frozen=True)
class SupersedeWorldEntityIdentityLink:
    successor: WorldEntityIdentityLink | Mapping[str, Any]


@dataclass(frozen=True)
class AssertStructuralWorldRelation:
    relation: StructuralWorldRelation | Mapping[str, Any]


@dataclass(frozen=True)
class RetireStructuralWorldRelation:
    relation_id: str
    retired_at: datetime | str
    source_refs: Sequence[str]


@dataclass(frozen=True)
class AssertKnowledgeWorldRelation:
    relation: KnowledgeWorldRelation | Mapping[str, Any]


@dataclass(frozen=True)
class RetireKnowledgeWorldRelation:
    relation_id: str
    retired_at: datetime | str
    source_refs: Sequence[str]


@dataclass(frozen=True)
class PublishWorldOntologyRevision:
    revision: WorldOntologyRevision | Mapping[str, Any]


@dataclass(frozen=True)
class SupersedeWorldOntologyRevision:
    revision_id: str
    successor_revision_id: str


@dataclass(frozen=True)
class WorldOntologyRevisionView:
    cutoff_at: datetime
    entities: tuple[WorldEntityRef, ...]
    entity_revision_refs: tuple[WorldEntityRevisionRef, ...]
    structural_relations: tuple[StructuralWorldRelation, ...]
    identity_map: WorldEntityIdentityMap
    identity_links: tuple[WorldEntityIdentityLink, ...]
    published_revision: WorldOntologyRevision | None
    entity_heads_hash: str
    structural_heads_hash: str
    identity_map_hash: str


@dataclass(frozen=True)
class WorldKnowledgeOverlayView:
    cutoff_at: datetime
    structural_revision_id: str
    structural_revision_hash: str
    relations: tuple[KnowledgeWorldRelation, ...]


def _as_cutoff(cutoff_at: datetime | str) -> datetime:
    return parse_utc_timestamp(cutoff_at, "cutoff_at")


def _is_available(evidence: AvailabilityEvidence, cutoff_at: datetime) -> bool:
    return PointInTimeEligibilityPolicy().is_eligible(evidence=evidence, cutoff_at=cutoff_at)


def _available_envelopes(envelopes: Sequence[Any], cutoff_at: datetime) -> tuple[Any, ...]:
    return tuple(envelope for envelope in envelopes if _is_available(envelope.evidence, cutoff_at))


def _order_envelopes(
    envelopes: Sequence[Any],
    *,
    leading_types: tuple[type, ...],
) -> tuple[Any, ...]:
    leading = [envelope for envelope in envelopes if isinstance(envelope.event, leading_types)]
    trailing = [envelope for envelope in envelopes if not isinstance(envelope.event, leading_types)]
    leading.sort(key=lambda envelope: envelope.event.event_id)
    trailing.sort(key=lambda envelope: envelope.event.event_id)
    return tuple(leading + trailing)


def _identity_fold_sort_key(envelope: WorldEntityIdentityEventEnvelope) -> tuple[datetime, int, str]:
    # Domain uniqueness is interval-based. Close (unlink/supersede) must precede
    # a same-instant replacement link; event_id is only a deterministic tie-break.
    event = envelope.event
    event_id = event.event_id or ""
    if isinstance(event, WorldEntityIdentityUnlinked):
        return (event.retired_at, 0, event_id)
    if isinstance(event, WorldEntityIdentityLinkSuperseded):
        return (event.successor.effective_from, 0, event_id)
    if isinstance(event, WorldEntityIdentityLinked):
        return (event.link.effective_from, 1, event_id)
    raise TypeError(f"unsupported identity event: {type(event).__name__}")


def _entity_heads_hash(entities: Sequence[WorldEntityRef]) -> str:
    ordered = tuple(sorted(entities, key=lambda item: item.node_id))
    return canonical_sha256([item.to_dict() for item in ordered])


def _structural_heads_hash(relations: Sequence[StructuralWorldRelation]) -> str:
    refs = tuple(
        sorted(
            (WorldStructuralRelationRef.from_relation(item) for item in relations),
            key=lambda item: (item.relation_id, item.content_sha256),
        )
    )
    return canonical_sha256([item.to_dict() for item in refs])


def _fold_entities(
    envelopes: Sequence[WorldEntityEventEnvelope],
    cutoff_at: datetime,
) -> tuple[WorldEntityAsserted, ...]:
    states: dict[str, tuple[WorldEntityAsserted, datetime | None, AvailabilityEvidence]] = {}
    for envelope in _order_envelopes(envelopes, leading_types=(WorldEntityAsserted,)):
        event = envelope.event
        if isinstance(event, WorldEntityAsserted):
            existing = states.get(event.entity.node_id)
            if existing is not None:
                if existing[0].as_ref().content_sha256 != event.as_ref().content_sha256:
                    raise ValueError("conflict: same entity identity with different content")
                continue
            states[event.entity.node_id] = (event, event.effective_until, envelope.evidence)
            continue
        if isinstance(event, WorldEntityRetired):
            current = states.get(event.entity.node_id)
            if current is None:
                raise ValueError("unknown entity")
            asserted, until, evidence = current
            if event.retired_at <= asserted.effective_from:
                raise ValueError("retired_at must be later than effective_from")
            tighter = event.retired_at if until is None or event.retired_at < until else until
            states[event.entity.node_id] = (asserted, tighter, evidence)
            continue
        if isinstance(event, WorldEntitySuperseded):
            current = states.get(event.entity.node_id)
            if current is None:
                raise ValueError("unknown entity")
            asserted, until, evidence = current
            if event.superseded_at <= asserted.effective_from:
                raise ValueError("superseded_at must be later than effective_from")
            tighter = event.superseded_at if until is None or event.superseded_at < until else until
            states[event.entity.node_id] = (asserted, tighter, evidence)
            continue
        raise TypeError(f"unsupported entity event: {type(event).__name__}")
    admitted: list[WorldEntityAsserted] = []
    for asserted, until, evidence in states.values():
        if (
            evaluate_world_graph_point_in_time(
                evidence=evidence,
                cutoff_at=cutoff_at,
                effective_from=asserted.effective_from,
                valid_until=until,
            ).status
            == "eligible"
        ):
            admitted.append(asserted)
    return tuple(sorted(admitted, key=lambda item: item.entity.node_id))


def _fold_identity_map(envelopes: Sequence[WorldEntityIdentityEventEnvelope]) -> WorldEntityIdentityMap:
    ordered = tuple(sorted(envelopes, key=_identity_fold_sort_key))
    return WorldEntityIdentityMap.from_events(tuple(envelope.event for envelope in ordered))


def _fold_structural_relations(
    envelopes: Sequence[WorldRelationEventEnvelope],
    cutoff_at: datetime,
) -> tuple[StructuralWorldRelation, ...]:
    ordered = _order_envelopes(envelopes, leading_types=(StructuralWorldRelationAsserted,))
    evidence_by_relation_id = {
        envelope.event.relation.relation_id: envelope.evidence
        for envelope in ordered
        if isinstance(envelope.event, StructuralWorldRelationAsserted)
    }
    return fold_structural_relation_events_at_cutoff(
        tuple(envelope.event for envelope in ordered),
        cutoff_at=cutoff_at,
        evidence_by_relation_id=evidence_by_relation_id,
    )


def _fold_knowledge_relations(
    envelopes: Sequence[WorldRelationEventEnvelope],
    cutoff_at: datetime,
) -> tuple[KnowledgeWorldRelation, ...]:
    ordered = _order_envelopes(envelopes, leading_types=(KnowledgeWorldRelationAsserted,))
    evidence_by_relation_id = {
        envelope.event.relation.relation_id: envelope.evidence
        for envelope in ordered
        if isinstance(envelope.event, KnowledgeWorldRelationAsserted)
    }
    return fold_knowledge_relation_events_at_cutoff(
        tuple(envelope.event for envelope in ordered),
        cutoff_at=cutoff_at,
        evidence_by_relation_id=evidence_by_relation_id,
    )


def _published_revision_at(
    envelopes: Sequence[WorldOntologyRevisionEventEnvelope],
) -> WorldOntologyRevision | None:
    ordered = _order_envelopes(envelopes, leading_types=(WorldOntologyRevisionPublished,))
    published: dict[str, WorldOntologyRevision] = {}
    superseded: set[str] = set()
    for envelope in ordered:
        event = envelope.event
        if isinstance(event, WorldOntologyRevisionPublished):
            existing = published.get(event.revision.revision_id)
            if existing is not None:
                published[event.revision.revision_id] = reconcile_world_ontology_revision(existing, event.revision)
            else:
                published[event.revision.revision_id] = event.revision
            continue
        if isinstance(event, WorldOntologyRevisionSuperseded):
            superseded.add(event.revision_id)
            continue
        raise TypeError(f"unsupported revision event: {type(event).__name__}")
    active = [revision for revision_id, revision in published.items() if revision_id not in superseded]
    if not active:
        return None
    return sorted(active, key=lambda item: item.revision_id)[-1]


def _published_revision_for_id(
    envelopes: Sequence[WorldOntologyRevisionEventEnvelope],
    revision_id: str,
) -> WorldOntologyRevision | None:
    """Return a published revision by exact id, even after a later supersede."""

    wanted = str(revision_id).strip()
    if not wanted:
        return None
    ordered = _order_envelopes(envelopes, leading_types=(WorldOntologyRevisionPublished,))
    published: WorldOntologyRevision | None = None
    for envelope in ordered:
        event = envelope.event
        if not isinstance(event, WorldOntologyRevisionPublished):
            continue
        if event.revision.revision_id != wanted:
            continue
        published = (
            event.revision
            if published is None
            else reconcile_world_ontology_revision(published, event.revision)
        )
    return published


class WorldOntologyResolver:
    """Unique author of structural heads and the identity map at a cutoff."""

    def __init__(self, ledger: WorldGraphLedger) -> None:
        self._ledger = ledger

    def at_cutoff(self, cutoff_at: datetime | str) -> WorldOntologyRevisionView:
        cutoff = _as_cutoff(cutoff_at)
        entity_envelopes = _available_envelopes(self._ledger.list_entity_events_available_through(cutoff), cutoff)
        identity_envelopes = _available_envelopes(self._ledger.list_identity_events_available_through(cutoff), cutoff)
        structural_envelopes = _available_envelopes(
            self._ledger.list_structural_relation_events_available_through(cutoff),
            cutoff,
        )
        revision_envelopes = _available_envelopes(self._ledger.list_revision_events_available_through(cutoff), cutoff)
        asserted = _fold_entities(entity_envelopes, cutoff)
        entities = tuple(item.entity for item in asserted)
        identity_map = _fold_identity_map(identity_envelopes)
        identity_links = identity_map.active_links_at(cutoff)
        structural_relations = _fold_structural_relations(structural_envelopes, cutoff)
        return WorldOntologyRevisionView(
            cutoff_at=cutoff,
            entities=entities,
            entity_revision_refs=tuple(item.as_ref() for item in asserted),
            structural_relations=structural_relations,
            identity_map=identity_map,
            identity_links=identity_links,
            published_revision=_published_revision_at(revision_envelopes),
            entity_heads_hash=_entity_heads_hash(entities),
            structural_heads_hash=_structural_heads_hash(structural_relations),
            identity_map_hash=identity_map.heads_hash_at(cutoff),
        )

    def published_revision(self, revision_id: str, *, at: datetime | str) -> WorldOntologyRevision | None:
        """Load one published revision by exact id. Does not move the live head."""

        cutoff = _as_cutoff(at)
        envelopes = _available_envelopes(self._ledger.list_revision_events_available_through(cutoff), cutoff)
        return _published_revision_for_id(envelopes, revision_id)


class WorldKnowledgeResolver:
    """Unique author of the knowledge overlay at a cutoff, bound to one structural revision."""

    def __init__(self, ledger: WorldGraphLedger) -> None:
        self._ledger = ledger

    def at_cutoff(
        self,
        cutoff_at: datetime | str,
        structural_revision: WorldOntologyRevision,
    ) -> WorldKnowledgeOverlayView:
        cutoff = _as_cutoff(cutoff_at)
        if not isinstance(structural_revision, WorldOntologyRevision):
            raise TypeError("knowledge overlay requires a WorldOntologyRevision")
        envelopes = _available_envelopes(self._ledger.list_knowledge_relation_events_available_through(cutoff), cutoff)
        relations = tuple(
            relation
            for relation in _fold_knowledge_relations(envelopes, cutoff)
            if relation.ontology_revision == structural_revision.revision_id
            and admits_macro_observes_relation(relation)
        )
        return WorldKnowledgeOverlayView(
            cutoff_at=cutoff,
            structural_revision_id=structural_revision.revision_id,
            structural_revision_hash=structural_revision.content_sha256,
            relations=relations,
        )


class WorldOntologyProofService:
    """Typed durable-heads query. Absence is None, never a fabricated revision."""

    def __init__(self, ledger: WorldGraphLedger) -> None:
        self._ontology = WorldOntologyResolver(ledger)

    def proven_heads(
        self,
        *,
        revision_id: str,
        scope_mapping_id: str,
        scope_mapping_hash: str,
        at: datetime | str,
    ) -> WorldOntologyHeadsProof | None:
        view = self._ontology.at_cutoff(at)
        published = view.published_revision
        if published is None:
            return None
        if published.revision_id != revision_id:
            return None
        if published.scope_mapping_id != scope_mapping_id or published.scope_mapping_hash != scope_mapping_hash:
            return None
        return WorldOntologyHeadsProof(
            revision_id=published.revision_id,
            content_sha256=published.content_sha256 or "",
            scope_mapping_id=published.scope_mapping_id,
            scope_mapping_hash=published.scope_mapping_hash,
            entity_heads_hash=published.entity_heads_hash or "",
            structural_heads_hash=published.structural_heads_hash or "",
        )


class WorldOntologyService:
    """Typed command handlers. Domain events are appended; availability stays store-assigned."""

    def __init__(self, ledger: WorldGraphLedger) -> None:
        self._ledger = ledger
        self.ontology = WorldOntologyResolver(ledger)
        self.knowledge = WorldKnowledgeResolver(ledger)

    def assert_entity(self, command: AssertWorldEntity) -> PersistedWorldRef[WorldEntityEventId]:
        event: WorldEntityEvent = WorldEntityAsserted(
            entity=command.entity,
            source_refs=command.source_refs,
            effective_from=command.effective_from,
            effective_until=command.effective_until,
        )
        return self._ledger.append_entity_event(event)

    def retire_entity(self, command: RetireWorldEntity) -> PersistedWorldRef[WorldEntityEventId]:
        event: WorldEntityEvent = WorldEntityRetired(
            entity=command.entity,
            retired_at=command.retired_at,
            source_refs=command.source_refs,
        )
        return self._ledger.append_entity_event(event)

    def supersede_entity(self, command: SupersedeWorldEntity) -> PersistedWorldRef[WorldEntityEventId]:
        event: WorldEntityEvent = WorldEntitySuperseded(
            entity=command.entity,
            successor=command.successor,
            superseded_at=command.superseded_at,
            source_refs=command.source_refs,
        )
        return self._ledger.append_entity_event(event)

    def link_identity(self, command: LinkWorldEntityIdentity) -> PersistedWorldRef[WorldEntityIdentityEventId]:
        event: WorldEntityIdentityEvent = WorldEntityIdentityLinked(link=command.link)
        return self._ledger.append_identity_event(event)

    def unlink_identity(self, command: UnlinkWorldEntityIdentity) -> PersistedWorldRef[WorldEntityIdentityEventId]:
        event: WorldEntityIdentityEvent = WorldEntityIdentityUnlinked(
            link_id=command.link_id,
            retired_at=command.retired_at,
            source_refs=command.source_refs,
        )
        return self._ledger.append_identity_event(event)

    def supersede_identity_link(
        self, command: SupersedeWorldEntityIdentityLink
    ) -> PersistedWorldRef[WorldEntityIdentityEventId]:
        successor = WorldEntityIdentityLink.from_mapping(command.successor)
        if successor.supersedes is None:
            raise ValueError("successor identity link must point at the predecessor via supersedes")
        event: WorldEntityIdentityEvent = WorldEntityIdentityLinkSuperseded(
            predecessor_link_id=successor.supersedes,
            successor=successor,
        )
        return self._ledger.append_identity_event(event)

    def assert_structural_relation(
        self, command: AssertStructuralWorldRelation
    ) -> PersistedWorldRef[WorldRelationEventId]:
        event: StructuralWorldRelationEvent = StructuralWorldRelationAsserted(relation=command.relation)
        return self._ledger.append_structural_relation_event(event)

    def retire_structural_relation(
        self, command: RetireStructuralWorldRelation
    ) -> PersistedWorldRef[WorldRelationEventId]:
        event: StructuralWorldRelationEvent = StructuralWorldRelationRetired(
            relation_id=command.relation_id,
            retired_at=command.retired_at,
            source_refs=command.source_refs,
        )
        return self._ledger.append_structural_relation_event(event)

    def assert_knowledge_relation(
        self, command: AssertKnowledgeWorldRelation
    ) -> PersistedWorldRef[WorldRelationEventId]:
        event: KnowledgeWorldRelationEvent = KnowledgeWorldRelationAsserted(relation=command.relation)
        return self._ledger.append_knowledge_relation_event(event)

    def retire_knowledge_relation(
        self, command: RetireKnowledgeWorldRelation
    ) -> PersistedWorldRef[WorldRelationEventId]:
        event: KnowledgeWorldRelationEvent = KnowledgeWorldRelationRetired(
            relation_id=command.relation_id,
            retired_at=command.retired_at,
            source_refs=command.source_refs,
        )
        return self._ledger.append_knowledge_relation_event(event)

    def publish_revision(
        self, command: PublishWorldOntologyRevision
    ) -> PersistedWorldRef[WorldOntologyRevisionEventId]:
        event: WorldOntologyRevisionEvent = WorldOntologyRevisionPublished(revision=command.revision)
        return self._ledger.append_revision_event(event)

    def supersede_revision(
        self, command: SupersedeWorldOntologyRevision
    ) -> PersistedWorldRef[WorldOntologyRevisionEventId]:
        event: WorldOntologyRevisionEvent = WorldOntologyRevisionSuperseded(
            revision_id=command.revision_id,
            successor_revision_id=command.successor_revision_id,
        )
        return self._ledger.append_revision_event(event)


__all__ = [
    "AssertKnowledgeWorldRelation",
    "AssertStructuralWorldRelation",
    "AssertWorldEntity",
    "LinkWorldEntityIdentity",
    "PublishWorldOntologyRevision",
    "RetireKnowledgeWorldRelation",
    "RetireStructuralWorldRelation",
    "RetireWorldEntity",
    "SupersedeWorldEntity",
    "SupersedeWorldEntityIdentityLink",
    "SupersedeWorldOntologyRevision",
    "UnlinkWorldEntityIdentity",
    "WorldKnowledgeOverlayView",
    "WorldKnowledgeResolver",
    "WorldOntologyProofService",
    "WorldOntologyResolver",
    "WorldOntologyRevisionView",
    "WorldOntologyService",
]
