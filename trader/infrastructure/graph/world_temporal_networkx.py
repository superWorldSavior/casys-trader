"""Fresh in-memory NetworkX projection of resolved world-graph views.

The projector is a traversal helper, never a persistence or lifecycle
authority. It reads only point-in-time structural and knowledge views; it never
inspects a current cache, store, or pickled graph. Mutating ``copy_backend()``
cannot change the source records.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol

import networkx as nx

from trader.domain.world_episode import parse_utc_timestamp
from trader.domain.world_graph import (
    FORBIDDEN_RELATION_KINDS,
    GEOGRAPHIC_ANCESTRY_WALK,
    GRAPH_TRAVERSAL_POLICY_VERSION,
    GRAPH_TRAVERSAL_V1_DIRECTIONS,
    ROOT_BRANCH_WALK,
    KnowledgeArtifactRef,
    KnowledgeWorldRelation,
    MacroSourceFactVersionRef,
    PatternHypothesisRef,
    SensorRef,
    StructuralWorldRelation,
    WorldEntityRef,
    WorldGraphNodeRef,
    WorldGraphSnapshotRef,
    WorldObservationRef,
    WorldOntologyRevision,
    graph_expansion_priority,
    parse_world_graph_node_ref,
)


GRAPH_TRAVERSAL_MAX_DEPTH = 4
GRAPH_TRAVERSAL_MAX_PATHS = 32
_FRESHNESS_4H = timedelta(hours=4)
_FRESHNESS_24H = timedelta(hours=24)
_FRESHNESS_7D = timedelta(days=7)


class _StructuralView(Protocol):
    cutoff_at: datetime
    entities: Sequence[WorldEntityRef]
    structural_relations: Sequence[StructuralWorldRelation]
    published_revision: WorldOntologyRevision | None


class _KnowledgeView(Protocol):
    cutoff_at: datetime
    structural_revision_id: str
    structural_revision_hash: str
    relations: Sequence[KnowledgeWorldRelation]


@dataclass(frozen=True)
class WorldTemporalPathStep:
    relation_id: str
    kind: str
    family: str
    direction: str
    source_kind: str
    target_kind: str
    source_node_id: str
    target_node_id: str
    freshness_bucket: str

    def signature_token(self) -> str:
        return f"{self.source_kind}|{self.kind}:{self.direction}:{self.freshness_bucket}|{self.target_kind}"


@dataclass(frozen=True)
class WorldTemporalPath:
    steps: tuple[WorldTemporalPathStep, ...]

    @property
    def signature(self) -> str:
        return ">".join(step.signature_token() for step in self.steps)


@dataclass(frozen=True)
class WorldTemporalPathSet:
    paths: tuple[WorldTemporalPath, ...]
    status: str
    policy_version: str = GRAPH_TRAVERSAL_POLICY_VERSION

    @property
    def truncated(self) -> bool:
        return self.status == "graph_budget_exceeded"


@dataclass(frozen=True)
class _Hop:
    relation_id: str
    kind: str
    family: str
    direction: str
    from_id: str
    to_id: str
    from_kind: str
    to_kind: str
    freshness_bucket: str


def _hop_rank(hop: _Hop) -> tuple[int, int]:
    """Prefer geographic ancestry and root branches over venue-peer fan-out."""

    return graph_expansion_priority(hop.kind, hop.direction, hop.to_kind)


@dataclass(frozen=True)
class _Pending:
    hop: _Hop
    visited: frozenset[str]
    steps: tuple[WorldTemporalPathStep, ...]

    def sort_key(self) -> tuple[object, ...]:
        rank = _hop_rank(self.hop)
        return (
            *rank,
            self.hop.relation_id,
            self.hop.direction,
            self.hop.to_id,
            tuple(step.relation_id for step in self.steps),
        )


def _as_cutoff(value: datetime | str) -> datetime:
    return parse_utc_timestamp(value, "cutoff_at").astimezone(timezone.utc)


def _reject_forbidden_kind(kind: str) -> None:
    compact = kind.upper().replace("-", "_")
    if compact in FORBIDDEN_RELATION_KINDS or "CAUS" in compact:
        raise ValueError("generic CAUSES edges are forbidden; the ontology is not a causal graph")


def _graph_node_id(node: WorldGraphNodeRef) -> str:
    if isinstance(node, WorldEntityRef):
        return node.node_id
    if isinstance(node, SensorRef):
        return node.node_id
    if isinstance(node, KnowledgeArtifactRef):
        return node.artifact_id
    if isinstance(node, WorldObservationRef):
        return node.observation_id
    if isinstance(node, WorldGraphSnapshotRef):
        return node.snapshot_id
    if isinstance(node, PatternHypothesisRef):
        return node.hypothesis_id
    if isinstance(node, MacroSourceFactVersionRef):
        return node.fact_version_id
    raise TypeError(f"unsupported graph node: {type(node).__name__}")


def _graph_node_kind(node: WorldGraphNodeRef) -> str:
    if isinstance(node, WorldEntityRef):
        return node.kind
    return node.node_kind


def _freshness_bucket(effective_from: datetime, cutoff: datetime) -> str:
    age = cutoff - effective_from
    if age < _FRESHNESS_4H:
        return "0-4h"
    if age < _FRESHNESS_24H:
        return "4-24h"
    if age < _FRESHNESS_7D:
        return "1-7d"
    return "older"


def _bind_knowledge_overlay(structural: _StructuralView, knowledge: _KnowledgeView, cutoff: datetime) -> None:
    knowledge_cutoff = _as_cutoff(knowledge.cutoff_at)
    if knowledge_cutoff != cutoff:
        raise ValueError("structural and knowledge views must share the same cutoff")
    published = structural.published_revision
    relations = tuple(knowledge.relations)
    if published is None:
        if relations:
            raise ValueError("knowledge overlay requires a published structural revision")
        return
    if knowledge.structural_revision_id != published.revision_id:
        raise ValueError("knowledge overlay revision does not match the published structural revision")
    if knowledge.structural_revision_hash != published.content_sha256:
        raise ValueError("knowledge overlay revision hash does not match the published structural revision")


def _admit_structural(
    relations: Sequence[StructuralWorldRelation], cutoff: datetime
) -> tuple[StructuralWorldRelation, ...]:
    admitted: list[StructuralWorldRelation] = []
    for relation in relations:
        _reject_forbidden_kind(relation.kind)
        if relation.effective_at(cutoff):
            admitted.append(relation)
    return tuple(admitted)


def _admit_knowledge(
    relations: Sequence[KnowledgeWorldRelation],
    cutoff: datetime,
    published: WorldOntologyRevision | None,
) -> tuple[KnowledgeWorldRelation, ...]:
    admitted: list[KnowledgeWorldRelation] = []
    for relation in relations:
        _reject_forbidden_kind(relation.kind)
        if relation.kind != "ABOUT" and published is not None and relation.ontology_revision != published.revision_id:
            continue
        if relation.effective_at(cutoff):
            admitted.append(relation)
    return tuple(admitted)


def _node_attributes(node: WorldGraphNodeRef) -> dict[str, object]:
    payload: dict[str, object] = dict(node.to_dict())
    payload["kind"] = _graph_node_kind(node)
    return payload


def _edge_attributes(relation: StructuralWorldRelation | KnowledgeWorldRelation) -> dict[str, object]:
    return {
        "family": relation.family,
        "kind": relation.kind,
        "relation_id": relation.relation_id,
        "content_sha256": relation.content_sha256,
        "ontology_revision": relation.ontology_revision,
        "effective_from": relation.effective_from.isoformat(),
        "effective_until": None if relation.effective_until is None else relation.effective_until.isoformat(),
        "source_refs": list(relation.source_refs),
    }


def _collect_nodes(
    entities: Sequence[WorldEntityRef],
    structural: Sequence[StructuralWorldRelation],
    knowledge: Sequence[KnowledgeWorldRelation],
) -> dict[str, WorldGraphNodeRef]:
    nodes: dict[str, WorldGraphNodeRef] = {}
    for entity in entities:
        nodes[_graph_node_id(entity)] = entity
    for relation in structural:
        nodes[_graph_node_id(relation.source)] = relation.source
        nodes[_graph_node_id(relation.target)] = relation.target
    for relation in knowledge:
        nodes[_graph_node_id(relation.source)] = relation.source
        nodes[_graph_node_id(relation.target)] = relation.target
    return nodes


def _build_backend(
    nodes: Mapping[str, WorldGraphNodeRef],
    structural: Sequence[StructuralWorldRelation],
    knowledge: Sequence[KnowledgeWorldRelation],
) -> nx.MultiDiGraph:
    graph = nx.MultiDiGraph()
    for node_id, node in sorted(nodes.items()):
        graph.add_node(node_id, **_node_attributes(node))
    ordered = sorted(
        (*structural, *knowledge),
        key=lambda item: (item.family, item.kind, item.relation_id or ""),
    )
    for relation in ordered:
        graph.add_edge(
            _graph_node_id(relation.source),
            _graph_node_id(relation.target),
            key=relation.content_sha256,
            **_edge_attributes(relation),
        )
    return graph


def _build_adjacency(
    structural: Sequence[StructuralWorldRelation],
    knowledge: Sequence[KnowledgeWorldRelation],
    cutoff: datetime,
) -> dict[str, tuple[_Hop, ...]]:
    hops: dict[str, list[_Hop]] = {}
    for relation in (*structural, *knowledge):
        allowed = GRAPH_TRAVERSAL_V1_DIRECTIONS.get(relation.kind, frozenset())
        if not allowed:
            continue
        source_id = _graph_node_id(relation.source)
        target_id = _graph_node_id(relation.target)
        source_kind = _graph_node_kind(relation.source)
        target_kind = _graph_node_kind(relation.target)
        bucket = _freshness_bucket(relation.effective_from, cutoff)
        if "forward" in allowed:
            hops.setdefault(source_id, []).append(
                _Hop(
                    relation_id=relation.relation_id or "",
                    kind=relation.kind,
                    family=relation.family,
                    direction="forward",
                    from_id=source_id,
                    to_id=target_id,
                    from_kind=source_kind,
                    to_kind=target_kind,
                    freshness_bucket=bucket,
                )
            )
        if "reverse" in allowed:
            hops.setdefault(target_id, []).append(
                _Hop(
                    relation_id=relation.relation_id or "",
                    kind=relation.kind,
                    family=relation.family,
                    direction="reverse",
                    from_id=target_id,
                    to_id=source_id,
                    from_kind=target_kind,
                    to_kind=source_kind,
                    freshness_bucket=bucket,
                )
            )
    return {
        node_id: tuple(sorted(items, key=lambda item: (item.relation_id, item.direction, item.to_id)))
        for node_id, items in hops.items()
    }


class WorldTemporalGraph:
    """Deterministic throw-away MultiDiGraph over resolved graph views."""

    def __init__(
        self,
        graph: nx.MultiDiGraph,
        *,
        cutoff_at: datetime,
        entities: tuple[WorldEntityRef, ...],
        structural: tuple[StructuralWorldRelation, ...],
        knowledge: tuple[KnowledgeWorldRelation, ...],
        adjacency: Mapping[str, tuple[_Hop, ...]],
        node_ids: frozenset[str],
    ) -> None:
        self._graph = graph
        self._cutoff = cutoff_at
        self._entities = entities
        self._structural = structural
        self._knowledge = knowledge
        self._adjacency = dict(adjacency)
        self._node_ids = node_ids

    @classmethod
    def from_resolved_views(cls, structural: _StructuralView, knowledge: _KnowledgeView) -> WorldTemporalGraph:
        cutoff = _as_cutoff(structural.cutoff_at)
        _bind_knowledge_overlay(structural, knowledge, cutoff)
        admitted_structural = _admit_structural(structural.structural_relations, cutoff)
        admitted_knowledge = _admit_knowledge(
            knowledge.relations,
            cutoff,
            structural.published_revision,
        )
        entities = tuple(structural.entities)
        nodes = _collect_nodes(entities, admitted_structural, admitted_knowledge)
        backend = _build_backend(nodes, admitted_structural, admitted_knowledge)
        adjacency = _build_adjacency(admitted_structural, admitted_knowledge, cutoff)
        return cls(
            backend,
            cutoff_at=cutoff,
            entities=entities,
            structural=admitted_structural,
            knowledge=admitted_knowledge,
            adjacency=adjacency,
            node_ids=frozenset(nodes),
        )

    @property
    def cutoff_at(self) -> datetime:
        return self._cutoff

    @property
    def traversal_policy_version(self) -> str:
        return GRAPH_TRAVERSAL_POLICY_VERSION

    @property
    def edge_count(self) -> int:
        return self._graph.number_of_edges()

    @property
    def structural_edge_count(self) -> int:
        return len(self._structural)

    @property
    def knowledge_edge_count(self) -> int:
        return len(self._knowledge)

    def copy_backend(self) -> nx.MultiDiGraph:
        """Return a detached NetworkX copy. Callers may mutate it without affecting records."""

        return copy.deepcopy(self._graph)

    def canonical_records(self) -> dict[str, object]:
        """JSON-serializable projection payload. Never embeds a NetworkX object."""

        return {
            "cutoff_at": self._cutoff.isoformat(),
            "traversal_policy_version": GRAPH_TRAVERSAL_POLICY_VERSION,
            "entities": [item.to_dict() for item in sorted(self._entities, key=lambda item: item.node_id)],
            "structural_relations": [
                item.to_dict() for item in sorted(self._structural, key=lambda item: item.relation_id or "")
            ],
            "knowledge_relations": [
                item.to_dict() for item in sorted(self._knowledge, key=lambda item: item.relation_id or "")
            ],
        }

    def enumerate_paths(
        self,
        root: WorldGraphNodeRef | Mapping[str, object],
        *,
        max_depth: int | None = None,
        max_paths: int | None = None,
    ) -> WorldTemporalPathSet:
        if max_depth is not None and max_depth < 0:
            raise ValueError("max_depth must not be negative")
        if max_paths is not None and max_paths < 0:
            raise ValueError("max_paths must not be negative")
        depth_limit = GRAPH_TRAVERSAL_MAX_DEPTH if max_depth is None else min(max_depth, GRAPH_TRAVERSAL_MAX_DEPTH)
        path_limit = GRAPH_TRAVERSAL_MAX_PATHS if max_paths is None else min(max_paths, GRAPH_TRAVERSAL_MAX_PATHS)
        root_id = _graph_node_id(parse_world_graph_node_ref(root))
        if root_id not in self._node_ids:
            return WorldTemporalPathSet(paths=(), status="complete")

        paths: list[WorldTemporalPath] = []
        truncated = False

        def pending_from(
            current_id: str, visited: frozenset[str], steps: tuple[WorldTemporalPathStep, ...]
        ) -> list[_Pending]:
            if len(steps) >= depth_limit:
                return []
            pending: list[_Pending] = []
            for hop in self._adjacency.get(current_id, ()):
                if hop.to_id in visited:
                    continue
                pending.append(_Pending(hop=hop, visited=visited, steps=steps))
            return pending

        frontier = pending_from(root_id, frozenset({root_id}), ())
        frontier.sort(key=lambda item: item.sort_key())
        while frontier:
            if len(paths) >= path_limit:
                truncated = True
                break
            item = frontier.pop(0)
            hop = item.hop
            if hop.to_id in item.visited:
                continue
            step = WorldTemporalPathStep(
                relation_id=hop.relation_id,
                kind=hop.kind,
                family=hop.family,
                direction=hop.direction,
                source_kind=hop.from_kind,
                target_kind=hop.to_kind,
                source_node_id=hop.from_id,
                target_node_id=hop.to_id,
                freshness_bucket=hop.freshness_bucket,
            )
            next_steps = (*item.steps, step)
            paths.append(WorldTemporalPath(steps=next_steps))
            frontier.extend(pending_from(hop.to_id, item.visited | {hop.to_id}, next_steps))
            frontier.sort(key=lambda pending: pending.sort_key())

        status = "graph_budget_exceeded" if truncated else "complete"
        return WorldTemporalPathSet(paths=tuple(paths), status=status)


__all__ = [
    "GEOGRAPHIC_ANCESTRY_WALK",
    "GRAPH_TRAVERSAL_MAX_DEPTH",
    "GRAPH_TRAVERSAL_MAX_PATHS",
    "GRAPH_TRAVERSAL_V1_DIRECTIONS",
    "ROOT_BRANCH_WALK",
    "WorldTemporalGraph",
    "WorldTemporalPath",
    "WorldTemporalPathSet",
    "WorldTemporalPathStep",
]
