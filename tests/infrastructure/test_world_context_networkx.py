from __future__ import annotations

from datetime import datetime, timezone

from trader.domain.world_context import EntityRef, TopologyEdge, bootstrap_instrument_topology
from trader.infrastructure.graph.world_context_networkx import WorldContextGraph


CUTOFF = datetime(2026, 8, 22, 10, 5, tzinfo=timezone.utc)


def test_graph_is_a_fresh_projection_and_cannot_mutate_domain_edges() -> None:
    edges = bootstrap_instrument_topology(symbol="AAA", venue="XTAI", family="equity", cutoff_at=CUTOFF)
    original = tuple(edge.content_sha256 for edge in edges)
    graph = WorldContextGraph.from_records(edges)
    backend = graph.copy_backend()
    for node in list(backend.nodes):
        backend.nodes[node]["kind"] = "mutated"
    for source, target, key in list(backend.edges(keys=True)):
        backend.edges[source, target, key]["kind"] = "CAUSES"
        backend.edges[source, target, key]["content_sha256"] = "mutated"
    assert tuple(edge.content_sha256 for edge in edges) == original
    assert all(edge.kind != "CAUSES" for edge in edges)
    graph.validate_serialized_edges([edge.to_dict() for edge in edges])


def test_copy_backend_is_deeply_isolated() -> None:
    edges = bootstrap_instrument_topology(symbol="AAA", venue="XTAI", family="equity", cutoff_at=CUTOFF)
    graph = WorldContextGraph.from_records(edges)
    first = graph.copy_backend()
    source, target, key = next(iter(first.edges(keys=True)))
    first.edges[source, target, key]["source_refs"].append("mutated")
    second = graph.copy_backend()
    assert "mutated" not in second.edges[source, target, key]["source_refs"]
    original = graph.copy_backend()
    assert "mutated" not in original.edges[source, target, key]["source_refs"]


def test_neighbors_and_path_are_deterministic() -> None:
    edges = bootstrap_instrument_topology(symbol="AAA", venue="XTAI", family="equity", cutoff_at=CUTOFF)
    graph = WorldContextGraph.from_records(edges)
    instrument = EntityRef("instrument", "AAA")
    neighbors = graph.relevant_neighbors(instrument)
    assert [item.kind for item in neighbors] == ["family", "venue"]
    path = graph.path(instrument, EntityRef("world", "market"))
    assert path
    assert path[0].source.entity_id == "AAA"


def test_parallel_edges_are_preserved_and_path_selects_a_stable_key() -> None:
    first = TopologyEdge(
        kind="TRADED_ON",
        source=EntityRef("instrument", "AAA"),
        target=EntityRef("venue", "XTAI"),
        effective_from=CUTOFF,
        ready_at=CUTOFF,
        ontology_revision="semantic_catalog.v1",
        source_refs=("catalog-a",),
    )
    second = TopologyEdge(
        kind="TRADED_ON",
        source=EntityRef("instrument", "AAA"),
        target=EntityRef("venue", "XTAI"),
        effective_from=CUTOFF,
        ready_at=CUTOFF,
        ontology_revision="semantic_catalog.v1",
        source_refs=("catalog-b",),
    )
    world = TopologyEdge(
        kind="PART_OF_WORLD",
        source=EntityRef("venue", "XTAI"),
        target=EntityRef("world", "market"),
        effective_from=CUTOFF,
        ready_at=CUTOFF,
        ontology_revision="semantic_catalog.v1",
        source_refs=("semantic_catalog",),
    )
    assert first.content_sha256 != second.content_sha256
    graph = WorldContextGraph.from_records((first, second, world))
    assert graph.edge_count == 3
    path = graph.path(EntityRef("instrument", "AAA"), EntityRef("venue", "XTAI"))
    assert len(path) == 1
    assert path[0].content_sha256 == min(first.content_sha256, second.content_sha256)
    graph.validate_serialized_edges([first.to_dict(), second.to_dict(), world.to_dict()])
