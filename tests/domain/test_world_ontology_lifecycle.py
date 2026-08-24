from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from tests.package_layout._helpers import REPO_ROOT, _domain_import_violations
from trader.domain.world_feature_contract import (
    WORLD_GRAPH_V3_ONTOLOGY_PREDECESSOR_REVISION,
    WORLD_GRAPH_V3_ONTOLOGY_REVISION,
    WORLD_SCOPE_MAPPING_ID,
    WORLD_SCOPE_MAPPING_PREDECESSOR_ID,
)
from trader.domain.world_graph import WorldEntityRef, WorldOntologyRevision
from trader.domain.world_ontology_lifecycle import (
    ONTOLOGY_PUBLICATION_ACTIONS,
    WorldOntologyLifecycleSpec,
    committed_world_ontology_lifecycle_spec,
    plan_world_ontology_publication,
)


MODULE_PATH = REPO_ROOT / "trader" / "domain" / "world_ontology_lifecycle.py"


def _revision(*, revision_id: str, mapping_id: str, mapping_hash: str = "a" * 64) -> WorldOntologyRevision:
    return WorldOntologyRevision(
        revision_id=revision_id,
        entities=(WorldEntityRef(kind="world", entity_id="market"),),
        structural_relation_refs=(),
        identity_link_refs=(),
        scope_mapping_id=mapping_id,
        scope_mapping_hash=mapping_hash,
    )


def test_lifecycle_module_is_stdlib_domain() -> None:
    assert MODULE_PATH.exists()
    assert _domain_import_violations([MODULE_PATH], REPO_ROOT) == []
    source = MODULE_PATH.read_text(encoding="utf-8")
    assert "trader.runtime" not in source
    assert "trader.infrastructure" not in source
    assert "backfill" not in source


def test_committed_spec_is_a_forward_successor_pair() -> None:
    spec = committed_world_ontology_lifecycle_spec()
    assert spec.successor_revision_id == WORLD_GRAPH_V3_ONTOLOGY_REVISION == "market_ontology.v2"
    assert spec.predecessor_revision_id == WORLD_GRAPH_V3_ONTOLOGY_PREDECESSOR_REVISION == "market_ontology.v1"
    assert spec.successor_mapping_id == WORLD_SCOPE_MAPPING_ID == "world_scope_mapping.v2"
    assert spec.predecessor_mapping_id == WORLD_SCOPE_MAPPING_PREDECESSOR_ID == "world_scope_mapping.v1"
    assert spec.successor_revision_id != spec.predecessor_revision_id
    assert spec.successor_mapping_id != spec.predecessor_mapping_id
    with pytest.raises(ValueError, match="differ"):
        WorldOntologyLifecycleSpec(
            successor_revision_id="market_ontology.v1",
            predecessor_revision_id="market_ontology.v1",
            successor_mapping_id="world_scope_mapping.v2",
            predecessor_mapping_id="world_scope_mapping.v1",
        )
    with pytest.raises(FrozenInstanceError):
        spec.successor_revision_id = "other"  # type: ignore[misc]


def test_empty_store_publishes_successor_without_requiring_predecessor() -> None:
    spec = committed_world_ontology_lifecycle_spec()
    expected = _revision(revision_id=spec.successor_revision_id, mapping_id=spec.successor_mapping_id)
    plan = plan_world_ontology_publication(published=None, expected=expected, spec=spec)
    assert plan.action == "publish"
    assert plan.published_revision_id is None
    assert plan.expected_revision_id == spec.successor_revision_id
    assert "publish" in ONTOLOGY_PUBLICATION_ACTIONS


def test_persisted_predecessor_plans_deterministic_supersession() -> None:
    spec = committed_world_ontology_lifecycle_spec()
    published = _revision(revision_id=spec.predecessor_revision_id, mapping_id=spec.predecessor_mapping_id)
    expected = _revision(revision_id=spec.successor_revision_id, mapping_id=spec.successor_mapping_id, mapping_hash="b" * 64)
    plan = plan_world_ontology_publication(published=published, expected=expected, spec=spec)
    assert plan.action == "supersede_and_publish"
    assert plan.published_revision_id == spec.predecessor_revision_id
    assert plan.to_dict()["action"] == "supersede_and_publish"


def test_matching_successor_is_ready_and_same_id_hash_drift_conflicts() -> None:
    spec = committed_world_ontology_lifecycle_spec()
    expected = _revision(revision_id=spec.successor_revision_id, mapping_id=spec.successor_mapping_id)
    ready = plan_world_ontology_publication(published=expected, expected=expected, spec=spec)
    assert ready.action == "ready"
    drifted = _revision(
        revision_id=spec.successor_revision_id,
        mapping_id=spec.successor_mapping_id,
        mapping_hash="c" * 64,
    )
    with pytest.raises(ValueError, match="conflict"):
        plan_world_ontology_publication(published=drifted, expected=expected, spec=spec)
    unknown = _revision(revision_id="market_ontology.v0", mapping_id="world_scope_mapping.v0")
    with pytest.raises(ValueError, match="lineage"):
        plan_world_ontology_publication(published=unknown, expected=expected, spec=spec)
    rollback = _revision(revision_id=spec.predecessor_revision_id, mapping_id=spec.predecessor_mapping_id)
    with pytest.raises(ValueError, match="successor"):
        plan_world_ontology_publication(published=expected, expected=rollback, spec=spec)
