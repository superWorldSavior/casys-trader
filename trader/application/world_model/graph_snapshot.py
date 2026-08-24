"""Point-in-time V3 WorldGraphSnapshot builder. NetworkX is never imported here."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from types import MappingProxyType
from typing import Any

from trader.application.world_model.graph_ports import (
    WorldGraphLedger,
    WorldGraphPathSet,
    WorldGraphSnapshotId,
    WorldGraphSnapshotLedger,
    WorldGraphTraversalPort,
    WorldRelationEventEnvelope,
)
from trader.application.world_model.ontology_service import (
    WorldKnowledgeOverlayView,
    WorldKnowledgeResolver,
    WorldOntologyResolver,
    WorldOntologyRevisionView,
)
from trader.domain.world_availability import PersistedWorldRef
from trader.domain.world_cohort import WorldCohortSlot
from trader.domain.world_episode import WorldEpisode, canonical_sha256, parse_utc_timestamp
from trader.domain.world_graph import (
    GRAPH_TRAVERSAL_POLICY_VERSION,
    WORLD_GRAPH_SNAPSHOT_SCHEMA,
    KnowledgeArtifactRef,
    KnowledgeWorldRelation,
    KnowledgeWorldRelationAsserted,
    StructuralWorldRelation,
    WorldEntityIdentityLink,
    WorldEntityRef,
    WorldGraphSnapshot,
    WorldKnowledgeRelationRef,
    WorldOntologyRevision,
    WorldStructuralRelationRef,
)
from trader.domain.world_macro import MACRO_PRODUCER_VERSION
from trader.domain.world_scope import WorldScopeMapping, WorldScopeResolution


_SCOPE_HEAD_KINDS = frozenset({"TRADED_ON", "LOCATED_IN", "PART_OF_WORLD"})
_PRODUCER_VERSIONS: Mapping[str, str] = MappingProxyType(
    {
        "graph_snapshot": WORLD_GRAPH_SNAPSHOT_SCHEMA,
        "graph_traversal": GRAPH_TRAVERSAL_POLICY_VERSION,
        "macro_producer": MACRO_PRODUCER_VERSION,
    }
)


@dataclass(frozen=True)
class WorldGraphSnapshotRequest:
    episode: WorldEpisode | Mapping[str, Any]
    root_entity: WorldEntityRef | Mapping[str, Any]
    cutoff_at: datetime | str
    scope_mapping: WorldScopeMapping
    scope_resolution: WorldScopeResolution | Mapping[str, Any]
    slot: WorldCohortSlot | Mapping[str, Any] | None = None
    max_depth: int | None = None
    max_paths: int | None = None

    def __post_init__(self) -> None:
        episode = self.episode if isinstance(self.episode, WorldEpisode) else WorldEpisode.from_dict(self.episode)
        if not isinstance(self.scope_mapping, WorldScopeMapping):
            raise TypeError("scope_mapping must be WorldScopeMapping")
        resolution = WorldScopeResolution.from_mapping(self.scope_resolution)
        slot = None if self.slot is None else WorldCohortSlot.from_mapping(self.slot)
        object.__setattr__(self, "episode", episode)
        object.__setattr__(self, "root_entity", WorldEntityRef.from_mapping(self.root_entity))
        object.__setattr__(self, "cutoff_at", parse_utc_timestamp(self.cutoff_at, "cutoff_at"))
        object.__setattr__(self, "scope_resolution", resolution)
        object.__setattr__(self, "slot", slot)


@dataclass(frozen=True)
class WorldGraphSnapshotBundle:
    snapshot: WorldGraphSnapshot
    paths: WorldGraphPathSet
    structural_relations: tuple[StructuralWorldRelation, ...]
    knowledge_relations: tuple[KnowledgeWorldRelation, ...]


def _empty_paths() -> WorldGraphPathSet:
    return WorldGraphPathSet(paths=(), status="complete", policy_version=GRAPH_TRAVERSAL_POLICY_VERSION)


def _scope_entity(scope: Any) -> WorldEntityRef:
    return WorldEntityRef(kind=scope.kind, entity_id=scope.entity_id)


def expected_scope_heads(mapping: WorldScopeMapping) -> frozenset[tuple[str, str, str]]:
    heads: set[tuple[str, str, str]] = set()
    for entry in mapping.entries:
        instrument = WorldEntityRef(
            kind="instrument",
            entity_id=f"{entry.venue.entity_id}:symbol:{entry.anchor.instrument}",
        )
        venue = _scope_entity(entry.venue)
        country = _scope_entity(entry.country)
        region = _scope_entity(entry.region)
        world = _scope_entity(entry.world)
        heads.add(("TRADED_ON", instrument.node_id, venue.node_id))
        heads.add(("LOCATED_IN", venue.node_id, country.node_id))
        heads.add(("LOCATED_IN", country.node_id, region.node_id))
        heads.add(("PART_OF_WORLD", region.node_id, world.node_id))
    return frozenset(heads)


def _actual_scope_heads(relations: Sequence[StructuralWorldRelation]) -> frozenset[tuple[str, str, str]]:
    return frozenset(
        (relation.kind, relation.source.node_id, relation.target.node_id)
        for relation in relations
        if relation.kind in _SCOPE_HEAD_KINDS
    )


def _assert_scope_heads(
    expected: frozenset[tuple[str, str, str]],
    actual: frozenset[tuple[str, str, str]],
) -> None:
    if expected - actual:
        raise ValueError("published revision topology does not match WorldScopeMapping heads")
    expected_targets: dict[tuple[str, str], set[str]] = {}
    for kind, source, target in expected:
        expected_targets.setdefault((kind, source), set()).add(target)
    for kind, source, target in actual:
        allowed = expected_targets.get((kind, source))
        if allowed is not None and target not in allowed:
            raise ValueError("published revision topology contradicts WorldScopeMapping heads")


def _revision_bound_view(
    view: WorldOntologyRevisionView,
    published: WorldOntologyRevision,
) -> WorldOntologyRevisionView:
    """A published hash may only expose the entity/structural/identity heads it froze."""

    allowed_entities = {entity.node_id for entity in published.entities}
    allowed_structural = frozenset(published.structural_relation_refs)
    allowed_identity = frozenset(published.identity_link_refs)
    entity_revision_refs = tuple(item for item in view.entity_revision_refs if item.entity.node_id in allowed_entities)
    entities = tuple(item.entity for item in entity_revision_refs)
    if not entities:
        entities = tuple(entity for entity in published.entities)
    structural_relations = tuple(
        relation
        for relation in view.structural_relations
        if WorldStructuralRelationRef.from_relation(relation) in allowed_structural
    )
    identity_links = tuple(
        link
        for link in view.identity_links
        if isinstance(link, WorldEntityIdentityLink) and link.as_ref() in allowed_identity
    )
    return replace(
        view,
        entities=entities,
        entity_revision_refs=entity_revision_refs,
        structural_relations=structural_relations,
        identity_links=identity_links,
        published_revision=published,
        entity_heads_hash=published.entity_heads_hash or view.entity_heads_hash,
        structural_heads_hash=published.structural_heads_hash or view.structural_heads_hash,
        identity_map_hash=published.identity_map_hash or view.identity_map_hash,
    )


def _require_mapping_alignment(request: WorldGraphSnapshotRequest, published: WorldOntologyRevision | None) -> None:
    mapping = request.scope_mapping
    resolution = request.scope_resolution
    if resolution.mapping_id != mapping.mapping_id or resolution.mapping_sha256 != mapping.content_sha256:
        raise ValueError("scope resolution mapping does not match WorldScopeMapping")
    if request.slot is not None:
        slot_resolution = request.slot.scope_resolution
        if slot_resolution is None:
            raise ValueError("WorldCohortSlot requires scope_resolution")
        if slot_resolution.mapping_id != mapping.mapping_id or slot_resolution.mapping_sha256 != mapping.content_sha256:
            raise ValueError("WorldCohortSlot scope mapping mismatch")
        if slot_resolution != resolution:
            raise ValueError("WorldCohortSlot scope resolution mismatch")
    computed = mapping.resolve(resolution.anchor)
    if resolution.status != "ambiguous":
        if computed.status != resolution.status:
            raise ValueError("scope resolution does not match WorldScopeMapping")
        if computed.status == "resolved" and tuple(computed.scopes) != tuple(resolution.scopes):
            raise ValueError("scope resolution does not match WorldScopeMapping")
    elif computed.status == "resolved":
        raise ValueError("ambiguous resolution contradicts a unique mapping row")
    if published is not None and (
        published.scope_mapping_id != mapping.mapping_id or published.scope_mapping_hash != mapping.content_sha256
    ):
        raise ValueError("scope_mapping_id/hash mismatch with published revision")


def _knowledge_receipts(ledger: WorldGraphLedger, cutoff: datetime) -> dict[str, str]:
    receipts: dict[str, str] = {}
    envelopes: Sequence[WorldRelationEventEnvelope] = ledger.list_knowledge_relation_events_available_through(cutoff)
    for envelope in envelopes:
        event = envelope.event
        if isinstance(event, KnowledgeWorldRelationAsserted):
            receipts[event.relation.relation_id] = envelope.evidence.receipt.receipt_id
    return receipts


def _artifact_ids(relations: Sequence[KnowledgeWorldRelation]) -> tuple[str, ...]:
    ids: set[str] = set()
    for relation in relations:
        for node in (relation.source, relation.target):
            if isinstance(node, KnowledgeArtifactRef):
                ids.add(node.artifact_id)
    return tuple(sorted(ids))


def _missing_snapshot(
    *,
    request: WorldGraphSnapshotRequest,
    view: WorldOntologyRevisionView,
    published: WorldOntologyRevision | None,
    missingness: Mapping[str, str],
    status: str,
) -> WorldGraphSnapshotBundle:
    ontology_revision = "unpublished" if published is None else published.revision_id
    ontology_hash = canonical_sha256({"status": "unpublished"}) if published is None else published.content_sha256
    identity_hash = view.identity_map_hash if published is None else published.identity_map_hash
    snapshot = WorldGraphSnapshot(
        root_episode_id=request.episode.episode_id,
        root_entity=request.root_entity,
        cutoff_at=request.cutoff_at,
        ontology_revision=ontology_revision,
        ontology_hash=ontology_hash,
        identity_map_hash=identity_hash,
        scope_mapping_id=request.scope_mapping.mapping_id,
        scope_mapping_hash=request.scope_mapping.content_sha256,
        entity_revision_refs=tuple(
            item for item in view.entity_revision_refs if item.entity.node_id == request.root_entity.node_id
        ),
        identity_link_refs=tuple(
            link.as_ref() for link in view.identity_links if link.v3_ref.node_id == request.root_entity.node_id
        ),
        structural_relation_refs=(),
        knowledge_relation_refs=(),
        artifact_refs=(),
        producer_versions=dict(_PRODUCER_VERSIONS),
        traversal_policy_version=GRAPH_TRAVERSAL_POLICY_VERSION,
        status=status,
        missingness=dict(missingness),
    )
    return WorldGraphSnapshotBundle(
        snapshot=snapshot,
        paths=_empty_paths(),
        structural_relations=(),
        knowledge_relations=(),
    )


class WorldGraphSnapshotService:
    """Build the exact V3 subgraph admissible at a cutoff. Does not persist unless asked."""

    def __init__(
        self,
        ledger: WorldGraphLedger,
        traversal: WorldGraphTraversalPort,
        snapshot_ledger: WorldGraphSnapshotLedger | None = None,
    ) -> None:
        self._ledger = ledger
        self._ontology = WorldOntologyResolver(ledger)
        self._knowledge = WorldKnowledgeResolver(ledger)
        self._traversal = traversal
        self._snapshot_ledger = snapshot_ledger

    def persist(self, snapshot: WorldGraphSnapshot) -> PersistedWorldRef[WorldGraphSnapshotId]:
        if self._snapshot_ledger is None:
            raise ValueError("snapshot ledger is required to persist")
        if not isinstance(snapshot, WorldGraphSnapshot):
            raise TypeError("snapshot must be WorldGraphSnapshot")
        return self._snapshot_ledger.append(snapshot)

    def build(self, request: WorldGraphSnapshotRequest | Mapping[str, Any]) -> WorldGraphSnapshotBundle:
        resolved = request if isinstance(request, WorldGraphSnapshotRequest) else WorldGraphSnapshotRequest(**request)
        cutoff = resolved.cutoff_at
        view = self._ontology.at_cutoff(cutoff)
        published = view.published_revision
        _require_mapping_alignment(resolved, published)
        if published is None:
            return _missing_snapshot(
                request=resolved,
                view=view,
                published=None,
                missingness={"ontology": "unpublished"},
                status="missing",
            )
        view = _revision_bound_view(view, published)
        _assert_scope_heads(
            expected_scope_heads(resolved.scope_mapping), _actual_scope_heads(view.structural_relations)
        )
        if resolved.scope_resolution.status in {"unmapped", "ambiguous"}:
            return _missing_snapshot(
                request=resolved,
                view=view,
                published=published,
                missingness={"scope": resolved.scope_resolution.status},
                status="missing",
            )
        overlay = self._knowledge.at_cutoff(cutoff, published)
        return self._bundle_from_views(resolved, view, overlay, published)

    def _bundle_from_views(
        self,
        request: WorldGraphSnapshotRequest,
        view: WorldOntologyRevisionView,
        overlay: WorldKnowledgeOverlayView,
        published: WorldOntologyRevision,
    ) -> WorldGraphSnapshotBundle:
        paths = self._traversal.enumerate_paths(
            view,
            overlay,
            request.root_entity,
            max_depth=request.max_depth,
            max_paths=request.max_paths,
        )
        relation_ids = {step.relation_id for path in paths.paths for step in path.steps}
        node_ids = {request.root_entity.node_id}
        for path in paths.paths:
            for step in path.steps:
                node_ids.add(step.source_node_id)
                node_ids.add(step.target_node_id)
        structural_members = tuple(
            relation for relation in view.structural_relations if relation.relation_id in relation_ids
        )
        knowledge_members = tuple(relation for relation in overlay.relations if relation.relation_id in relation_ids)
        receipts = _knowledge_receipts(self._ledger, request.cutoff_at)
        knowledge_refs: set[WorldKnowledgeRelationRef] = set()
        for relation in knowledge_members:
            receipt_id = receipts.get(relation.relation_id)
            if receipt_id is None:
                raise ValueError("knowledge relation missing AvailabilityEvidence")
            knowledge_refs.add(WorldKnowledgeRelationRef.from_relation(relation, availability_receipt_id=receipt_id))
        missingness: dict[str, str] = {}
        if paths.status == "graph_budget_exceeded":
            status = "partial"
            missingness["budget"] = "graph_budget_exceeded"
        elif not paths.paths and not structural_members:
            status = "missing"
            missingness["coverage"] = "empty_subgraph"
        else:
            status = "complete"
        snapshot = WorldGraphSnapshot(
            root_episode_id=request.episode.episode_id,
            root_entity=request.root_entity,
            cutoff_at=request.cutoff_at,
            ontology_revision=published.revision_id,
            ontology_hash=published.content_sha256,
            identity_map_hash=published.identity_map_hash,
            scope_mapping_id=request.scope_mapping.mapping_id,
            scope_mapping_hash=request.scope_mapping.content_sha256,
            entity_revision_refs=tuple(item for item in view.entity_revision_refs if item.entity.node_id in node_ids),
            identity_link_refs=tuple(link.as_ref() for link in view.identity_links if link.v3_ref.node_id in node_ids),
            structural_relation_refs=frozenset(
                WorldStructuralRelationRef.from_relation(item) for item in structural_members
            ),
            knowledge_relation_refs=frozenset(knowledge_refs),
            artifact_refs=_artifact_ids(knowledge_members),
            producer_versions=dict(_PRODUCER_VERSIONS),
            traversal_policy_version=GRAPH_TRAVERSAL_POLICY_VERSION,
            status=status,
            missingness=missingness,
        )
        return WorldGraphSnapshotBundle(
            snapshot=snapshot,
            paths=paths,
            structural_relations=structural_members,
            knowledge_relations=knowledge_members,
        )


__all__ = [
    "WorldGraphSnapshotBundle",
    "WorldGraphSnapshotRequest",
    "WorldGraphSnapshotService",
    "expected_scope_heads",
]
