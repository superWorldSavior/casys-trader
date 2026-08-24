from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from tests.package_layout._helpers import REPO_ROOT, _domain_import_violations
from trader.domain.world_feature_contract import (
    WORLD_GRAPH_V3_ONTOLOGY_REVISION,
    WORLD_GRAPH_V3_ONTOLOGY_SHA256,
    WORLD_SCOPE_MAPPING_ID,
    WORLD_SCOPE_MAPPING_SHA256,
)
from trader.domain.world_graph import WorldEntityRef, WorldOntologyRevision
from trader.domain.world_ontology_lifecycle import (
    ONTOLOGY_PUBLICATION_ACTIONS,
    WorldOntologyLifecycleSpec,
    committed_world_ontology_lifecycle_spec,
    plan_world_ontology_publication,
    require_committed_ontology_revision,
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


def _spec_for(expected: WorldOntologyRevision) -> WorldOntologyLifecycleSpec:
    return WorldOntologyLifecycleSpec(
        revision_id=expected.revision_id,
        mapping_id=expected.scope_mapping_id,
        revision_hash=expected.content_sha256,
        mapping_sha256=expected.scope_mapping_hash,
    )


def test_lifecycle_module_is_stdlib_domain() -> None:
    assert MODULE_PATH.exists()
    assert _domain_import_violations([MODULE_PATH], REPO_ROOT) == []
    source = MODULE_PATH.read_text(encoding="utf-8")
    assert "trader.runtime" not in source
    assert "trader.infrastructure" not in source
    assert "backfill" not in source
    assert "supersede" not in source
    assert "predecessor" not in source


def test_committed_spec_is_the_current_identity() -> None:
    spec = committed_world_ontology_lifecycle_spec()
    assert spec.revision_id == WORLD_GRAPH_V3_ONTOLOGY_REVISION == "market_ontology.v2"
    assert spec.mapping_id == WORLD_SCOPE_MAPPING_ID == "world_scope_mapping.v2"
    assert spec.revision_hash == WORLD_GRAPH_V3_ONTOLOGY_SHA256
    assert spec.mapping_sha256 == WORLD_SCOPE_MAPPING_SHA256
    payload = spec.to_dict()
    assert payload == {
        "revision_id": WORLD_GRAPH_V3_ONTOLOGY_REVISION,
        "mapping_id": WORLD_SCOPE_MAPPING_ID,
        "revision_hash": WORLD_GRAPH_V3_ONTOLOGY_SHA256,
        "mapping_sha256": WORLD_SCOPE_MAPPING_SHA256,
    }
    assert WorldOntologyLifecycleSpec.from_mapping(payload) == spec
    with pytest.raises(FrozenInstanceError):
        spec.revision_id = "other"  # type: ignore[misc]


def test_empty_store_publishes_current_revision() -> None:
    expected = _revision(
        revision_id=WORLD_GRAPH_V3_ONTOLOGY_REVISION,
        mapping_id=WORLD_SCOPE_MAPPING_ID,
        mapping_hash=WORLD_SCOPE_MAPPING_SHA256,
    )
    spec = _spec_for(expected)
    plan = plan_world_ontology_publication(published=None, expected=expected, spec=spec)
    assert plan.action == "publish"
    assert plan.published_revision_id is None
    assert plan.expected_revision_id == spec.revision_id
    assert ONTOLOGY_PUBLICATION_ACTIONS == frozenset({"ready", "publish"})


def test_matching_current_revision_is_ready_and_other_identity_conflicts() -> None:
    expected = _revision(
        revision_id=WORLD_GRAPH_V3_ONTOLOGY_REVISION,
        mapping_id=WORLD_SCOPE_MAPPING_ID,
        mapping_hash=WORLD_SCOPE_MAPPING_SHA256,
    )
    spec = _spec_for(expected)
    ready = plan_world_ontology_publication(published=expected, expected=expected, spec=spec)
    assert ready.action == "ready"
    drifted = _revision(
        revision_id=spec.revision_id,
        mapping_id=spec.mapping_id,
        mapping_hash="c" * 64,
    )
    with pytest.raises(ValueError, match="conflict"):
        plan_world_ontology_publication(published=drifted, expected=expected, spec=spec)
    unknown = _revision(revision_id="market_ontology.v0", mapping_id="world_scope_mapping.v0")
    with pytest.raises(ValueError, match="current committed identity"):
        plan_world_ontology_publication(published=unknown, expected=expected, spec=spec)


def test_committed_spec_rejects_expected_hash_drift_on_empty_store() -> None:
    spec = committed_world_ontology_lifecycle_spec()
    expected = _revision(
        revision_id=spec.revision_id,
        mapping_id=spec.mapping_id,
        mapping_hash=spec.mapping_sha256,
    )
    with pytest.raises(ValueError, match="ontology hash drifted"):
        plan_world_ontology_publication(published=None, expected=expected, spec=spec)
    custom = _spec_for(expected)
    plan = plan_world_ontology_publication(published=None, expected=expected, spec=custom)
    assert plan.action == "publish"


def test_require_committed_ontology_revision_rejects_mapping_id_with_drifted_hash() -> None:
    from trader.domain.world_scope import (
        WorldCanonicalScopeRef,
        WorldMarketAnchorRef,
        WorldScopeMapping,
        WorldScopeMappingEntry,
    )

    mapping = WorldScopeMapping(
        mapping_id=WORLD_SCOPE_MAPPING_ID,
        entries=(
            WorldScopeMappingEntry(
                anchor=WorldMarketAnchorRef(market_venue="US", instrument="GM"),
                venue=WorldCanonicalScopeRef(kind="venue", entity_id="mic:XNYS"),
                country=WorldCanonicalScopeRef(kind="country", entity_id="iso-3166:US"),
                region=WorldCanonicalScopeRef(kind="region", entity_id="iso-un-m49:021"),
                world=WorldCanonicalScopeRef(kind="world", entity_id="market"),
                provider_proofs=("provider:gm",),
                taxonomy_version="sessions_mic.v1",
            ),
        ),
    )
    revision = _revision(
        revision_id=WORLD_GRAPH_V3_ONTOLOGY_REVISION,
        mapping_id=WORLD_SCOPE_MAPPING_ID,
        mapping_hash=mapping.content_sha256,
    )
    with pytest.raises(ValueError, match="mapping hash drifted"):
        require_committed_ontology_revision(revision, mapping)
