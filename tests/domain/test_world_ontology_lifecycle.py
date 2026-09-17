from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from tests.package_layout._helpers import REPO_ROOT, _domain_import_violations
from trader.domain.world_feature_contract import MARKET_ONTOLOGY_REVISION, WORLD_SCOPE_MAPPING_ID
from trader.domain.world_graph import WorldEntityRef, WorldOntologyRevision
from trader.domain.world_ontology_lifecycle import (
    market_ontology_extended_revision_id,
    require_extended_ontology_revision,
    ONTOLOGY_PUBLICATION_ACTIONS,
    WorldOntologyLifecycleSpec,
    admits_market_ontology_family,
    committed_world_ontology_lifecycle_spec,
    is_market_ontology_revision_instance,
    market_ontology_revision_id,
    market_ontology_revision_id_for_mapping_hash,
    plan_world_ontology_publication,
    require_mapping_aligned_ontology_revision,
)
from trader.domain.world_scope import (
    WorldCanonicalScopeRef,
    WorldMarketAnchorRef,
    WorldScopeMapping,
    WorldScopeMappingEntry,
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


def _mapping() -> WorldScopeMapping:
    return WorldScopeMapping(
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


def test_lifecycle_module_is_stdlib_domain() -> None:
    assert MODULE_PATH.exists()
    assert _domain_import_violations([MODULE_PATH], REPO_ROOT) == []
    source = MODULE_PATH.read_text(encoding="utf-8")
    assert "trader.runtime" not in source
    assert "trader.infrastructure" not in source
    assert "backfill" not in source
    assert "open(" not in source
    assert "Path(" not in source
    assert "generation_pending" not in source


def test_revision_instance_id_is_derived_from_mapping_hash() -> None:
    mapping = _mapping()
    revision_id = market_ontology_revision_id(mapping)
    assert revision_id == market_ontology_revision_id_for_mapping_hash(mapping.content_sha256)
    assert revision_id.startswith("market_ontology:v1:")
    assert revision_id.endswith(mapping.content_sha256)
    assert admits_market_ontology_family(revision_id)
    assert admits_market_ontology_family(MARKET_ONTOLOGY_REVISION)
    assert not admits_market_ontology_family("market_ontology.v0")
    with pytest.raises(TypeError, match="derived from mapping"):
        committed_world_ontology_lifecycle_spec()


def test_empty_store_publishes_current_revision() -> None:
    mapping = _mapping()
    expected = _revision(
        revision_id=market_ontology_revision_id(mapping),
        mapping_id=WORLD_SCOPE_MAPPING_ID,
        mapping_hash=mapping.content_sha256,
    )
    spec = _spec_for(expected)
    plan = plan_world_ontology_publication(published=None, expected=expected, spec=spec)
    assert plan.action == "publish"
    assert plan.published_revision_id is None
    assert plan.expected_revision_id == spec.revision_id
    assert ONTOLOGY_PUBLICATION_ACTIONS == frozenset({"ready", "publish", "supersede"})


def test_matching_current_revision_is_ready() -> None:
    mapping = _mapping()
    expected = _revision(
        revision_id=market_ontology_revision_id(mapping),
        mapping_id=WORLD_SCOPE_MAPPING_ID,
        mapping_hash=mapping.content_sha256,
    )
    spec = _spec_for(expected)
    ready = plan_world_ontology_publication(published=expected, expected=expected, spec=spec)
    assert ready.action == "ready"
    unknown = _revision(revision_id="market_ontology.v0", mapping_id="world_scope_mapping.v0")
    with pytest.raises(ValueError, match="current committed identity"):
        plan_world_ontology_publication(published=unknown, expected=expected, spec=spec)


def test_legacy_family_instance_is_superseded_by_derived_generation() -> None:
    mapping = _mapping()
    expected = _revision(
        revision_id=market_ontology_revision_id(mapping),
        mapping_id=WORLD_SCOPE_MAPPING_ID,
        mapping_hash=mapping.content_sha256,
    )
    published = _revision(
        revision_id=MARKET_ONTOLOGY_REVISION,
        mapping_id=WORLD_SCOPE_MAPPING_ID,
        mapping_hash="a" * 64,
    )
    plan = plan_world_ontology_publication(published=published, expected=expected, spec=_spec_for(expected))
    assert plan.action == "supersede"
    assert plan.published_revision_id == MARKET_ONTOLOGY_REVISION
    assert plan.expected_revision_id == expected.revision_id
    assert plan.published_revision_id != plan.expected_revision_id


def test_new_mapping_hash_supersedes_prior_derived_generation() -> None:
    first = _revision(
        revision_id=market_ontology_revision_id_for_mapping_hash("a" * 64),
        mapping_id=WORLD_SCOPE_MAPPING_ID,
        mapping_hash="a" * 64,
    )
    second = _revision(
        revision_id=market_ontology_revision_id_for_mapping_hash("b" * 64),
        mapping_id=WORLD_SCOPE_MAPPING_ID,
        mapping_hash="b" * 64,
    )
    plan = plan_world_ontology_publication(published=first, expected=second, spec=_spec_for(second))
    assert plan.action == "supersede"
    assert plan.published_revision_id == first.revision_id
    assert plan.expected_revision_id == second.revision_id


def test_same_instance_id_hash_drift_is_still_a_conflict() -> None:
    mapping_hash = "b" * 64
    expected = _revision(
        revision_id=market_ontology_revision_id_for_mapping_hash(mapping_hash),
        mapping_id=WORLD_SCOPE_MAPPING_ID,
        mapping_hash=mapping_hash,
    )
    published = _revision(
        revision_id=expected.revision_id,
        mapping_id=WORLD_SCOPE_MAPPING_ID,
        mapping_hash="a" * 64,
    )
    with pytest.raises(ValueError, match="conflict"):
        plan_world_ontology_publication(published=published, expected=expected, spec=_spec_for(expected))


def test_require_aligned_revision_rejects_family_id_and_hash_mismatch() -> None:
    mapping = _mapping()
    family = _revision(
        revision_id=MARKET_ONTOLOGY_REVISION,
        mapping_id=WORLD_SCOPE_MAPPING_ID,
        mapping_hash=mapping.content_sha256,
    )
    with pytest.raises(ValueError, match="mapping generation"):
        require_mapping_aligned_ontology_revision(family, mapping)
    derived = _revision(
        revision_id=market_ontology_revision_id(mapping),
        mapping_id=WORLD_SCOPE_MAPPING_ID,
        mapping_hash=mapping.content_sha256,
    )
    assert require_mapping_aligned_ontology_revision(derived, mapping) is derived
    with pytest.raises(FrozenInstanceError):
        derived.revision_id = "other"  # type: ignore[misc]


def test_extended_revision_id_commits_to_all_three_inputs() -> None:
    mapping = _mapping()
    first = market_ontology_extended_revision_id(mapping, registry_sha256="b" * 64, catalog_sha256="c" * 64)
    assert first.startswith("market_ontology:v1:")
    assert first == market_ontology_extended_revision_id(
        mapping, registry_sha256="b" * 64, catalog_sha256="c" * 64
    )
    assert first != market_ontology_revision_id(mapping)
    assert first != market_ontology_extended_revision_id(
        mapping, registry_sha256="d" * 64, catalog_sha256="c" * 64
    )
    assert first != market_ontology_extended_revision_id(
        mapping, registry_sha256="b" * 64, catalog_sha256="d" * 64
    )
    with pytest.raises(ValueError, match="sha256"):
        market_ontology_extended_revision_id(mapping, registry_sha256="zz", catalog_sha256="c" * 64)


def test_require_extended_revision_rejects_drift() -> None:
    mapping = _mapping()
    revision_id = market_ontology_extended_revision_id(
        mapping, registry_sha256="b" * 64, catalog_sha256="c" * 64
    )
    derived = _revision(
        revision_id=revision_id, mapping_id=WORLD_SCOPE_MAPPING_ID, mapping_hash=mapping.content_sha256
    )
    assert (
        require_extended_ontology_revision(
            derived, mapping, registry_sha256="b" * 64, catalog_sha256="c" * 64
        )
        is derived
    )
    with pytest.raises(ValueError, match="drifted from extended inputs"):
        require_extended_ontology_revision(
            derived, mapping, registry_sha256="d" * 64, catalog_sha256="c" * 64
        )
    legacy = _revision(
        revision_id=market_ontology_revision_id(mapping),
        mapping_id=WORLD_SCOPE_MAPPING_ID,
        mapping_hash=mapping.content_sha256,
    )
    with pytest.raises(ValueError, match="drifted from extended inputs"):
        require_extended_ontology_revision(
            legacy, mapping, registry_sha256="b" * 64, catalog_sha256="c" * 64
        )


def test_revision_instance_predicate_rejects_family_tag_and_malformed_ids() -> None:
    assert is_market_ontology_revision_instance(f"market_ontology:v1:{'ab' * 32}") is True
    assert is_market_ontology_revision_instance(MARKET_ONTOLOGY_REVISION) is False
    assert is_market_ontology_revision_instance("market_ontology:v1:abc") is False
    assert is_market_ontology_revision_instance("market_ontology:v1:" + "zz" * 32) is False
    with pytest.raises((TypeError, ValueError)):
        is_market_ontology_revision_instance("  ")
