from __future__ import annotations

import pytest

from trader.application.world_model.ontology_service import (
    AssertKnowledgeWorldRelation,
    PublishWorldOntologyRevision,
    WorldOntologyService,
)
from trader.domain.world_feature_contract import (
    GRAPH_CONTENT_CATEGORICAL_FEATURES,
    GRAPH_FEATURE_CONTRACT_VERSION,
    GRAPH_STATUS_CATEGORICAL_FEATURES,
    WORLD_V3_ENCODER_IDENTITY,
    WorldFeatureMask,
    world_v1_feature_contract,
    world_v3_feature_contract,
    world_v3_graph_content_mask,
    world_v3_topology_status_only_mask,
)
from trader.domain.world_scope import WorldMarketAnchorRef, WorldScopeResolution

from tests.application.test_world_graph_snapshot import (
    CUTOFF,
    LATER,
    _InMemoryWorldGraphLedger,
    _knowledge,
    _mapping,
    _request,
    _revision_for,
    _seed_rfc_graph,
    _service,
)


def _encode(bundle, *, mask=None):
    from trader.application.world_model.graph_features import encode_world_graph_features

    return encode_world_graph_features(bundle, mask=mask)


def test_same_snapshot_yields_the_same_bounded_features_with_per_feature_provenance() -> None:
    mapping = _mapping()
    ledger = _InMemoryWorldGraphLedger()
    _seed_rfc_graph(ledger, mapping)
    bundle = _service(ledger).build(_request(mapping))
    first = _encode(bundle)
    second = _encode(bundle)
    contract = world_v3_feature_contract()
    mask = world_v3_graph_content_mask()
    assert first.snapshot_id == bundle.snapshot.snapshot_id == second.snapshot_id
    assert first.cutoff_at == CUTOFF == second.cutoff_at
    assert first.contract_id == GRAPH_FEATURE_CONTRACT_VERSION == contract.contract_id
    assert first.contract_fingerprint == contract.fingerprint == second.contract_fingerprint
    assert first.mask_id == mask.mask_id
    assert first.mask_fingerprint == mask.fingerprint
    assert first.encoder_identity == WORLD_V3_ENCODER_IDENTITY
    assert first.categorical_features == second.categorical_features
    assert dict(first.numeric_features) == {}
    assert GRAPH_STATUS_CATEGORICAL_FEATURES <= set(first.categorical_features)
    assert GRAPH_CONTENT_CATEGORICAL_FEATURES <= set(first.categorical_features)
    assert all(isinstance(value, str) and 0 < len(value) <= 80 for value in first.categorical_features.values())
    names = {item.feature_name for item in first.provenance}
    assert names == set(first.categorical_features)
    by_name = {item.feature_name: item for item in first.provenance}
    assert by_name["graph_status"].group_id == "graph_status"
    assert by_name["graph_path_signature"].group_id == "graph"
    assert by_name["graph_status"].source_refs == (bundle.snapshot.snapshot_id,)
    assert by_name["graph_path_signature"].source_refs
    assert first.categorical_features["graph_status"] == "complete"
    assert first.categorical_features["graph_scope_status"] == "resolved"
    assert first.categorical_features["graph_coverage_status"] == "complete"
    assert first.categorical_features["graph_missingness_status"] == "none"
    assert first.categorical_features["graph_path_signature"].startswith("sig:")
    payload = first.to_observation_payload()
    assert payload["categorical_features"]["graph_status"] == "complete"
    assert "graph_path_signature" in payload["categorical_features"]


def test_topology_status_only_drops_graph_content_and_stays_stable_when_paths_change() -> None:
    mapping = _mapping()
    ledger = _InMemoryWorldGraphLedger()
    _seed_rfc_graph(ledger, mapping)
    full = _service(ledger).build(_request(mapping))
    truncated = _service(ledger).build(_request(mapping, max_paths=1))
    status_mask = world_v3_topology_status_only_mask()
    content_mask = world_v3_graph_content_mask()
    status_full = _encode(full, mask=status_mask)
    status_again = _encode(full, mask=status_mask)
    content_full = _encode(full, mask=content_mask)
    content_truncated = _encode(truncated, mask=content_mask)
    assert GRAPH_STATUS_CATEGORICAL_FEATURES <= set(status_full.categorical_features)
    assert set(status_full.categorical_features).isdisjoint(GRAPH_CONTENT_CATEGORICAL_FEATURES)
    assert "graph_path_signature" not in status_full.categorical_features
    assert status_full.mask_id == "topology_status_only.v1"
    assert content_full.mask_id == "graph_content.v1"
    assert status_full.categorical_features == status_again.categorical_features
    assert (
        content_full.categorical_features["graph_path_signature"]
        != content_truncated.categorical_features["graph_path_signature"]
    )
    assert {item.feature_name for item in status_full.provenance} == set(status_full.categorical_features)
    assert "graph_path_signature" not in {item.feature_name for item in status_full.provenance}


def test_late_data_and_unmapped_missingness_are_encoded_without_networkx() -> None:
    mapping = _mapping()
    late_ledger = _InMemoryWorldGraphLedger()
    _seed_rfc_graph(late_ledger, mapping, knowledge=())
    late_ledger.now = LATER
    WorldOntologyService(late_ledger).assert_knowledge_relation(AssertKnowledgeWorldRelation(relation=_knowledge()))
    late_features = _encode(_service(late_ledger).build(_request(mapping)))
    assert late_features.categorical_features["graph_window_0_4h_count_bucket"] == "b0"
    assert late_features.categorical_features["graph_macro_agreement_status"] in {"none", "missing"}

    empty = _mapping(entries=())
    empty_ledger = _InMemoryWorldGraphLedger()
    WorldOntologyService(empty_ledger).publish_revision(
        PublishWorldOntologyRevision(revision=_revision_for(empty, entities=(), structural=(), identity_links=()))
    )
    unmapped = WorldScopeResolution(
        mapping_id=empty.mapping_id,
        mapping_sha256=empty.content_sha256,
        anchor=WorldMarketAnchorRef(market_venue="TW", instrument="2330"),
        status="unmapped",
    )
    missing = _encode(_service(empty_ledger).build(_request(empty, root_entity=None, scope_resolution=unmapped)))
    assert missing.categorical_features["graph_status"] == "missing"
    assert missing.categorical_features["graph_scope_status"] == "unmapped"
    assert missing.categorical_features["graph_missingness_status"] == "unmapped"
    assert missing.categorical_features["graph_path_count_bucket"] == "b0"
    assert missing.categorical_features["graph_macro_agreement_status"] == "missing"
    assert all(item.source_refs for item in missing.provenance)


def test_window_counts_and_macro_agreement_use_only_snapshot_members() -> None:
    mapping = _mapping()
    ledger = _InMemoryWorldGraphLedger()
    extra = _knowledge(
        source={"node_kind": "world_observation", "observation_id": "world_observation:v1:" + "e" * 64},
        source_refs=("macro_world_observation:v1:" + "e" * 64, "producer:world_macro_source.v1"),
    )
    _seed_rfc_graph(ledger, mapping, knowledge=(_knowledge(), extra))
    bundle = _service(ledger).build(_request(mapping))
    features = _encode(bundle)
    assert features.categorical_features["graph_macro_agreement_status"] == "agree"
    assert features.categorical_features["graph_window_0_4h_count_bucket"] in {"b1", "b2", "b3", "b4"}
    member_knowledge_ids = {item.relation_id for item in bundle.knowledge_relations}
    snapshot_knowledge_ids = {ref.relation_id for ref in bundle.snapshot.knowledge_relation_refs}
    assert member_knowledge_ids == snapshot_knowledge_ids
    provenance = {item.feature_name: item for item in features.provenance}
    assert set(provenance["graph_window_0_4h_count_bucket"].source_refs) <= member_knowledge_ids | set(
        bundle.snapshot.artifact_refs
    )


def test_feature_encoder_rejects_incompatible_contract_or_mask() -> None:
    from trader.application.world_model.graph_features import encode_world_graph_features

    mapping = _mapping()
    ledger = _InMemoryWorldGraphLedger()
    _seed_rfc_graph(ledger, mapping)
    bundle = _service(ledger).build(_request(mapping))
    v1 = world_v1_feature_contract()
    with pytest.raises(ValueError, match="contract|mask"):
        encode_world_graph_features(
            bundle,
            contract=v1,
            mask=WorldFeatureMask.bind(v1, mask_id="market.v1", selected_groups=("market",)),
        )
