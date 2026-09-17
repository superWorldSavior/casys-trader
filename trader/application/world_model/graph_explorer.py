"""Read-only current published overview of the canonical World Graph.

Desktop consumes ``world_graph_explorer.v1``. The view is
``current_published_overview``: revision-bound structural heads plus knowledge
relations bound to that exact active revision. Parsers, folds, and PIT stay
here. The service never invents ``CAUSES`` and does not accept a historical
cutoff.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from typing import Any

from trader.application.world_model.graph_explorer_ports import (
    WORLD_GRAPH_EXPLORER_SCHEMA,
    WORLD_GRAPH_EXPLORER_VIEW,
    WorldGraphExplorerEventRecord,
    WorldGraphExplorerQuery,
    WorldGraphExplorerQuerySnapshot,
)
from trader.application.world_model.graph_ports import (
    WorldEntityEventEnvelope,
    WorldEntityIdentityEventEnvelope,
    WorldOntologyRevisionEventEnvelope,
    WorldRelationEventEnvelope,
)
from trader.application.world_model.ontology_service import WorldKnowledgeResolver, WorldOntologyResolver
from trader.domain.world_availability import (
    AvailabilityEvidence,
    WorldAvailabilityReceipt,
    _attest_verified_store_receipt,
)
from trader.domain.world_cohort import COHORT_AUTHORITY, COHORT_DECISION_EFFECT, COHORT_RECOMMENDATION
from trader.domain.world_episode import canonical_sha256, parse_utc_timestamp
from trader.domain.world_graph import (
    FORBIDDEN_RELATION_KINDS,
    KnowledgeWorldRelation,
    SensorRef,
    StructuralWorldRelation,
    WorldEntityRef,
    WorldGraphNodeRef,
    WorldOntologyRevision,
    parse_world_entity_event,
    parse_world_entity_identity_event,
    parse_world_ontology_revision_event,
    parse_world_relation_event,
)

UTC = timezone.utc
WORLD_GRAPH_EXPLORER_NODE_LIMIT = 400
WORLD_GRAPH_EXPLORER_EDGE_LIMIT = 800
_CLAIM_FIELDS = {
    "authority": COHORT_AUTHORITY,
    "decision_effect": COHORT_DECISION_EFFECT,
    "recommendation": COHORT_RECOMMENDATION,
    "causal_claim": False,
    "pnl_claim": False,
}
_GRAPH_TABLES = frozenset(
    {
        "world_entity_events",
        "world_entity_identity_events",
        "world_relation_events",
        "world_ontology_revisions",
    }
)
_UtcClock = Callable[[], datetime]


def _iso(value: datetime) -> str:
    return parse_utc_timestamp(value, "timestamp").astimezone(UTC).isoformat()


def _graph_node_id(node: WorldGraphNodeRef) -> str:
    if isinstance(node, (WorldEntityRef, SensorRef)):
        return node.node_id
    payload = node.to_dict()
    for key in ("observation_id", "artifact_id", "snapshot_id", "hypothesis_id", "fact_version_id"):
        value = payload.get(key)
        if value:
            return str(value)
    raise ValueError(f"graph node is missing a stable id: {payload}")


def _node_label(node: WorldGraphNodeRef) -> str:
    if isinstance(node, WorldEntityRef):
        entity_id = node.entity_id
        if node.kind == "instrument" and ":symbol:" in entity_id:
            return entity_id.split(":symbol:", 1)[1]
        if node.kind == "venue" and entity_id.startswith("mic:"):
            return entity_id.removeprefix("mic:")
        if node.kind == "country" and entity_id.startswith("iso-3166:"):
            return entity_id.removeprefix("iso-3166:")
        if node.kind == "region" and entity_id.startswith("region:"):
            return entity_id.removeprefix("region:")
        return entity_id
    return _graph_node_id(node)


def _node_payload(node: WorldGraphNodeRef, *, layer: str) -> dict[str, Any]:
    payload = dict(node.to_dict())
    payload["id"] = _graph_node_id(node)
    payload["layer"] = layer
    payload["label"] = _node_label(node)
    if isinstance(node, WorldEntityRef):
        payload["entity_kind"] = node.kind
    return payload


def _empty_counts() -> dict[str, int]:
    return {
        "nodes": 0,
        "edges": 0,
        "structural_nodes": 0,
        "knowledge_nodes": 0,
        "structural_edges": 0,
        "knowledge_edges": 0,
        "returned_nodes": 0,
        "returned_edges": 0,
        "identity_links": 0,
    }


def _empty_missingness() -> dict[str, Any]:
    return {
        "ontology": None,
        "schema": None,
        "unproven_events": 0,
        "unreadable_events": 0,
        "unhydrated_structural_heads": 0,
        "knowledge_other_revision": 0,
    }


def _empty_truncation() -> dict[str, Any]:
    return {
        "truncated": False,
        "node_limit": WORLD_GRAPH_EXPLORER_NODE_LIMIT,
        "edge_limit": WORLD_GRAPH_EXPLORER_EDGE_LIMIT,
        "omitted_nodes": 0,
        "omitted_edges": 0,
    }


def _empty_provenance() -> dict[str, Any]:
    return {
        "source_refs": [],
        "identity_links": [],
        "receipt_count": 0,
    }


def _claim_projection(
    *,
    status: str,
    generated_at: datetime,
    cutoff_at: datetime,
    exists: bool,
    ontology: dict[str, Any] | None = None,
    nodes: Sequence[Mapping[str, Any]] = (),
    edges: Sequence[Mapping[str, Any]] = (),
    counts: Mapping[str, int] | None = None,
    provenance: Mapping[str, Any] | None = None,
    missingness: Mapping[str, Any] | None = None,
    truncation: Mapping[str, Any] | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": WORLD_GRAPH_EXPLORER_SCHEMA,
        "status": status,
        "exists": exists,
        "generated_at": _iso(generated_at),
        "cutoff_at": _iso(cutoff_at),
        "view": WORLD_GRAPH_EXPLORER_VIEW,
        "ontology": ontology,
        "revision": ontology,
        "nodes": list(nodes),
        "edges": list(edges),
        "counts": dict(counts or _empty_counts()),
        "provenance": dict(provenance or _empty_provenance()),
        "missingness": dict(missingness or _empty_missingness()),
        "truncation": dict(truncation or _empty_truncation()),
        "truncated": bool((truncation or _empty_truncation()).get("truncated")),
        **_CLAIM_FIELDS,
    }
    if error is not None:
        payload["error"] = error
    return payload


class _ReadonlyExplorerLedger:
    """In-memory list surface for the existing ontology/knowledge resolvers."""

    def __init__(
        self,
        *,
        entities: Sequence[WorldEntityEventEnvelope],
        identities: Sequence[WorldEntityIdentityEventEnvelope],
        structural: Sequence[WorldRelationEventEnvelope],
        knowledge: Sequence[WorldRelationEventEnvelope],
        revisions: Sequence[WorldOntologyRevisionEventEnvelope],
    ) -> None:
        self._entities = tuple(entities)
        self._identities = tuple(identities)
        self._structural = tuple(structural)
        self._knowledge = tuple(knowledge)
        self._revisions = tuple(revisions)

    def list_entity_events_available_through(self, cutoff_at: datetime) -> tuple[WorldEntityEventEnvelope, ...]:
        del cutoff_at
        return self._entities

    def list_identity_events_available_through(
        self, cutoff_at: datetime
    ) -> tuple[WorldEntityIdentityEventEnvelope, ...]:
        del cutoff_at
        return self._identities

    def list_structural_relation_events_available_through(
        self, cutoff_at: datetime
    ) -> tuple[WorldRelationEventEnvelope, ...]:
        del cutoff_at
        return self._structural

    def list_knowledge_relation_events_available_through(
        self, cutoff_at: datetime
    ) -> tuple[WorldRelationEventEnvelope, ...]:
        del cutoff_at
        return self._knowledge

    def list_revision_events_available_through(
        self, cutoff_at: datetime
    ) -> tuple[WorldOntologyRevisionEventEnvelope, ...]:
        del cutoff_at
        return self._revisions


class WorldGraphExplorerService:
    """Projects the current published World Graph for Desktop. Shadow-only."""

    def __init__(self, query: WorldGraphExplorerQuery, *, clock: _UtcClock | None = None) -> None:
        self._query = query
        self._clock = clock or (lambda: datetime.now(UTC))

    def current_published_overview(self) -> dict[str, Any]:
        generated_at = parse_utc_timestamp(self._clock(), "generated_at")
        cutoff_at = generated_at
        snapshot = self._query.load_current_records()
        if snapshot.status == "not_started":
            missingness = _empty_missingness()
            if not snapshot.exists:
                missingness["ontology"] = "unpublished"
            return _claim_projection(
                status="not_started",
                generated_at=generated_at,
                cutoff_at=cutoff_at,
                exists=snapshot.exists,
                missingness=missingness,
            )
        if snapshot.status != "ready":
            missingness = _empty_missingness()
            if snapshot.missing_tables:
                missingness["schema"] = "missing_tables"
            return _claim_projection(
                status="unavailable",
                generated_at=generated_at,
                cutoff_at=cutoff_at,
                exists=snapshot.exists,
                missingness=missingness,
                error=snapshot.error,
            )
        try:
            return self._project_ready(snapshot, generated_at=generated_at, cutoff_at=cutoff_at)
        except (TypeError, ValueError) as exc:
            return _claim_projection(
                status="unavailable",
                generated_at=generated_at,
                cutoff_at=cutoff_at,
                exists=snapshot.exists,
                missingness={**_empty_missingness(), "ontology": "unreadable"},
                error=f"{type(exc).__name__}:{exc}",
            )

    def _project_ready(
        self,
        snapshot: WorldGraphExplorerQuerySnapshot,
        *,
        generated_at: datetime,
        cutoff_at: datetime,
    ) -> dict[str, Any]:
        bound, missingness = self._bind_records(snapshot.records, first_seen_at=generated_at)
        ledger = _ReadonlyExplorerLedger(**bound)
        view = WorldOntologyResolver(ledger).at_cutoff(cutoff_at)
        published = view.published_revision
        if published is None:
            missingness["ontology"] = "unpublished"
            return _claim_projection(
                status="not_started",
                generated_at=generated_at,
                cutoff_at=cutoff_at,
                exists=True,
                missingness=missingness,
                provenance={
                    "source_refs": [],
                    "identity_links": [],
                    "receipt_count": sum(1 for record in snapshot.records if record.receipt_payload is not None),
                },
            )
        structural = _revision_bound_structural(view.structural_relations, published)
        missingness["unhydrated_structural_heads"] = len(published.structural_relation_refs) - len(structural)
        knowledge_view = WorldKnowledgeResolver(ledger).at_cutoff(cutoff_at, published)
        knowledge_other = 0
        for envelope in bound["knowledge"]:
            event = envelope.event
            relation = getattr(event, "relation", None)
            if (
                isinstance(relation, KnowledgeWorldRelation)
                and relation.kind != "ABOUT"
                and relation.ontology_revision != published.revision_id
            ):
                knowledge_other += 1
        missingness["knowledge_other_revision"] = knowledge_other
        nodes, edges, counts, provenance, truncation = _project_graph(
            published,
            structural=structural,
            knowledge=knowledge_view.relations,
            receipt_count=sum(1 for record in snapshot.records if record.receipt_payload is not None),
        )
        return _claim_projection(
            status="loaded",
            generated_at=generated_at,
            cutoff_at=cutoff_at,
            exists=True,
            ontology={
                "revision_id": published.revision_id,
                "content_sha256": published.content_sha256,
                "entity_heads_hash": published.entity_heads_hash,
                "structural_heads_hash": published.structural_heads_hash,
                "identity_map_hash": published.identity_map_hash,
                "scope_mapping_id": published.scope_mapping_id,
                "scope_mapping_hash": published.scope_mapping_hash,
            },
            nodes=nodes,
            edges=edges,
            counts=counts,
            provenance=provenance,
            missingness=missingness,
            truncation=truncation,
        )

    def _bind_records(
        self,
        records: Sequence[WorldGraphExplorerEventRecord],
        *,
        first_seen_at: datetime,
    ) -> tuple[dict[str, tuple[Any, ...]], dict[str, Any]]:
        missingness = _empty_missingness()
        entities: list[WorldEntityEventEnvelope] = []
        identities: list[WorldEntityIdentityEventEnvelope] = []
        structural: list[WorldRelationEventEnvelope] = []
        knowledge: list[WorldRelationEventEnvelope] = []
        revisions: list[WorldOntologyRevisionEventEnvelope] = []
        revision_errors = 0
        for record in records:
            try:
                event, evidence = _rehydrate_record(record, first_seen_at=first_seen_at)
            except (TypeError, ValueError, KeyError):
                missingness["unreadable_events"] += 1
                if record.subject_kind == "world_ontology_revision_event":
                    revision_errors += 1
                continue
            if evidence is None:
                missingness["unproven_events"] += 1
                if record.subject_kind == "world_ontology_revision_event":
                    revision_errors += 1
                continue
            if record.subject_kind == "world_entity_event":
                entities.append(WorldEntityEventEnvelope(event=event, evidence=evidence))
            elif record.subject_kind == "world_entity_identity_event":
                identities.append(WorldEntityIdentityEventEnvelope(event=event, evidence=evidence))
            elif record.subject_kind == "world_relation_event":
                family = record.family or getattr(event, "family", None)
                envelope = WorldRelationEventEnvelope(event=event, evidence=evidence)
                if family == "structural":
                    structural.append(envelope)
                elif family == "knowledge":
                    knowledge.append(envelope)
                else:
                    missingness["unreadable_events"] += 1
            elif record.subject_kind == "world_ontology_revision_event":
                revisions.append(WorldOntologyRevisionEventEnvelope(event=event, evidence=evidence))
            else:
                missingness["unreadable_events"] += 1
        if revision_errors and not revisions:
            raise ValueError("active ontology revision events are unreadable")
        return (
            {
                "entities": tuple(entities),
                "identities": tuple(identities),
                "structural": tuple(structural),
                "knowledge": tuple(knowledge),
                "revisions": tuple(revisions),
            },
            missingness,
        )


def _rehydrate_record(
    record: WorldGraphExplorerEventRecord,
    *,
    first_seen_at: datetime,
) -> tuple[Any, AvailabilityEvidence | None]:
    if record.table not in _GRAPH_TABLES:
        raise ValueError("explorer record table is not a graph event table")
    parser = {
        "world_entity_event": parse_world_entity_event,
        "world_entity_identity_event": parse_world_entity_identity_event,
        "world_relation_event": parse_world_relation_event,
        "world_ontology_revision_event": parse_world_ontology_revision_event,
    }[record.subject_kind]
    event = parser(record.payload)
    digest = canonical_sha256(event.to_dict())
    if digest != record.payload_sha256:
        raise ValueError("graph event payload hash mismatch")
    if record.receipt_payload is None:
        return event, None
    receipt = WorldAvailabilityReceipt.from_mapping(record.receipt_payload)
    if receipt.subject.subject_id != event.event_id or receipt.subject.content_sha256 != digest:
        raise ValueError("availability receipt does not match the graph event")
    if receipt.subject.kind != record.subject_kind:
        raise ValueError("availability receipt subject kind mismatch")
    locator = receipt.storage_locator
    if locator.kind != "sqlite" or locator.table != record.table or locator.row_id != event.event_id:
        raise ValueError("availability receipt locator does not match the graph event")
    attested = _attest_verified_store_receipt(
        receipt,
        expected_subject=receipt.subject,
        expected_scope=receipt.scope,
        expected_locator=locator,
    )
    return event, AvailabilityEvidence(receipt=attested, first_seen_at=first_seen_at)


def _revision_bound_structural(
    relations: Sequence[StructuralWorldRelation],
    published: WorldOntologyRevision,
) -> tuple[StructuralWorldRelation, ...]:
    heads = {(ref.relation_id, ref.content_sha256) for ref in published.structural_relation_refs}
    admitted = [
        relation
        for relation in relations
        if relation.ontology_revision == published.revision_id
        and (relation.relation_id, relation.content_sha256) in heads
        and relation.kind not in FORBIDDEN_RELATION_KINDS
    ]
    return tuple(sorted(admitted, key=lambda item: item.relation_id))


def _project_graph(
    published: WorldOntologyRevision,
    *,
    structural: Sequence[StructuralWorldRelation],
    knowledge: Sequence[KnowledgeWorldRelation],
    receipt_count: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, int], dict[str, Any], dict[str, Any]]:
    node_map: dict[str, dict[str, Any]] = {}
    for entity in published.entities:
        payload = _node_payload(entity, layer="structural")
        node_map[payload["id"]] = payload
    admitted_knowledge = [
        relation for relation in knowledge if relation.kind not in FORBIDDEN_RELATION_KINDS
    ]
    for relation in structural:
        for endpoint in (relation.source, relation.target):
            payload = _node_payload(endpoint, layer="structural")
            node_map.setdefault(payload["id"], payload)
    for relation in admitted_knowledge:
        for endpoint in (relation.source, relation.target):
            payload = _node_payload(endpoint, layer="knowledge" if not isinstance(endpoint, WorldEntityRef) else "structural")
            node_map.setdefault(payload["id"], payload)
    edges = [_edge_payload(relation, family="structural") for relation in structural]
    edges.extend(_edge_payload(relation, family="knowledge") for relation in admitted_knowledge)
    edges.sort(key=lambda item: (item["family"], item["relation_id"]))
    nodes = sorted(node_map.values(), key=lambda item: item["id"])
    source_refs = sorted({ref for edge in edges for ref in edge["source_refs"]})
    identity_links = [ref.to_dict() for ref in published.identity_link_refs]
    counts = {
        "nodes": len(nodes),
        "edges": len(edges),
        "structural_nodes": sum(1 for node in nodes if node["layer"] == "structural"),
        "knowledge_nodes": sum(1 for node in nodes if node["layer"] == "knowledge"),
        "structural_edges": sum(1 for edge in edges if edge["family"] == "structural"),
        "knowledge_edges": sum(1 for edge in edges if edge["family"] == "knowledge"),
        "returned_nodes": len(nodes),
        "returned_edges": len(edges),
        "identity_links": len(identity_links),
    }
    returned_nodes, returned_edges, truncation = _truncate(nodes, edges)
    counts["returned_nodes"] = len(returned_nodes)
    counts["returned_edges"] = len(returned_edges)
    provenance = {
        "source_refs": source_refs,
        "identity_links": identity_links,
        "receipt_count": receipt_count,
    }
    return returned_nodes, returned_edges, counts, provenance, truncation


def _edge_payload(relation: StructuralWorldRelation | KnowledgeWorldRelation, *, family: str) -> dict[str, Any]:
    if relation.kind in FORBIDDEN_RELATION_KINDS:
        raise ValueError("CAUSES edges are forbidden")
    return {
        "id": relation.relation_id,
        "relation_id": relation.relation_id,
        "family": family,
        "kind": relation.kind,
        "source": _graph_node_id(relation.source),
        "target": _graph_node_id(relation.target),
        "source_node": relation.source.to_dict(),
        "target_node": relation.target.to_dict(),
        "ontology_revision": relation.ontology_revision,
        "effective_from": _iso(relation.effective_from),
        "effective_until": None if relation.effective_until is None else _iso(relation.effective_until),
        "source_refs": list(relation.source_refs),
        "content_sha256": relation.content_sha256,
        "supersedes": relation.supersedes,
    }


def _truncate(
    nodes: Sequence[Mapping[str, Any]],
    edges: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    kept_nodes = [dict(node) for node in nodes[:WORLD_GRAPH_EXPLORER_NODE_LIMIT]]
    kept_ids = {node["id"] for node in kept_nodes}
    kept_edges: list[dict[str, Any]] = []
    for edge in edges:
        if len(kept_edges) >= WORLD_GRAPH_EXPLORER_EDGE_LIMIT:
            break
        if edge["source"] in kept_ids and edge["target"] in kept_ids:
            kept_edges.append(dict(edge))
    omitted_nodes = max(0, len(nodes) - len(kept_nodes))
    omitted_edges = max(0, len(edges) - len(kept_edges))
    return (
        kept_nodes,
        kept_edges,
        {
            "truncated": omitted_nodes > 0 or omitted_edges > 0,
            "node_limit": WORLD_GRAPH_EXPLORER_NODE_LIMIT,
            "edge_limit": WORLD_GRAPH_EXPLORER_EDGE_LIMIT,
            "omitted_nodes": omitted_nodes,
            "omitted_edges": omitted_edges,
        },
    )


__all__ = [
    "WORLD_GRAPH_EXPLORER_EDGE_LIMIT",
    "WORLD_GRAPH_EXPLORER_NODE_LIMIT",
    "WorldGraphExplorerService",
]
