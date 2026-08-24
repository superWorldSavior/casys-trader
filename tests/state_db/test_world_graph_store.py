"""Append-only SQLite graph ledger: events, snapshots, receipts, crash, restart."""

from __future__ import annotations

import inspect
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from trader.application.world_model.graph_ports import (
    WorldEntityEventEnvelope,
    WorldEntityEventId,
    WorldEntityIdentityEventEnvelope,
    WorldGraphSnapshotId,
    WorldOntologyRevisionEventEnvelope,
    WorldRelationEventEnvelope,
)
from trader.domain.world_availability import AvailabilityEvidence, PersistedWorldRef
from trader.domain.world_context import EntityRef
from trader.domain.world_episode import canonical_sha256
from trader.domain.world_graph import (
    FORBIDDEN_RELATION_KINDS,
    KnowledgeWorldRelation,
    KnowledgeWorldRelationAsserted,
    KnowledgeWorldRelationRetired,
    MacroGraphBridgeRegistry,
    MacroGraphBridgeRunSpec,
    MacroObservationCursor,
    MacroObservationCursorReservation,
    StaleBridgeEpoch,
    StructuralWorldRelation,
    StructuralWorldRelationAsserted,
    StructuralWorldRelationRetired,
    WorldEntityAsserted,
    WorldEntityIdentityLink,
    WorldEntityIdentityLinked,
    WorldEntityIdentityLinkSuperseded,
    WorldEntityIdentityUnlinked,
    WorldEntityRef,
    WorldEntityRetired,
    WorldEntitySuperseded,
    WorldGraphSnapshot,
    WorldKnowledgeRelationRef,
    WorldObservationRef,
    WorldOntologyRevision,
    WorldOntologyRevisionPublished,
    WorldOntologyRevisionSuperseded,
    WorldStructuralRelationRef,
)
from trader.domain.world_macro import (
    MACRO_PRODUCER_VERSION,
    MacroCollectionPlan,
    MacroCollectionTarget,
    MacroScope,
)
from trader.infrastructure.state_db.world_graph_store import (
    WORLD_GRAPH_TABLES,
    WorldGraphStore,
)
from trader.infrastructure.state_db.world_model_store import (
    WORLD_MODEL_MIGRATIONS,
    WorldModelConflictError,
    WorldModelStore,
)


UTC = timezone.utc
T0 = datetime(2026, 1, 1, tzinfo=UTC)
T1 = datetime(2026, 6, 1, tzinfo=UTC)
CUTOFF = datetime(2026, 8, 23, 13, 0, tzinfo=UTC)
READY = datetime(2026, 8, 24, 0, 5, tzinfo=UTC)
MID = datetime(2026, 8, 24, 0, 30, tzinfo=UTC)
BOOT = datetime(2026, 8, 24, 1, 0, tzinfo=UTC)
LATER = datetime(2026, 8, 24, 2, 0, tzinfo=UTC)
SHA = "a" * 64

_GRAPH_TABLES = (
    "world_entity_events",
    "world_entity_identity_events",
    "world_relation_events",
    "world_ontology_revisions",
    "world_graph_snapshots",
    "world_graph_snapshot_members",
)


def _entity(*, kind: str = "instrument", entity_id: str = "mic:XTAI:symbol:2330") -> WorldEntityRef:
    return WorldEntityRef(kind=kind, entity_id=entity_id)


def _venue() -> WorldEntityRef:
    return WorldEntityRef(kind="venue", entity_id="mic:XTAI")


def _entity_event(**overrides: object) -> WorldEntityAsserted:
    values: dict[str, object] = {
        "entity": _entity(),
        "source_refs": ("provider:instrument-master:2330",),
        "effective_from": T0,
    }
    values.update(overrides)
    return WorldEntityAsserted(**values)  # type: ignore[arg-type]


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
        "target": WorldEntityRef(kind="country", entity_id="iso-3166:TW"),
        "effective_from": T0,
        "ontology_revision": "market_ontology.v1",
        "source_refs": ("macro_world_observation:v1:" + SHA,),
    }
    values.update(overrides)
    return KnowledgeWorldRelation(**values)  # type: ignore[arg-type]


def _link(**overrides: object) -> WorldEntityIdentityLink:
    values: dict[str, object] = {
        "context_ref": EntityRef(kind="instrument", entity_id="2330"),
        "graph_ref": _entity(),
        "source_refs": ("provider:instrument-master:2330",),
        "effective_from": T0,
    }
    values.update(overrides)
    return WorldEntityIdentityLink(**values)  # type: ignore[arg-type]


def _revision(**overrides: object) -> WorldOntologyRevision:
    entity = _entity()
    structural = _structural()
    link = _link()
    values: dict[str, object] = {
        "revision_id": "market_ontology.v1",
        "entities": (entity, _venue()),
        "structural_relation_refs": (WorldStructuralRelationRef.from_relation(structural),),
        "identity_link_refs": (link.as_ref(),),
        "scope_mapping_id": "world_scope_mapping.v1",
        "scope_mapping_hash": SHA,
    }
    values.update(overrides)
    return WorldOntologyRevision(**values)  # type: ignore[arg-type]


def _snapshot(**overrides: object) -> WorldGraphSnapshot:
    revision = _revision()
    knowledge = _knowledge()
    values: dict[str, object] = {
        "root_episode_id": f"world-episode:v1:{SHA}",
        "root_entity": _entity(),
        "cutoff_at": CUTOFF,
        "ontology_revision": revision.revision_id,
        "ontology_hash": revision.content_sha256,
        "identity_map_hash": revision.identity_map_hash,
        "scope_mapping_id": revision.scope_mapping_id,
        "scope_mapping_hash": revision.scope_mapping_hash,
        "entity_revision_refs": tuple(
            WorldEntityAsserted(
                entity=item,
                source_refs=("provider:instrument-master:2330",),
                effective_from=T0,
            ).as_ref()
            for item in revision.entities
        ),
        "identity_link_refs": tuple(revision.identity_link_refs),
        "structural_relation_refs": frozenset(revision.structural_relation_refs),
        "knowledge_relation_refs": frozenset(
            {
                WorldKnowledgeRelationRef.from_relation(
                    knowledge,
                    availability_receipt_id=f"world-availability-receipt:v1:{SHA}",
                )
            }
        ),
        "artifact_refs": (f"knowledge_artifact:v1:{SHA}",),
        "producer_versions": {"graph_snapshot": "world_graph_snapshot.v1"},
        "status": "complete",
    }
    values.update(overrides)
    return WorldGraphSnapshot(**values)  # type: ignore[arg-type]


def _store(tmp_path: Path, *, clock: datetime = READY) -> WorldGraphStore:
    return WorldGraphStore(tmp_path / "world_model.db", clock=lambda: clock)


def test_ports_surface_has_no_caller_ready_at_or_status_mutation() -> None:
    assert not hasattr(WorldGraphStore, "set_status")
    append_names = (
        "append_entity_event",
        "append_identity_event",
        "append_structural_relation_event",
        "append_knowledge_relation_event",
        "append_revision_event",
        "append",
    )
    for name in append_names:
        parameters = inspect.signature(getattr(WorldGraphStore, name)).parameters
        assert "ready_at" not in parameters
        assert "first_seen_at" not in parameters
        assert "expected_sequence" not in parameters


def test_module_does_not_import_networkx_or_pickle() -> None:
    import trader.infrastructure.state_db.world_graph_store as module

    source = Path(module.__file__).read_text(encoding="utf-8")
    assert "import networkx" not in source
    assert "from networkx" not in source
    assert "import pickle" not in source
    assert "pickle.dumps" not in source
    assert "nx." not in source


def test_append_entity_assigns_store_ready_at_after_subject_commit(tmp_path: Path) -> None:
    path = tmp_path / "world_model.db"
    holder: dict[str, WorldGraphStore] = {}
    order: list[str] = []

    def clock() -> datetime:
        store = holder["store"]
        order.append("clock")
        assert store._db.query_one("SELECT event_id FROM world_entity_events") is not None
        assert store._db.query_one("SELECT receipt_id FROM world_availability_receipts") is None
        return READY

    store = WorldGraphStore(path, clock=clock)
    holder["store"] = store
    event = _entity_event()
    persisted = store.append_entity_event(event)
    assert order == ["clock"]
    assert isinstance(persisted, PersistedWorldRef)
    assert persisted.identity == WorldEntityEventId(event.event_id)
    assert persisted.receipt.ready_at == READY
    assert persisted.receipt.storage_locator.kind == "sqlite"
    assert persisted.receipt.storage_locator.table == "world_entity_events"
    assert persisted.receipt.storage_locator.row_id == event.event_id
    assert persisted.receipt.subject.kind == "world_entity_event"
    assert persisted.receipt.subject.content_sha256 == canonical_sha256(event.to_dict())
    envelopes = store.list_entity_events_available_through(LATER)
    assert len(envelopes) == 1
    assert isinstance(envelopes[0], WorldEntityEventEnvelope)
    evidence = envelopes[0].evidence
    assert isinstance(evidence, AvailabilityEvidence)
    assert evidence.receipt.ready_at == READY
    assert evidence.first_seen_at == READY
    assert envelopes[0].event.event_id == event.event_id


def test_crash_before_subject_commit_leaves_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = _store(tmp_path)
    event = _entity_event()

    def boom(*_args: object, **_kwargs: object) -> Any:
        raise OSError("subject commit failed")

    monkeypatch.setattr(store, "_persist_graph_event", boom)
    with pytest.raises(OSError, match="subject commit failed"):
        store.append_entity_event(event)
    assert store._db.query_one("SELECT COUNT(*) FROM world_entity_events")[0] == 0
    assert store._db.query_one("SELECT COUNT(*) FROM world_availability_receipts")[0] == 0
    assert store.list_entity_events_available_through(LATER) == ()


def test_crash_between_subject_and_receipt_is_unproven_until_idempotent_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "world_model.db"
    store = WorldGraphStore(path, clock=lambda: READY)
    event = _entity_event()

    def boom(*_args: object, **_kwargs: object) -> Any:
        raise OSError("receipt commit failed")

    monkeypatch.setattr(store, "_commit_availability_receipt", boom)
    with pytest.raises(OSError, match="receipt commit failed"):
        store.append_entity_event(event)
    assert store._db.query_one("SELECT COUNT(*) FROM world_entity_events")[0] == 1
    assert store._db.query_one("SELECT COUNT(*) FROM world_availability_receipts")[0] == 0
    assert store.list_entity_events_available_through(LATER) == ()

    monkeypatch.undo()
    retried = WorldGraphStore(path, clock=lambda: READY)
    recovered = retried.append_entity_event(event)
    assert recovered.receipt.ready_at == READY
    assert retried._db.query_one("SELECT COUNT(*) FROM world_entity_events")[0] == 1
    assert retried._db.query_one("SELECT COUNT(*) FROM world_availability_receipts")[0] == 1
    listed = retried.list_entity_events_available_through(LATER)
    assert len(listed) == 1
    assert listed[0].event.event_id == event.event_id
    replay = retried.append_entity_event(event)
    assert replay.receipt.receipt_id == recovered.receipt.receipt_id
    assert replay.receipt.ready_at == READY


def test_identical_event_repairs_missing_receipt_after_head_advances(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "world_model.db"
    store = WorldGraphStore(path, clock=lambda: READY)
    first = _entity_event()
    second = _entity_event(entity=_entity(entity_id="mic:XTAI:symbol:2317"))

    def boom(*_args: object, **_kwargs: object) -> Any:
        raise OSError("receipt commit failed")

    monkeypatch.setattr(store, "_commit_availability_receipt", boom)
    with pytest.raises(OSError, match="receipt commit failed"):
        store.append_entity_event(first)
    assert store._db.query_one("SELECT COUNT(*) FROM world_entity_events")[0] == 1
    assert store._db.query_one("SELECT COUNT(*) FROM world_availability_receipts")[0] == 0
    assert store.list_entity_events_available_through(LATER) == ()

    monkeypatch.undo()
    advanced = store.append_entity_event(second)
    assert advanced.receipt.ready_at == READY
    assert store._db.query_one("SELECT COUNT(*) FROM world_entity_events")[0] == 2
    sequences = [
        int(row["sequence"])
        for row in store._db.query_all("SELECT sequence FROM world_entity_events ORDER BY sequence")
    ]
    assert sequences == [1, 2]

    repaired = store.append_entity_event(first)
    assert repaired.receipt.ready_at == READY
    assert store._db.query_one("SELECT COUNT(*) FROM world_entity_events")[0] == 2
    assert store._db.query_one("SELECT COUNT(*) FROM world_availability_receipts")[0] == 2
    first_row = store._db.query_one(
        "SELECT sequence FROM world_entity_events WHERE event_id=?",
        (first.event_id,),
    )
    assert int(first_row["sequence"]) == 1
    listed = store.list_entity_events_available_through(LATER)
    assert [item.event.event_id for item in listed] == [first.event_id, second.event_id]


def test_same_event_id_different_content_is_conflict(tmp_path: Path) -> None:
    store = _store(tmp_path)
    first = _entity_event()
    store.append_entity_event(first)
    with store._db.transaction() as cur:
        cur.execute("DROP TRIGGER world_entity_events_no_update")
        cur.execute(
            "UPDATE world_entity_events SET payload_json=?, payload_sha256=? WHERE event_id=?",
            ('{"event_type":"tampered"}', "0" * 64, first.event_id),
        )
        cur.execute(
            """
            CREATE TRIGGER world_entity_events_no_update
            BEFORE UPDATE ON world_entity_events
            BEGIN
                SELECT RAISE(ABORT, 'world_entity_events are append-only');
            END
            """
        )
    with pytest.raises(WorldModelConflictError, match="different canonical content"):
        store.append_entity_event(first)


def test_restart_first_seen_is_conservative_and_does_not_mutate_ready_at(tmp_path: Path) -> None:
    path = tmp_path / "world_model.db"
    writer = WorldGraphStore(path, clock=lambda: READY)
    event = _entity_event()
    written = writer.append_entity_event(event)
    assert written.receipt.ready_at == READY
    listed = writer.list_entity_events_available_through(MID)
    assert listed[0].evidence.first_seen_at == READY
    assert listed[0].evidence.receipt.ready_at == READY
    writer.close()

    restarted = WorldGraphStore(path, clock=lambda: BOOT)
    replayed = restarted.append_entity_event(event)
    assert replayed.receipt.ready_at == READY
    after_boot = restarted.list_entity_events_available_through(LATER)
    assert len(after_boot) == 1
    evidence = after_boot[0].evidence
    assert evidence.first_seen_at == BOOT
    assert evidence.effective_ready_at == BOOT
    assert evidence.receipt.ready_at == READY
    assert restarted.list_entity_events_available_through(MID) == ()


def test_future_ready_event_is_invisible_at_earlier_cutoff(tmp_path: Path) -> None:
    store = WorldGraphStore(tmp_path / "world_model.db", clock=lambda: LATER)
    store.append_entity_event(_entity_event())
    assert store.list_entity_events_available_through(READY) == ()
    assert len(store.list_entity_events_available_through(LATER)) == 1


def test_identity_relation_revision_events_round_trip_and_split_families(tmp_path: Path) -> None:
    store = _store(tmp_path)
    asserted = store.append_entity_event(_entity_event())
    retired = WorldEntityRetired(entity=_entity(), retired_at=T1, source_refs=("provider:instrument-master:2330",))
    store.append_entity_event(retired)
    superseded = WorldEntitySuperseded(
        entity=_entity(),
        successor=_entity(entity_id="mic:XTAI:symbol:2330.TW"),
        superseded_at=T1,
        source_refs=("provider:instrument-master:2330:corrected",),
    )
    store.append_entity_event(superseded)

    link = _link()
    identity = store.append_identity_event(WorldEntityIdentityLinked(link=link))
    store.append_identity_event(
        WorldEntityIdentityUnlinked(
            link_id=link.link_id,
            retired_at=T1,
            source_refs=("provider:instrument-master:2330",),
        )
    )
    successor = _link(
        effective_from=T1,
        supersedes=link.link_id,
        source_refs=("provider:instrument-master:2330:corrected",),
        graph_ref=_entity(entity_id="mic:XTAI:symbol:2330.TW"),
    )
    store.append_identity_event(
        WorldEntityIdentityLinkSuperseded(predecessor_link_id=link.link_id, successor=successor)
    )

    structural = _structural()
    store.append_structural_relation_event(StructuralWorldRelationAsserted(relation=structural))
    store.append_structural_relation_event(
        StructuralWorldRelationRetired(
            relation_id=structural.relation_id,
            retired_at=T1,
            source_refs=("provider:instrument-master:2330",),
        )
    )
    knowledge = _knowledge()
    store.append_knowledge_relation_event(KnowledgeWorldRelationAsserted(relation=knowledge))
    store.append_knowledge_relation_event(
        KnowledgeWorldRelationRetired(
            relation_id=knowledge.relation_id,
            retired_at=T1,
            source_refs=("macro_world_observation:v1:" + SHA,),
        )
    )

    revision = _revision()
    store.append_revision_event(WorldOntologyRevisionPublished(revision=revision))
    store.append_revision_event(
        WorldOntologyRevisionSuperseded(revision_id=revision.revision_id, successor_revision_id="market_ontology.other")
    )

    entities = store.list_entity_events_available_through(LATER)
    assert [item.event.event_type for item in entities] == [
        "world_entity_asserted",
        "world_entity_retired",
        "world_entity_superseded",
    ]
    identities = store.list_identity_events_available_through(LATER)
    assert isinstance(identities[0], WorldEntityIdentityEventEnvelope)
    assert identities[0].event.event_id == identity.identity
    structural_events = store.list_structural_relation_events_available_through(LATER)
    knowledge_events = store.list_knowledge_relation_events_available_through(LATER)
    assert all(isinstance(item, WorldRelationEventEnvelope) for item in structural_events)
    assert all(item.event.family == "structural" for item in structural_events)
    assert all(item.event.family == "knowledge" for item in knowledge_events)
    assert {item.event.event_type for item in structural_events} == {
        "world_relation_asserted",
        "world_relation_retired",
    }
    revisions = store.list_revision_events_available_through(LATER)
    assert isinstance(revisions[0], WorldOntologyRevisionEventEnvelope)
    assert revisions[0].event.event_type == "world_ontology_revision_published"
    assert asserted.receipt.subject.kind == "world_entity_event"
    families = {row["family"] for row in store._db.query_all("SELECT family FROM world_relation_events")}
    assert families == {"structural", "knowledge"}
    for envelope in (*structural_events, *knowledge_events):
        assert canonical_sha256(envelope.event.to_dict()) == envelope.evidence.receipt.subject.content_sha256


def test_unmapped_missing_snapshot_persists_without_a_world_entity_root(tmp_path: Path) -> None:
    store = _store(tmp_path)
    snapshot = _snapshot(
        root_entity=None,
        status="missing",
        missingness={"scope": "unmapped"},
        entity_revision_refs=(),
        identity_link_refs=(),
        structural_relation_refs=(),
        knowledge_relation_refs=(),
        artifact_refs=(),
    )
    persisted = store.append(snapshot)
    loaded = store.get(persisted.identity)
    assert loaded is not None
    assert loaded.root_entity is None
    assert loaded.missingness["scope"] == "unmapped"
    assert loaded.entity_revision_refs == ()
    assert loaded.identity_link_refs == ()
    assert loaded.structural_relation_refs == frozenset()
    assert loaded.knowledge_relation_refs == frozenset()
    assert loaded.artifact_refs == ()
    row = store._db.query_one(
        "SELECT root_entity_kind, root_entity_id, payload_json FROM world_graph_snapshots WHERE snapshot_id=?",
        (snapshot.snapshot_id,),
    )
    assert row["root_entity_kind"] == ""
    assert row["root_entity_id"] == ""
    with pytest.raises((TypeError, ValueError)):
        WorldEntityRef(kind=row["root_entity_kind"], entity_id=row["root_entity_id"])
    assert "XNYS" not in row["payload_json"]
    assert '"root_entity":null' in row["payload_json"]
    members = store._db.query_all(
        "SELECT member_kind FROM world_graph_snapshot_members WHERE snapshot_id=?",
        (snapshot.snapshot_id,),
    )
    assert members == []


def test_append_rejects_wrong_relation_family(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(TypeError):
        store.append_structural_relation_event(KnowledgeWorldRelationAsserted(relation=_knowledge()))
    with pytest.raises(TypeError):
        store.append_knowledge_relation_event(StructuralWorldRelationAsserted(relation=_structural()))


def test_persisted_payloads_never_contain_networkx_or_causes(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.append_entity_event(_entity_event())
    store.append_structural_relation_event(StructuralWorldRelationAsserted(relation=_structural()))
    store.append_knowledge_relation_event(KnowledgeWorldRelationAsserted(relation=_knowledge()))
    store.append(_snapshot())
    blobs = [
        row[0]
        for table in (
            "world_entity_events",
            "world_relation_events",
            "world_graph_snapshots",
            "world_graph_snapshot_members",
        )
        for row in store._db.query_all(f"SELECT payload_json FROM {table}")
    ]
    joined = "\n".join(blobs).lower()
    assert "networkx" not in joined
    assert "multidigraph" not in joined
    assert "pickle" not in joined
    for forbidden in FORBIDDEN_RELATION_KINDS:
        assert f'"kind":"{forbidden.lower()}"' not in joined


def test_snapshot_append_get_membership_and_replay(tmp_path: Path) -> None:
    store = _store(tmp_path)
    snapshot = _snapshot()
    persisted = store.append(snapshot)
    assert persisted.identity == WorldGraphSnapshotId(snapshot.snapshot_id)
    assert persisted.receipt.storage_locator.table == "world_graph_snapshots"
    loaded = store.get(WorldGraphSnapshotId(snapshot.snapshot_id))
    assert loaded == snapshot
    members = store._db.query_all(
        "SELECT member_kind, member_id FROM world_graph_snapshot_members WHERE snapshot_id=? ORDER BY member_kind, ordinal, member_id",
        (snapshot.snapshot_id,),
    )
    kinds = {row["member_kind"] for row in members}
    assert kinds == {
        "entity_revision",
        "identity_link",
        "structural_relation",
        "knowledge_relation",
        "artifact",
    }
    assert any(row["member_kind"] == "artifact" for row in members)
    replay = store.append(snapshot)
    assert replay.receipt.receipt_id == persisted.receipt.receipt_id
    assert store._db.query_one("SELECT COUNT(*) FROM world_graph_snapshots")[0] == 1
    assert store.get(WorldGraphSnapshotId("world_graph_snapshot:v1:" + "b" * 64)) is None

    conflicting = _snapshot(status="partial")
    assert conflicting.snapshot_id != snapshot.snapshot_id
    store.append(conflicting)
    with store._db.transaction() as cur:
        cur.execute("DROP TRIGGER world_graph_snapshots_no_update")
        cur.execute(
            "UPDATE world_graph_snapshots SET payload_sha256=? WHERE snapshot_id=?",
            ("0" * 64, snapshot.snapshot_id),
        )
        cur.execute(
            """
            CREATE TRIGGER world_graph_snapshots_no_update
            BEFORE UPDATE ON world_graph_snapshots
            BEGIN
                SELECT RAISE(ABORT, 'world_graph_snapshots are append-only');
            END
            """
        )
    with pytest.raises(WorldModelConflictError, match="different canonical content"):
        store.append(snapshot)


def test_snapshot_crash_between_subject_and_receipt_repairs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "world_model.db"
    store = WorldGraphStore(path, clock=lambda: READY)
    snapshot = _snapshot()

    def boom(*_args: object, **_kwargs: object) -> Any:
        raise OSError("receipt commit failed")

    monkeypatch.setattr(store, "_commit_availability_receipt", boom)
    with pytest.raises(OSError, match="receipt commit failed"):
        store.append(snapshot)
    assert store._db.query_one("SELECT COUNT(*) FROM world_graph_snapshots")[0] == 1
    assert store._db.query_one("SELECT COUNT(*) FROM world_graph_snapshot_members")[0] > 0
    assert store._db.query_one("SELECT COUNT(*) FROM world_availability_receipts")[0] == 0
    loaded = store.get(WorldGraphSnapshotId(snapshot.snapshot_id))
    assert loaded == snapshot

    monkeypatch.undo()
    recovered = store.append(snapshot)
    assert recovered.receipt.ready_at == READY
    assert store._db.query_one("SELECT COUNT(*) FROM world_graph_snapshots")[0] == 1
    assert store._db.query_one("SELECT COUNT(*) FROM world_availability_receipts")[0] == 1


def test_anti_update_delete_triggers_cover_graph_and_shared_receipt_tables(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.append_entity_event(_entity_event())
    store.append_identity_event(WorldEntityIdentityLinked(link=_link()))
    store.append_structural_relation_event(StructuralWorldRelationAsserted(relation=_structural()))
    store.append_revision_event(WorldOntologyRevisionPublished(revision=_revision()))
    store.append(_snapshot())
    mutations = (
        "UPDATE world_entity_events SET event_type='tampered'",
        "DELETE FROM world_entity_events",
        "UPDATE world_entity_identity_events SET event_type='tampered'",
        "DELETE FROM world_entity_identity_events",
        "UPDATE world_relation_events SET family='tampered'",
        "DELETE FROM world_relation_events",
        "UPDATE world_ontology_revisions SET revision_id='tampered'",
        "DELETE FROM world_ontology_revisions",
        "UPDATE world_graph_snapshots SET status='tampered'",
        "DELETE FROM world_graph_snapshots",
        "UPDATE world_graph_snapshot_members SET member_id='tampered'",
        "DELETE FROM world_graph_snapshot_members",
        "UPDATE world_availability_receipts SET scope='tampered'",
        "DELETE FROM world_availability_receipts",
    )
    for sql in mutations:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            with store._db.transaction() as cur:
                cur.execute(sql)


def test_migration_creates_append_only_graph_schema(tmp_path: Path) -> None:
    store = _store(tmp_path)
    versions = {row["version"] for row in store._db.query_all("SELECT version FROM schema_migrations")}
    assert versions == {1}
    names = {row["name"] for row in store._db.query_all("SELECT name FROM sqlite_master WHERE type='table'")}
    for table in _GRAPH_TABLES:
        assert table in names
        assert table in WORLD_GRAPH_TABLES
    assert "world_availability_receipts" in names
    current_sql = "\n".join(WORLD_MODEL_MIGRATIONS[0][1])
    for table in _GRAPH_TABLES:
        assert table in current_sql
        assert f"{table}_no_update" in current_sql
        assert f"{table}_no_delete" in current_sql
    assert "CREATE TABLE IF NOT EXISTS world_availability_receipts" in current_sql
    assert "sequence" in current_sql


def test_sequences_are_monotone_and_replay_keeps_original_sequence(tmp_path: Path) -> None:
    store = _store(tmp_path)
    first = _entity_event()
    second = _entity_event(entity=_entity(entity_id="mic:XTAI:symbol:2317"))
    store.append_entity_event(first)
    store.append_entity_event(second)
    store.append_entity_event(first)
    rows = store._db.query_all("SELECT event_id, sequence FROM world_entity_events ORDER BY sequence")
    assert [int(row["sequence"]) for row in rows] == [1, 2]
    assert rows[0]["event_id"] == first.event_id
    assert rows[1]["event_id"] == second.event_id


def test_concurrent_new_insertions_cas_on_sequence_and_both_distinct_events_persist(tmp_path: Path) -> None:
    path = tmp_path / "world_model.db"
    setup = WorldGraphStore(path, clock=lambda: READY)
    setup.close()
    first = _entity_event()
    second = _entity_event(entity=_entity(entity_id="mic:XTAI:symbol:2317"))
    barrier = threading.Barrier(2)
    outcomes: list[tuple[str, str]] = []
    lock = threading.Lock()

    def worker(event: WorldEntityAsserted) -> None:
        barrier.wait()
        local = WorldGraphStore(path, clock=lambda: READY)
        try:
            persisted = local.append_entity_event(event)
            with lock:
                outcomes.append(("ok", persisted.identity))
        except WorldModelConflictError as exc:
            with lock:
                outcomes.append(("conflict", str(exc)))
        finally:
            local.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = (executor.submit(worker, first), executor.submit(worker, second))
        for future in futures:
            future.result()

    oks = [item for item in outcomes if item[0] == "ok"]
    assert len(oks) == 2
    verifier = WorldGraphStore(path, clock=lambda: READY)
    try:
        assert verifier._db.query_one("SELECT COUNT(*) FROM world_entity_events")[0] == 2
        sequences = {int(row["sequence"]) for row in verifier._db.query_all("SELECT sequence FROM world_entity_events")}
        assert sequences == {1, 2}
        listed = verifier.list_entity_events_available_through(LATER)
        assert {item.event.event_id for item in listed} == {first.event_id, second.event_id}
    finally:
        verifier.close()


def test_world_model_store_applies_graph_migration_without_owning_graph_api(tmp_path: Path) -> None:
    model = WorldModelStore(tmp_path / "world_model.db")
    try:
        names = {row["name"] for row in model._db.query_all("SELECT name FROM sqlite_master WHERE type='table'")}
        for table in _GRAPH_TABLES:
            assert table in names
        assert not hasattr(model, "append_entity_event")
    finally:
        model.close()


def test_shared_receipt_table_does_not_mix_cohort_and_graph_subjects(tmp_path: Path) -> None:
    path = tmp_path / "world_model.db"
    graph = WorldGraphStore(path, clock=lambda: READY)
    graph.append_entity_event(_entity_event())
    receipt = graph._db.query_one("SELECT subject_kind FROM world_availability_receipts")
    assert receipt["subject_kind"] == "world_entity_event"
    assert graph._db.query_one("SELECT COUNT(*) FROM world_cohort_events")[0] == 0


BRIDGE_KEY = "macro_graph_bridge.v1"
REQUEST_ID = "macro_graph_bridge_request:v1:" + "c" * 64


def _reservation() -> MacroObservationCursorReservation:
    return MacroObservationCursorReservation(
        bridge_key=BRIDGE_KEY,
        request_id=REQUEST_ID,
        cursor=MacroObservationCursor(receipt_log_generation=1, ordinal=0),
    )


def _bridge_plan() -> MacroCollectionPlan:
    return MacroCollectionPlan(
        registry_version="world_macro_sources.v1",
        registry_content_sha256="b" * 64,
        targets=(
            MacroCollectionTarget(
                scope=MacroScope(kind="venue", entity_id="mic:XTAI"),
                source_ids=("fed_policy_rate",),
            ),
        ),
    )


def _bridge_spec(revision: WorldOntologyRevision | None = None) -> MacroGraphBridgeRunSpec:
    resolved = revision if revision is not None else _revision()
    plan = _bridge_plan()
    return MacroGraphBridgeRunSpec(
        scope_mapping_id=resolved.scope_mapping_id,
        scope_mapping_hash=resolved.scope_mapping_hash,
        ontology_revision_id=resolved.revision_id,
        ontology_revision_hash=resolved.content_sha256,
        collection_plan_id=plan.plan_id,
        collection_plan_hash=plan.content_sha256,
        producer_version=MACRO_PRODUCER_VERSION,
    )


def _activate(store: WorldGraphStore, spec: MacroGraphBridgeRunSpec | None = None) -> MacroGraphBridgeRegistry:
    registry = store.load(BRIDGE_KEY)
    updated = registry.activate(
        reservation=_reservation(), spec=spec or _bridge_spec(), expected_version=registry.version
    )
    if updated.version != registry.version:
        store.append_event(updated.events[-1], expected_registry_version=registry.version, fence=None)
        return store.load(BRIDGE_KEY)
    return updated


def test_bridge_migration_is_version_7_and_does_not_rewrite_graph_v6(tmp_path: Path) -> None:
    store = _store(tmp_path)
    versions = {row["version"] for row in store._db.query_all("SELECT version FROM schema_migrations")}
    assert versions == {1}
    names = {row["name"] for row in store._db.query_all("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "world_macro_graph_bridge_events" in names
    current_sql = "\n".join(WORLD_MODEL_MIGRATIONS[0][1])
    assert "CREATE TABLE IF NOT EXISTS world_macro_graph_bridge_events" in current_sql
    assert "CREATE TABLE IF NOT EXISTS world_availability_receipts" in current_sql
    assert "world_macro_graph_bridge_events_no_update" in current_sql
    _activate(store)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        with store._db.transaction() as cur:
            cur.execute("UPDATE world_macro_graph_bridge_events SET event_type='tampered'")


def test_bridge_event_cas_and_identical_retry_repairs_receipt_after_head_advances(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "world_model.db"
    store = WorldGraphStore(path, clock=lambda: READY)
    spec = _bridge_spec()
    empty = MacroGraphBridgeRegistry.empty(BRIDGE_KEY)
    activated = empty.activate(reservation=_reservation(), spec=spec, expected_version=0)
    event = activated.events[-1]

    def boom(*_args: object, **_kwargs: object):
        raise OSError("bridge receipt failed")

    monkeypatch.setattr(store, "_commit_availability_receipt", boom)
    with pytest.raises(OSError, match="bridge receipt failed"):
        store.append_event(event, expected_registry_version=0, fence=None)
    assert store._db.query_one("SELECT COUNT(*) FROM world_macro_graph_bridge_events")[0] == 1
    assert store.load(BRIDGE_KEY).events == ()
    monkeypatch.undo()
    blocked = activated.block(reason="config_drift", expected_version=1)
    store.append_event(blocked.events[-1], expected_registry_version=0, fence=None)
    repaired = store.append_event(event, expected_registry_version=0, fence=None)
    assert repaired.receipt.ready_at == READY
    loaded = store.load(BRIDGE_KEY)
    assert loaded.version == 2
    assert loaded.events[0].event_id == event.event_id
    with pytest.raises(WorldModelConflictError):
        other = MacroGraphBridgeRegistry.empty(BRIDGE_KEY).activate(
            reservation=MacroObservationCursorReservation(
                bridge_key=BRIDGE_KEY,
                request_id="macro_graph_bridge_request:v1:" + "d" * 64,
                cursor=MacroObservationCursor(receipt_log_generation=1, ordinal=0),
            ),
            spec=spec,
            expected_version=0,
        )
        store.append_event(other.events[-1], expected_registry_version=0, fence=None)


def test_fenced_knowledge_append_and_old_worker_is_rejected_after_handoff(tmp_path: Path) -> None:
    store = _store(tmp_path)
    registry = _activate(store)
    fence = registry.fence
    knowledge = KnowledgeWorldRelationAsserted(relation=_knowledge())
    persisted = store.append_knowledge_relation_event(
        knowledge,
        fence=fence,
        expected_registry_version=registry.version,
    )
    assert persisted.receipt.storage_locator.table == "world_relation_events"
    visible = store.list_knowledge_relation_events_available_through(LATER)
    assert len(visible) == 1
    blocked = registry.block(reason="config_drift", expected_version=registry.version)
    store.append_event(blocked.events[-1], expected_registry_version=registry.version, fence=fence)
    next_plan = MacroCollectionPlan(
        registry_version="world_macro_sources.v1",
        registry_content_sha256="c" * 64,
        targets=(
            MacroCollectionTarget(
                scope=MacroScope(kind="venue", entity_id="mic:XTAI"),
                source_ids=("fed_policy_rate",),
            ),
        ),
    )
    next_spec = MacroGraphBridgeRunSpec(
        scope_mapping_id="world_scope_mapping.v1",
        scope_mapping_hash="b" * 64,
        ontology_revision_id="market_ontology.v1",
        ontology_revision_hash="c" * 64,
        collection_plan_id=next_plan.plan_id,
        collection_plan_hash=next_plan.content_sha256,
        producer_version=MACRO_PRODUCER_VERSION,
    )
    handed = store.load(BRIDGE_KEY).handoff(
        active_run_id=blocked.active_run.run_id,
        next_run_spec=next_spec,
        expected_version=2,
    )
    store.append_event(handed.events[-1], expected_registry_version=2, fence=None)
    other = _knowledge(source=WorldObservationRef(observation_id=f"world_observation:v1:{'b' * 64}"))
    with pytest.raises(StaleBridgeEpoch, match="stale_bridge_epoch"):
        store.append_knowledge_relation_event(
            KnowledgeWorldRelationAsserted(relation=other),
            fence=fence,
            expected_registry_version=1,
        )
    unproven = store.list_knowledge_relation_events_available_through(CUTOFF)
    assert unproven == ()
