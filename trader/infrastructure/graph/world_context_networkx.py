"""NetworkX projection of typed World-context topology records.

The graph is built fresh from immutable domain edges.  It is a traversal and
validation helper, never a persistence authority: mutating the graph cannot
change the source records, and the domain never stores a NetworkX object.
Parallel temporal/provenance edges are preserved in a MultiDiGraph keyed by
the edge content digest.
"""

from __future__ import annotations

import copy
from collections.abc import Iterable, Mapping, Sequence

import networkx as nx

from trader.domain.world_context import EntityRef, TopologyEdge


def _node_id(entity: EntityRef) -> str:
    return entity.node_id


class WorldContextGraph:
    """Deterministic, throw-away directed multigraph over topology edges."""

    def __init__(self, graph: nx.MultiDiGraph, edges: tuple[TopologyEdge, ...]) -> None:
        self._graph = graph
        self._edges = edges
        self._by_digest = {edge.content_sha256: edge for edge in edges}

    @classmethod
    def from_records(
        cls,
        edges: Sequence[TopologyEdge] | Iterable[TopologyEdge],
        *,
        entities: Sequence[EntityRef] | None = None,
    ) -> WorldContextGraph:
        materialised = tuple(edges)
        graph = nx.MultiDiGraph()
        nodes = {(_node_id(edge.source), edge.source) for edge in materialised}
        nodes.update((_node_id(edge.target), edge.target) for edge in materialised)
        if entities:
            nodes.update((_node_id(entity), entity) for entity in entities)
        for node_id, entity in sorted(nodes, key=lambda item: item[0]):
            graph.add_node(
                node_id,
                kind=entity.kind,
                entity_id=entity.entity_id,
            )
        for edge in sorted(materialised, key=lambda item: (item.kind, item.content_sha256 or "")):
            graph.add_edge(
                _node_id(edge.source),
                _node_id(edge.target),
                key=edge.content_sha256,
                kind=edge.kind,
                content_sha256=edge.content_sha256,
                ontology_revision=edge.ontology_revision,
                ready_at=edge.ready_at.isoformat(),
                effective_from=edge.effective_from.isoformat(),
                effective_until=None if edge.effective_until is None else edge.effective_until.isoformat(),
                source_refs=list(edge.source_refs),
            )
        return cls(graph, materialised)

    @property
    def edge_count(self) -> int:
        return self._graph.number_of_edges()

    def relevant_neighbors(
        self,
        entity: EntityRef | Mapping[str, str],
        *,
        kinds: Sequence[str] | None = None,
    ) -> tuple[EntityRef, ...]:
        node = _node_id(entity if isinstance(entity, EntityRef) else EntityRef.from_mapping(entity))
        allowed = None if kinds is None else {kind.upper() for kind in kinds}
        found: list[EntityRef] = []
        seen: set[str] = set()
        if node not in self._graph:
            return ()
        incident = list(self._graph.in_edges(node, data=True, keys=True)) + list(
            self._graph.out_edges(node, data=True, keys=True)
        )
        for source, target, _key, data in incident:
            kind = str(data.get("kind") or "")
            if allowed is not None and kind not in allowed:
                continue
            other = target if source == node else source
            if other in seen:
                continue
            seen.add(other)
            other_data = self._graph.nodes[other]
            found.append(EntityRef(kind=other_data["kind"], entity_id=other_data["entity_id"]))
        return tuple(sorted(found, key=lambda item: (item.kind, item.entity_id)))

    def path(
        self,
        source: EntityRef | Mapping[str, str],
        target: EntityRef | Mapping[str, str],
    ) -> tuple[TopologyEdge, ...]:
        start = _node_id(source if isinstance(source, EntityRef) else EntityRef.from_mapping(source))
        end = _node_id(target if isinstance(target, EntityRef) else EntityRef.from_mapping(target))
        try:
            nodes = nx.shortest_path(self._graph, start, end)
        except (nx.NetworkXNoPath, nx.NodeNotFound):
            return ()
        path_edges: list[TopologyEdge] = []
        for left, right in zip(nodes, nodes[1:]):
            data = self._graph.get_edge_data(left, right) or {}
            if not data:
                continue
            key = sorted(str(item) for item in data)[0]
            edge = self._by_digest.get(key)
            if edge is not None:
                path_edges.append(edge)
        return tuple(path_edges)

    def validate_serialized_edges(self, serialized: Sequence[Mapping[str, object] | TopologyEdge]) -> None:
        """Fail if any domain-serialized edge is missing from this projection."""

        missing: list[str] = []
        for item in serialized:
            edge = item if isinstance(item, TopologyEdge) else TopologyEdge.from_mapping(item)
            data = self._graph.get_edge_data(_node_id(edge.source), _node_id(edge.target)) or {}
            if edge.content_sha256 not in data:
                missing.append(edge.content_sha256 or edge.kind)
        if missing:
            rendered = ", ".join(missing)
            raise ValueError(f"serialized topology edges missing from graph projection: {rendered}")

    def copy_backend(self) -> nx.MultiDiGraph:
        """Return a detached NetworkX copy. Callers may mutate it without affecting records."""

        return copy.deepcopy(self._graph)


__all__ = ["WorldContextGraph"]
