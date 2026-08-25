from __future__ import annotations

import ast
import json
from datetime import datetime, timezone
from pathlib import Path

from tests.package_layout._helpers import REPO_ROOT
from trader.application.world_model.graph_explorer_ports import (
    WORLD_GRAPH_EXPLORER_SCHEMA,
    WORLD_GRAPH_EXPLORER_VIEW,
    WorldGraphExplorerEventRecord,
    WorldGraphExplorerQuerySnapshot,
)
from trader.domain.world_availability import (
    WorldAvailabilityReceipt,
    WorldAvailabilitySubjectRef,
    WorldStorageLocator,
    _attest_verified_store_receipt,
    _iso,
    _receipt_hash_payload,
    _receipt_id_for,
    _receipt_identity_payload,
)
from trader.domain.world_context import EntityRef
from trader.domain.world_episode import canonical_sha256
from trader.domain.world_graph import (
    FORBIDDEN_RELATION_KINDS,
    KnowledgeWorldRelation,
    KnowledgeWorldRelationAsserted,
    StructuralWorldRelation,
    StructuralWorldRelationAsserted,
    WorldEntityAsserted,
    WorldEntityIdentityLink,
    WorldEntityIdentityLinked,
    WorldEntityRef,
    WorldObservationRef,
    WorldOntologyRevision,
    WorldOntologyRevisionPublished,
    WorldStructuralRelationRef,
)
from trader.domain.world_macro import MACRO_PRODUCER_VERSION, macro_observes_producer_ref


UTC = timezone.utc
T0 = datetime(2026, 1, 1, tzinfo=UTC)
NOW = datetime(2026, 8, 25, 6, 0, tzinfo=UTC)
READY = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)
SHA = "a" * 64
SCOPE_HASH = "b" * 64
OTHER_REVISION = "market_ontology.v1:other"

_EXPLORER = REPO_ROOT / "trader" / "application" / "world_model" / "graph_explorer.py"
_PORTS = REPO_ROOT / "trader" / "application" / "world_model" / "graph_explorer_ports.py"
_FORBIDDEN_IMPORT_PREFIXES = (
    "networkx",
    "sqlite3",
    "trader.infrastructure",
    "trader.runtime",
    "trader.reporting",
)


def _entity(*, kind: str = "instrument", entity_id: str = "mic:XTAI:symbol:2330") -> WorldEntityRef:
    return WorldEntityRef(kind=kind, entity_id=entity_id)


def _venue() -> WorldEntityRef:
    return WorldEntityRef(kind="venue", entity_id="mic:XTAI")


def _country() -> WorldEntityRef:
    return WorldEntityRef(kind="country", entity_id="iso-3166:TW")


def _family() -> WorldEntityRef:
    return WorldEntityRef(kind="family", entity_id="taxonomy:v1:semiconductors")


def _company() -> WorldEntityRef:
    return WorldEntityRef(kind="company", entity_id="lei:549300ABCDEFGHIJKLMN")


def _structural(**overrides: object) -> StructuralWorldRelation:
    values: dict[str, object] = {
        "kind": "TRADED_ON",
        "source": _entity(),
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
        "source": WorldObservationRef(observation_id=f"world_observation:v1:{SHA}"),
        "target": _country(),
        "effective_from": T0,
        "ontology_revision": "market_ontology.v1",
        "source_refs": (
            "macro_world_observation:v1:" + SHA,
            macro_observes_producer_ref(MACRO_PRODUCER_VERSION),
        ),
    }
    values.update(overrides)
    return KnowledgeWorldRelation(**values)  # type: ignore[arg-type]


def _link() -> WorldEntityIdentityLink:
    return WorldEntityIdentityLink(
        context_ref=EntityRef(kind="instrument", entity_id="2330"),
        graph_ref=_entity(),
        source_refs=("provider:instrument-master:2330",),
        effective_from=T0,
    )


def _revision(*, structural: StructuralWorldRelation, extra_refs: tuple[WorldStructuralRelationRef, ...] = ()) -> WorldOntologyRevision:
    return WorldOntologyRevision(
        revision_id="market_ontology.v1",
        entities=(_entity(), _venue(), _country()),
        structural_relation_refs=(WorldStructuralRelationRef.from_relation(structural), *extra_refs),
        identity_link_refs=(_link().as_ref(),),
        scope_mapping_id="world_scope_mapping.v1",
        scope_mapping_hash=SCOPE_HASH,
    )


def _attested_receipt(*, kind: str, subject_id: str, content_sha256: str, table: str) -> WorldAvailabilityReceipt:
    subject = WorldAvailabilitySubjectRef(kind=kind, subject_id=subject_id, content_sha256=content_sha256)
    locator = WorldStorageLocator(kind="sqlite", store_id="world-model.db.v1", table=table, row_id=subject_id)
    identity = _receipt_identity_payload(
        schema_version="world_availability_receipt.v1",
        subject=subject,
        scope=f"world_graph:{kind}",
        storage_locator=locator,
    )
    receipt_id = _receipt_id_for(identity)
    digest = canonical_sha256(_receipt_hash_payload(identity, receipt_id=receipt_id, ready_at=READY))
    receipt = WorldAvailabilityReceipt.from_mapping(
        {
            **identity,
            "receipt_id": receipt_id,
            "ready_at": _iso(READY),
            "receipt_sha256": digest,
        }
    )
    return _attest_verified_store_receipt(
        receipt,
        expected_subject=receipt.subject,
        expected_scope=receipt.scope,
        expected_locator=receipt.storage_locator,
    )


def _record(event: object, *, kind: str, table: str, family: str | None = None) -> WorldGraphExplorerEventRecord:
    payload = event.to_dict()  # type: ignore[union-attr]
    digest = canonical_sha256(payload)
    receipt = _attested_receipt(kind=kind, subject_id=event.event_id, content_sha256=digest, table=table)  # type: ignore[union-attr]
    return WorldGraphExplorerEventRecord(
        subject_kind=kind,
        table=table,
        family=family,
        payload=payload,
        payload_sha256=digest,
        receipt_payload=receipt.to_dict(),
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
    if "WorldGraphStore" in source or "world_graph_store" in source:
        violations.append(f"{rel_path}: WorldGraphStore mentioned")
    return violations


class _MemoryQuery:
    def __init__(self, snapshot: WorldGraphExplorerQuerySnapshot) -> None:
        self.snapshot = snapshot

    def load_current_records(self) -> WorldGraphExplorerQuerySnapshot:
        return self.snapshot


def _service(records: tuple[WorldGraphExplorerEventRecord, ...]):
    from trader.application.world_model.graph_explorer import WorldGraphExplorerService

    snapshot = WorldGraphExplorerQuerySnapshot(status="ready", exists=True, missing_tables=(), records=records)
    return WorldGraphExplorerService(_MemoryQuery(snapshot), clock=lambda: NOW)


def test_explorer_application_modules_stay_store_free() -> None:
    assert _import_violations(_EXPLORER) == []
    assert _import_violations(_PORTS) == []
    source = _EXPLORER.read_text(encoding="utf-8")
    assert "cutoff_at" in source
    assert "generated_at" in source
    assert "current_published_overview" in source
    assert "CAUSES" in source


def test_missing_database_is_typed_not_started() -> None:
    from trader.application.world_model.graph_explorer import WorldGraphExplorerService

    snapshot = WorldGraphExplorerQuerySnapshot(status="not_started", exists=False, missing_tables=(), records=())
    payload = WorldGraphExplorerService(_MemoryQuery(snapshot), clock=lambda: NOW).current_published_overview()
    assert payload["schema_version"] == WORLD_GRAPH_EXPLORER_SCHEMA == "world_graph_explorer.v1"
    assert payload["status"] == "not_started"
    assert payload["view"] == WORLD_GRAPH_EXPLORER_VIEW
    assert payload["authority"] == "shadow_only"
    assert payload["decision_effect"] == "none"
    assert payload["causal_claim"] is False
    assert payload["generated_at"] == _iso(NOW)
    assert payload["cutoff_at"] == _iso(NOW)
    assert "generated_at" in payload and "cutoff_at" in payload
    assert payload["generated_at"] is not payload["cutoff_at"]
    assert payload["nodes"] == []
    assert payload["edges"] == []


def test_missing_schema_is_typed_unavailable() -> None:
    from trader.application.world_model.graph_explorer import WorldGraphExplorerService

    snapshot = WorldGraphExplorerQuerySnapshot(
        status="unavailable",
        exists=True,
        missing_tables=("world_ontology_revisions",),
        records=(),
        error="JSONDecodeError: payload_json",
    )
    payload = WorldGraphExplorerService(_MemoryQuery(snapshot), clock=lambda: NOW).current_published_overview()
    assert payload["status"] == "unavailable"
    assert payload["status"] not in {"missing", "error", "ready"}
    assert payload["missingness"]["schema"] == "missing_tables"
    assert payload["ontology"] is None
    assert payload["error"] == "JSONDecodeError: payload_json"


def test_missing_revision_is_typed_not_started() -> None:
    entity = WorldEntityAsserted(entity=_entity(), source_refs=("provider:instrument-master:2330",), effective_from=T0)
    payload = _service((_record(entity, kind="world_entity_event", table="world_entity_events"),)).current_published_overview()
    assert payload["status"] == "not_started"
    assert payload["missingness"]["ontology"] == "unpublished"
    assert payload["exists"] is True


def test_current_overview_keeps_revision_bound_heads_parallel_edges_and_provenance() -> None:
    first = _structural()
    second = _structural(source_refs=("provider:instrument-master:2330:alt",))
    other_structural = _structural(
        ontology_revision=OTHER_REVISION,
        source_refs=("provider:instrument-master:other",),
    )
    admitted = _knowledge()
    foreign = _knowledge(
        source=WorldObservationRef(observation_id=f"world_observation:v1:{'c' * 64}"),
        ontology_revision=OTHER_REVISION,
        source_refs=("macro_world_observation:v1:" + "c" * 64, macro_observes_producer_ref(MACRO_PRODUCER_VERSION)),
    )
    rejected_observes = _knowledge(
        source=WorldObservationRef(observation_id=f"world_observation:v1:{'d' * 64}"),
        source_refs=("macro_world_observation:v1:" + "d" * 64,),
    )
    revision = _revision(
        structural=first,
        extra_refs=(WorldStructuralRelationRef.from_relation(second),),
    )
    records = (
        _record(
            WorldEntityAsserted(entity=_entity(), source_refs=("provider:instrument-master:2330",), effective_from=T0),
            kind="world_entity_event",
            table="world_entity_events",
        ),
        _record(
            WorldEntityAsserted(entity=_venue(), source_refs=("provider:venue-master:XTAI",), effective_from=T0),
            kind="world_entity_event",
            table="world_entity_events",
        ),
        _record(
            WorldEntityAsserted(entity=_country(), source_refs=("provider:iso-3166:TW",), effective_from=T0),
            kind="world_entity_event",
            table="world_entity_events",
        ),
        _record(
            WorldEntityIdentityLinked(link=_link()),
            kind="world_entity_identity_event",
            table="world_entity_identity_events",
        ),
        _record(
            StructuralWorldRelationAsserted(relation=first),
            kind="world_relation_event",
            table="world_relation_events",
            family="structural",
        ),
        _record(
            StructuralWorldRelationAsserted(relation=second),
            kind="world_relation_event",
            table="world_relation_events",
            family="structural",
        ),
        _record(
            StructuralWorldRelationAsserted(relation=other_structural),
            kind="world_relation_event",
            table="world_relation_events",
            family="structural",
        ),
        _record(
            KnowledgeWorldRelationAsserted(relation=admitted),
            kind="world_relation_event",
            table="world_relation_events",
            family="knowledge",
        ),
        _record(
            KnowledgeWorldRelationAsserted(relation=foreign),
            kind="world_relation_event",
            table="world_relation_events",
            family="knowledge",
        ),
        _record(
            KnowledgeWorldRelationAsserted(relation=rejected_observes),
            kind="world_relation_event",
            table="world_relation_events",
            family="knowledge",
        ),
        _record(
            WorldOntologyRevisionPublished(revision=revision),
            kind="world_ontology_revision_event",
            table="world_ontology_revisions",
        ),
    )
    payload = _service(records).current_published_overview()
    assert payload["status"] == "loaded"
    assert payload["schema_version"] == "world_graph_explorer.v1"
    assert payload["view"] == "current_published_overview"
    assert payload["authority"] == "shadow_only"
    assert payload["decision_effect"] == "none"
    assert payload["causal_claim"] is False
    assert payload["generated_at"] == _iso(NOW)
    assert payload["cutoff_at"] == _iso(NOW)
    assert "generated_at" in payload and "cutoff_at" in payload
    assert payload["ontology"]["revision_id"] == "market_ontology.v1"
    assert payload["revision"] == payload["ontology"]
    assert payload["ontology"]["content_sha256"] == revision.content_sha256
    assert payload["truncated"] is False
    instrument = next(node for node in payload["nodes"] if node["id"] == _entity().node_id)
    assert instrument["entity_kind"] == "instrument"
    assert instrument["label"] == "2330"
    edge_ids = [edge["relation_id"] for edge in payload["edges"]]
    assert first.relation_id in edge_ids
    assert second.relation_id in edge_ids
    assert other_structural.relation_id not in edge_ids
    assert admitted.relation_id in edge_ids
    assert foreign.relation_id not in edge_ids
    assert rejected_observes.relation_id not in edge_ids
    parallel = [edge for edge in payload["edges"] if edge["family"] == "structural"]
    assert len(parallel) == 2
    assert {edge["source"] for edge in parallel} == {_entity().node_id}
    assert {edge["target"] for edge in parallel} == {_venue().node_id}
    assert all(edge["kind"] not in FORBIDDEN_RELATION_KINDS for edge in payload["edges"])
    assert payload["counts"]["structural_edges"] == 2
    assert payload["counts"]["knowledge_edges"] == 1
    assert payload["provenance"]["source_refs"]
    assert payload["provenance"]["identity_links"][0]["link_id"] == _link().link_id
    assert payload["missingness"]["knowledge_other_revision"] == 1
    assert payload["truncation"]["truncated"] is False
    node_ids = {node["id"] for node in payload["nodes"]}
    assert _entity().node_id in node_ids
    assert f"world_observation:v1:{SHA}" in node_ids


def test_unreadable_revision_events_are_unavailable() -> None:
    payload = _service(
        (
            WorldGraphExplorerEventRecord(
                subject_kind="world_ontology_revision_event",
                table="world_ontology_revisions",
                family=None,
                payload={"event_type": "world_ontology_revision_published"},
                payload_sha256="0" * 64,
                receipt_payload=None,
            ),
        )
    ).current_published_overview()
    assert payload["status"] == "unavailable"
    assert payload["status"] not in {"missing", "error"}
    assert payload["missingness"]["ontology"] == "unreadable"
    assert payload["error"]


def test_truncation_is_declared_without_inventing_edges(monkeypatch) -> None:
    from trader.application.world_model import graph_explorer as explorer_mod

    monkeypatch.setattr(explorer_mod, "WORLD_GRAPH_EXPLORER_NODE_LIMIT", 1)
    monkeypatch.setattr(explorer_mod, "WORLD_GRAPH_EXPLORER_EDGE_LIMIT", 1)
    structural = _structural()
    revision = _revision(structural=structural)
    records = (
        _record(
            WorldEntityAsserted(entity=_entity(), source_refs=("provider:instrument-master:2330",), effective_from=T0),
            kind="world_entity_event",
            table="world_entity_events",
        ),
        _record(
            WorldEntityAsserted(entity=_venue(), source_refs=("provider:venue-master:XTAI",), effective_from=T0),
            kind="world_entity_event",
            table="world_entity_events",
        ),
        _record(
            StructuralWorldRelationAsserted(relation=structural),
            kind="world_relation_event",
            table="world_relation_events",
            family="structural",
        ),
        _record(
            WorldOntologyRevisionPublished(revision=revision),
            kind="world_ontology_revision_event",
            table="world_ontology_revisions",
        ),
    )
    payload = _service(records).current_published_overview()
    assert payload["status"] == "loaded"
    assert payload["truncation"]["truncated"] is True
    assert payload["truncated"] is True
    assert payload["counts"]["nodes"] >= 2
    assert payload["counts"]["returned_nodes"] == 1
    for edge in payload["edges"]:
        assert edge["kind"] not in FORBIDDEN_RELATION_KINDS


def test_family_and_company_heads_project_d3_fields_as_stable_json() -> None:
    traded = _structural()
    membership = _structural(
        kind="MEMBER_OF_FAMILY",
        source=_entity(),
        target=_family(),
        source_refs=("taxonomy:v1:semiconductors",),
    )
    issued = _structural(
        kind="ISSUED_BY",
        source=_entity(),
        target=_company(),
        source_refs=("provider:lei:549300ABCDEFGHIJKLMN",),
    )
    revision = WorldOntologyRevision(
        revision_id="market_ontology.v1",
        entities=(_entity(), _venue(), _country(), _family(), _company()),
        structural_relation_refs=(
            WorldStructuralRelationRef.from_relation(traded),
            WorldStructuralRelationRef.from_relation(membership),
            WorldStructuralRelationRef.from_relation(issued),
        ),
        identity_link_refs=(_link().as_ref(),),
        scope_mapping_id="world_scope_mapping.v1",
        scope_mapping_hash=SCOPE_HASH,
    )
    records = (
        _record(
            WorldEntityAsserted(entity=_entity(), source_refs=("provider:instrument-master:2330",), effective_from=T0),
            kind="world_entity_event",
            table="world_entity_events",
        ),
        _record(
            WorldEntityAsserted(entity=_venue(), source_refs=("provider:venue-master:XTAI",), effective_from=T0),
            kind="world_entity_event",
            table="world_entity_events",
        ),
        _record(
            WorldEntityAsserted(entity=_country(), source_refs=("provider:iso-3166:TW",), effective_from=T0),
            kind="world_entity_event",
            table="world_entity_events",
        ),
        _record(
            WorldEntityAsserted(entity=_family(), source_refs=("taxonomy:v1:semiconductors",), effective_from=T0),
            kind="world_entity_event",
            table="world_entity_events",
        ),
        _record(
            WorldEntityAsserted(entity=_company(), source_refs=("provider:lei:549300ABCDEFGHIJKLMN",), effective_from=T0),
            kind="world_entity_event",
            table="world_entity_events",
        ),
        _record(
            StructuralWorldRelationAsserted(relation=traded),
            kind="world_relation_event",
            table="world_relation_events",
            family="structural",
        ),
        _record(
            StructuralWorldRelationAsserted(relation=membership),
            kind="world_relation_event",
            table="world_relation_events",
            family="structural",
        ),
        _record(
            StructuralWorldRelationAsserted(relation=issued),
            kind="world_relation_event",
            table="world_relation_events",
            family="structural",
        ),
        _record(
            WorldOntologyRevisionPublished(revision=revision),
            kind="world_ontology_revision_event",
            table="world_ontology_revisions",
        ),
    )
    service = _service(records)
    payload = service.current_published_overview()
    replay = service.current_published_overview()
    encoded = json.dumps(payload, allow_nan=False)
    assert json.dumps(replay, allow_nan=False) == encoded
    json.loads(encoded)
    assert payload["status"] == "loaded"
    nodes = {node["id"]: node for node in payload["nodes"]}
    family = nodes[_family().node_id]
    company = nodes[_company().node_id]
    assert family["entity_kind"] == "family"
    assert family["node_kind"] == "world_entity"
    assert family["entity_id"] == "taxonomy:v1:semiconductors"
    assert family["label"]
    assert company["entity_kind"] == "company"
    assert company["node_kind"] == "world_entity"
    assert company["entity_id"] == "lei:549300ABCDEFGHIJKLMN"
    kinds = {edge["kind"] for edge in payload["edges"]}
    assert "MEMBER_OF_FAMILY" in kinds
    assert "ISSUED_BY" in kinds
    assert all(node.get("entity_kind") not in {"domain", "driver"} for node in payload["nodes"])
    assert [node["id"] for node in payload["nodes"]] == sorted(node["id"] for node in payload["nodes"])
    assert [edge["relation_id"] for edge in payload["edges"]] == [
        edge["relation_id"] for edge in sorted(payload["edges"], key=lambda item: (item["family"], item["relation_id"]))
    ]


def test_structural_head_endpoints_are_projected_even_when_absent_from_revision_entities() -> None:
    traded = _structural()
    membership = _structural(
        kind="MEMBER_OF_FAMILY",
        source=_entity(),
        target=_family(),
        source_refs=("taxonomy:v1:semiconductors",),
    )
    revision = WorldOntologyRevision(
        revision_id="market_ontology.v1",
        entities=(_entity(), _venue(), _country()),
        structural_relation_refs=(
            WorldStructuralRelationRef.from_relation(traded),
            WorldStructuralRelationRef.from_relation(membership),
        ),
        identity_link_refs=(_link().as_ref(),),
        scope_mapping_id="world_scope_mapping.v1",
        scope_mapping_hash=SCOPE_HASH,
    )
    records = (
        _record(
            WorldEntityAsserted(entity=_entity(), source_refs=("provider:instrument-master:2330",), effective_from=T0),
            kind="world_entity_event",
            table="world_entity_events",
        ),
        _record(
            WorldEntityAsserted(entity=_venue(), source_refs=("provider:venue-master:XTAI",), effective_from=T0),
            kind="world_entity_event",
            table="world_entity_events",
        ),
        _record(
            WorldEntityAsserted(entity=_country(), source_refs=("provider:iso-3166:TW",), effective_from=T0),
            kind="world_entity_event",
            table="world_entity_events",
        ),
        _record(
            StructuralWorldRelationAsserted(relation=traded),
            kind="world_relation_event",
            table="world_relation_events",
            family="structural",
        ),
        _record(
            StructuralWorldRelationAsserted(relation=membership),
            kind="world_relation_event",
            table="world_relation_events",
            family="structural",
        ),
        _record(
            WorldOntologyRevisionPublished(revision=revision),
            kind="world_ontology_revision_event",
            table="world_ontology_revisions",
        ),
    )
    payload = _service(records).current_published_overview()
    node_ids = {node["id"] for node in payload["nodes"]}
    assert _family().node_id in node_ids
    family = next(node for node in payload["nodes"] if node["id"] == _family().node_id)
    assert family["entity_kind"] == "family"
    assert any(edge["kind"] == "MEMBER_OF_FAMILY" and edge["target"] == _family().node_id for edge in payload["edges"])
    assert payload["truncated"] is False
    assert payload["truncation"]["omitted_edges"] == 0


def test_desktop_bridge_registers_read_only_world_graph_resource() -> None:
    source = (REPO_ROOT / "desktop" / "bridge" / "api.py").read_text(encoding="utf-8")
    assert '"world-graph": handle_world_graph' in source
    assert "WorldGraphExplorerService" in source
    assert "SqliteWorldGraphExplorerQuery" in source
    assert "WorldGraphStore" not in source
    assert "current_published_overview" in source
