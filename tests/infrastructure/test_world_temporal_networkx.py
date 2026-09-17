from __future__ import annotations

import inspect
import json
from datetime import datetime, timedelta, timezone

import networkx as nx
import pytest

from trader.application.world_model.ontology_service import (
    WorldKnowledgeOverlayView,
    WorldOntologyRevisionView,
)
from trader.domain.world_graph import (
    GRAPH_TRAVERSAL_POLICY_VERSION,
    GRAPH_TRAVERSAL_V1_DIRECTIONS,
    KnowledgeArtifactRef,
    KnowledgeWorldRelation,
    StructuralWorldRelation,
    WorldEntityIdentityMap,
    WorldEntityRef,
    WorldObservationRef,
    WorldOntologyRevision,
    WorldStructuralRelationRef,
)
from trader.infrastructure.graph import world_temporal_networkx as nx_projector
from trader.infrastructure.graph.world_temporal_networkx import (
    GRAPH_TRAVERSAL_MAX_DEPTH,
    GRAPH_TRAVERSAL_MAX_PATHS,
    WorldTemporalGraph,
)


UTC = timezone.utc
T0 = datetime(2026, 1, 1, tzinfo=UTC)
CUTOFF = datetime(2026, 8, 23, 13, 0, tzinfo=UTC)
LATER = datetime(2026, 8, 23, 18, 0, tzinfo=UTC)
SHA = "a" * 64
SCOPE_HASH = "b" * 64
OBS_SHA = "c" * 64
ART_SHA = "d" * 64


def _instrument(symbol: str = "2330") -> WorldEntityRef:
    return WorldEntityRef(kind="instrument", entity_id=f"mic:XTAI:symbol:{symbol}")


def _venue() -> WorldEntityRef:
    return WorldEntityRef(kind="venue", entity_id="mic:XTAI")


def _region() -> WorldEntityRef:
    return WorldEntityRef(kind="region", entity_id="iso-un-m49:030")


def _country() -> WorldEntityRef:
    return WorldEntityRef(kind="country", entity_id="iso-3166:TW")


def _family() -> WorldEntityRef:
    return WorldEntityRef(kind="family", entity_id="taxonomy:v1:semiconductors")


def _company() -> WorldEntityRef:
    return WorldEntityRef(kind="company", entity_id="lei:549300ABCDEFGHIJKLMN")


def _world() -> WorldEntityRef:
    return WorldEntityRef(kind="world", entity_id="market")


def _observation() -> WorldObservationRef:
    return WorldObservationRef(observation_id=f"world_observation:v1:{OBS_SHA}")


def _artifact() -> KnowledgeArtifactRef:
    return KnowledgeArtifactRef(artifact_id=f"knowledge_artifact:v1:{ART_SHA}", content_sha256=ART_SHA)


def _structural(**overrides: object) -> StructuralWorldRelation:
    values: dict[str, object] = {
        "kind": "TRADED_ON",
        "source": _instrument(),
        "target": _venue(),
        "effective_from": T0,
        "ontology_revision": "market_ontology.v1",
        "source_refs": ("provider:instrument-master:2330",),
    }
    values.update(overrides)
    return StructuralWorldRelation(**values)  # type: ignore[arg-type]


def _knowledge(**overrides: object) -> KnowledgeWorldRelation:
    values: dict[str, object] = {
        "kind": "OBSERVES",
        "source": _observation(),
        "target": _region(),
        "effective_from": CUTOFF - timedelta(hours=1),
        "ontology_revision": "market_ontology.v1",
        "source_refs": (f"macro_world_observation:v1:{OBS_SHA}",),
    }
    values.update(overrides)
    return KnowledgeWorldRelation(**values)  # type: ignore[arg-type]


def _views(
    *,
    entities: tuple[WorldEntityRef, ...] | None = None,
    structural_relations: tuple[StructuralWorldRelation, ...] = (),
    knowledge_relations: tuple[KnowledgeWorldRelation, ...] = (),
    cutoff_at: datetime = CUTOFF,
    knowledge_cutoff_at: datetime | None = None,
    revision_id: str = "market_ontology.v1",
    overlay_revision_id: str | None = None,
    overlay_revision_hash: str | None = None,
) -> tuple[WorldOntologyRevisionView, WorldKnowledgeOverlayView]:
    resolved_entities = entities if entities is not None else ()
    published = WorldOntologyRevision(
        revision_id=revision_id,
        entities=resolved_entities,
        structural_relation_refs=tuple(WorldStructuralRelationRef.from_relation(item) for item in structural_relations),
        identity_link_refs=(),
        scope_mapping_id="world_scope_mapping.v1",
        scope_mapping_hash=SCOPE_HASH,
    )
    structural = WorldOntologyRevisionView(
        cutoff_at=cutoff_at,
        entities=resolved_entities,
        entity_revision_refs=(),
        structural_relations=structural_relations,
        identity_map=WorldEntityIdentityMap.empty(),
        identity_links=(),
        published_revision=published,
        entity_heads_hash=published.entity_heads_hash,
        structural_heads_hash=published.structural_heads_hash,
        identity_map_hash=published.identity_map_hash,
    )
    knowledge = WorldKnowledgeOverlayView(
        cutoff_at=cutoff_at if knowledge_cutoff_at is None else knowledge_cutoff_at,
        structural_revision_id=overlay_revision_id if overlay_revision_id is not None else published.revision_id,
        structural_revision_hash=(
            overlay_revision_hash if overlay_revision_hash is not None else published.content_sha256
        ),
        relations=knowledge_relations,
    )
    return structural, knowledge


def _rfc_topology() -> tuple[tuple[WorldEntityRef, ...], tuple[StructuralWorldRelation, ...], KnowledgeWorldRelation]:
    entities = (_instrument(), _venue(), _region(), _country(), _family(), _company(), _world())
    structural = (
        _structural(),
        _structural(
            kind="LOCATED_IN",
            source=_venue(),
            target=_region(),
            source_refs=("world-scope-mapping:xtai-region",),
        ),
        _structural(
            kind="LOCATED_IN",
            source=_country(),
            target=_region(),
            source_refs=("world-scope-mapping:tw-region",),
        ),
        _structural(
            kind="ISSUED_BY",
            source=_instrument(),
            target=_company(),
            source_refs=("provider:lei:549300ABCDEFGHIJKLMN",),
        ),
        _structural(
            kind="MEMBER_OF_FAMILY",
            source=_instrument(),
            target=_family(),
            source_refs=("taxonomy:v1:semiconductors",),
        ),
        _structural(
            kind="PART_OF_WORLD",
            source=_region(),
            target=_world(),
            source_refs=("world-scope-mapping:region-world",),
        ),
        _structural(
            kind="TRADED_ON",
            source=_instrument("2330"),
            target=_venue(),
            source_refs=("provider:instrument-master:2330:duplicate-catalog",),
        ),
    )
    return entities, structural, _knowledge()


def test_from_resolved_views_is_a_fresh_projection_of_views_not_a_ledger() -> None:
    entities, structural_relations, knowledge = _rfc_topology()
    structural, overlay = _views(
        entities=entities,
        structural_relations=structural_relations,
        knowledge_relations=(knowledge,),
    )
    graph = WorldTemporalGraph.from_resolved_views(structural, overlay)
    parameters = inspect.signature(WorldTemporalGraph.from_resolved_views).parameters
    assert "structural" in parameters
    assert "knowledge" in parameters
    assert "ledger" not in parameters
    assert "store" not in parameters
    assert "current" not in parameters
    assert graph.cutoff_at == CUTOFF
    assert graph.traversal_policy_version == GRAPH_TRAVERSAL_POLICY_VERSION == "graph_traversal.v1"
    backend = graph.copy_backend()
    assert isinstance(backend, nx.MultiDiGraph)
    assert graph.structural_edge_count == 7
    assert graph.knowledge_edge_count == 1
    assert graph.edge_count == 8


def test_future_and_expired_relations_in_a_view_are_invisible_at_cutoff() -> None:
    present = _structural()
    future = _structural(effective_from=LATER, source_refs=("provider:instrument-master:future",))
    expired = _structural(
        effective_from=T0,
        effective_until=CUTOFF,
        source_refs=("provider:instrument-master:expired",),
    )
    late_knowledge = _knowledge(effective_from=LATER, source_refs=(f"macro_world_observation:v1:{SHA}",))
    structural, overlay = _views(
        entities=(_instrument(), _venue(), _region()),
        structural_relations=(present, future, expired),
        knowledge_relations=(late_knowledge,),
    )
    graph = WorldTemporalGraph.from_resolved_views(structural, overlay)
    backend = graph.copy_backend()
    digests = {data["content_sha256"] for _s, _t, data in backend.edges(data=True)}
    assert present.content_sha256 in digests
    assert future.content_sha256 not in digests
    assert expired.content_sha256 not in digests
    assert graph.knowledge_edge_count == 0
    assert all(data["family"] == "structural" for _s, _t, data in backend.edges(data=True))


def test_mismatched_view_cutoffs_and_revision_bindings_are_rejected() -> None:
    structural, overlay = _views(
        entities=(_instrument(), _venue()),
        structural_relations=(_structural(),),
        knowledge_cutoff_at=LATER,
    )
    with pytest.raises(ValueError, match="cutoff"):
        WorldTemporalGraph.from_resolved_views(structural, overlay)

    structural, overlay = _views(
        entities=(_instrument(), _venue()),
        structural_relations=(_structural(),),
        overlay_revision_id="market_ontology.other",
    )
    with pytest.raises(ValueError, match="revision"):
        WorldTemporalGraph.from_resolved_views(structural, overlay)

    structural, overlay = _views(
        entities=(_instrument(), _venue()),
        structural_relations=(_structural(),),
        overlay_revision_hash="f" * 64,
    )
    with pytest.raises(ValueError, match="revision"):
        WorldTemporalGraph.from_resolved_views(structural, overlay)


def test_family_stamped_about_bypasses_tip_binding() -> None:
    about = _knowledge(
        kind="ABOUT",
        source=_artifact(),
        target=_instrument(),
        ontology_revision="market_ontology.v1",
    )
    structural, overlay = _views(
        entities=(_instrument(), _venue()),
        structural_relations=(_structural(),),
        knowledge_relations=(about,),
        revision_id="market_ontology:v1:" + "ab" * 32,
    )
    graph = WorldTemporalGraph.from_resolved_views(structural, overlay)
    assert graph.knowledge_edge_count == 1


def test_knowledge_and_structural_edges_stay_separate_and_keep_parallel_keys() -> None:
    first, second = _rfc_topology()[1][0], _rfc_topology()[1][-1]
    assert first.kind == second.kind == "TRADED_ON"
    assert first.content_sha256 != second.content_sha256
    about = KnowledgeWorldRelation(
        kind="ABOUT",
        source=_artifact(),
        target=_instrument(),
        effective_from=CUTOFF - timedelta(hours=2),
        ontology_revision="market_ontology.v1",
        source_refs=(f"artifact:{ART_SHA}",),
    )
    structural, overlay = _views(
        entities=(_instrument(), _venue(), _region()),
        structural_relations=(first, second),
        knowledge_relations=(_knowledge(), about),
    )
    graph = WorldTemporalGraph.from_resolved_views(structural, overlay)
    backend = graph.copy_backend()
    traded = backend.get_edge_data(_instrument().node_id, _venue().node_id) or {}
    assert set(traded) == {first.content_sha256, second.content_sha256}
    families = {data["family"] for _s, _t, data in backend.edges(data=True)}
    assert families == {"structural", "knowledge"}
    observes = backend.get_edge_data(_observation().observation_id, _region().node_id) or {}
    assert _knowledge().content_sha256 in observes
    about_edges = backend.get_edge_data(_artifact().artifact_id, _instrument().node_id) or {}
    assert about.content_sha256 in about_edges
    assert graph.structural_edge_count == 2
    assert graph.knowledge_edge_count == 2


def test_copy_backend_mutation_is_detached_from_domain_records_and_reprojections() -> None:
    original = _structural()
    payload = original.to_dict()
    structural, overlay = _views(
        entities=(_instrument(), _venue()),
        structural_relations=(original,),
    )
    graph = WorldTemporalGraph.from_resolved_views(structural, overlay)
    backend = graph.copy_backend()
    source, target, key = next(iter(backend.edges(keys=True)))
    backend.edges[source, target, key]["kind"] = "CAUSES"
    backend.edges[source, target, key]["source_refs"].append("mutated")
    backend.nodes[source]["kind"] = "mutated"
    second = graph.copy_backend()
    assert second.edges[source, target, key]["kind"] == "TRADED_ON"
    assert "mutated" not in second.edges[source, target, key]["source_refs"]
    assert original.to_dict() == payload
    assert original.kind != "CAUSES"
    records = graph.canonical_records()
    assert records["structural_relations"][0]["kind"] == "TRADED_ON"
    assert "mutated" not in json.dumps(records)


def test_canonical_records_are_json_payloads_without_networkx_objects() -> None:
    entities, structural_relations, knowledge = _rfc_topology()
    structural, overlay = _views(
        entities=entities,
        structural_relations=structural_relations,
        knowledge_relations=(knowledge,),
    )
    graph = WorldTemporalGraph.from_resolved_views(structural, overlay)
    records = graph.canonical_records()
    encoded = json.dumps(records)
    assert "networkx" not in encoded.lower()
    assert "MultiDiGraph" not in encoded
    assert records["traversal_policy_version"] == "graph_traversal.v1"
    assert records["cutoff_at"] == CUTOFF.isoformat()

    def _walk(value: object) -> None:
        assert not isinstance(value, nx.Graph)
        if isinstance(value, dict):
            for nested in value.values():
                _walk(nested)
        elif isinstance(value, (list, tuple)):
            for nested in value:
                _walk(nested)

    _walk(records)


def test_observation_reaches_instrument_via_allowed_structural_reverse() -> None:
    entities, structural_relations, knowledge = _rfc_topology()
    structural, overlay = _views(
        entities=entities,
        structural_relations=structural_relations,
        knowledge_relations=(knowledge,),
    )
    graph = WorldTemporalGraph.from_resolved_views(structural, overlay)
    enumerated = graph.enumerate_paths(_observation())
    assert enumerated.status == "complete"
    assert enumerated.policy_version == "graph_traversal.v1"
    signatures = [path.signature for path in enumerated.paths]
    expected = (
        "world_observation|OBSERVES:forward:0-4h|region"
        ">region|LOCATED_IN:reverse:older|venue"
        ">venue|TRADED_ON:reverse:older|instrument"
    )
    assert expected in signatures
    matching = next(path for path in enumerated.paths if path.signature == expected)
    assert [step.direction for step in matching.steps] == ["forward", "reverse", "reverse"]
    assert matching.steps[0].source_node_id == _observation().observation_id
    assert matching.steps[-1].target_node_id == _instrument().node_id
    assert "2330" not in matching.signature
    assert "549300" not in matching.signature
    assert _instrument().entity_id not in matching.signature
    assert _observation().observation_id not in matching.signature
    assert "iso-un-m49:030" not in matching.signature


def test_family_company_shortcut_and_disallowed_reverse_are_rejected() -> None:
    entities, structural_relations, knowledge = _rfc_topology()
    structural, overlay = _views(
        entities=entities,
        structural_relations=structural_relations,
        knowledge_relations=(knowledge,),
    )
    graph = WorldTemporalGraph.from_resolved_views(structural, overlay)
    from_family = graph.enumerate_paths(_family())
    from_company = graph.enumerate_paths(_company())
    from_world = graph.enumerate_paths(_world())
    assert from_family.paths == ()
    assert from_company.paths == ()
    assert from_world.paths == ()
    from_instrument = graph.enumerate_paths(_instrument())
    kinds = {step.kind for path in from_instrument.paths for step in path.steps}
    assert "MEMBER_OF_FAMILY" in kinds
    assert "ISSUED_BY" in kinds
    assert all(
        not (step.kind == "MEMBER_OF_FAMILY" and step.direction == "reverse")
        for path in from_instrument.paths
        for step in path.steps
    )
    assert not any(
        any(step.kind == "MEMBER_OF_FAMILY" for step in path.steps)
        and any(step.target_kind == "company" for step in path.steps)
        for path in from_instrument.paths
    )
    assert not any(
        any(step.kind == "ISSUED_BY" for step in path.steps)
        and any(step.target_kind == "family" for step in path.steps)
        for path in from_instrument.paths
    )


def test_path_enumeration_is_bounded_acyclic_and_ordered_by_relation_id() -> None:
    venue = _venue()
    traded_on = tuple(
        _structural(
            source=_instrument(f"I{index:02d}"),
            source_refs=(f"provider:instrument-master:I{index:02d}",),
        )
        for index in range(33)
    )
    entities = (venue, *(item.source for item in traded_on))
    structural, overlay = _views(entities=entities, structural_relations=traded_on)
    graph = WorldTemporalGraph.from_resolved_views(structural, overlay)
    assert GRAPH_TRAVERSAL_MAX_DEPTH == 4
    assert GRAPH_TRAVERSAL_MAX_PATHS == 32
    enumerated = graph.enumerate_paths(venue)
    expected_ids = tuple(item.relation_id for item in sorted(traded_on, key=lambda item: item.relation_id))
    assert enumerated.status == "graph_budget_exceeded"
    assert len(enumerated.paths) == 32
    assert tuple(path.steps[0].relation_id for path in enumerated.paths) == expected_ids[:32]
    assert all(len(path.steps) == 1 for path in enumerated.paths)
    for path in enumerated.paths:
        assert path.steps[0].direction == "reverse"
        assert path.steps[0].source_node_id == venue.node_id
        assert path.steps[0].target_node_id != venue.node_id

    depth_limited = graph.enumerate_paths(_instrument("I00"), max_depth=0)
    assert depth_limited.paths == ()
    assert depth_limited.status == "complete"

    entities, structural_relations, knowledge = _rfc_topology()
    structural, overlay = _views(
        entities=entities,
        structural_relations=structural_relations,
        knowledge_relations=(knowledge,),
    )
    rfc_graph = WorldTemporalGraph.from_resolved_views(structural, overlay)
    depth_two = rfc_graph.enumerate_paths(_observation(), max_depth=2)
    assert all(len(path.steps) <= 2 for path in depth_two.paths)
    assert all("instrument" not in path.signature for path in depth_two.paths)
    assert any(path.steps[-1].target_kind == "venue" for path in depth_two.paths)

    reversed_structural, overlay = _views(
        entities=entities,
        structural_relations=tuple(reversed(structural_relations)),
        knowledge_relations=(knowledge,),
    )
    reordered = WorldTemporalGraph.from_resolved_views(reversed_structural, overlay)
    first = rfc_graph.enumerate_paths(_observation())
    second = reordered.enumerate_paths(_observation())
    assert [path.signature for path in first.paths] == [path.signature for path in second.paths]
    assert [tuple(step.relation_id for step in path.steps) for path in first.paths] == [
        tuple(step.relation_id for step in path.steps) for path in second.paths
    ]
    for path in first.paths:
        nodes = (path.steps[0].source_node_id, *(step.target_node_id for step in path.steps))
        assert len(nodes) == len(set(nodes))
        assert all(step.signature_token().count("|") == 2 for step in path.steps)
        assert "2330" not in path.signature
        assert _company().entity_id not in path.signature


def test_projector_does_not_fold_supersession_or_read_a_current_graph() -> None:
    original = _structural()
    correction = original.corrected(
        target=WorldEntityRef(kind="venue", entity_id="mic:XTAF"),
        effective_from=T0,
        source_refs=("provider:instrument-master:2330:corrected",),
    )
    structural, overlay = _views(
        entities=(_instrument(), _venue(), WorldEntityRef(kind="venue", entity_id="mic:XTAF")),
        structural_relations=(original, correction),
    )
    graph = WorldTemporalGraph.from_resolved_views(structural, overlay)
    assert graph.structural_edge_count == 2
    digests = {item["content_sha256"] for item in graph.canonical_records()["structural_relations"]}
    assert original.content_sha256 in digests
    assert correction.content_sha256 in digests
    assert correction.supersedes == original.relation_id


def test_projector_imports_sole_graph_traversal_v1_directions() -> None:
    assert nx_projector.GRAPH_TRAVERSAL_V1_DIRECTIONS is GRAPH_TRAVERSAL_V1_DIRECTIONS
    source = inspect.getsource(nx_projector)
    assert 'GRAPH_TRAVERSAL_V1_DIRECTIONS = MappingProxyType' not in source


def test_projector_imports_the_domain_geographic_ancestry_walk() -> None:
    from trader.application.world_model import pattern_path
    from trader.domain.world_graph import GEOGRAPHIC_ANCESTRY_WALK, ROOT_BRANCH_WALK

    assert nx_projector.GEOGRAPHIC_ANCESTRY_WALK is GEOGRAPHIC_ANCESTRY_WALK
    assert pattern_path.GEOGRAPHIC_ANCESTRY_WALK is GEOGRAPHIC_ANCESTRY_WALK
    assert nx_projector.ROOT_BRANCH_WALK is ROOT_BRANCH_WALK
    assert pattern_path.ROOT_BRANCH_WALK is ROOT_BRANCH_WALK
    projector_source = inspect.getsource(nx_projector)
    pattern_source = inspect.getsource(pattern_path)
    assert "GEOGRAPHIC_ANCESTRY_WALK =" not in projector_source
    assert "GEOGRAPHIC_ANCESTRY_WALK =" not in pattern_source
    assert "_BACKBONE_FORWARD" not in projector_source
    assert "_ANCESTRY_WALK =" not in pattern_source
    assert "GEOGRAPHIC_ANCESTRY_WALK = (" not in projector_source
    assert "ROOT_BRANCH_WALK = (" not in projector_source


def _peers_sorting_before(target: StructuralWorldRelation, count: int) -> tuple[StructuralWorldRelation, ...]:
    peers: list[StructuralWorldRelation] = []
    index = 0
    while sum(1 for item in peers if item.relation_id < target.relation_id) < count:
        peers.append(
            _structural(
                source=_instrument(f"P{index:04d}"),
                source_refs=(f"provider:instrument-master:P{index:04d}",),
            )
        )
        index += 1
        if index > 5000:
            raise RuntimeError("could not synthesize peer relations before target")
    return tuple(peers)


def _dense_venue_ancestry(
    *, reverse: bool = False
) -> tuple[tuple[WorldEntityRef, ...], tuple[StructuralWorldRelation, ...]]:
    root = _instrument()
    venue = _venue()
    country = _country()
    region = _region()
    world = _world()
    company = _company()
    family = _family()
    located_country = _structural(
        kind="LOCATED_IN",
        source=venue,
        target=country,
        source_refs=("world-scope-mapping:xtai-country",),
    )
    backbone = (
        _structural(),
        located_country,
        _structural(
            kind="LOCATED_IN",
            source=country,
            target=region,
            source_refs=("world-scope-mapping:tw-region",),
        ),
        _structural(
            kind="PART_OF_WORLD",
            source=region,
            target=world,
            source_refs=("world-scope-mapping:region-world",),
        ),
        _structural(
            kind="ISSUED_BY",
            source=root,
            target=company,
            source_refs=("provider:lei:549300ABCDEFGHIJKLMN",),
        ),
        _structural(
            kind="MEMBER_OF_FAMILY",
            source=root,
            target=family,
            source_refs=("taxonomy:v1:semiconductors",),
        ),
    )
    peers = _peers_sorting_before(located_country, 40)
    structural = (*backbone, *peers)
    if reverse:
        structural = tuple(reversed(structural))
    entities = (root, venue, country, region, world, company, family, *(item.source for item in peers))
    return entities, structural


def test_dense_venue_peers_do_not_starve_instrument_geographic_ancestry() -> None:
    entities, structural = _dense_venue_ancestry()
    view, overlay = _views(entities=entities, structural_relations=structural)
    graph = WorldTemporalGraph.from_resolved_views(view, overlay)
    enumerated = graph.enumerate_paths(_instrument())
    assert GRAPH_TRAVERSAL_MAX_PATHS == 32
    assert GRAPH_TRAVERSAL_MAX_DEPTH == 4
    assert len(enumerated.paths) == 32
    assert enumerated.status == "graph_budget_exceeded"
    chains = [tuple((step.kind, step.target_kind) for step in path.steps) for path in enumerated.paths]
    assert (
        ("TRADED_ON", "venue"),
        ("LOCATED_IN", "country"),
        ("LOCATED_IN", "region"),
        ("PART_OF_WORLD", "world"),
    ) in chains
    target_kinds = {step.target_kind for path in enumerated.paths for step in path.steps}
    assert {"venue", "country", "region", "world", "company", "family"} <= target_kinds
    assert any(step.target_kind == "instrument" for path in enumerated.paths for step in path.steps)


def test_dense_instrument_traversal_is_deterministic_and_stays_within_budget() -> None:
    first_entities, first_structural = _dense_venue_ancestry()
    second_entities, second_structural = _dense_venue_ancestry(reverse=True)
    first = WorldTemporalGraph.from_resolved_views(*_views(entities=first_entities, structural_relations=first_structural))
    second = WorldTemporalGraph.from_resolved_views(
        *_views(entities=second_entities, structural_relations=second_structural)
    )
    left = first.enumerate_paths(_instrument())
    right = second.enumerate_paths(_instrument())
    assert [path.signature for path in left.paths] == [path.signature for path in right.paths]
    assert [tuple(step.relation_id for step in path.steps) for path in left.paths] == [
        tuple(step.relation_id for step in path.steps) for path in right.paths
    ]
    assert len(left.paths) == GRAPH_TRAVERSAL_MAX_PATHS
    assert left.status == right.status == "graph_budget_exceeded"
    for path in left.paths:
        nodes = (path.steps[0].source_node_id, *(step.target_node_id for step in path.steps))
        assert len(nodes) == len(set(nodes))
        assert len(path.steps) <= GRAPH_TRAVERSAL_MAX_DEPTH


def test_foreign_knowledge_relation_is_dropped_from_the_bound_overlay() -> None:
    native = _knowledge()
    foreign = _knowledge(
        ontology_revision="market_ontology.other",
        source_refs=(f"macro_world_observation:v1:{SHA}",),
        source=WorldObservationRef(observation_id=f"world_observation:v1:{SHA}"),
    )
    structural, overlay = _views(
        entities=(_instrument(), _venue(), _region()),
        structural_relations=(_structural(),),
        knowledge_relations=(native, foreign),
    )
    graph = WorldTemporalGraph.from_resolved_views(structural, overlay)
    assert graph.knowledge_edge_count == 1
    records = graph.canonical_records()
    assert [item["relation_id"] for item in records["knowledge_relations"]] == [native.relation_id]
