"""Deterministic production bootstrap for the committed market ontology.

Derives instrument/venue/country/region/world entities and TRADED_ON /
LOCATED_IN / PART_OF_WORLD heads solely from the versioned WorldScopeMapping.
No issuer/company inference, no causal edges, no silent suffix fallback.
Fresh state publishes the current mapping and ontology. Existing different
identity or hash is drift and fails closed.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timezone

from trader.application.world_model.graph_ports import (
    WorldGraphLedger,
    WorldOntologyReadiness,
)
from trader.application.world_model.graph_snapshot import expected_scope_heads
from trader.application.world_model.cohort_ports import WorldOntologyHeadsProof
from trader.application.world_model.ontology_service import (
    AssertStructuralWorldRelation,
    AssertWorldEntity,
    PublishWorldOntologyRevision,
    WorldOntologyProofService,
    WorldOntologyService,
)
from trader.domain.world_episode import parse_utc_timestamp
from trader.domain.world_feature_contract import MARKET_ONTOLOGY_REVISION
from trader.domain.world_graph import (
    FORBIDDEN_RELATION_KINDS,
    StructuralWorldRelation,
    WorldEntityRef,
    WorldOntologyRevision,
    WorldStructuralRelationRef,
)
from trader.domain.world_ontology_lifecycle import (
    WorldOntologyLifecycleSpec,
    WorldOntologyPublicationPlan,
    committed_world_ontology_lifecycle_spec,
    plan_world_ontology_publication,
    require_committed_ontology_revision,
)
from trader.domain.world_scope import WorldCanonicalScopeRef, WorldScopeMapping


MARKET_ONTOLOGY_REVISION_ID = MARKET_ONTOLOGY_REVISION
MARKET_ONTOLOGY_EFFECTIVE_FROM = datetime(2026, 1, 1, tzinfo=timezone.utc)
_SCOPE_HEAD_KINDS = frozenset({"TRADED_ON", "LOCATED_IN", "PART_OF_WORLD"})
_BOOTSTRAP_KINDS = frozenset({"instrument", "venue", "country", "region", "world"})
_READINESS_STATUSES = frozenset({"ready", "unpublished", "drifted"})


def _utc(value: datetime | str | None, field_name: str) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    return parse_utc_timestamp(value, field_name)


def _entity_from_scope(scope: WorldCanonicalScopeRef) -> WorldEntityRef:
    return WorldEntityRef(kind=scope.kind, entity_id=scope.entity_id)


def _instrument_entity(entry: object) -> WorldEntityRef:
    venue = getattr(entry, "venue")
    anchor = getattr(entry, "anchor")
    return WorldEntityRef(kind="instrument", entity_id=f"{venue.entity_id}:symbol:{anchor.instrument}")


def _proofs(entry: object) -> tuple[str, ...]:
    return tuple(str(item) for item in getattr(entry, "provider_proofs") or ())


def derive_market_ontology(
    mapping: WorldScopeMapping,
    *,
    revision_id: str = MARKET_ONTOLOGY_REVISION_ID,
    effective_from: datetime | str = MARKET_ONTOLOGY_EFFECTIVE_FROM,
) -> tuple[tuple[WorldEntityRef, ...], tuple[StructuralWorldRelation, ...], WorldOntologyRevision]:
    """Pure mapping → frozen heads. Identity map stays empty."""

    if not isinstance(mapping, WorldScopeMapping):
        raise TypeError("mapping must be WorldScopeMapping")
    when = _utc(effective_from, "effective_from")
    entities: dict[str, WorldEntityRef] = {}
    proofs: dict[str, set[str]] = {}
    edges: dict[tuple[str, str, str], set[str]] = {}

    def add_entity(entity: WorldEntityRef, extra: Sequence[str]) -> None:
        if entity.kind not in _BOOTSTRAP_KINDS:
            raise ValueError(f"bootstrap forbids entity kind {entity.kind}")
        entities[entity.node_id] = entity
        proofs.setdefault(entity.node_id, set()).update(str(item) for item in extra if str(item).strip())

    def add_edge(kind: str, source: WorldEntityRef, target: WorldEntityRef, extra: Sequence[str]) -> None:
        compact = kind.upper().replace("-", "_")
        if compact in FORBIDDEN_RELATION_KINDS or "CAUS" in compact:
            raise ValueError("generic CAUSES edges are forbidden; the ontology is not a causal graph")
        if kind not in _SCOPE_HEAD_KINDS:
            raise ValueError("bootstrap publishes only TRADED_ON, LOCATED_IN, and PART_OF_WORLD")
        add_entity(source, extra)
        add_entity(target, extra)
        edges.setdefault((kind, source.node_id, target.node_id), set()).update(
            str(item) for item in extra if str(item).strip()
        )

    for entry in mapping.entries:
        extra = _proofs(entry)
        if not extra:
            extra = (f"{mapping.mapping_id}:{entry.anchor.market_venue}:{entry.anchor.instrument}",)
        instrument = _instrument_entity(entry)
        venue = _entity_from_scope(entry.venue)
        country = _entity_from_scope(entry.country)
        region = _entity_from_scope(entry.region)
        world = _entity_from_scope(entry.world)
        add_entity(instrument, extra)
        add_edge("TRADED_ON", instrument, venue, extra)
        add_edge("LOCATED_IN", venue, country, extra)
        add_edge("LOCATED_IN", country, region, extra)
        add_edge("PART_OF_WORLD", region, world, extra)

    ordered_entities = tuple(sorted(entities.values(), key=lambda item: item.node_id))
    relations = tuple(
        StructuralWorldRelation(
            kind=kind,
            source=entities[source_id],
            target=entities[target_id],
            effective_from=when,
            ontology_revision=revision_id,
            source_refs=tuple(sorted(edge_proofs)) or (f"{mapping.mapping_id}:{kind}:{source_id}:{target_id}",),
        )
        for (kind, source_id, target_id), edge_proofs in sorted(edges.items())
    )
    actual_heads = frozenset(
        (relation.kind, relation.source.node_id, relation.target.node_id) for relation in relations
    )
    if actual_heads != expected_scope_heads(mapping):
        raise ValueError("published revision topology does not match WorldScopeMapping heads")
    if any(entity.kind == "company" for entity in ordered_entities):
        raise ValueError("bootstrap must not infer issuer or company entities")
    revision = WorldOntologyRevision(
        revision_id=revision_id,
        entities=ordered_entities,
        structural_relation_refs=tuple(WorldStructuralRelationRef.from_relation(item) for item in relations),
        identity_link_refs=(),
        scope_mapping_id=mapping.mapping_id,
        scope_mapping_hash=mapping.content_sha256,
    )
    return ordered_entities, relations, revision


def _readiness(
    *,
    status: str,
    mapping: WorldScopeMapping,
    revision: WorldOntologyRevision | None,
    reason: str,
    entity_count: int = 0,
    structural_relation_count: int = 0,
    identity_link_count: int = 0,
) -> WorldOntologyReadiness:
    if status not in _READINESS_STATUSES:
        raise ValueError(f"readiness status must be one of: {sorted(_READINESS_STATUSES)}")
    return WorldOntologyReadiness(
        status=status,
        scope_mapping_id=mapping.mapping_id,
        scope_mapping_hash=mapping.content_sha256,
        revision_id=None if revision is None else revision.revision_id,
        ontology_hash=None if revision is None else revision.content_sha256,
        entity_count=entity_count if revision is None else len(revision.entities),
        structural_relation_count=structural_relation_count
        if revision is None
        else len(revision.structural_relation_refs),
        identity_link_count=identity_link_count if revision is None else len(revision.identity_link_refs),
        reason=reason,
    )


class WorldOntologyBootstrapService:
    """Append-only publisher of the committed market ontology. Implements the readiness port."""

    def __init__(
        self,
        ledger: WorldGraphLedger,
        mapping: WorldScopeMapping,
        *,
        revision_id: str = MARKET_ONTOLOGY_REVISION_ID,
        effective_from: datetime | str = MARKET_ONTOLOGY_EFFECTIVE_FROM,
        lifecycle_spec: WorldOntologyLifecycleSpec | None = None,
    ) -> None:
        if not isinstance(mapping, WorldScopeMapping):
            raise TypeError("mapping must be WorldScopeMapping")
        self._ledger = ledger
        self._mapping = mapping
        self._revision_id = str(revision_id).strip() or MARKET_ONTOLOGY_REVISION_ID
        self._effective_from = _utc(effective_from, "effective_from")
        self._lifecycle_spec = lifecycle_spec or committed_world_ontology_lifecycle_spec()
        self._service = WorldOntologyService(ledger)

    def expected_revision(self) -> WorldOntologyRevision:
        revision = derive_market_ontology(
            self._mapping,
            revision_id=self._revision_id,
            effective_from=self._effective_from,
        )[2]
        if self._lifecycle_spec == committed_world_ontology_lifecycle_spec():
            return require_committed_ontology_revision(revision, self._mapping)
        return revision

    def _plan(
        self, cutoff_at: datetime
    ) -> tuple[WorldOntologyPublicationPlan, WorldOntologyRevision | None, WorldOntologyRevision]:
        expected = self.expected_revision()
        published = self._service.ontology.at_cutoff(cutoff_at).published_revision
        return (
            plan_world_ontology_publication(
                published=published,
                expected=expected,
                spec=self._lifecycle_spec,
            ),
            published,
            expected,
        )

    def readiness(self, cutoff_at: datetime | str | None = None) -> WorldOntologyReadiness:
        cutoff = _utc(cutoff_at, "cutoff_at")
        published = self._service.ontology.at_cutoff(cutoff).published_revision
        try:
            expected = self.expected_revision()
            plan = plan_world_ontology_publication(
                published=published,
                expected=expected,
                spec=self._lifecycle_spec,
            )
        except ValueError:
            return _readiness(status="drifted", mapping=self._mapping, revision=published, reason="revision_drift")
        if plan.action == "ready":
            return _readiness(status="ready", mapping=self._mapping, revision=published, reason="attested")
        return _readiness(status="unpublished", mapping=self._mapping, revision=expected, reason="unpublished")

    def ensure_published(self, *, now: datetime | str | None = None) -> WorldOntologyReadiness:
        cutoff = _utc(now, "now")
        current = self.readiness(cutoff)
        if current.status == "ready":
            return current
        if current.status == "drifted":
            raise ValueError("conflict: committed ontology heads do not match the published revision")
        plan, _published, revision = self._plan(cutoff)
        if plan.action != "publish":
            raise ValueError("conflict: committed ontology heads do not match the published revision")
        entities, relations, expected = derive_market_ontology(
            self._mapping,
            revision_id=self._revision_id,
            effective_from=self._effective_from,
        )
        if expected.content_sha256 != revision.content_sha256:
            raise ValueError("conflict: derived ontology revision drifted during publish")
        view = self._service.ontology.at_cutoff(cutoff)
        existing_nodes = {entity.node_id for entity in view.entities}
        proofs: dict[str, tuple[str, ...]] = {}
        for relation in relations:
            proofs.setdefault(relation.source.node_id, relation.source_refs)
            proofs.setdefault(relation.target.node_id, relation.source_refs)
        for entity in entities:
            if entity.node_id in existing_nodes:
                continue
            source_refs = proofs.get(entity.node_id) or (f"{self._mapping.mapping_id}:{entity.node_id}",)
            self._service.assert_entity(
                AssertWorldEntity(entity=entity, source_refs=source_refs, effective_from=self._effective_from)
            )
        for relation in relations:
            self._service.assert_structural_relation(AssertStructuralWorldRelation(relation=relation))
        self._service.publish_revision(PublishWorldOntologyRevision(revision=expected))
        # Receipts are store-stamped at append time; do not require the pre-append cutoff
        # to observe them (ready_at can be a few microseconds later than the captured now).
        return _readiness(status="ready", mapping=self._mapping, revision=expected, reason="published")


class WorldOntologyAttestation:
    """Single application authority for committed ontology bootstrap and PIT heads proof."""

    def __init__(
        self,
        ledger: WorldGraphLedger,
        mapping: WorldScopeMapping,
        *,
        revision_id: str = MARKET_ONTOLOGY_REVISION_ID,
        effective_from: datetime | str = MARKET_ONTOLOGY_EFFECTIVE_FROM,
        lifecycle_spec: WorldOntologyLifecycleSpec | None = None,
    ) -> None:
        self._ledger = ledger
        self._mapping = mapping
        self._bootstrap = WorldOntologyBootstrapService(
            ledger,
            mapping,
            revision_id=revision_id,
            effective_from=effective_from,
            lifecycle_spec=lifecycle_spec,
        )
        self._proof = WorldOntologyProofService(ledger)

    @property
    def ledger(self) -> WorldGraphLedger:
        return self._ledger

    @property
    def mapping(self) -> WorldScopeMapping:
        return self._mapping

    def expected_revision(self) -> WorldOntologyRevision:
        return self._bootstrap.expected_revision()

    def readiness(self, cutoff_at: datetime | str | None = None) -> WorldOntologyReadiness:
        return self._bootstrap.readiness(cutoff_at)

    def ensure_published(self, *, now: datetime | str | None = None) -> WorldOntologyReadiness:
        return self._bootstrap.ensure_published(now=now)

    def proven_heads(
        self,
        *,
        revision_id: str,
        scope_mapping_id: str,
        scope_mapping_hash: str,
        at: datetime | str,
    ) -> WorldOntologyHeadsProof | None:
        return self._proof.proven_heads(
            revision_id=revision_id,
            scope_mapping_id=scope_mapping_id,
            scope_mapping_hash=scope_mapping_hash,
            at=at,
        )


__all__ = [
    "MARKET_ONTOLOGY_EFFECTIVE_FROM",
    "MARKET_ONTOLOGY_REVISION_ID",
    "WorldOntologyAttestation",
    "WorldOntologyBootstrapService",
    "WorldOntologyReadiness",
    "derive_market_ontology",
]
