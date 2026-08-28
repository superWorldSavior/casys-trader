"""Append-only SQLite pattern ledger: events, outcome links, receipts, crash, restart."""

from __future__ import annotations

import inspect
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from trader.application.world_model.pattern_lifecycle_ports import PatternLifecycleLedger
from trader.application.world_model.pattern_ports import (
    OccurrenceEventEnvelope,
    PatternHypothesisEventEnvelope,
    PatternPayloadConflict,
)
from trader.domain.world_availability import AvailabilityEvidence
from trader.domain.world_episode import (
    MARKET_FEATURE_CONTRACT_ID,
    WorldOutcome,
    WorldPrediction,
    canonical_sha256,
)
from trader.domain.world_feature_contract import WorldFeatureContract, WorldFeatureGroup, WorldFeatureMask
from trader.domain.world_graph import WorldEntityRef
from trader.domain.world_pattern import (
    PATTERN_ASSOCIATION_METRIC,
    PatternDiscoveryCompleted,
    PatternEvaluationStarted,
    PatternFormationStats,
    PatternHypothesis,
    PatternHypothesisId,
    PatternHypothesisRegistered,
    PatternHypothesisSpec,
    PatternMatchedHop,
    PatternOccurrence,
    PatternOccurrenceId,
    PatternOccurrenceRecorded,
    PatternOutcomeLinkSuperseded,
    PatternOutcomeLinked,
    PatternStep,
    PatternTarget,
)
from trader.infrastructure.state_db.world_model_store import (
    WORLD_MODEL_MIGRATIONS,
    WorldModelConflictError,
    WorldModelStore,
)
from trader.infrastructure.state_db.world_pattern_store import (
    WORLD_PATTERN_TABLES,
    WorldPatternStore,
)


UTC = timezone.utc
FORMATION = datetime(2026, 9, 1, tzinfo=UTC)
EVAL_NOT_BEFORE = datetime(2026, 9, 2, tzinfo=UTC)
EVAL_STARTED = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)
CUTOFF = datetime(2026, 9, 2, 13, 0, tzinfo=UTC)
OUTCOME_AT = datetime(2026, 9, 3, tzinfo=UTC)
READY = datetime(2026, 9, 1, 0, 5, tzinfo=UTC)
BOOT = datetime(2026, 9, 5, tzinfo=UTC)
LATER = datetime(2026, 9, 6, tzinfo=UTC)
EPISODE_ID = f"world-episode:v1:{'b' * 64}"
FORMATION_FP = canonical_sha256({"dataset": "formation-pilot"})
EVAL_FP = canonical_sha256({"dataset": "prospective-confirm"})
SOURCE_SHA = "c" * 64
COHORT_ID = "world_cohort:v1:" + "c" * 64

_PATTERN_TABLES = (
    "world_pattern_hypothesis_events",
    "world_pattern_lifecycle_events",
    "world_pattern_occurrence_events",
    "world_pattern_outcome_links",
)
DISCOVERY_STARTED = f"world_cohort_event:v1:{canonical_sha256({'started': 'discovery'})}"
DISCOVERY_MANIFEST = canonical_sha256({"manifest": "pattern-discovery"})


def _contract() -> WorldFeatureContract:
    return WorldFeatureContract(
        contract_id="world_feature.graph.v1",
        accepted_episode_contract=MARKET_FEATURE_CONTRACT_ID,
        projection_version="graph_projection.v3",
        encoder_identity="world_feature_encoder.graph.v1",
        groups=(
            WorldFeatureGroup(
                group_id="market",
                categorical_features=frozenset({"venue"}),
                numeric_features=frozenset({"return"}),
            ),
        ),
        ontology_revision="market_ontology.v1",
        vocabulary_version="graph_vocab.v3",
        path_rule_version="macro_path_rule.v1",
    )


def _mask(contract: WorldFeatureContract | None = None) -> WorldFeatureMask:
    resolved = contract if contract is not None else _contract()
    return WorldFeatureMask.bind(resolved, mask_id="graph_content.v1", selected_groups=("market",))


def _step(
    ordinal: int,
    *,
    source_kind: str,
    relation_kind: str,
    target_kind: str,
    direction: str = "forward",
    freshness_bucket: str = "0-4h",
) -> PatternStep:
    return PatternStep(
        ordinal=ordinal,
        source_kind=source_kind,
        relation_kind=relation_kind,
        direction=direction,
        target_kind=target_kind,
        freshness_bucket=freshness_bucket,
        evidence_rule_version="macro_path_rule.v1" if ordinal == 0 else "market_ontology.v1",
    )


def _stats(**overrides: object) -> PatternFormationStats:
    values: dict[str, object] = {
        "support": 7,
        "population_support": 7,
        "class_counts": {"DOWN": 1, "FLAT": 2, "UP": 4},
        "population_class_counts": {"DOWN": 1, "FLAT": 2, "UP": 4},
        "smoothing_alpha": 1.0,
        "association_metric": PATTERN_ASSOCIATION_METRIC,
        "association_score": 0.0,
    }
    values.update(overrides)
    return PatternFormationStats(**values)  # type: ignore[arg-type]


def _spec(**overrides: object) -> PatternHypothesisSpec:
    contract = _contract()
    mask = _mask(contract)
    values: dict[str, object] = {
        "evaluation_start_not_before": EVAL_NOT_BEFORE,
        "target": PatternTarget(
            entity_kind="instrument",
            horizon_id="elapsed_1d.v1",
            move_distribution={"DOWN": 0.20, "FLAT": 0.30, "UP": 0.50},
        ),
        "steps": (
            _step(0, source_kind="instrument", relation_kind="TRADED_ON", target_kind="venue"),
            _step(1, source_kind="venue", relation_kind="LOCATED_IN", target_kind="country", freshness_bucket="4-24h"),
        ),
        "formation_cutoff": FORMATION,
        "formation_dataset_fingerprint": FORMATION_FP,
        "feature_contract_id": contract.contract_id,
        "feature_contract_fingerprint": contract.fingerprint,
        "feature_mask_id": mask.mask_id,
        "feature_mask_fingerprint": mask.fingerprint,
        "model_identity": "online_gru_world_challenger@graph.v1",
        "ontology_revision": "market_ontology.v1",
        "stats": _stats(),
        "source_refs": (),
        "causal_claim": False,
    }
    values.update(overrides)
    return PatternHypothesisSpec(**values)  # type: ignore[arg-type]


def _registered(**overrides: object) -> PatternHypothesisRegistered:
    return PatternHypothesis.register(_spec(**overrides), registered_at=FORMATION).registered


def _evaluating(registered: PatternHypothesisRegistered | None = None) -> PatternHypothesis:
    hypothesis = PatternHypothesis.from_events((registered if registered is not None else _registered(),))
    return hypothesis.start_evaluation(
        started_at=EVAL_STARTED,
        evaluation_dataset_fingerprint=EVAL_FP,
        evaluation_cohort_id=COHORT_ID,
    )


def _instrument() -> WorldEntityRef:
    return WorldEntityRef(kind="instrument", entity_id="mic:XTAI:symbol:2330")


def _path() -> tuple[PatternMatchedHop, PatternMatchedHop]:
    steps = (
        _step(0, source_kind="instrument", relation_kind="TRADED_ON", target_kind="venue"),
        _step(1, source_kind="venue", relation_kind="LOCATED_IN", target_kind="country", freshness_bucket="4-24h"),
    )
    refs = (("macro_source_fact_version:v1:" + "d" * 64,), ("world_observation:v1:" + "e" * 64,))
    return tuple(
        PatternMatchedHop(
            ordinal=step.ordinal,
            source_kind=step.source_kind,
            relation_kind=step.relation_kind,
            direction=step.direction,
            target_kind=step.target_kind,
            freshness_bucket=step.freshness_bucket,
            evidence_rule_version=step.evidence_rule_version,
            evidence_refs=evidence,
        )
        for step, evidence in zip(steps, refs, strict=True)
    )


def _prediction(**overrides: object) -> WorldPrediction:
    values: dict[str, object] = {
        "episode_id": EPISODE_ID,
        "horizon_id": "elapsed_1d.v1",
        "model_id": "online_gru_world_challenger@graph.v1",
        "model_version": "graph.v1",
        "feature_hash": "feature-view",
        "created_at": CUTOFF,
        "probabilities": {"DOWN": 0.20, "FLAT": 0.30, "UP": 0.50},
        "status": "shadow_only",
    }
    values.update(overrides)
    return WorldPrediction(**values)  # type: ignore[arg-type]


def _recorded(hypothesis: PatternHypothesis | None = None, **overrides: object) -> PatternOccurrenceRecorded:
    resolved = hypothesis if hypothesis is not None else _evaluating()
    values: dict[str, object] = {
        "cohort_id": COHORT_ID,
        "instrument": _instrument(),
        "cutoff_at": CUTOFF,
        "exact_path": _path(),
        "forecast": _prediction(),
        "artifact_refs": ("knowledge_artifact:v1:" + "a" * 64,),
        "fact_refs": ("macro_source_fact_version:v1:" + "d" * 64,),
    }
    values.update(overrides)
    return PatternOccurrence.record(resolved, **values).recorded  # type: ignore[arg-type]


def _outcome(**overrides: object) -> WorldOutcome:
    values: dict[str, object] = {
        "episode_id": EPISODE_ID,
        "horizon": {"horizon_id": "elapsed_1d.v1", "duration_seconds": 24 * 60 * 60},
        "status": "observed",
        "target_at": OUTCOME_AT,
        "available_at": datetime(2026, 9, 3, 0, 5, tzinfo=UTC),
        "computed_at": datetime(2026, 9, 3, 0, 5, tzinfo=UTC),
        "anchor_close": 100.0,
        "endpoint_close": 102.0,
        "endpoint_bar_ts": OUTCOME_AT,
        "source": "analysis_bars",
        "source_raw_sha256": SOURCE_SHA,
    }
    values.update(overrides)
    return WorldOutcome(**values)  # type: ignore[arg-type]


def _store(tmp_path: Path, *, clock: datetime = READY) -> WorldPatternStore:
    return WorldPatternStore(tmp_path / "world_model.db", clock=lambda: clock)


def test_ports_surface_has_no_caller_ready_at_or_expected_sequence() -> None:
    append_event = inspect.signature(WorldPatternStore.append_event)
    load = inspect.signature(WorldPatternStore.load)
    evidence_for = inspect.signature(WorldPatternStore.evidence_for)
    assert list(append_event.parameters) == ["self", "event"]
    assert "expected_sequence" not in append_event.parameters
    assert "ready_at" not in append_event.parameters
    assert "first_seen_at" not in append_event.parameters
    assert "clock" not in append_event.parameters
    assert list(load.parameters) == ["self", "identity"]
    assert list(evidence_for.parameters) == ["self", "event"]
    assert "ready_at" not in evidence_for.parameters
    assert not hasattr(WorldPatternStore, "set_status")
    append_discovery = inspect.signature(WorldPatternStore.append_discovery_completed)
    get_discovery = inspect.signature(WorldPatternStore.get_discovery_completed)
    assert list(append_discovery.parameters) == ["self", "event"]
    assert list(get_discovery.parameters) == ["self", "evaluation_cohort_id", "started_event_id"]
    assert "ready_at" not in append_discovery.parameters
    assert "clock" not in append_discovery.parameters
    assert PatternLifecycleLedger.__module__ == "trader.application.world_model.pattern_lifecycle_ports"
    assert not hasattr(PatternLifecycleLedger, "evidence_for")


def test_append_hypothesis_assigns_store_ready_at_after_subject_commit(tmp_path: Path) -> None:
    path = tmp_path / "world_model.db"
    holder: dict[str, WorldPatternStore] = {}
    order: list[str] = []

    def clock() -> datetime:
        store = holder["store"]
        order.append("clock")
        assert store._db.query_one("SELECT event_id FROM world_pattern_hypothesis_events") is not None
        assert store._db.query_one("SELECT receipt_id FROM world_availability_receipts") is None
        return READY

    store = WorldPatternStore(path, clock=clock)
    holder["store"] = store
    event = _registered()
    envelope = store.append_event(event)
    assert order == ["clock"]
    assert isinstance(envelope, PatternHypothesisEventEnvelope)
    assert envelope.event.event_id == event.event_id
    assert envelope.availability_status == "eligible"
    evidence = envelope.require_proven()
    assert isinstance(evidence, AvailabilityEvidence)
    assert evidence.receipt.ready_at == READY
    assert evidence.first_seen_at == READY
    assert evidence.first_seen_at == evidence.receipt.ready_at
    assert evidence.receipt.storage_locator.kind == "sqlite"
    assert evidence.receipt.storage_locator.table == "world_pattern_hypothesis_events"
    assert evidence.receipt.storage_locator.row_id == event.event_id
    assert evidence.receipt.subject.kind == "pattern_hypothesis_event"
    assert evidence.receipt.subject.content_sha256 == canonical_sha256(event.to_dict())
    loaded = store.load(PatternHypothesisId(event.hypothesis_id))
    assert loaded.hypothesis_id == event.hypothesis_id
    assert loaded.status == "registered"
    assert store.evidence_for(event) is not None
    assert store.evidence_for(event).receipt.receipt_id == evidence.receipt.receipt_id


def test_crash_before_subject_commit_leaves_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = _store(tmp_path)
    event = _registered()

    def boom(*_args: object, **_kwargs: object) -> Any:
        raise OSError("subject commit failed")

    monkeypatch.setattr(store, "_persist_event", boom)
    with pytest.raises(OSError, match="subject commit failed"):
        store.append_event(event)
    assert store._db.query_one("SELECT COUNT(*) FROM world_pattern_hypothesis_events")[0] == 0
    assert store._db.query_one("SELECT COUNT(*) FROM world_availability_receipts")[0] == 0
    with pytest.raises(LookupError):
        store.load(PatternHypothesisId(event.hypothesis_id))
    assert store.evidence_for(event) is None


def test_crash_between_subject_and_receipt_is_unproven_until_idempotent_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "world_model.db"
    store = WorldPatternStore(path, clock=lambda: READY)
    event = _registered()

    def boom(*_args: object, **_kwargs: object) -> Any:
        raise OSError("receipt commit failed")

    monkeypatch.setattr(store, "_commit_availability_receipt", boom)
    with pytest.raises(OSError, match="receipt commit failed"):
        store.append_event(event)
    assert store._db.query_one("SELECT COUNT(*) FROM world_pattern_hypothesis_events")[0] == 1
    assert store._db.query_one("SELECT COUNT(*) FROM world_availability_receipts")[0] == 0
    loaded = store.load(PatternHypothesisId(event.hypothesis_id))
    assert loaded.status == "registered"
    assert store.evidence_for(event) is None

    monkeypatch.undo()
    retried = WorldPatternStore(path, clock=lambda: READY)
    recovered = retried.append_event(event)
    assert recovered.availability_status == "eligible"
    assert recovered.require_proven().receipt.ready_at == READY
    assert retried._db.query_one("SELECT COUNT(*) FROM world_pattern_hypothesis_events")[0] == 1
    assert retried._db.query_one("SELECT COUNT(*) FROM world_availability_receipts")[0] == 1
    replay = retried.append_event(event)
    assert replay.require_proven().receipt.receipt_id == recovered.require_proven().receipt.receipt_id
    assert replay.require_proven().receipt.ready_at == READY


def test_identical_event_repairs_missing_receipt_after_head_advances(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "world_model.db"
    store = WorldPatternStore(path, clock=lambda: READY)
    registered = _registered()
    started = PatternEvaluationStarted(
        hypothesis_id=registered.hypothesis_id,
        evaluation_cohort_id=COHORT_ID,
        started_at=EVAL_STARTED,
        evaluation_dataset_fingerprint=EVAL_FP,
    )

    def boom(*_args: object, **_kwargs: object) -> Any:
        raise OSError("receipt commit failed")

    monkeypatch.setattr(store, "_commit_availability_receipt", boom)
    with pytest.raises(OSError, match="receipt commit failed"):
        store.append_event(registered)
    assert store._db.query_one("SELECT COUNT(*) FROM world_pattern_hypothesis_events")[0] == 1
    assert store._db.query_one("SELECT COUNT(*) FROM world_availability_receipts")[0] == 0
    assert store.evidence_for(registered) is None

    monkeypatch.undo()
    advanced = store.append_event(started)
    assert isinstance(advanced, PatternHypothesisEventEnvelope)
    assert store._db.query_one("SELECT COUNT(*) FROM world_pattern_hypothesis_events")[0] == 2
    sequences = [
        int(row["sequence"])
        for row in store._db.query_all("SELECT sequence FROM world_pattern_hypothesis_events ORDER BY sequence")
    ]
    assert sequences == [1, 2]
    assert store.evidence_for(started) is not None
    assert store.evidence_for(registered) is None

    repaired = store.append_event(registered)
    assert repaired.event.event_id == registered.event_id
    assert repaired.require_proven().receipt.ready_at == READY
    assert store._db.query_one("SELECT COUNT(*) FROM world_pattern_hypothesis_events")[0] == 2
    assert store._db.query_one("SELECT COUNT(*) FROM world_availability_receipts")[0] == 2
    first_row = store._db.query_one(
        "SELECT sequence FROM world_pattern_hypothesis_events WHERE event_id=?",
        (registered.event_id,),
    )
    assert int(first_row["sequence"]) == 1
    loaded = store.load(PatternHypothesisId(registered.hypothesis_id))
    assert [item.event_type for item in loaded.events] == [
        "pattern_hypothesis_registered",
        "pattern_evaluation_started",
    ]


def test_same_event_id_different_payload_is_typed_conflict(tmp_path: Path) -> None:
    store = _store(tmp_path)
    first = _registered()
    store.append_event(first)
    with store._db.transaction() as cur:
        cur.execute("DROP TRIGGER world_pattern_hypothesis_events_no_update")
        cur.execute(
            "UPDATE world_pattern_hypothesis_events SET payload_json=?, payload_sha256=? WHERE event_id=?",
            ('{"event_type":"tampered"}', "0" * 64, first.event_id),
        )
        cur.execute(
            """
            CREATE TRIGGER world_pattern_hypothesis_events_no_update
            BEFORE UPDATE ON world_pattern_hypothesis_events
            BEGIN
                SELECT RAISE(ABORT, 'world_pattern_hypothesis_events are append-only');
            END
            """
        )
    with pytest.raises(PatternPayloadConflict, match="different canonical content|different payload"):
        store.append_event(first)


def test_new_event_id_does_not_insert_when_unique_sequence_conflicts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    registered = _registered()
    store.append_event(registered)
    started = PatternEvaluationStarted(
        hypothesis_id=registered.hypothesis_id,
        evaluation_cohort_id=COHORT_ID,
        started_at=EVAL_STARTED,
        evaluation_dataset_fingerprint=EVAL_FP,
    )
    original = store._db.transaction

    class _CursorProxy:
        def __init__(self, inner: sqlite3.Cursor) -> None:
            self._inner = inner

        def execute(self, sql: str, params: tuple[Any, ...] = ()) -> Any:
            if "COUNT(*)" in sql:

                class _CountResult:
                    def fetchone(self) -> tuple[int, ...]:
                        return (0,)

                return _CountResult()
            return self._inner.execute(sql, params)

        def __getattr__(self, name: str) -> Any:
            return getattr(self._inner, name)

    def stale_count_transaction():
        from contextlib import contextmanager

        @contextmanager
        def wrapped():
            with original() as cur:
                yield _CursorProxy(cur)

        return wrapped()

    monkeypatch.setattr(store._db, "transaction", stale_count_transaction)
    with pytest.raises(WorldModelConflictError, match="sequence"):
        store.append_event(started)
    monkeypatch.undo()
    assert store._db.query_one("SELECT COUNT(*) FROM world_pattern_hypothesis_events")[0] == 1
    assert (
        store._db.query_one(
            "SELECT COUNT(*) FROM world_pattern_hypothesis_events WHERE event_id=?",
            (started.event_id,),
        )[0]
        == 0
    )
    recovered = store.append_event(started)
    assert recovered.event.event_id == started.event_id
    assert store._db.query_one("SELECT COUNT(*) FROM world_pattern_hypothesis_events")[0] == 2


def test_restart_first_seen_is_conservative_and_does_not_mutate_ready_at(tmp_path: Path) -> None:
    path = tmp_path / "world_model.db"
    writer = WorldPatternStore(path, clock=lambda: READY)
    event = _registered()
    written = writer.append_event(event)
    assert written.require_proven().first_seen_at == READY
    assert written.require_proven().receipt.ready_at == READY
    writer.close()

    restarted = WorldPatternStore(path, clock=lambda: BOOT)
    loaded = restarted.load(PatternHypothesisId(event.hypothesis_id))
    assert loaded.status == "registered"
    seen = restarted.evidence_for(event)
    assert seen is not None
    assert seen.first_seen_at == BOOT
    assert seen.effective_ready_at == BOOT
    assert seen.receipt.ready_at == READY
    replayed = restarted.append_event(event)
    evidence = replayed.require_proven()
    assert evidence.first_seen_at == BOOT
    assert evidence.effective_ready_at == BOOT
    assert evidence.receipt.ready_at == READY
    again = restarted.append_event(event)
    assert again.require_proven().first_seen_at == BOOT
    assert again.require_proven().receipt.ready_at == READY
    assert restarted._db.query_one("SELECT COUNT(*) FROM world_pattern_hypothesis_events")[0] == 1


def test_occurrence_and_outcome_link_round_trip_with_cohort_cutoff_horizon_indexes(tmp_path: Path) -> None:
    store = _store(tmp_path)
    registered = store.append_event(_registered())
    evaluating = _evaluating(registered.event)
    store.append_event(evaluating.events[-1])
    recorded = _recorded(evaluating)
    occurrence_envelope = store.append_event(recorded)
    assert isinstance(occurrence_envelope, OccurrenceEventEnvelope)
    assert occurrence_envelope.require_proven().receipt.subject.kind == "pattern_occurrence_event"
    assert occurrence_envelope.require_proven().receipt.storage_locator.table == "world_pattern_occurrence_events"
    loaded_occurrence = store.load(PatternOccurrenceId(recorded.occurrence_id))
    assert loaded_occurrence.occurrence_id == recorded.occurrence_id
    assert loaded_occurrence.spec.cohort_id == COHORT_ID
    assert loaded_occurrence.spec.cutoff_at == CUTOFF

    outcome = _outcome()
    linked = loaded_occurrence.link_outcome(outcome)
    link_event = linked.events[-1]
    assert isinstance(link_event, PatternOutcomeLinked)
    linked_envelope = store.append_event(link_event)
    assert isinstance(linked_envelope.event, PatternOutcomeLinked)
    row = store._db.query_one(
        "SELECT occurrence_id, horizon_id, world_outcome_event_id FROM world_pattern_outcome_links WHERE link_id=?",
        (link_event.link.link_id,),
    )
    assert row["occurrence_id"] == recorded.occurrence_id
    assert row["horizon_id"] == "elapsed_1d.v1"
    assert row["world_outcome_event_id"] == outcome.event_id

    corrected = _outcome(endpoint_close=103.0, supersedes_event_id=outcome.event_id)
    reloaded = store.load(PatternOccurrenceId(recorded.occurrence_id))
    superseded = reloaded.supersede_outcome_link(corrected)
    supersede_event = superseded.events[-1]
    assert isinstance(supersede_event, PatternOutcomeLinkSuperseded)
    store.append_event(supersede_event)
    assert store._db.query_one("SELECT COUNT(*) FROM world_pattern_outcome_links")[0] == 2
    reconstructed = store.load(PatternOccurrenceId(recorded.occurrence_id))
    assert reconstructed.active_outcome_link("elapsed_1d.v1").world_outcome_event_id == corrected.event_id
    assert reconstructed.outcome_links[-1].supersedes_link_id == link_event.link.link_id

    occ_row = store._db.query_one(
        "SELECT cohort_id, cutoff_at, horizon_id FROM world_pattern_occurrence_events WHERE event_id=?",
        (recorded.event_id,),
    )
    assert occ_row["cohort_id"] == COHORT_ID
    assert "2026-09-02T13:00:00" in occ_row["cutoff_at"]
    assert occ_row["horizon_id"] == "elapsed_1d.v1"
    indexes = {row["name"] for row in store._db.query_all("SELECT name FROM sqlite_master WHERE type='index'")}
    assert any("cohort" in name and "cutoff" in name for name in indexes)
    assert any("horizon" in name for name in indexes)


def test_sequences_are_per_aggregate_and_replay_keeps_original_sequence(tmp_path: Path) -> None:
    store = _store(tmp_path)
    first = _registered()
    second = _registered(formation_dataset_fingerprint="d" * 64)
    assert first.hypothesis_id != second.hypothesis_id
    store.append_event(first)
    store.append_event(second)
    started = PatternEvaluationStarted(
        hypothesis_id=first.hypothesis_id,
        evaluation_cohort_id=COHORT_ID,
        started_at=EVAL_STARTED,
        evaluation_dataset_fingerprint=EVAL_FP,
    )
    store.append_event(started)
    store.append_event(first)
    rows = store._db.query_all(
        "SELECT hypothesis_id, sequence FROM world_pattern_hypothesis_events ORDER BY hypothesis_id, sequence"
    )
    by_hypothesis: dict[str, list[int]] = {}
    for row in rows:
        by_hypothesis.setdefault(row["hypothesis_id"], []).append(int(row["sequence"]))
    assert by_hypothesis[first.hypothesis_id] == [1, 2]
    assert by_hypothesis[second.hypothesis_id] == [1]


def test_unique_sequence_rejects_duplicate_head_for_same_aggregate(tmp_path: Path) -> None:
    store = _store(tmp_path)
    event = _registered()
    store.append_event(event)
    with pytest.raises(sqlite3.IntegrityError):
        with store._db.transaction() as cur:
            cur.execute(
                """
                INSERT INTO world_pattern_hypothesis_events(
                    event_id, hypothesis_id, event_type, sequence,
                    payload_json, payload_sha256, recorded_at
                ) VALUES (?, ?, 'pattern_hypothesis_invalidated', 1, '{}', ?, ?)
                """,
                ("pattern_hypothesis_event:v1:" + "f" * 64, event.hypothesis_id, "1" * 64, READY.isoformat()),
            )


def test_anti_update_delete_triggers_cover_pattern_tables(tmp_path: Path) -> None:
    store = _store(tmp_path)
    registered = store.append_event(_registered())
    evaluating = _evaluating(registered.event)
    store.append_event(evaluating.events[-1])
    recorded = _recorded(evaluating)
    store.append_event(recorded)
    occurrence = store.load(PatternOccurrenceId(recorded.occurrence_id))
    store.append_event(occurrence.link_outcome(_outcome()).events[-1])
    store.append_discovery_completed(_discovery_completed())
    mutations = (
        "UPDATE world_pattern_hypothesis_events SET event_type='tampered'",
        "DELETE FROM world_pattern_hypothesis_events",
        "UPDATE world_pattern_occurrence_events SET event_type='tampered'",
        "DELETE FROM world_pattern_occurrence_events",
        "UPDATE world_pattern_outcome_links SET horizon_id='tampered'",
        "DELETE FROM world_pattern_outcome_links",
        "UPDATE world_pattern_lifecycle_events SET event_type='tampered'",
        "DELETE FROM world_pattern_lifecycle_events",
        "UPDATE world_availability_receipts SET scope='tampered'",
        "DELETE FROM world_availability_receipts",
    )
    for sql in mutations:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            with store._db.transaction() as cur:
                cur.execute(sql)


def test_concurrent_identical_append_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "world_model.db"
    setup = WorldPatternStore(path, clock=lambda: READY)
    event = _registered()
    setup.close()
    barrier = threading.Barrier(2)
    outcomes: list[str] = []
    lock = threading.Lock()

    def worker() -> None:
        barrier.wait()
        local = WorldPatternStore(path, clock=lambda: READY)
        try:
            envelope = local.append_event(event)
            with lock:
                outcomes.append(envelope.event.event_id)
        finally:
            local.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = (executor.submit(worker), executor.submit(worker))
        for future in futures:
            future.result()

    assert outcomes == [event.event_id, event.event_id]
    verifier = WorldPatternStore(path, clock=lambda: READY)
    try:
        assert verifier._db.query_one("SELECT COUNT(*) FROM world_pattern_hypothesis_events")[0] == 1
        assert verifier._db.query_one("SELECT COUNT(*) FROM world_availability_receipts")[0] == 1
    finally:
        verifier.close()


def test_migration_creates_append_only_pattern_schema_and_reuses_receipts(tmp_path: Path) -> None:
    store = _store(tmp_path)
    versions = {row["version"] for row in store._db.query_all("SELECT version FROM schema_migrations")}
    assert versions == {1}
    names = {row["name"] for row in store._db.query_all("SELECT name FROM sqlite_master WHERE type='table'")}
    for table in _PATTERN_TABLES:
        assert table in names
        assert table in WORLD_PATTERN_TABLES
    assert "world_availability_receipts" in names
    current_sql = "\n".join(WORLD_MODEL_MIGRATIONS[0][1])
    for table in _PATTERN_TABLES:
        assert table in current_sql
        assert f"{table}_no_update" in current_sql
        assert f"{table}_no_delete" in current_sql
    assert "CREATE TABLE IF NOT EXISTS world_availability_receipts" in current_sql
    assert "idx_world_pattern_occurrence_events_cohort_cutoff" in current_sql
    assert "idx_world_pattern_outcome_links_horizon" in current_sql
    assert "idx_world_pattern_lifecycle_events_cohort_start" in current_sql
    assert "idx_world_pattern_lifecycle_events_evaluation_ready" in current_sql
    compact = current_sql.replace(" ", "").replace("\n", "")
    assert "UNIQUE(hypothesis_id,sequence)" in compact
    assert "UNIQUE(occurrence_id,sequence)" in compact
    assert "UNIQUE(evaluation_cohort_id,started_event_id)" in compact
    store.append_event(_registered())
    for table in ("world_pattern_hypothesis_events",):
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            with store._db.transaction() as cur:
                cur.execute(f"UPDATE {table} SET recorded_at='tampered'")  # noqa: S608


def test_world_model_store_applies_pattern_migration_without_owning_pattern_api(tmp_path: Path) -> None:
    model = WorldModelStore(tmp_path / "world_model.db")
    try:
        names = {row["name"] for row in model._db.query_all("SELECT name FROM sqlite_master WHERE type='table'")}
        for table in _PATTERN_TABLES:
            assert table in names
        assert not hasattr(model, "append_event") or WorldModelStore.append_event is not WorldPatternStore.append_event
        assert not hasattr(model, "evidence_for")
    finally:
        model.close()


def test_shared_receipt_table_does_not_mix_pattern_and_graph_subjects(tmp_path: Path) -> None:
    path = tmp_path / "world_model.db"
    pattern = WorldPatternStore(path, clock=lambda: READY)
    pattern.append_event(_registered())
    receipt = pattern._db.query_one("SELECT subject_kind FROM world_availability_receipts")
    assert receipt["subject_kind"] == "pattern_hypothesis_event"
    assert pattern._db.query_one("SELECT COUNT(*) FROM world_entity_events")[0] == 0
    assert pattern._db.query_one("SELECT COUNT(*) FROM world_cohort_events")[0] == 0


def test_occurrence_cannot_link_before_recorded_event(tmp_path: Path) -> None:
    store = _store(tmp_path)
    registered = store.append_event(_registered())
    evaluating = _evaluating(registered.event)
    store.append_event(evaluating.events[-1])
    recorded = _recorded(evaluating)
    occurrence = PatternOccurrence.from_events((recorded,))
    link_event = occurrence.link_outcome(_outcome()).events[-1]
    with pytest.raises(LookupError):
        store.append_event(link_event)
    assert store._db.query_one("SELECT COUNT(*) FROM world_pattern_occurrence_events")[0] == 0
    assert store._db.query_one("SELECT COUNT(*) FROM world_pattern_outcome_links")[0] == 0


def test_never_seals_receipt_for_conflicting_payload(tmp_path: Path) -> None:
    store = _store(tmp_path)
    first = _registered()
    store.append_event(first)
    other = _registered(source_refs=("pattern:other",))
    with store._db.transaction() as cur:
        cur.execute("DROP TRIGGER world_pattern_hypothesis_events_no_update")
        cur.execute(
            "UPDATE world_pattern_hypothesis_events SET event_id=? WHERE event_id=?",
            (other.event_id, first.event_id),
        )
        cur.execute(
            """
            CREATE TRIGGER world_pattern_hypothesis_events_no_update
            BEFORE UPDATE ON world_pattern_hypothesis_events
            BEGIN
                SELECT RAISE(ABORT, 'world_pattern_hypothesis_events are append-only');
            END
            """
        )
    with pytest.raises(PatternPayloadConflict):
        store.append_event(other)
    hashes = [
        row["content_sha256"]
        for row in store._db.query_all(
            "SELECT content_sha256 FROM world_availability_receipts WHERE subject_id=?",
            (other.event_id,),
        )
    ]
    assert canonical_sha256(other.to_dict()) not in hashes


def test_evaluating_hypothesis_is_listed_with_zero_occurrences(tmp_path: Path) -> None:
    store = _store(tmp_path)
    hypothesis = _evaluating()
    store.append_event(hypothesis.registered)
    started = next(event for event in hypothesis.events if isinstance(event, PatternEvaluationStarted))
    store.append_event(started)
    listed = store.list_evaluating_hypotheses(
        evaluation_cohort_id=COHORT_ID,
        evaluation_dataset_fingerprint=EVAL_FP,
    )
    assert [item.hypothesis_id for item in listed] == [hypothesis.hypothesis_id]
    assert store.list_recorded_occurrences(evaluation_cohort_id=COHORT_ID) == ()
    from trader.infrastructure.state_db.world_pattern_catalog_query import SqlitePatternCatalogQuery

    catalog = SqlitePatternCatalogQuery(store.path)
    assert [
        item.hypothesis_id
        for item in catalog.list_evaluating_hypotheses(
            evaluation_cohort_id=COHORT_ID,
            evaluation_dataset_fingerprint=EVAL_FP,
        )
    ] == [hypothesis.hypothesis_id]
    assert catalog.list_recorded_occurrences(evaluation_cohort_id=COHORT_ID) == ()
    other = _evaluating(_registered(formation_dataset_fingerprint=canonical_sha256({"dataset": "other-formation"})))
    store.append_event(other.registered)
    store.append_event(next(event for event in other.events if isinstance(event, PatternEvaluationStarted)))
    statements: list[str] = []
    original = store._db.query_all

    def traced(sql: str, params: tuple = ()) -> list:
        statements.append(sql)
        return original(sql, params)

    store._db.query_all = traced  # type: ignore[method-assign]
    selected = store.list_evaluating_hypotheses(
        evaluation_cohort_id=COHORT_ID,
        evaluation_dataset_fingerprint=EVAL_FP,
        hypothesis_ids=(hypothesis.hypothesis_id,),
    )
    assert [item.hypothesis_id for item in selected] == [hypothesis.hypothesis_id]
    compact = [" ".join(sql.split()) for sql in statements]
    assert any("hypothesis_id IN" in sql for sql in compact)
    assert not any(sql.startswith("SELECT DISTINCT hypothesis_id") for sql in compact)
    store.close()


def _discovery_completed(**overrides: object) -> PatternDiscoveryCompleted:
    values: dict[str, object] = {
        "evaluation_cohort_id": COHORT_ID,
        "manifest_sha256": DISCOVERY_MANIFEST,
        "formation_dataset_fingerprint": FORMATION_FP,
        "evaluation_dataset_fingerprint": EVAL_FP,
        "started_event_id": DISCOVERY_STARTED,
        "formation_cutoff": FORMATION,
        "evaluation_start_not_before": EVAL_NOT_BEFORE,
        "selected_hypothesis_ids": (),
        "selected_count": 0,
    }
    values.update(overrides)
    return PatternDiscoveryCompleted(**values)  # type: ignore[arg-type]


def test_discovery_completed_zero_candidate_append_get_and_exact_replay(tmp_path: Path) -> None:
    store = _store(tmp_path)
    event = _discovery_completed()
    assert event.selected_count == 0
    assert event.selected_hypothesis_ids == ()
    assert store.append_discovery_completed(event) is True
    loaded = store.get_discovery_completed(event.evaluation_cohort_id, event.started_event_id)
    assert loaded == event
    assert loaded is not None
    assert loaded.selected_count == 0
    assert store.append_discovery_completed(event) is False
    assert store.append_discovery_completed(PatternDiscoveryCompleted.from_mapping(event.to_dict())) is False
    assert store._db.query_one("SELECT COUNT(*) FROM world_pattern_lifecycle_events")[0] == 1
    assert store._db.query_one("SELECT COUNT(*) FROM world_availability_receipts")[0] == 0
    assert store.get_discovery_completed("world_cohort:v1:" + "d" * 64, DISCOVERY_STARTED) is None


def test_discovery_completed_same_cohort_start_different_payload_conflicts(tmp_path: Path) -> None:
    store = _store(tmp_path)
    first = _discovery_completed()
    assert store.append_discovery_completed(first) is True
    other = _discovery_completed(manifest_sha256=canonical_sha256({"manifest": "other"}))
    assert other.evaluation_cohort_id == first.evaluation_cohort_id
    assert other.started_event_id == first.started_event_id
    assert other.event_id != first.event_id
    with pytest.raises(PatternPayloadConflict, match="different canonical content|different payload"):
        store.append_discovery_completed(other)
    assert store.get_discovery_completed(first.evaluation_cohort_id, first.started_event_id) == first
    assert store._db.query_one("SELECT COUNT(*) FROM world_pattern_lifecycle_events")[0] == 1
    assert store._db.query_one("SELECT COUNT(*) FROM world_availability_receipts")[0] == 0


def test_discovery_completed_same_event_id_different_payload_conflicts(tmp_path: Path) -> None:
    store = _store(tmp_path)
    event = _discovery_completed()
    store.append_discovery_completed(event)
    with store._db.transaction() as cur:
        cur.execute("DROP TRIGGER world_pattern_lifecycle_events_no_update")
        cur.execute(
            "UPDATE world_pattern_lifecycle_events SET payload_json=?, payload_sha256=? WHERE event_id=?",
            ('{"event_type":"tampered"}', "0" * 64, event.event_id),
        )
        cur.execute(
            """
            CREATE TRIGGER world_pattern_lifecycle_events_no_update
            BEFORE UPDATE ON world_pattern_lifecycle_events
            BEGIN
                SELECT RAISE(ABORT, 'world_pattern_lifecycle_events are append-only');
            END
            """
        )
    with pytest.raises(PatternPayloadConflict, match="different canonical content|different payload"):
        store.append_discovery_completed(event)


def test_discovery_completed_direct_sql_update_and_delete_are_blocked(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.append_discovery_completed(_discovery_completed())
    for sql in (
        "UPDATE world_pattern_lifecycle_events SET selected_count=1",
        "DELETE FROM world_pattern_lifecycle_events",
    ):
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            with store._db.transaction() as cur:
                cur.execute(sql)


def test_discovery_completed_get_detects_payload_hash_and_index_tamper(tmp_path: Path) -> None:
    store = _store(tmp_path)
    event = _discovery_completed()
    store.append_discovery_completed(event)

    def _restore_trigger(cur: sqlite3.Cursor) -> None:
        cur.execute(
            """
            CREATE TRIGGER world_pattern_lifecycle_events_no_update
            BEFORE UPDATE ON world_pattern_lifecycle_events
            BEGIN
                SELECT RAISE(ABORT, 'world_pattern_lifecycle_events are append-only');
            END
            """
        )

    with store._db.transaction() as cur:
        cur.execute("DROP TRIGGER world_pattern_lifecycle_events_no_update")
        cur.execute(
            "UPDATE world_pattern_lifecycle_events SET payload_sha256=? WHERE event_id=?",
            ("0" * 64, event.event_id),
        )
        _restore_trigger(cur)
    with pytest.raises(ValueError, match="tamper"):
        store.get_discovery_completed(event.evaluation_cohort_id, event.started_event_id)

    with store._db.transaction() as cur:
        cur.execute("DROP TRIGGER world_pattern_lifecycle_events_no_update")
        cur.execute(
            "UPDATE world_pattern_lifecycle_events SET payload_sha256=?, selected_count=? WHERE event_id=?",
            (canonical_sha256(event.to_dict()), 99, event.event_id),
        )
        _restore_trigger(cur)
    with pytest.raises(ValueError, match="tamper"):
        store.get_discovery_completed(event.evaluation_cohort_id, event.started_event_id)


def test_discovery_completed_survives_close_and_reopen(tmp_path: Path) -> None:
    path = tmp_path / "world_model.db"
    writer = WorldPatternStore(path, clock=lambda: READY)
    event = _discovery_completed()
    assert writer.append_discovery_completed(event) is True
    writer.close()

    restarted = WorldPatternStore(path, clock=lambda: BOOT)
    loaded = restarted.get_discovery_completed(event.evaluation_cohort_id, event.started_event_id)
    assert loaded == event
    assert restarted.append_discovery_completed(event) is False
    assert restarted._db.query_one("SELECT COUNT(*) FROM world_availability_receipts")[0] == 0
    indexes = {row["name"] for row in restarted._db.query_all("SELECT name FROM sqlite_master WHERE type='index'")}
    assert "idx_world_pattern_lifecycle_events_cohort_start" in indexes
    assert "idx_world_pattern_lifecycle_events_evaluation_ready" in indexes
    restarted.close()
