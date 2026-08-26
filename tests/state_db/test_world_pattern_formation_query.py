from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

import pytest

from tests.package_layout._helpers import REPO_ROOT
from trader.application.world_model.pattern_formation_request import PatternFormationRequest
from trader.domain.world_episode import (
    GRAPH_FEATURE_CONTRACT_ID,
    MARKET_FEATURE_CONTRACT_ID,
    AnchorBar,
    WorldEpisode,
    WorldObservation,
    WorldOutcome,
    canonical_sha256,
)
from trader.domain.world_driver import DriverState
from trader.domain.world_graph import (
    KnowledgeArtifactRef,
    KnowledgeWorldRelation,
    KnowledgeWorldRelationAsserted,
    MacroObservationKnowledgeLink,
    StructuralWorldRelation,
    StructuralWorldRelationAsserted,
    WorldEntityRef,
    WorldGraphSnapshot,
    WorldKnowledgeRelationRef,
    WorldStructuralRelationRef,
    reconstruct_macro_observes_provenance,
    world_observation_ref_for_observation_id,
)
from trader.domain.world_macro import (
    MACRO_PRODUCER_VERSION,
    MACRO_SOURCE_REGISTRY_VERSION,
    MACRO_TRANSFORM_VERSION,
    MacroCoverage,
    MacroDimensionState,
    MacroFactSource,
    MacroNumericValue,
    MacroObservationProvenance,
    MacroScope,
    MacroSourceFact,
    MacroWorldObservation,
)
from trader.infrastructure.state_db import world_pattern_formation_query as formation_query
from trader.infrastructure.state_db.world_graph_store import WorldGraphStore
from trader.infrastructure.state_db.world_macro_store import WorldMacroStore
from trader.infrastructure.state_db.world_model_store import WorldModelStore
from trader.infrastructure.state_db.world_pattern_formation_query import SqlitePatternFormationSource


UTC = timezone.utc
SHA = "a" * 64
AS_OF = datetime(2026, 8, 25, tzinfo=UTC)
RECORDED = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)
RELATION_READY = datetime(2026, 7, 1, tzinfo=UTC)
SNAPSHOT_READY = datetime(2026, 8, 25, 0, 5, tzinfo=UTC)
FORMATION = datetime(2026, 9, 1, tzinfo=UTC)
EVAL_NOT_BEFORE = datetime(2026, 9, 2, tzinfo=UTC)
T0 = datetime(2026, 1, 1, tzinfo=UTC)
_QUERY = REPO_ROOT / "trader" / "infrastructure" / "state_db" / "world_pattern_formation_query.py"


def _request(**overrides: object) -> PatternFormationRequest:
    values: dict[str, object] = {
        "formation_cutoff": FORMATION,
        "evaluation_start_not_before": EVAL_NOT_BEFORE,
        "horizons": ("elapsed_1d.v1",),
    }
    values.update(overrides)
    return PatternFormationRequest(**values)  # type: ignore[arg-type]


def _instrument(symbol: str = "2330") -> WorldEntityRef:
    return WorldEntityRef(kind="instrument", entity_id=f"mic:XTAI:symbol:{symbol}")


def _venue() -> WorldEntityRef:
    return WorldEntityRef(kind="venue", entity_id="mic:XTAI")


def _structural(*, symbol: str = "2330", effective_from: datetime = T0) -> StructuralWorldRelation:
    return StructuralWorldRelation(
        kind="TRADED_ON",
        source=_instrument(symbol),
        target=_venue(),
        effective_from=effective_from,
        ontology_revision="market_ontology.v1",
        source_refs=("provider:instrument-master:2330",),
    )


def _about(*, digest: str = SHA, effective_from: datetime = T0) -> KnowledgeWorldRelation:
    return KnowledgeWorldRelation(
        kind="ABOUT",
        source=KnowledgeArtifactRef(artifact_id=f"knowledge_artifact:v1:{digest}", content_sha256=digest),
        target=_venue(),
        effective_from=effective_from,
        ontology_revision="market_ontology.v1",
        source_refs=(f"artifact:{digest}",),
    )


def _observation(*, symbol: str, as_of: datetime, **overrides: object) -> WorldObservation:
    values: dict[str, object] = {
        "venue": "TW",
        "symbol": symbol,
        "bar_interval": "1h",
        "as_of_bar_ts": as_of,
        "feature_contract_version": GRAPH_FEATURE_CONTRACT_ID,
        "sampling_policy_version": "active_tradable_completed_bar.v1",
        "anchor": AnchorBar(
            ts=as_of,
            open=100.0,
            high=101.0,
            low=99.0,
            close=100.5,
            volume=1000.0,
            source="unit-pattern-formation-query",
            timestamp_semantics="bar_close",
        ),
        "available_at": as_of,
        "captured_at": as_of,
        "freshness": {"status": "fresh", "data_age_minutes": 5.0},
        "categorical_features": {"venue": "TW", "bar_interval": "1h"},
        "numeric_features": {"return": 0.01},
    }
    values.update(overrides)
    return WorldObservation(**values)  # type: ignore[arg-type]


def _market_episode(*, symbol: str, as_of: datetime) -> WorldEpisode:
    return WorldEpisode(
        observation=_observation(
            symbol=symbol,
            as_of=as_of,
            feature_contract_version=MARKET_FEATURE_CONTRACT_ID,
            graph=None,
            graph_features=None,
        )
    )


def _graph_episode(*, market: WorldEpisode, snapshot: WorldGraphSnapshot) -> WorldEpisode:
    observation = market.observation
    return WorldEpisode(
        observation=_observation(
            symbol=observation.symbol,
            as_of=observation.as_of_bar_ts,
            graph=snapshot,
            graph_features={"snapshot": snapshot.to_dict()},
        )
    )


def _close_for(label: str) -> float:
    if label == "UP":
        return 102.0
    if label == "DOWN":
        return 98.0
    return 100.2


def _outcome(
    *,
    episode_id: str,
    as_of: datetime,
    label: str,
    digest: str = SHA,
    supersedes_event_id: str | None = None,
) -> WorldOutcome:
    target = as_of + timedelta(days=1)
    return WorldOutcome(
        episode_id=episode_id,
        horizon={"horizon_id": "elapsed_1d.v1", "duration_seconds": 24 * 60 * 60},
        status="observed",
        target_at=target,
        available_at=target + timedelta(minutes=5),
        computed_at=target + timedelta(minutes=5),
        anchor_close=100.0,
        endpoint_close=_close_for(label),
        endpoint_bar_ts=target,
        source="analysis_bars",
        source_raw_sha256=digest,
        supersedes_event_id=supersedes_event_id,
    )


def _snapshot(
    market: WorldEpisode,
    *,
    structural: tuple[StructuralWorldRelation, ...],
    knowledge: tuple[KnowledgeWorldRelation, ...] = (),
    knowledge_receipt_id: str,
    status: str = "complete",
    cutoff_at: datetime | None = None,
) -> WorldGraphSnapshot:
    return WorldGraphSnapshot(
        root_episode_id=market.episode_id,
        root_entity=_instrument(market.observation.symbol),
        cutoff_at=cutoff_at or market.observation.as_of_bar_ts,
        ontology_revision="market_ontology.v1",
        ontology_hash=SHA,
        identity_map_hash="b" * 64,
        scope_mapping_id="world_scope_mapping.v1",
        scope_mapping_hash="c" * 64,
        structural_relation_refs=frozenset(WorldStructuralRelationRef.from_relation(item) for item in structural),
        knowledge_relation_refs=frozenset(
            WorldKnowledgeRelationRef.from_relation(item, availability_receipt_id=knowledge_receipt_id)
            for item in knowledge
        ),
        status=status,
    )


def _freeze_recorded(monkeypatch: pytest.MonkeyPatch, when: datetime) -> None:
    stamp = when.isoformat()
    monkeypatch.setattr("trader.infrastructure.state_db.world_graph_store._utc_now", lambda: stamp)
    monkeypatch.setattr("trader.infrastructure.state_db.world_model_store._utc_now", lambda: stamp)


def _activate_observes_fence(graph: WorldGraphStore):
    from tests.domain.test_world_graph import _reservation, _run_spec

    registry = graph.load("macro_graph_bridge.v1")
    updated = registry.activate(
        reservation=_reservation(),
        spec=_run_spec(),
        expected_version=registry.version,
    )
    if updated.version != registry.version:
        graph.append_event(updated.events[-1], expected_registry_version=registry.version, fence=None)
        return graph.load("macro_graph_bridge.v1")
    return updated


def _open_stores(db_path: Path, *, clock: datetime) -> tuple[WorldGraphStore, WorldModelStore]:
    graph = WorldGraphStore(db_path, clock=lambda: clock)
    world = WorldModelStore(graph._db, clock=lambda: clock)
    return graph, world


def _seed_labeled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    symbol: str = "2330",
    as_of: datetime = AS_OF,
    label: str = "UP",
    status: str = "complete",
    structural: StructuralWorldRelation | None = None,
    knowledge: KnowledgeWorldRelation | None = None,
    knowledge_receipt_override: str | None = None,
    relation_clock: datetime = RELATION_READY,
    snapshot_clock: datetime = SNAPSHOT_READY,
    recorded_at: datetime = RECORDED,
    persist_outcome: bool = True,
    close: bool = True,
    include_knowledge: bool = True,
) -> dict[str, object]:
    _freeze_recorded(monkeypatch, recorded_at)
    db_path = tmp_path / "world_model.db"
    graph, world = _open_stores(db_path, clock=relation_clock)
    market = _market_episode(symbol=symbol, as_of=as_of)
    world.append_episode(market)
    traded = structural or _structural(symbol=symbol)
    about = None if not include_knowledge else (knowledge or _about())
    structural_ref = graph.append_structural_relation_event(StructuralWorldRelationAsserted(relation=traded))
    graph._clock = lambda: relation_clock
    knowledge_ref = None
    receipt_id = knowledge_receipt_override
    if about is not None:
        fence = None
        expected_version = None
        if about.kind == "OBSERVES":
            registry = _activate_observes_fence(graph)
            fence = registry.fence
            expected_version = registry.version
        knowledge_ref = graph.append_knowledge_relation_event(
            KnowledgeWorldRelationAsserted(relation=about),
            fence=fence,
            expected_registry_version=expected_version,
        )
        receipt_id = knowledge_receipt_override or knowledge_ref.receipt.receipt_id
    snapshot = _snapshot(
        market,
        structural=(traded,),
        knowledge=() if about is None else (about,),
        knowledge_receipt_id=receipt_id or "",
        status=status,
        cutoff_at=as_of,
    )
    graph._clock = lambda: snapshot_clock
    snapshot_ref = graph.append(snapshot)
    episode = _graph_episode(market=market, snapshot=snapshot)
    world.append_episode(episode)
    outcome = _outcome(
        episode_id=episode.episode_id,
        as_of=as_of,
        label=label,
        digest=canonical_sha256({"symbol": symbol, "as_of": as_of.isoformat(), "label": label}),
    )
    if persist_outcome:
        world.append_outcome_event(outcome)
    if close:
        world.close()
        graph.close()
    return {
        "db_path": db_path,
        "graph": graph,
        "world": world,
        "market": market,
        "episode": episode,
        "snapshot": snapshot,
        "outcome": outcome,
        "structural": traded,
        "knowledge": about,
        "structural_event_id": str(structural_ref.identity),
        "knowledge_event_id": None if knowledge_ref is None else str(knowledge_ref.identity),
        "knowledge_receipt_id": None if knowledge_ref is None else knowledge_ref.receipt.receipt_id,
        "snapshot_receipt_id": snapshot_ref.receipt.receipt_id,
    }


def _seed_many(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    symbols: tuple[str, ...],
) -> Path:
    _freeze_recorded(monkeypatch, RECORDED)
    db_path = tmp_path / "world_model.db"
    graph, world = _open_stores(db_path, clock=RELATION_READY)
    about = _about()
    knowledge_ref = graph.append_knowledge_relation_event(KnowledgeWorldRelationAsserted(relation=about))
    for symbol in symbols:
        market = _market_episode(symbol=symbol, as_of=AS_OF)
        world.append_episode(market)
        traded = _structural(symbol=symbol)
        graph.append_structural_relation_event(StructuralWorldRelationAsserted(relation=traded))
        snapshot = _snapshot(
            market,
            structural=(traded,),
            knowledge=(about,),
            knowledge_receipt_id=knowledge_ref.receipt.receipt_id,
        )
        graph._clock = lambda: SNAPSHOT_READY
        graph.append(snapshot)
        episode = _graph_episode(market=market, snapshot=snapshot)
        world.append_episode(episode)
        world.append_outcome_event(
            _outcome(
                episode_id=episode.episode_id,
                as_of=AS_OF,
                label="UP",
                digest=canonical_sha256({"symbol": symbol, "as_of": AS_OF.isoformat()}),
            )
        )
    world.close()
    graph.close()
    return db_path


_READONLY_CONNECTION = formation_query._readonly_connection


def _count_selects(monkeypatch: pytest.MonkeyPatch, db_path: Path) -> int:
    statements: list[str] = []

    def traced(path: Path) -> sqlite3.Connection:
        connection = _READONLY_CONNECTION(path)
        connection.set_trace_callback(lambda sql: statements.append(sql))
        return connection

    monkeypatch.setattr(formation_query, "_readonly_connection", traced)
    batch = SqlitePatternFormationSource(db_path).load_formation_batch(_request())
    assert batch.records
    return sum(1 for sql in statements if sql.lstrip().upper().startswith("SELECT"))


def test_source_is_readonly_and_never_opens_stores_or_heavy_models() -> None:
    source = _QUERY.read_text(encoding="utf-8")
    assert "mode=ro" in source
    assert "uri=True" in source
    assert "query_only=ON" in source
    assert "immutable=1" not in source
    assert "StateDb(" not in source
    assert "WorldGraphStore(" not in source
    assert "WorldModelStore(" not in source
    assert "apply_current_world_model_schema" not in source
    assert "CREATE TABLE" not in source
    assert "INSERT " not in source
    assert "UPDATE " not in source
    assert "DELETE " not in source
    assert "networkx" not in source.lower()
    assert "gru" not in source.lower()
    assert "from trader.application.world_model.pattern_discovery import" not in source
    assert "pattern_discovery.py" not in source
    assert "WorldMacroStore(" not in source
    assert ".mkdir(" not in source
    assert "append_observation(" not in source


def test_missing_database_stays_absent_and_empty_schema_stays_unmigrated(tmp_path: Path) -> None:
    missing_dir = tmp_path / "absent"
    db_path = missing_dir / "world_model.db"
    batch = SqlitePatternFormationSource(db_path).load_formation_batch(_request())
    assert batch.records == ()
    assert batch.rejection_counts == {"missing_db": 1}
    assert not db_path.exists()
    assert not missing_dir.exists()

    schema = tmp_path / "world_model.db"
    sqlite3.connect(schema).close()
    before = schema.stat().st_mtime_ns
    before_sql = sqlite3.connect(schema).execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    batch = SqlitePatternFormationSource(schema).load_formation_batch(_request())
    assert batch.records == ()
    assert batch.rejection_counts == {"schema_unavailable": 1}
    assert schema.stat().st_mtime_ns == before
    after_sql = sqlite3.connect(schema).execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    assert after_sql == before_sql
    names = {str(row[0]) for row in after_sql}
    assert "world_episodes" not in names
    assert "world_graph_snapshots" not in names


def test_embedded_snapshot_join_ignores_decoy_root_episode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seeded = _seed_labeled(tmp_path, monkeypatch, close=False)
    db_path = seeded["db_path"]
    graph_store: WorldGraphStore = seeded["graph"]  # type: ignore[assignment]
    episode: WorldEpisode = seeded["episode"]  # type: ignore[assignment]
    snapshot: WorldGraphSnapshot = seeded["snapshot"]  # type: ignore[assignment]
    market: WorldEpisode = seeded["market"]  # type: ignore[assignment]
    decoy = WorldGraphSnapshot(
        root_episode_id=episode.episode_id,
        root_entity=_instrument("9999"),
        cutoff_at=AS_OF,
        ontology_revision="market_ontology.v1",
        ontology_hash="d" * 64,
        identity_map_hash="e" * 64,
        scope_mapping_id="world_scope_mapping.v1",
        scope_mapping_hash="f" * 64,
        status="complete",
    )
    graph_store.append(decoy)
    graph_store.close()
    batch = SqlitePatternFormationSource(db_path).load_formation_batch(_request())
    assert len(batch.records) == 1
    record = batch.records[0]
    assert record.episode.episode_id == episode.episode_id
    assert record.snapshot.snapshot_id == snapshot.snapshot_id
    assert record.snapshot.root_episode_id == market.episode_id
    assert record.snapshot.root_episode_id != record.episode.episode_id
    assert record.snapshot.snapshot_id != decoy.snapshot_id
    assert record.outcome.episode_id == episode.episode_id
    uri = f"file:{quote(str(db_path.resolve()), safe='/')}?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        wrong_root = connection.execute(
            "SELECT snapshot_id FROM world_graph_snapshots WHERE root_episode_id=?",
            (episode.episode_id,),
        ).fetchone()
        assert wrong_root is not None
        assert wrong_root[0] == decoy.snapshot_id


def test_active_outcome_leaf_ignores_successor_recorded_after_cutoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seeded = _seed_labeled(tmp_path, monkeypatch, close=False)
    world: WorldModelStore = seeded["world"]  # type: ignore[assignment]
    episode: WorldEpisode = seeded["episode"]  # type: ignore[assignment]
    first: WorldOutcome = seeded["outcome"]  # type: ignore[assignment]
    _freeze_recorded(monkeypatch, FORMATION + timedelta(days=1))
    successor = _outcome(
        episode_id=episode.episode_id,
        as_of=AS_OF,
        label="DOWN",
        digest="c" * 64,
        supersedes_event_id=first.event_id,
    )
    world.append_outcome_event(successor)
    world.close()
    seeded["graph"].close()  # type: ignore[union-attr]
    batch = SqlitePatternFormationSource(seeded["db_path"]).load_formation_batch(_request())
    assert len(batch.records) == 1
    assert batch.records[0].outcome.event_id == first.event_id
    assert batch.records[0].outcome.direction == "UP"


def test_successor_recorded_before_cutoff_becomes_active_leaf(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seeded = _seed_labeled(tmp_path, monkeypatch, close=False)
    world: WorldModelStore = seeded["world"]  # type: ignore[assignment]
    episode: WorldEpisode = seeded["episode"]  # type: ignore[assignment]
    first: WorldOutcome = seeded["outcome"]  # type: ignore[assignment]
    _freeze_recorded(monkeypatch, RECORDED + timedelta(hours=1))
    successor = _outcome(
        episode_id=episode.episode_id,
        as_of=AS_OF,
        label="DOWN",
        digest="c" * 64,
        supersedes_event_id=first.event_id,
    )
    world.append_outcome_event(successor)
    world.close()
    seeded["graph"].close()  # type: ignore[union-attr]
    batch = SqlitePatternFormationSource(seeded["db_path"]).load_formation_batch(_request())
    assert len(batch.records) == 1
    assert batch.records[0].outcome.event_id == successor.event_id
    assert batch.records[0].outcome.direction == "DOWN"


def test_snapshot_and_relation_receipts_must_be_proven_with_exact_hash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    late_snapshot = _seed_labeled(
        tmp_path / "late_snapshot",
        monkeypatch,
        snapshot_clock=FORMATION + timedelta(hours=1),
        recorded_at=RECORDED,
        symbol="2301",
    )
    late_batch = SqlitePatternFormationSource(late_snapshot["db_path"]).load_formation_batch(_request())
    assert late_batch.records == ()
    assert late_batch.rejection_counts.get("snapshot_receipt_unproven") or late_batch.rejection_counts.get(
        "snapshot_after_formation_cutoff"
    )

    late_relation = _seed_labeled(
        tmp_path / "late_relation",
        monkeypatch,
        symbol="2302",
        relation_clock=AS_OF + timedelta(hours=2),
        recorded_at=RECORDED,
    )
    late_rel_batch = SqlitePatternFormationSource(late_relation["db_path"]).load_formation_batch(_request())
    assert late_rel_batch.records
    assert late_rel_batch.records[0].knowledge_relations == ()
    assert late_rel_batch.rejection_counts["relation_unproven"] >= 1

    future_struct = _structural(symbol="2303", effective_from=AS_OF + timedelta(days=2))
    not_effective = _seed_labeled(
        tmp_path / "not_effective",
        monkeypatch,
        symbol="2303",
        structural=future_struct,
    )
    ineffective = SqlitePatternFormationSource(not_effective["db_path"]).load_formation_batch(_request())
    assert ineffective.records
    assert ineffective.records[0].structural_relations == ()
    assert ineffective.rejection_counts["relation_not_effective"] >= 1

    hashed = _seed_labeled(tmp_path / "hash", monkeypatch, symbol="2304", close=False)
    db_path = hashed["db_path"]
    traded: StructuralWorldRelation = hashed["structural"]  # type: ignore[assignment]
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO world_relation_events(
                event_id, event_type, family, relation_id, relation_kind, sequence,
                payload_json, payload_sha256, recorded_at
            ) VALUES (?, 'world_relation_asserted', 'structural', ?, 'TRADED_ON', 999,
                      '{}', ?, ?)
            """,
            ("decoy-event", traded.relation_id, "0" * 64, RECORDED.isoformat()),
        )
        connection.commit()
    hashed["graph"].close()  # type: ignore[union-attr]
    hashed["world"].close()  # type: ignore[union-attr]
    matched = SqlitePatternFormationSource(db_path).load_formation_batch(_request())
    assert matched.records
    assert [item.relation_id for item in matched.records[0].structural_relations] == [traded.relation_id]
    assert matched.records[0].structural_relations[0].content_sha256 == traded.content_sha256


def test_complete_and_partial_admitted_missing_and_stale_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    complete = _seed_labeled(tmp_path / "complete", monkeypatch, symbol="2330", status="complete")
    partial = _seed_labeled(tmp_path / "partial", monkeypatch, symbol="2331", status="partial")
    missing = _seed_labeled(tmp_path / "missing", monkeypatch, symbol="2332", status="missing")
    stale = _seed_labeled(tmp_path / "stale", monkeypatch, symbol="2333", status="stale")
    complete_batch = SqlitePatternFormationSource(complete["db_path"]).load_formation_batch(_request())
    partial_batch = SqlitePatternFormationSource(partial["db_path"]).load_formation_batch(_request())
    missing_batch = SqlitePatternFormationSource(missing["db_path"]).load_formation_batch(_request())
    stale_batch = SqlitePatternFormationSource(stale["db_path"]).load_formation_batch(_request())
    assert len(complete_batch.records) == 1
    assert complete_batch.records[0].snapshot.status == "complete"
    assert len(partial_batch.records) == 1
    assert partial_batch.records[0].snapshot.status == "partial"
    assert missing_batch.records == ()
    assert stale_batch.records == ()
    assert missing_batch.rejection_counts["snapshot_not_admitted"] == 1
    assert stale_batch.rejection_counts["snapshot_not_admitted"] == 1


def test_wal_committed_uncheckpointed_rows_are_visible_and_read_does_not_mutate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seeded = _seed_labeled(tmp_path, monkeypatch, close=False)
    db_path: Path = seeded["db_path"]  # type: ignore[assignment]
    wal_path = Path(str(db_path) + "-wal")
    before_db = (db_path.stat().st_size, db_path.stat().st_mtime_ns)
    before_wal = (wal_path.stat().st_size, wal_path.stat().st_mtime_ns) if wal_path.exists() else None
    batch = SqlitePatternFormationSource(db_path).load_formation_batch(_request())
    assert len(batch.records) == 1
    assert batch.records[0].episode.episode_id == seeded["episode"].episode_id  # type: ignore[union-attr]
    after_db = (db_path.stat().st_size, db_path.stat().st_mtime_ns)
    assert after_db == before_db
    if before_wal is not None:
        assert (wal_path.stat().st_size, wal_path.stat().st_mtime_ns) == before_wal
    source = _QUERY.read_text(encoding="utf-8")
    assert "immutable=1" not in source
    seeded["world"].close()  # type: ignore[union-attr]
    seeded["graph"].close()  # type: ignore[union-attr]


def test_happy_path_evidence_and_conservative_clocks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seeded = _seed_labeled(tmp_path, monkeypatch)
    batch = SqlitePatternFormationSource(seeded["db_path"]).load_formation_batch(_request())
    assert len(batch.records) == 1
    record = batch.records[0]
    assert record.structural_relations[0].relation_id == seeded["structural"].relation_id  # type: ignore[union-attr]
    assert record.knowledge_relations[0].relation_id == seeded["knowledge"].relation_id  # type: ignore[union-attr]
    assert record.recorded_at <= FORMATION
    assert record.available_at <= FORMATION
    evidence = set(batch.source_evidence_ids)
    assert seeded["episode"].episode_id in evidence  # type: ignore[union-attr]
    assert seeded["snapshot"].snapshot_id in evidence  # type: ignore[union-attr]
    assert seeded["outcome"].event_id in evidence  # type: ignore[union-attr]
    assert seeded["snapshot_receipt_id"] in evidence
    assert seeded["structural_event_id"] in evidence
    assert seeded["knowledge_event_id"] in evidence
    assert seeded["knowledge_receipt_id"] in evidence
    assert list(batch.source_evidence_ids) == sorted(batch.source_evidence_ids)
    assert SqlitePatternFormationSource(seeded["db_path"]).load_formation_batch(_request()) == batch


def test_wrong_request_type_is_a_contract_error(tmp_path: Path) -> None:
    with pytest.raises(TypeError, match="PatternFormationRequest"):
        SqlitePatternFormationSource(tmp_path / "world_model.db").load_formation_batch(object())  # type: ignore[arg-type]


def test_select_count_does_not_grow_with_eligible_row_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    small = _seed_many(tmp_path / "small", monkeypatch, ("2330", "2331", "2332"))
    large = _seed_many(tmp_path / "large", monkeypatch, tuple(f"23{index:02d}" for index in range(12)))
    small_selects = _count_selects(monkeypatch, small)
    large_selects = _count_selects(monkeypatch, large)
    assert small_selects == large_selects
    assert 3 <= small_selects <= 8
    assert SqlitePatternFormationSource(large).load_formation_batch(_request()).records
    assert len(SqlitePatternFormationSource(large).load_formation_batch(_request()).records) == 12


def test_non_observed_or_ineligible_leaf_is_not_payload_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pending_seed = _seed_labeled(tmp_path / "pending", monkeypatch, persist_outcome=False, close=False)
    pending = WorldOutcome(
        episode_id=pending_seed["episode"].episode_id,  # type: ignore[union-attr]
        horizon={"horizon_id": "elapsed_1d.v1", "duration_seconds": 24 * 60 * 60},
        status="pending",
        target_at=AS_OF + timedelta(days=1),
        training_eligible=False,
    )
    pending_seed["world"].append_outcome_event(pending)  # type: ignore[union-attr]
    pending_seed["world"].close()  # type: ignore[union-attr]
    pending_seed["graph"].close()  # type: ignore[union-attr]
    pending_batch = SqlitePatternFormationSource(pending_seed["db_path"]).load_formation_batch(_request())
    assert pending_batch.records == ()
    assert pending_batch.rejection_counts.get("outcome_payload_mismatch", 0) == 0
    assert pending_batch.rejection_counts["outcome_not_active_observed"] == 1

    ineligible_seed = _seed_labeled(tmp_path / "ineligible", monkeypatch, persist_outcome=False, close=False)
    observed = WorldOutcome(
        episode_id=ineligible_seed["episode"].episode_id,  # type: ignore[union-attr]
        horizon={"horizon_id": "elapsed_1d.v1", "duration_seconds": 24 * 60 * 60},
        status="observed",
        target_at=AS_OF + timedelta(days=1),
        available_at=AS_OF + timedelta(days=1, minutes=5),
        computed_at=AS_OF + timedelta(days=1, minutes=5),
        anchor_close=100.0,
        endpoint_close=100.2,
        endpoint_bar_ts=AS_OF + timedelta(days=1),
        source="analysis_bars",
        source_raw_sha256="d" * 64,
        training_eligible=False,
    )
    ineligible_seed["world"].append_outcome_event(observed)  # type: ignore[union-attr]
    ineligible_seed["world"].close()  # type: ignore[union-attr]
    ineligible_seed["graph"].close()  # type: ignore[union-attr]
    ineligible_batch = SqlitePatternFormationSource(ineligible_seed["db_path"]).load_formation_batch(_request())
    assert ineligible_batch.records == ()
    assert ineligible_batch.rejection_counts.get("outcome_payload_mismatch", 0) == 0
    assert ineligible_batch.rejection_counts["outcome_not_active_observed"] == 1


OBS_CUTOFF = datetime(2026, 8, 24, 13, 0, tzinfo=UTC)
OBS_UNTIL = datetime(2026, 8, 26, tzinfo=UTC)
OBS_READY = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)
LATE_READY = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)
_CLI = REPO_ROOT / "trader" / "interfaces" / "cli" / "world_model.py"


def _macro_scope() -> MacroScope:
    return MacroScope(kind="venue", entity_id="mic:XTAI")


def _macro_observation(*, rates_regime: str = "rising", **overrides: object) -> MacroWorldObservation:
    fact = MacroSourceFact(
        fact_kind="series_point",
        metric_key="policy_rate",
        scope=_macro_scope(),
        value=MacroNumericValue(number=4.25, unit="percent"),
        period="2026-08",
        occurred_at="2026-08-23T00:00:00Z",
        published_at="2026-08-23T12:30:00Z",
        ingested_at="2026-08-23T12:31:10Z",
        source=MacroFactSource(
            provider_id="dbnomics",
            adapter_version="world_dbnomics_series.v1",
            source_record_id=f"stable-{rates_regime}",
            source_ref="https://source.example/record",
        ),
    )
    fact_ref = fact.fact_version_id.value
    coverage = "unknown" if rates_regime == "unknown" else "complete"
    values: dict[str, object] = {
        "scope": _macro_scope(),
        "cutoff_at": OBS_CUTOFF,
        "producer_version": MACRO_PRODUCER_VERSION,
        "transform_version": MACRO_TRANSFORM_VERSION,
        "source_registry_version": MACRO_SOURCE_REGISTRY_VERSION,
        "fact_refs": (fact_ref,),
        "features": {"macro_regime": "unknown", "rates_regime": rates_regime, "usd_regime": "unknown"},
        "dimensions": (
            MacroDimensionState(
                dimension="macro_regime",
                value="unknown",
                coverage_status="unknown",
                method=MACRO_TRANSFORM_VERSION,
            ),
            MacroDimensionState(
                dimension="rates_regime",
                value=rates_regime,
                coverage_status=coverage,
                method=MACRO_TRANSFORM_VERSION,
                fact_refs=(fact_ref,),
            ),
            MacroDimensionState(
                dimension="usd_regime",
                value="unknown",
                coverage_status="unknown",
                method=MACRO_TRANSFORM_VERSION,
            ),
        ),
        "coverage": MacroCoverage(
            status="partial",
            required_sources=3,
            fresh_sources=1,
            missing_source_ids=("broad_usd_index", "cpi_yoy"),
        ),
        "valid_until": OBS_UNTIL,
    }
    values.update(overrides)
    return MacroWorldObservation(**values)  # type: ignore[arg-type]


def _observes_from_store(
    observation: MacroWorldObservation,
    macro_root: Path,
    *,
    ready_at: datetime,
) -> tuple[KnowledgeWorldRelation, object]:
    from tests.domain.test_world_graph import _revision_for_mapping, _scope_mapping

    envelope = WorldMacroStore(macro_root, clock=lambda: ready_at).append_observation(observation)
    mapping = _scope_mapping()
    link = MacroObservationKnowledgeLink.from_envelope(envelope, mapping, _revision_for_mapping(mapping))
    assert link.relation is not None
    return link.relation, envelope


def _source(db_path: Path, macro_root: Path | None = None) -> SqlitePatternFormationSource:
    return SqlitePatternFormationSource(db_path, macro_root=macro_root)


def _overlay_binding(record: object) -> object:
    return record.driver_state_bindings[0]  # type: ignore[attr-defined]


def test_cli_wires_state_world_macro_root() -> None:
    source = _CLI.read_text(encoding="utf-8")
    assert 'Path(state_dir) / "world_macro"' in source
    assert "SqlitePatternFormationSource(" in source
    assert "macro_root=" in source


def test_about_and_structural_records_get_honest_bindings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    about = _seed_labeled(tmp_path / "about", monkeypatch, symbol="2330")
    about_batch = _source(about["db_path"]).load_formation_batch(_request())  # type: ignore[arg-type]
    assert len(about_batch.records) == 1
    binding = _overlay_binding(about_batch.records[0])
    assert binding.driver_state == DriverState.unspecified(source_family="knowledge_artifact")
    assert about["knowledge"].source.artifact_id in binding.evidence_refs  # type: ignore[union-attr]
    assert "observation_id" not in binding.driver_state.to_dict()

    structural = _seed_labeled(
        tmp_path / "structural",
        monkeypatch,
        symbol="2331",
        include_knowledge=False,
    )
    structural_batch = _source(structural["db_path"]).load_formation_batch(_request())  # type: ignore[arg-type]
    assert len(structural_batch.records) == 1
    assert structural_batch.records[0].knowledge_relations == ()
    assert structural_batch.records[0].driver_state_bindings == ()
    assert structural_batch.records[0].structural_relations


def test_observes_exact_join_and_rising_versus_falling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rising_obs = _macro_observation(rates_regime="rising")
    falling_obs = _macro_observation(rates_regime="falling")
    rising_rel, rising_env = _observes_from_store(rising_obs, tmp_path / "world_macro", ready_at=OBS_READY)
    falling_rel, _falling_env = _observes_from_store(falling_obs, tmp_path / "world_macro", ready_at=OBS_READY)
    rising = _seed_labeled(tmp_path / "rising", monkeypatch, symbol="2330", knowledge=rising_rel)
    falling = _seed_labeled(tmp_path / "falling", monkeypatch, symbol="2331", knowledge=falling_rel)
    rising_batch = _source(rising["db_path"], tmp_path / "world_macro").load_formation_batch(_request())  # type: ignore[arg-type]
    falling_batch = _source(falling["db_path"], tmp_path / "world_macro").load_formation_batch(_request())  # type: ignore[arg-type]
    assert len(rising_batch.records) == 1
    assert len(falling_batch.records) == 1
    rising_binding = _overlay_binding(rising_batch.records[0])
    falling_binding = _overlay_binding(falling_batch.records[0])
    expected_rising = DriverState.from_observation(rising_obs)
    expected_falling = DriverState.from_observation(falling_obs)
    assert rising_binding.driver_state == expected_rising
    assert falling_binding.driver_state == expected_falling
    assert rising_binding.driver_state.identity_tuple() != falling_binding.driver_state.identity_tuple()
    assert rising_binding.driver_state.regimes.rates_regime == "rising"
    assert falling_binding.driver_state.regimes.rates_regime == "falling"
    assert rising_obs.observation_id in rising_binding.evidence_refs
    assert rising_env.persisted.receipt.receipt_id in rising_binding.evidence_refs
    assert "observation_id" not in rising_binding.driver_state.to_dict()
    assert reconstruct_macro_observes_provenance(rising_rel).observation_id == rising_obs.observation_id
    assert world_observation_ref_for_observation_id(rising_obs.observation_id) == rising_rel.source


def test_producer_unknown_is_not_unjoined_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    unknown_obs = _macro_observation(rates_regime="unknown")
    unknown_rel, _envelope = _observes_from_store(unknown_obs, tmp_path / "world_macro", ready_at=OBS_READY)
    unknown_seed = _seed_labeled(tmp_path / "unknown", monkeypatch, symbol="2330", knowledge=unknown_rel)
    missing_rel, _missing_env = _observes_from_store(
        _macro_observation(rates_regime="rising"),
        tmp_path / "other_macro",
        ready_at=OBS_READY,
    )
    missing_seed = _seed_labeled(tmp_path / "missing", monkeypatch, symbol="2331", knowledge=missing_rel)
    unknown_batch = _source(unknown_seed["db_path"], tmp_path / "world_macro").load_formation_batch(_request())  # type: ignore[arg-type]
    missing_batch = _source(missing_seed["db_path"], tmp_path / "world_macro").load_formation_batch(_request())  # type: ignore[arg-type]
    unknown_state = _overlay_binding(unknown_batch.records[0]).driver_state
    missing_state = _overlay_binding(missing_batch.records[0]).driver_state
    assert unknown_state == DriverState.from_observation(unknown_obs)
    assert unknown_state.signal_class == "regime_bundle"
    assert unknown_state.regimes.rates_regime == "unknown"
    assert unknown_state.missingness == "none"
    assert missing_state == DriverState.missing(
        missingness="observation_unjoined",
        artifact_kind="macro_world_observation",
    )
    assert unknown_state.identity_tuple() != missing_state.identity_tuple()


def test_hash_mismatch_late_receipt_missing_root_and_no_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.domain.test_world_graph import _revision_for_mapping, _scope_mapping

    observation = _macro_observation(rates_regime="rising")
    macro_root = tmp_path / "world_macro"
    relation, envelope = _observes_from_store(observation, macro_root, ready_at=OBS_READY)
    mapping = _scope_mapping()
    revision = _revision_for_mapping(mapping)
    tampered = KnowledgeWorldRelation(
        kind="OBSERVES",
        source=world_observation_ref_for_observation_id(observation.observation_id),
        target=WorldEntityRef(kind=observation.scope.kind, entity_id=observation.scope.entity_id),
        effective_from=observation.cutoff_at,
        effective_until=observation.valid_until,
        ontology_revision=revision.revision_id,
        source_refs=(
            *MacroObservationProvenance(
                producer_version=observation.producer_version,
                origin_scope=observation.scope,
                observation_id=observation.observation_id,
                observation_sha256="0" * 64,
                fact_refs=observation.fact_refs,
                mapping_id=mapping.mapping_id,
                mapping_sha256=mapping.content_sha256,
            ).to_source_refs(),
            f"{envelope.persisted.receipt.receipt_id}/{envelope.persisted.receipt.receipt_sha256}",
        ),
    )
    hashed = _seed_labeled(tmp_path / "hash", monkeypatch, symbol="2330", knowledge=tampered)
    hashed_batch = _source(hashed["db_path"], macro_root).load_formation_batch(_request())  # type: ignore[arg-type]
    assert _overlay_binding(hashed_batch.records[0]).driver_state.missingness == "observation_hash_mismatch"

    late_obs = _macro_observation(rates_regime="falling")
    late_rel, _late_env = _observes_from_store(late_obs, tmp_path / "late_macro", ready_at=LATE_READY)
    late = _seed_labeled(tmp_path / "late", monkeypatch, symbol="2332", knowledge=late_rel)
    late_batch = _source(late["db_path"], tmp_path / "late_macro").load_formation_batch(_request())  # type: ignore[arg-type]
    assert _overlay_binding(late_batch.records[0]).driver_state == DriverState.missing(
        missingness="observation_unjoined",
        artifact_kind="macro_world_observation",
    )

    absent = tmp_path / "absent_macro"
    missing_root = _source(hashed["db_path"], absent).load_formation_batch(_request())  # type: ignore[arg-type]
    assert not absent.exists()
    assert _overlay_binding(missing_root.records[0]).driver_state.missingness == "observation_unjoined"

    before_macro = {(path.relative_to(macro_root), path.stat().st_size, path.stat().st_mtime_ns) for path in macro_root.rglob("*") if path.is_file()}
    db_path: Path = hashed["db_path"]  # type: ignore[assignment]
    before_db = (db_path.stat().st_size, db_path.stat().st_mtime_ns)
    replay = _source(db_path, macro_root).load_formation_batch(_request())
    assert replay.records
    assert (db_path.stat().st_size, db_path.stat().st_mtime_ns) == before_db
    after_macro = {(path.relative_to(macro_root), path.stat().st_size, path.stat().st_mtime_ns) for path in macro_root.rglob("*") if path.is_file()}
    assert after_macro == before_macro
    assert relation.kind == "OBSERVES"
