from __future__ import annotations

import ast
from datetime import datetime, timezone
from pathlib import Path

from tests.package_layout._helpers import REPO_ROOT
from trader.application.world_model.graph_ports import WorldOntologyReadinessPort
from trader.application.world_model.graph_snapshot import expected_scope_heads
from trader.application.world_model.ontology_bootstrap import (
    MARKET_ONTOLOGY_REVISION_ID,
    WorldOntologyBootstrapService,
    derive_market_ontology,
)
from trader.application.world_model.ontology_service import WorldOntologyService
from trader.application.world_model.world_scope_resolver import WorldScopeResolver
from trader.domain.world_graph import WorldEntityRef, WorldStructuralRelationRef
from trader.infrastructure.state_db.world_graph_store import WorldGraphStore


UTC = timezone.utc
CUTOFF = datetime(2026, 8, 23, 13, 0, tzinfo=UTC)
CONFIG_DIR = REPO_ROOT / "config"
_BOOTSTRAP = REPO_ROOT / "trader" / "application" / "world_model" / "ontology_bootstrap.py"
_FORBIDDEN_IMPORT_PREFIXES = (
    "networkx",
    "trader.infrastructure",
    "trader.runtime",
    "trader.reporting",
)


def _import_violations(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    rel_path = path.relative_to(REPO_ROOT)
    violations: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            if any(node.module == prefix or node.module.startswith(f"{prefix}.") for prefix in _FORBIDDEN_IMPORT_PREFIXES):
                violations.append(f"{rel_path}: from {node.module} import ...")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if any(alias.name == prefix or alias.name.startswith(f"{prefix}.") for prefix in _FORBIDDEN_IMPORT_PREFIXES):
                    violations.append(f"{rel_path}: import {alias.name}")
    source = path.read_text(encoding="utf-8")
    if "world_graph_store" in source:
        violations.append(f"{rel_path}: world_graph_store mentioned")
    return violations


def test_ontology_bootstrap_is_application_owned_without_infrastructure() -> None:
    assert _BOOTSTRAP.exists()
    assert _import_violations(_BOOTSTRAP) == []
    assert callable(WorldOntologyBootstrapService.readiness)
    assert callable(WorldOntologyBootstrapService.ensure_published)
    assert WorldOntologyReadinessPort.__name__ == "WorldOntologyReadinessPort"


def test_derive_market_ontology_uses_exact_mapping_heads_without_issuer_or_suffix_fallback() -> None:
    mapping = WorldScopeResolver.load(CONFIG_DIR).mapping
    entities, relations, revision = derive_market_ontology(mapping)
    kinds = {entity.kind for entity in entities}
    assert kinds == {"instrument", "venue", "country", "region", "world"}
    assert all(entity.kind != "company" for entity in entities)
    assert {relation.kind for relation in relations} <= {"TRADED_ON", "LOCATED_IN", "PART_OF_WORLD"}
    assert all("CAUS" not in relation.kind for relation in relations)
    actual = frozenset((item.kind, item.source.node_id, item.target.node_id) for item in relations)
    assert actual == expected_scope_heads(mapping)
    instrument_ids = {entity.entity_id for entity in entities if entity.kind == "instrument"}
    assert any(item.endswith(":symbol:2301.TW") for item in instrument_ids)
    assert not any(item.endswith(":symbol:2301") for item in instrument_ids)
    assert revision.revision_id == MARKET_ONTOLOGY_REVISION_ID
    assert revision.scope_mapping_id == mapping.mapping_id
    assert revision.scope_mapping_hash == mapping.content_sha256
    assert revision.identity_link_refs == ()
    second = derive_market_ontology(mapping)[2]
    assert second.content_sha256 == revision.content_sha256


def test_committed_market_ontology_bootstrap_is_deterministic_and_idempotent_on_restart(tmp_path: Path) -> None:
    mapping = WorldScopeResolver.load(CONFIG_DIR).mapping
    path = tmp_path / "world_model.db"

    def clock() -> datetime:
        return CUTOFF

    first = WorldGraphStore(path, clock=clock)
    try:
        service = WorldOntologyBootstrapService(first, mapping)
        assert service.readiness(CUTOFF).status == "unpublished"
        ready = service.ensure_published(now=CUTOFF)
        assert ready.status == "ready"
        assert ready.revision_id == MARKET_ONTOLOGY_REVISION_ID
        entity_events = first.list_entity_events_available_through(CUTOFF)
        relation_events = first.list_structural_relation_events_available_through(CUTOFF)
        revision_events = first.list_revision_events_available_through(CUTOFF)
        kinds = {envelope.event.entity.kind for envelope in entity_events}
        assert kinds <= {"instrument", "venue", "country", "region", "world"}
        assert "company" not in kinds
        relation_kinds = {
            getattr(envelope.event, "relation").kind
            for envelope in relation_events
            if getattr(envelope.event, "relation", None) is not None
        }
        assert relation_kinds <= {"TRADED_ON", "LOCATED_IN", "PART_OF_WORLD"}
        view = WorldOntologyService(first).ontology.at_cutoff(CUTOFF)
        actual = {(item.kind, item.source.node_id, item.target.node_id) for item in view.structural_relations}
        assert actual == expected_scope_heads(mapping)
        assert view.published_revision is not None
        asserted_refs = {
            WorldStructuralRelationRef.from_relation(item.event.relation)
            for item in relation_events
            if getattr(item.event, "relation", None) is not None
        }
        assert frozenset(view.published_revision.structural_relation_refs) == asserted_refs
        hashes = ready.ontology_hash
    finally:
        first.close()

    second = WorldGraphStore(path, clock=clock)
    try:
        again = WorldOntologyBootstrapService(second, mapping).ensure_published(now=CUTOFF)
        assert again.status == "ready"
        assert again.ontology_hash == hashes
        assert len(second.list_entity_events_available_through(CUTOFF)) == len(entity_events)
        assert len(second.list_structural_relation_events_available_through(CUTOFF)) == len(relation_events)
        assert len(second.list_revision_events_available_through(CUTOFF)) == len(revision_events)
        assert WorldOntologyBootstrapService(second, mapping).readiness(CUTOFF).status == "ready"
    finally:
        second.close()


def test_unmapped_instrument_is_not_invented_by_bootstrap() -> None:
    mapping = WorldScopeResolver.load(CONFIG_DIR).mapping
    entities, _relations, _revision = derive_market_ontology(mapping)
    unknown = WorldEntityRef(kind="instrument", entity_id="mic:XTAI:symbol:9999")
    assert unknown not in entities
    assert all(not entity.entity_id.endswith(":symbol:9999") for entity in entities)
