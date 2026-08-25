from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from trader.domain.world_context import EntityRef
from trader.domain.world_graph import (
    KnowledgeArtifactRef,
    KnowledgeWorldRelation,
    KnowledgeWorldRelationAsserted,
    StructuralWorldRelation,
    StructuralWorldRelationAsserted,
    WorldEntityAsserted,
    WorldEntityIdentityLink,
    WorldEntityIdentityLinked,
    WorldEntityRef,
    WorldOntologyRevision,
    WorldOntologyRevisionPublished,
    WorldStructuralRelationRef,
)
from trader.infrastructure.state_db.world_graph_explorer_query import SqliteWorldGraphExplorerQuery
from trader.infrastructure.state_db.world_graph_store import WorldGraphStore


UTC = timezone.utc
T0 = datetime(2026, 1, 1, tzinfo=UTC)
READY = datetime(2026, 8, 24, 0, 5, tzinfo=UTC)
NOW = datetime(2026, 8, 25, 6, 0, tzinfo=UTC)
SHA = "a" * 64


def _entity(*, kind: str = "instrument", entity_id: str = "mic:XTAI:symbol:2330") -> WorldEntityRef:
    return WorldEntityRef(kind=kind, entity_id=entity_id)


def _venue() -> WorldEntityRef:
    return WorldEntityRef(kind="venue", entity_id="mic:XTAI")


def _structural() -> StructuralWorldRelation:
    return StructuralWorldRelation(
        kind="TRADED_ON",
        source=_entity(),
        target=_venue(),
        effective_from=T0,
        ontology_revision="market_ontology.v1",
        source_refs=("provider:instrument-master:2330",),
    )


def _about() -> KnowledgeWorldRelation:
    return KnowledgeWorldRelation(
        kind="ABOUT",
        source=KnowledgeArtifactRef(artifact_id=f"knowledge_artifact:v1:{SHA}", content_sha256=SHA),
        target=_venue(),
        effective_from=T0,
        ontology_revision="market_ontology.v1",
        source_refs=("artifact:proof",),
    )


def _link() -> WorldEntityIdentityLink:
    return WorldEntityIdentityLink(
        context_ref=EntityRef(kind="instrument", entity_id="2330"),
        graph_ref=_entity(),
        source_refs=("provider:instrument-master:2330",),
        effective_from=T0,
    )


def _revision(structural: StructuralWorldRelation) -> WorldOntologyRevision:
    return WorldOntologyRevision(
        revision_id="market_ontology.v1",
        entities=(_entity(), _venue()),
        structural_relation_refs=(WorldStructuralRelationRef.from_relation(structural),),
        identity_link_refs=(_link().as_ref(),),
        scope_mapping_id="world_scope_mapping.v1",
        scope_mapping_hash=SHA,
    )


def test_adapter_source_is_readonly_and_never_opens_world_graph_store() -> None:
    path = Path(__file__).resolve().parents[2] / "trader" / "infrastructure" / "state_db" / "world_graph_explorer_query.py"
    source = path.read_text(encoding="utf-8")
    assert "mode=ro" in source
    assert "uri=True" in source
    assert "from trader.infrastructure.state_db.world_graph_store" not in source
    assert "import WorldGraphStore" not in source
    assert "WorldGraphStore(" not in source
    assert "apply_current_world_model_schema" not in source
    assert "CREATE TABLE" not in source
    assert "INSERT " not in source


def test_missing_database_stays_missing_and_not_started(tmp_path: Path) -> None:
    db_path = tmp_path / "world_model.db"
    snapshot = SqliteWorldGraphExplorerQuery(db_path).load_current_records()
    assert snapshot.status == "not_started"
    assert snapshot.exists is False
    assert snapshot.records == ()
    assert not db_path.exists()


def test_missing_schema_is_unavailable_without_migration(tmp_path: Path) -> None:
    db_path = tmp_path / "world_model.db"
    sqlite3.connect(db_path).close()
    before = db_path.stat().st_mtime_ns
    snapshot = SqliteWorldGraphExplorerQuery(db_path).load_current_records()
    assert snapshot.status == "unavailable"
    assert snapshot.exists is True
    assert "world_ontology_revisions" in snapshot.missing_tables
    assert snapshot.records == ()
    assert db_path.stat().st_mtime_ns == before


def test_readonly_uri_rejects_writes_and_preserves_relation_ids(tmp_path: Path) -> None:
    db_path = tmp_path / "world_model.db"
    store = WorldGraphStore(db_path, clock=lambda: READY)
    structural = _structural()
    about = _about()
    store.append_entity_event(
        WorldEntityAsserted(entity=_entity(), source_refs=("provider:instrument-master:2330",), effective_from=T0)
    )
    store.append_entity_event(
        WorldEntityAsserted(entity=_venue(), source_refs=("provider:venue-master:XTAI",), effective_from=T0)
    )
    store.append_identity_event(WorldEntityIdentityLinked(link=_link()))
    store.append_structural_relation_event(StructuralWorldRelationAsserted(relation=structural))
    store.append_knowledge_relation_event(KnowledgeWorldRelationAsserted(relation=about))
    store.append_revision_event(WorldOntologyRevisionPublished(revision=_revision(structural)))
    store.close()
    before = db_path.stat().st_mtime_ns
    snapshot = SqliteWorldGraphExplorerQuery(db_path).load_current_records()
    assert snapshot.status == "ready"
    assert snapshot.exists is True
    assert any(
        record.family == "structural" and record.payload.get("relation", {}).get("relation_id") == structural.relation_id
        for record in snapshot.records
    )
    assert any(
        record.family == "knowledge" and record.payload.get("relation", {}).get("relation_id") == about.relation_id
        for record in snapshot.records
    )
    from urllib.parse import quote

    uri = f"file:{quote(str(db_path.resolve()), safe='/')}?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        try:
            connection.execute("INSERT INTO world_entity_events(event_id) VALUES ('x')")
        except sqlite3.Error as exc:
            assert "readonly" in str(exc).lower() or "read-only" in str(exc).lower()
        else:
            raise AssertionError("readonly URI accepted a write")
    assert db_path.stat().st_mtime_ns == before

    from trader.application.world_model.graph_explorer import WorldGraphExplorerService

    payload = WorldGraphExplorerService(
        SqliteWorldGraphExplorerQuery(db_path),
        clock=lambda: NOW,
    ).current_published_overview()
    assert payload["status"] == "loaded"
    assert payload["schema_version"] == "world_graph_explorer.v1"
    assert {edge["relation_id"] for edge in payload["edges"]} == {structural.relation_id, about.relation_id}
    assert all(edge["kind"] != "CAUSES" for edge in payload["edges"])
    assert payload["ontology"]["revision_id"] == "market_ontology.v1"
    assert payload["status"] not in {"missing", "error"}
    assert db_path.stat().st_mtime_ns == before


def test_corrupt_payload_is_unavailable_without_write(tmp_path: Path) -> None:
    db_path = tmp_path / "world_model.db"
    with sqlite3.connect(db_path) as connection:
        connection.executescript(
            """
            CREATE TABLE world_entity_events (
                event_id TEXT, payload_json TEXT, payload_sha256 TEXT, sequence INTEGER
            );
            CREATE TABLE world_entity_identity_events (
                event_id TEXT, payload_json TEXT, payload_sha256 TEXT, sequence INTEGER
            );
            CREATE TABLE world_relation_events (
                event_id TEXT, family TEXT, payload_json TEXT, payload_sha256 TEXT, sequence INTEGER
            );
            CREATE TABLE world_ontology_revisions (
                event_id TEXT, payload_json TEXT, payload_sha256 TEXT, sequence INTEGER
            );
            CREATE TABLE world_availability_receipts (
                subject_kind TEXT, subject_id TEXT, content_sha256 TEXT, payload_json TEXT
            );
            INSERT INTO world_entity_events(event_id, payload_json, payload_sha256, sequence)
            VALUES ('e1', '{', 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 1);
            """
        )
        connection.commit()
    before = db_path.stat().st_mtime_ns
    snapshot = SqliteWorldGraphExplorerQuery(db_path).load_current_records()
    assert snapshot.status == "unavailable"
    assert snapshot.exists is True
    assert snapshot.records == ()
    assert snapshot.error
    assert "JSONDecodeError" in snapshot.error
    assert db_path.stat().st_mtime_ns == before

    from trader.application.world_model.graph_explorer import WorldGraphExplorerService

    payload = WorldGraphExplorerService(
        SqliteWorldGraphExplorerQuery(db_path),
        clock=lambda: NOW,
    ).current_published_overview()
    assert payload["status"] == "unavailable"
    assert payload["status"] not in {"missing", "error"}
    assert payload["error"]
    assert payload["nodes"] == []
    assert db_path.stat().st_mtime_ns == before
