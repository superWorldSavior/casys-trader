"""Contract tests for the isolated immutable world-model SQLite ledger."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path

import pytest

from trader.domain.world_episode import (
    MARKET_FEATURE_CONTRACT_VERSION,
    AnchorBar,
    OutcomeHorizon,
    WorldEpisode,
    WorldObservation,
    WorldOutcome,
    WorldPrediction,
)
from trader.infrastructure.state_db.connection import StateDb
from trader.infrastructure.state_db.world_model_store import (
    WORLD_MODEL_MIGRATIONS,
    WorldModelConflictError,
    WorldModelStore,
)


def _episode(episode_id: str = "episode-1", **overrides: object) -> dict[str, object]:
    episode: dict[str, object] = {
        "episode_id": episode_id,
        "capture_id": "capture-2026-08-22T00:00:00Z",
        "observed_at": "2026-08-22T00:00:00+00:00",
        "available_at": "2026-08-22T00:00:00+00:00",
        "venue": "US",
        "symbol": "AAPL",
        "bar_interval": "1h",
        "as_of_bar_ts": "2026-08-22T00:00:00+00:00",
        "feature_contract_version": "world-features-v1",
        "sampling_policy_version": "fresh-active-v1",
        "training_eligible": True,
        "training_reason": "fresh_pre_dispatch",
        "observation": {
            "symbol": "AAPL",
            "as_of_bar_ts": "2026-08-22T00:00:00+00:00",
            "features": {"return_1h": 0.01, "volume_z": 0.2},
        },
        "source_evidence": {
            "bars": [
                {"ts": "2026-08-21T23:00:00+00:00", "close": 100.0},
                {"ts": "2026-08-22T00:00:00+00:00", "close": 101.0},
            ],
            "source": "fixture",
        },
    }
    episode.update(overrides)
    return episode


def _outcome(
    outcome_event_id: str = "outcome-4h-v1",
    *,
    episode_id: str = "episode-1",
    **overrides: object,
) -> dict[str, object]:
    outcome: dict[str, object] = {
        "outcome_event_id": outcome_event_id,
        "episode_id": episode_id,
        "horizon_code": "4h",
        "label_schema_version": "world-label-v1",
        "status": "observed",
        "move_class": "up",
        "label_available_at": "2026-08-22T04:00:00+00:00",
        "sealed_at": "2026-08-22T04:01:00+00:00",
        "label": {"forward_return": 0.03, "move_class": "up"},
        "evidence": {
            "initial_bar": {"ts": "2026-08-22T00:00:00+00:00", "close": 101.0},
            "target_bar": {"ts": "2026-08-22T04:00:00+00:00", "close": 104.03},
        },
    }
    outcome.update(overrides)
    return outcome


def _prediction(prediction_id: str = "prediction-1", **overrides: object) -> dict[str, object]:
    prediction: dict[str, object] = {
        "prediction_id": prediction_id,
        "run_id": "markov-run-1",
        "episode_id": "episode-1",
        "model_kind": "markov",
        "model_version": "markov-v1",
        "horizon_id": "elapsed_4h.v1",
        "predicted_at": "2026-08-22T00:00:00+00:00",
        "input": {"return_1h": 0.01, "volume_z": 0.2},
        "prediction": {
            "p_up": 0.6,
            "p_flat": 0.3,
            "p_down": 0.1,
            "probabilities": {"up": 0.6, "flat": 0.3, "down": 0.1},
            "status": "ok",
            "support": 12,
            "backoff_tier": "symbol",
        },
    }
    prediction.update(overrides)
    return prediction


@pytest.fixture()
def store(tmp_path: Path) -> WorldModelStore:
    return WorldModelStore(tmp_path / "world_model.db")


def test_episode_exact_replay_is_noop_and_content_change_fails_closed(store: WorldModelStore) -> None:
    episode = _episode()

    assert store.append_episode(episode) is True
    assert store.append_episode(deepcopy(episode)) is False

    stored = store.get_episode("episode-1")
    assert stored is not None
    assert stored["training_eligible"] is True
    assert stored["episode"]["observation"]["features"]["return_1h"] == 0.01

    conflicting = deepcopy(episode)
    conflicting["observation"]["features"]["return_1h"] = -0.01  # type: ignore[index]
    with pytest.raises(WorldModelConflictError, match="different canonical content"):
        store.append_episode(conflicting)


def test_immutable_triggers_reject_update_and_delete(store: WorldModelStore) -> None:
    assert store.append_episode(_episode())
    assert store.append_legacy_outcome_event(_outcome())
    assert store.append_legacy_prediction(_prediction())

    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        with store._db.transaction() as cur:
            cur.execute("UPDATE world_episodes SET symbol='MSFT' WHERE episode_id='episode-1'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        with store._db.transaction() as cur:
            cur.execute("DELETE FROM world_outcome_events WHERE outcome_event_id='outcome-4h-v1'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        with store._db.transaction() as cur:
            cur.execute("UPDATE world_shadow_predictions SET run_id='other' WHERE prediction_id='prediction-1'")


def test_world_store_is_separate_wal_database_with_healthy_integrity(tmp_path: Path) -> None:
    production = StateDb(tmp_path / "casys.db")
    production.executescript("CREATE TABLE production_only (id INTEGER PRIMARY KEY)")
    try:
        with pytest.raises(ValueError, match="dedicated world_model.db"):
            WorldModelStore(tmp_path / "casys.db")
        with pytest.raises(ValueError, match="dedicated world_model.db"):
            WorldModelStore(production)

        store = WorldModelStore(tmp_path / "world_model.db")
        try:
            assert store.append_episode(_episode())
            assert store.path != production.path
            assert production.query_one("SELECT name FROM sqlite_master WHERE name='world_episodes'") is None
            assert store._db.query_one("PRAGMA journal_mode")[0] == "wal"
            assert store.integrity_check() == ["ok"]
            assert store.counts() == {"episodes": 1, "outcome_events": 0, "predictions": 0}
        finally:
            store.close()
    finally:
        production.close()


def test_store_accepts_supplied_dedicated_state_db_without_owning_its_lifecycle(tmp_path: Path) -> None:
    db = StateDb(tmp_path / "provided-world.db")
    store = WorldModelStore(db)

    assert store.append_episode(_episode())
    store.close()

    # ``close`` must not tear down a daemon-owned StateDb supplied by the caller.
    assert db.query_one("SELECT COUNT(*) FROM world_episodes")[0] == 1
    db.close()


def test_concurrent_replays_leave_exactly_one_episode(tmp_path: Path) -> None:
    db_path = tmp_path / "world_model.db"
    episode = _episode()

    def append_once(_: int) -> bool:
        local_store = WorldModelStore(db_path)
        try:
            return local_store.append_episode(deepcopy(episode))
        finally:
            local_store.close()

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(append_once, range(8)))

    verifier = WorldModelStore(db_path)
    try:
        assert sum(results) == 1
        assert verifier.counts()["episodes"] == 1
    finally:
        verifier.close()


def test_outcome_revisions_are_retained_and_pending_query_is_horizon_specific(store: WorldModelStore) -> None:
    assert store.append_episode(_episode())
    first = _outcome()
    second = _outcome(
        "outcome-4h-v2",
        label_schema_version="world-label-v2",
        label={
            "forward_return": 0.029,
            "move_class": "up",
            # Runtime keeps the complete label nested in its immutable
            # envelope, including a correction link if supplied by a labeler.
            "supersedes_event_id": "outcome-4h-v1",
        },
        evidence={"provider_revision": "2026-08-23", "target_close": 103.929},
    )

    assert store.append_legacy_outcome(first) is True
    assert store.append_legacy_outcome(deepcopy(first)) is False
    assert store.append_legacy_outcome_event(second) is True

    rows = store.list_outcome_events(episode_id="episode-1", horizon_code="4h")
    assert [row["outcome_event_id"] for row in rows] == ["outcome-4h-v1", "outcome-4h-v2"]
    assert rows[1]["supersedes_outcome_event_id"] == "outcome-4h-v1"
    assert store.list_pending_episodes(horizon_code="4h") == []
    assert [row["episode_id"] for row in store.list_pending_episodes(horizon_code="1d")] == ["episode-1"]
    assert [row["episode_id"] for row in store.list_pending_episodes(horizon_id="1d")] == ["episode-1"]
    assert [row["outcome_event_id"] for row in store.list_observed_outcomes(horizon_id="4h")] == [
        "outcome-4h-v2",
    ]
    assert [
        row["outcome_event_id"] for row in store.list_observed_outcomes(horizon_id="4h", include_superseded=True)
    ] == ["outcome-4h-v1", "outcome-4h-v2"]

    conflict = deepcopy(first)
    conflict["label"]["forward_return"] = -0.03  # type: ignore[index]
    with pytest.raises(WorldModelConflictError):
        store.append_legacy_outcome_event(conflict)

    with pytest.raises(ValueError, match="unknown outcome_event_id"):
        store.append_legacy_outcome_event(
            _outcome("outcome-invalid-revision", supersedes_outcome_event_id="missing-revision")
        )


def test_pending_requires_an_active_observation_leaf(store: WorldModelStore) -> None:
    assert store.append_episode(_episode())
    assert store.append_legacy_outcome_event(
        _outcome(
            "outcome-inferred",
            training_eligible=False,
            label={"move_class": "up", "availability_provenance": "inferred"},
        )
    )
    # Outcome terminality and baseline trainability are separate: an observed
    # audit-only label must not be recomputed on every wake.
    assert store.list_pending_episodes(horizon_code="4h") == []

    assert store.append_legacy_outcome_event(
        _outcome(
            "outcome-explicit",
            supersedes_outcome_event_id="outcome-inferred",
            training_eligible=True,
        )
    )
    assert store.list_pending_episodes(horizon_code="4h") == []

    assert store.append_legacy_outcome_event(
        _outcome(
            "outcome-invalidated",
            supersedes_outcome_event_id="outcome-explicit",
            status="pending",
            training_eligible=False,
            move_class=None,
            label={"reason": "provider_correction_unavailable"},
        )
    )
    assert [row["episode_id"] for row in store.list_pending_episodes(horizon_code="4h")] == ["episode-1"]


def test_predictions_are_immutable_and_listed_deterministically(store: WorldModelStore) -> None:
    assert store.append_episode(_episode())
    prediction = _prediction()

    assert store.append_legacy_prediction(prediction) is True
    assert store.append_legacy_prediction(deepcopy(prediction)) is False
    rows = store.list_predictions(run_id="markov-run-1")
    assert len(rows) == 1
    assert rows[0]["prediction_id"] == "prediction-1"
    assert rows[0]["prediction"]["p_up"] == 0.6
    assert rows[0]["horizon_code"] == "elapsed_4h.v1"
    assert [row["prediction_id"] for row in store.list_predictions(horizon_id="elapsed_4h.v1")] == ["prediction-1"]

    conflict = deepcopy(prediction)
    conflict["prediction"]["p_up"] = 0.5  # type: ignore[index]
    with pytest.raises(WorldModelConflictError, match="different canonical content"):
        store.append_legacy_prediction(conflict)

    no_horizon = _prediction("prediction-without-horizon")
    no_horizon.pop("horizon_id")
    with pytest.raises(ValueError, match="requires horizon_code"):
        store.append_legacy_prediction(no_horizon)


def test_store_accepts_domain_records_and_preserves_domain_ids(store: WorldModelStore) -> None:
    observation = WorldObservation(
        venue="US",
        symbol="MSFT",
        bar_interval="1h",
        as_of_bar_ts="2026-08-22T00:00:00+00:00",
        feature_contract_version="world-features-v1",
        sampling_policy_version="fresh-active-v1",
        anchor=AnchorBar(
            ts="2026-08-22T00:00:00+00:00",
            open=100.0,
            high=102.0,
            low=99.0,
            close=101.0,
            volume=1_000.0,
            source="fixture",
        ),
        available_at="2026-08-22T00:00:00+00:00",
        captured_at="2026-08-22T00:01:00+00:00",
        freshness="fresh",
        categorical_features={"venue": "US"},
        numeric_features={"return_1h": 0.01},
    )
    episode = WorldEpisode(observation=observation)
    horizon = OutcomeHorizon("elapsed_4h.v1", 4 * 60 * 60)
    outcome = WorldOutcome(
        episode_id=episode.episode_id,
        horizon=horizon,
        status="observed",
        target_at="2026-08-22T04:00:00+00:00",
        available_at="2026-08-22T04:00:00+00:00",
        computed_at="2026-08-22T04:01:00+00:00",
        anchor_close=101.0,
        endpoint_close=104.03,
        endpoint_bar_ts="2026-08-22T04:00:00+00:00",
        source="fixture",
        source_raw_sha256="a" * 64,
    )
    prediction = WorldPrediction(
        episode_id=episode.episode_id,
        horizon_id=horizon.horizon_id,
        model_id="markov",
        model_version="markov-v1",
        feature_hash=observation.feature_hash,
        predicted_return=0.02,
        created_at="2026-08-22T00:01:00+00:00",
    )

    assert store.append_episode(episode)
    assert store.append_outcome(outcome)
    assert store.append_prediction(prediction)

    stored_episode = store.get_episode(episode.episode_id)
    assert stored_episode is not None
    assert stored_episode["payload_sha256"] == f"sha256:{episode.payload_hash}"
    outcomes = store.list_observed_outcomes(horizon_code=horizon.horizon_id)
    assert outcomes[0]["outcome_event_id"] == outcome.event_id
    assert outcomes[0]["label"]["simple_return"] == pytest.approx(outcome.simple_return)
    predictions = store.list_predictions(run_id="markov")
    assert predictions[0]["prediction_id"] == prediction.prediction_id
    assert predictions[0]["prediction"]["predicted_return"] == 0.02
    assert predictions[0]["input_sha256"] == f"sha256:{observation.feature_hash}"


def test_top_level_baseline_prediction_metadata_and_direction_are_preserved(store: WorldModelStore) -> None:
    assert store.append_episode(_episode())
    assert store.append_legacy_outcome(
        _outcome(
            direction="up",
            move_class=None,
        )
    )
    assert store.append_legacy_prediction(
        _prediction(
            prediction_id="prediction-baseline-shape",
            prediction=None,
            probabilities={"up": 0.7, "flat": 0.2, "down": 0.1},
            status="no_go",
            support=3,
            backoff_tier="global",
            tier="global",
            global_support=3,
            training_cutoff="2026-08-22T00:00:00+00:00",
            model_fingerprint="markov:fixture",
            authority="shadow_only",
            decision_effect="none",
        )
    )

    outcome = store.list_observed_outcomes()[0]
    assert outcome["move_class"] == "UP"
    prediction = store.list_predictions(run_id="markov-run-1")[-1]
    assert prediction["prediction"]["probabilities"]["up"] == 0.7
    assert prediction["prediction"]["status"] == "no_go"
    assert prediction["prediction"]["support"] == 3
    assert prediction["prediction"]["backoff_tier"] == "global"
    assert prediction["prediction"]["tier"] == "global"
    assert prediction["prediction"]["training_cutoff"] == "2026-08-22T00:00:00+00:00"
    assert prediction["prediction"]["model_fingerprint"] == "markov:fixture"
    assert prediction["prediction"]["authority"] == "shadow_only"
    assert prediction["prediction"]["decision_effect"] == "none"


def test_live_append_rejects_invalid_canonical_outcome_and_prediction(store: WorldModelStore) -> None:
    assert store.append_episode(_episode())

    with pytest.raises(ValueError, match="move_class must be one of"):
        store.append_outcome_event(_outcome())
    with pytest.raises(ValueError, match="immutable source evidence"):
        store.append_outcome_event(
            _outcome(
                move_class="UP",
                training_eligible=True,
                label={"move_class": "UP", "forward_return": 0.03},
            )
        )
    with pytest.raises(ValueError, match="status must be one of"):
        store.append_prediction(_prediction())
    with pytest.raises(ValueError, match="probabilities must contain exactly"):
        store.append_prediction(
            _prediction(
                prediction={
                    "probabilities": {"up": 0.6, "flat": 0.3, "down": 0.1},
                    "status": "shadow_only",
                    "recommendation": "NO_GO",
                    "authority": "shadow_only",
                    "decision_effect": "none",
                }
            )
        )

    live_outcome = _outcome(
        "live-outcome-1",
        move_class="UP",
        training_eligible=True,
        source_raw_sha256="b" * 64,
        label={
            "move_class": "UP",
            "forward_return": 0.03,
            "source_raw_sha256": "b" * 64,
        },
        evidence={"source_raw_sha256": "b" * 64, "source": "fixture"},
    )
    live_prediction = _prediction(
        "live-prediction-1",
        prediction={
            "probabilities": {"DOWN": 0.1, "FLAT": 0.3, "UP": 0.6},
            "predicted_class": "UP",
            "status": "shadow_only",
            "recommendation": "NO_GO",
            "authority": "shadow_only",
            "decision_effect": "none",
        },
    )
    assert store.append_outcome_event(live_outcome) is True
    assert store.append_prediction(live_prediction) is True
    assert store.list_observed_outcomes()[0]["move_class"] == "UP"


def test_legacy_append_keeps_historical_mappings_readable_as_canonical_classes(
    store: WorldModelStore,
) -> None:
    assert store.append_episode(_episode())
    assert store.append_legacy_outcome_event(_outcome()) is True
    assert store.append_legacy_prediction(_prediction()) is True

    outcome = store.list_observed_outcomes()[0]
    assert outcome["move_class"] == "UP"
    assert outcome["label"]["move_class"] == "UP"
    prediction = store.list_predictions()[0]
    assert prediction["prediction"]["probabilities"]["up"] == 0.6
    assert prediction["prediction"]["status"] == "ok"


def _v2_pair():
    from trader.application.world_model.context_capture import attach_world_context
    from trader.domain.world_context import EntityRef, KnowledgeArtifact, SensorEvidence
    from tests.application.test_world_context_capture import _FakeSource, _v1_episode

    v1 = _v1_episode()
    missing = SensorEvidence(status="missing", reason="no_artifact")
    artifact = KnowledgeArtifact(
        kind="company_intelligence",
        artifact_id="aaa-complete",
        subjects=[EntityRef("instrument", "AAA")],
        schema_version="company_intelligence_brief.v1",
        content_sha256="abc",
        ready_at="2026-08-22T09:00:00+00:00",
    )
    complete = SensorEvidence(
        status="complete",
        reason="sidecar_ready",
        proven=True,
        payload={
            "symbol": "AAA",
            "company_thesis": {"status": "intact"},
            "coverage": {"status": "partial"},
            "source_refs": ["src-a"],
        },
        artifact=artifact,
    )
    first = attach_world_context((v1,), _FakeSource(missing, missing))[0]
    second = attach_world_context((v1,), _FakeSource(missing, complete))[0]
    return first, second


def test_v2_market_slot_keeps_one_canonical_episode_and_conflicts_on_reuse(store: WorldModelStore) -> None:
    first, second = _v2_pair()
    assert first.episode_id != second.episode_id
    assert store.append_episode(first) is True
    assert store.append_episode(first.to_dict()) is False
    with pytest.raises(WorldModelConflictError, match="V2 market slot"):
        store.append_episode(second)
    stored = store.get_episode_by_v2_slot(
        venue=first.observation.venue,
        symbol=first.observation.symbol,
        bar_interval=first.observation.bar_interval,
        as_of_bar_ts=first.observation.as_of_bar_ts.isoformat(),
        feature_contract_version=first.observation.feature_contract_version,
        sampling_policy_version=first.observation.sampling_policy_version,
    )
    assert stored is not None
    assert stored["episode_id"] == first.episode_id
    assert store.counts()["episodes"] == 1


def test_v2_slot_lookup_is_preserved_after_reopen(tmp_path: Path) -> None:
    first, second = _v2_pair()
    db_path = tmp_path / "world_model.db"
    store = WorldModelStore(db_path)
    try:
        assert store.append_episode(first) is True
    finally:
        store.close()
    restarted = WorldModelStore(db_path)
    try:
        stored = restarted.get_episode_by_v2_slot(
            venue=first.observation.venue,
            symbol=first.observation.symbol,
            bar_interval=first.observation.bar_interval,
            as_of_bar_ts=first.observation.as_of_bar_ts.isoformat(),
            feature_contract_version=first.observation.feature_contract_version,
            sampling_policy_version=first.observation.sampling_policy_version,
        )
        assert stored is not None
        assert stored["episode_id"] == first.episode_id
        with pytest.raises(WorldModelConflictError, match="V2 market slot"):
            restarted.append_episode(second)
    finally:
        restarted.close()


def _replace_offset_with_z(value: object) -> object:
    if isinstance(value, dict):
        return {key: _replace_offset_with_z(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_replace_offset_with_z(item) for item in value]
    if isinstance(value, str) and value.endswith("+00:00"):
        return value[: -len("+00:00")] + "Z"
    return value


def _insert_raw_v2_episode(
    db: StateDb,
    *,
    episode_id: str,
    as_of_bar_ts: str,
    recorded_at: str,
    venue: str = "XTAI",
    symbol: str = "AAA",
    bar_interval: str = "1h",
    sampling_policy_version: str = "active_tradable_completed_bar.v1",
) -> None:
    payload = {"episode_id": episode_id, "as_of_bar_ts": as_of_bar_ts}
    payload_json = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    digest = "sha256:" + hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
    with db.transaction() as cur:
        cur.execute(
            """
            INSERT INTO world_episodes(
                episode_id, capture_id, venue, symbol, observed_at, available_at,
                as_of_bar_ts, bar_interval, feature_contract_version, sampling_policy_version,
                training_eligible, training_reason, payload_json, payload_sha256,
                source_evidence_json, source_evidence_sha256, recorded_at
            ) VALUES (?, NULL, ?, ?, ?, ?, ?, ?, 'market_ohlcv_context.v2', ?, 1, NULL, ?, ?, '{}', ?, ?)
            """,
            (
                episode_id,
                venue,
                symbol,
                as_of_bar_ts,
                as_of_bar_ts,
                as_of_bar_ts,
                bar_interval,
                sampling_policy_version,
                payload_json,
                digest,
                digest,
                recorded_at,
            ),
        )


def test_v2_mapping_zulu_and_offset_timestamps_share_one_canonical_slot(store: WorldModelStore) -> None:
    first, second = _v2_pair()
    payload = first.to_dict()
    z_payload = _replace_offset_with_z(deepcopy(payload))
    assert isinstance(z_payload, dict)
    assert store.append_episode(payload) is True
    assert store.append_episode(z_payload) is False
    assert store.counts()["episodes"] == 1
    stored = store.get_episode_by_v2_slot(
        venue=first.observation.venue,
        symbol=first.observation.symbol,
        bar_interval=first.observation.bar_interval,
        as_of_bar_ts="2026-08-22T10:00:00Z",
        feature_contract_version=first.observation.feature_contract_version,
        sampling_policy_version=first.observation.sampling_policy_version,
    )
    assert stored is not None
    assert stored["episode_id"] == first.episode_id
    assert stored["as_of_bar_ts"].endswith("+00:00")
    with pytest.raises(WorldModelConflictError, match="V2 market slot"):
        store.append_episode(second)


def test_v2_mapping_rejects_contradictory_envelope_versions(store: WorldModelStore) -> None:
    from tests.application.test_world_context_capture import _v1_episode

    first, _second = _v2_pair()
    nested_v2_top_v1 = first.to_dict()
    nested_v2_top_v1["feature_contract_version"] = MARKET_FEATURE_CONTRACT_VERSION
    nested_v1_top_v2 = _v1_episode().to_dict()
    nested_v1_top_v2["feature_contract_version"] = first.observation.feature_contract_version
    with pytest.raises(ValueError, match="contradict"):
        store.append_episode(nested_v2_top_v1)
    with pytest.raises(ValueError, match="contradict"):
        store.append_episode(nested_v1_top_v2)
    assert store.counts()["episodes"] == 0


def test_concurrent_conflicting_v2_slots_are_rejected_atomically(tmp_path: Path) -> None:
    first, second = _v2_pair()
    db_path = tmp_path / "world_model.db"
    WorldModelStore(db_path).close()
    barrier = threading.Barrier(8)
    episodes = (first, second) * 4

    def worker(episode: WorldEpisode) -> bool:
        barrier.wait()
        local = WorldModelStore(db_path)
        try:
            return local.append_episode(episode)
        except WorldModelConflictError:
            return False
        finally:
            local.close()

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(worker, episodes))

    verifier = WorldModelStore(db_path)
    try:
        assert sum(1 for item in results if item is True) == 1
        assert verifier.counts()["episodes"] == 1
        stored = verifier.get_episode_by_v2_slot(
            venue=first.observation.venue,
            symbol=first.observation.symbol,
            bar_interval=first.observation.bar_interval,
            as_of_bar_ts=first.observation.as_of_bar_ts.isoformat(),
            feature_contract_version=first.observation.feature_contract_version,
            sampling_policy_version=first.observation.sampling_policy_version,
        )
        assert stored is not None
        assert stored["episode_id"] in {first.episode_id, second.episode_id}
    finally:
        verifier.close()


def test_migration_v1_duplicate_v2_slots_upgrade_without_mutating_legacy_rows(tmp_path: Path) -> None:
    db_path = tmp_path / "world_model.db"
    db = StateDb(db_path)
    db.apply_migrations(WORLD_MODEL_MIGRATIONS[:1])
    _insert_raw_v2_episode(
        db,
        episode_id="dup-b",
        as_of_bar_ts="2026-08-22T10:00:00+00:00",
        recorded_at="2026-08-22T12:00:00+00:00",
    )
    _insert_raw_v2_episode(
        db,
        episode_id="dup-a",
        as_of_bar_ts="2026-08-22T10:00:00+00:00",
        recorded_at="2026-08-22T11:00:00+00:00",
    )
    assert db.query_one("SELECT COUNT(*) FROM world_episodes")[0] == 2
    db.close()

    store = WorldModelStore(db_path)
    try:
        assert store.counts()["episodes"] == 2
        stored = store.get_episode_by_v2_slot(
            venue="XTAI",
            symbol="AAA",
            bar_interval="1h",
            as_of_bar_ts="2026-08-22T10:00:00+00:00",
            feature_contract_version="market_ohlcv_context.v2",
            sampling_policy_version="active_tradable_completed_bar.v1",
        )
        assert stored is not None
        assert stored["episode_id"] == "dup-a"
        first, _second = _v2_pair()
        with pytest.raises(WorldModelConflictError, match="V2 market slot"):
            store.append_episode(first)
        ids = [
            row["episode_id"]
            for row in store._db.query_all(
                "SELECT episode_id, recorded_at FROM world_episodes ORDER BY recorded_at, episode_id"
            )
        ]
        assert ids == ["dup-a", "dup-b"]
        assert store.counts()["episodes"] == 2
    finally:
        store.close()

    restarted = WorldModelStore(db_path)
    try:
        stored = restarted.get_episode_by_v2_slot(
            venue="XTAI",
            symbol="AAA",
            bar_interval="1h",
            as_of_bar_ts="2026-08-22T10:00:00Z",
            feature_contract_version="market_ohlcv_context.v2",
            sampling_policy_version="active_tradable_completed_bar.v1",
        )
        assert stored is not None
        assert stored["episode_id"] == "dup-a"
        assert restarted.counts()["episodes"] == 2
    finally:
        restarted.close()


_OLD_UNIXEPOCH_V2_TRIGGER = """
            CREATE TRIGGER IF NOT EXISTS world_episodes_v2_canonical_first_write
            BEFORE INSERT ON world_episodes
            WHEN NEW.feature_contract_version = 'market_ohlcv_context.v2'
            BEGIN
                SELECT RAISE(ABORT, 'V2 market slot already exists')
                WHERE EXISTS (
                    SELECT 1 FROM world_episodes AS existing
                    WHERE existing.symbol = NEW.symbol
                      AND existing.venue IS NEW.venue
                      AND existing.bar_interval IS NEW.bar_interval
                      AND existing.feature_contract_version = 'market_ohlcv_context.v2'
                      AND existing.sampling_policy_version IS NEW.sampling_policy_version
                      AND existing.episode_id != NEW.episode_id
                      AND unixepoch(existing.as_of_bar_ts) = unixepoch(NEW.as_of_bar_ts)
                );
            END
            """


def _v2_episode_at(as_of_bar_ts: str) -> WorldEpisode:
    first, _second = _v2_pair()
    observation = first.observation
    return WorldEpisode(
        WorldObservation(
            venue=observation.venue,
            symbol=observation.symbol,
            bar_interval=observation.bar_interval,
            as_of_bar_ts=as_of_bar_ts,
            feature_contract_version=observation.feature_contract_version,
            sampling_policy_version=observation.sampling_policy_version,
            anchor={**observation.anchor.to_dict(), "ts": as_of_bar_ts},
            available_at=observation.available_at,
            captured_at=observation.captured_at,
            freshness=observation.freshness,
            categorical_features=dict(observation.categorical_features),
            numeric_features=dict(observation.numeric_features),
            context=observation.context,
        )
    )


def _slot_lookup(store: WorldModelStore, episode: WorldEpisode, *, as_of_bar_ts: str | None = None) -> dict | None:
    observation = episode.observation
    return store.get_episode_by_v2_slot(
        venue=observation.venue,
        symbol=observation.symbol,
        bar_interval=observation.bar_interval,
        as_of_bar_ts=as_of_bar_ts or observation.as_of_bar_ts.isoformat(),
        feature_contract_version=observation.feature_contract_version,
        sampling_policy_version=observation.sampling_policy_version,
    )


def test_legacy_plus0000_slot_is_found_by_canonical_offset_without_second_append(tmp_path: Path) -> None:
    db_path = tmp_path / "world_model.db"
    db = StateDb(db_path)
    db.apply_migrations(WORLD_MODEL_MIGRATIONS[:1])
    _insert_raw_v2_episode(
        db,
        episode_id="legacy-plus0000",
        as_of_bar_ts="2026-08-22T10:00:00+0000",
        recorded_at="2026-08-22T11:00:00+00:00",
    )
    db.close()

    store = WorldModelStore(db_path)
    try:
        stored = store.get_episode_by_v2_slot(
            venue="XTAI",
            symbol="AAA",
            bar_interval="1h",
            as_of_bar_ts="2026-08-22T10:00:00+00:00",
            feature_contract_version="market_ohlcv_context.v2",
            sampling_policy_version="active_tradable_completed_bar.v1",
        )
        assert stored is not None
        assert stored["episode_id"] == "legacy-plus0000"
        assert stored["as_of_bar_ts"] == "2026-08-22T10:00:00+0000"
        first, _second = _v2_pair()
        with pytest.raises(WorldModelConflictError, match="V2 market slot"):
            store.append_episode(first)
        assert store.counts()["episodes"] == 1
    finally:
        store.close()


def test_fractional_v2_slots_remain_distinct_and_lookup_returns_each_row(tmp_path: Path) -> None:
    early = _v2_episode_at("2026-08-22T10:00:00.100000+00:00")
    late = _v2_episode_at("2026-08-22T10:00:00.900000+00:00")
    db_path = tmp_path / "world_model.db"
    store = WorldModelStore(db_path)
    try:
        assert store.append_episode(early) is True
        assert store.append_episode(late) is True
        assert store.counts()["episodes"] == 2
        found_early = _slot_lookup(store, early)
        found_late = _slot_lookup(store, late)
        assert found_early is not None and found_early["episode_id"] == early.episode_id
        assert found_late is not None and found_late["episode_id"] == late.episode_id
        assert found_early["as_of_bar_ts"] == "2026-08-22T10:00:00.100000+00:00"
        assert found_late["as_of_bar_ts"] == "2026-08-22T10:00:00.900000+00:00"
    finally:
        store.close()

    restarted = WorldModelStore(db_path)
    try:
        found_early = _slot_lookup(restarted, early)
        found_late = _slot_lookup(restarted, late)
        assert found_early is not None and found_early["episode_id"] == early.episode_id
        assert found_late is not None and found_late["episode_id"] == late.episode_id
    finally:
        restarted.close()


def test_migration_upgrades_old_unixepoch_trigger_and_keeps_legacy_plus0000(tmp_path: Path) -> None:
    db_path = tmp_path / "world_model.db"
    db = StateDb(db_path)
    db.apply_migrations(WORLD_MODEL_MIGRATIONS[:1])
    with db.transaction() as cur:
        cur.execute(_OLD_UNIXEPOCH_V2_TRIGGER)
        cur.execute(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (2, ?)",
            ("2026-08-22T00:00:00+00:00",),
        )
    _insert_raw_v2_episode(
        db,
        episode_id="legacy-plus0000",
        as_of_bar_ts="2026-08-22T10:00:00+0000",
        recorded_at="2026-08-22T11:00:00+00:00",
    )
    db.close()

    store = WorldModelStore(db_path)
    try:
        trigger = store._db.query_one(
            "SELECT sql FROM sqlite_master WHERE type='trigger' AND name='world_episodes_v2_canonical_first_write'"
        )
        assert trigger is not None
        assert "unixepoch" not in trigger["sql"]
        index = store._db.query_one(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_world_episodes_v2_market_slot_candidates'"
        )
        assert index is not None
        stored = store.get_episode_by_v2_slot(
            venue="XTAI",
            symbol="AAA",
            bar_interval="1h",
            as_of_bar_ts="2026-08-22T10:00:00+00:00",
            feature_contract_version="market_ohlcv_context.v2",
            sampling_policy_version="active_tradable_completed_bar.v1",
        )
        assert stored is not None
        assert stored["episode_id"] == "legacy-plus0000"
        assert stored["as_of_bar_ts"] == "2026-08-22T10:00:00+0000"
        first, _second = _v2_pair()
        with pytest.raises(WorldModelConflictError, match="V2 market slot"):
            store.append_episode(first)
        early = _v2_episode_at("2026-08-22T10:00:00.100000+00:00")
        late = _v2_episode_at("2026-08-22T10:00:00.900000+00:00")
        assert store.append_episode(early) is True
        assert store.append_episode(late) is True
        assert store.counts()["episodes"] == 3
    finally:
        store.close()


def test_cohort_migration_is_version_5_and_preserves_v1_v4_statements() -> None:
    versions = [version for version, _statements in WORLD_MODEL_MIGRATIONS]
    assert versions == [1, 2, 3, 4, 5, 6]
    v1_sql = "\n".join(WORLD_MODEL_MIGRATIONS[0][1])
    assert "CREATE TABLE IF NOT EXISTS world_episodes" in v1_sql
    assert "CREATE TABLE IF NOT EXISTS world_outcome_events" in v1_sql
    assert "CREATE TABLE IF NOT EXISTS world_shadow_predictions" in v1_sql
    for version, statements in WORLD_MODEL_MIGRATIONS[:4]:
        blob = "\n".join(statements)
        assert "world_cohort_manifests" not in blob
        assert "world_cohort_events" not in blob
        assert "world_availability_receipts" not in blob
        assert "study_cohort_id" not in blob
        assert version < 5
    v5_sql = "\n".join(WORLD_MODEL_MIGRATIONS[4][1])
    assert "world_cohort_manifests" in v5_sql
    assert "world_cohort_events" in v5_sql
    assert "world_cohort_slots" in v5_sql
    assert "world_availability_receipts" in v5_sql
    assert "study_cohort_id" in v5_sql
    assert "feature_mask_fingerprint" in v5_sql
    assert "world_entity_events" not in v5_sql
    assert "world_graph_snapshots" not in v5_sql


def test_graph_migration_is_version_6_reuses_receipts_and_does_not_rewrite_v1_v5() -> None:
    versions = [version for version, _statements in WORLD_MODEL_MIGRATIONS]
    assert versions[-1] == 6
    for version, statements in WORLD_MODEL_MIGRATIONS[:5]:
        blob = "\n".join(statements)
        assert "world_entity_events" not in blob
        assert "world_entity_identity_events" not in blob
        assert "world_relation_events" not in blob
        assert "world_ontology_revisions" not in blob
        assert "world_graph_snapshots" not in blob
        assert "world_graph_snapshot_members" not in blob
        assert version < 6
    v6_sql = "\n".join(WORLD_MODEL_MIGRATIONS[5][1])
    assert "CREATE TABLE IF NOT EXISTS world_entity_events" in v6_sql
    assert "CREATE TABLE IF NOT EXISTS world_entity_identity_events" in v6_sql
    assert "CREATE TABLE IF NOT EXISTS world_relation_events" in v6_sql
    assert "CREATE TABLE IF NOT EXISTS world_ontology_revisions" in v6_sql
    assert "CREATE TABLE IF NOT EXISTS world_graph_snapshots" in v6_sql
    assert "CREATE TABLE IF NOT EXISTS world_graph_snapshot_members" in v6_sql
    assert "CREATE TABLE IF NOT EXISTS world_availability_receipts" not in v6_sql
    assert "sequence" in v6_sql
    for table in (
        "world_entity_events",
        "world_entity_identity_events",
        "world_relation_events",
        "world_ontology_revisions",
        "world_graph_snapshots",
        "world_graph_snapshot_members",
    ):
        assert f"{table}_no_update" in v6_sql
        assert f"{table}_no_delete" in v6_sql


def test_v5_does_not_rewrite_existing_episode_outcome_or_prediction_bytes(tmp_path: Path) -> None:
    db_path = tmp_path / "world_model.db"
    db = StateDb(db_path)
    db.apply_migrations(WORLD_MODEL_MIGRATIONS[:4])
    episode_payload = json.dumps({"episode_id": "legacy-1", "keep": True}, separators=(",", ":"), sort_keys=True)
    outcome_payload = json.dumps({"outcome_event_id": "legacy-out", "keep": True}, separators=(",", ":"), sort_keys=True)
    prediction_payload = json.dumps(
        {"prediction_id": "legacy-pred", "keep": True},
        separators=(",", ":"),
        sort_keys=True,
    )
    episode_digest = "sha256:" + hashlib.sha256(episode_payload.encode("utf-8")).hexdigest()
    outcome_digest = "sha256:" + hashlib.sha256(outcome_payload.encode("utf-8")).hexdigest()
    prediction_digest = "sha256:" + hashlib.sha256(prediction_payload.encode("utf-8")).hexdigest()
    recorded = "2026-08-22T11:00:00+00:00"
    with db.transaction() as cur:
        cur.execute(
            """
            INSERT INTO world_episodes(
                episode_id, capture_id, venue, symbol, observed_at, available_at,
                as_of_bar_ts, bar_interval, feature_contract_version, sampling_policy_version,
                training_eligible, training_reason, payload_json, payload_sha256,
                source_evidence_json, source_evidence_sha256, recorded_at
            ) VALUES ('legacy-1', NULL, 'US', 'AAPL', ?, ?, ?, '1h', 'world-features-v1', 'fresh-active-v1',
                      1, NULL, ?, ?, '{}', ?, ?)
            """,
            (recorded, recorded, recorded, episode_payload, episode_digest, episode_digest, recorded),
        )
        cur.execute(
            """
            INSERT INTO world_outcome_events(
                outcome_event_id, episode_id, horizon_code, label_schema_version, status, move_class,
                training_eligible, label_available_at, sealed_at, supersedes_outcome_event_id,
                label_json, evidence_json, evidence_sha256, payload_json, payload_sha256, recorded_at
            ) VALUES ('legacy-out', 'legacy-1', '4h', 'world-label-v1', 'observed', 'UP',
                      1, ?, ?, NULL, '{}', '{}', ?, ?, ?, ?)
            """,
            (recorded, recorded, outcome_digest, outcome_payload, outcome_digest, recorded),
        )
        cur.execute(
            """
            INSERT INTO world_shadow_predictions(
                prediction_id, run_id, episode_id, horizon_code, model_kind, model_version,
                predicted_at, input_sha256, prediction_json, prediction_sha256,
                payload_json, payload_sha256, recorded_at
            ) VALUES ('legacy-pred', 'run-1', 'legacy-1', 'elapsed_4h.v1', 'markov', 'v1',
                      ?, ?, '{}', ?, ?, ?, ?)
            """,
            (recorded, prediction_digest, prediction_digest, prediction_payload, prediction_digest, recorded),
        )
    before = db.query_one(
        "SELECT e.payload_json AS episode_json, e.payload_sha256 AS episode_sha, "
        "o.payload_json AS outcome_json, o.payload_sha256 AS outcome_sha, "
        "p.payload_json AS prediction_json, p.payload_sha256 AS prediction_sha "
        "FROM world_episodes e "
        "JOIN world_outcome_events o ON o.episode_id=e.episode_id "
        "JOIN world_shadow_predictions p ON p.episode_id=e.episode_id"
    )
    db.close()

    store = WorldModelStore(db_path)
    try:
        after = store._db.query_one(
            "SELECT e.payload_json AS episode_json, e.payload_sha256 AS episode_sha, "
            "o.payload_json AS outcome_json, o.payload_sha256 AS outcome_sha, "
            "p.payload_json AS prediction_json, p.payload_sha256 AS prediction_sha, "
            "p.study_cohort_id, p.lane_id, p.manifest_sha256, "
            "p.feature_contract_fingerprint, p.feature_mask_fingerprint "
            "FROM world_episodes e "
            "JOIN world_outcome_events o ON o.episode_id=e.episode_id "
            "JOIN world_shadow_predictions p ON p.episode_id=e.episode_id"
        )
        assert after["episode_json"] == before["episode_json"]
        assert after["episode_sha"] == before["episode_sha"]
        assert after["outcome_json"] == before["outcome_json"]
        assert after["outcome_sha"] == before["outcome_sha"]
        assert after["prediction_json"] == before["prediction_json"]
        assert after["prediction_sha"] == before["prediction_sha"]
        assert after["study_cohort_id"] is None
        assert after["lane_id"] is None
        assert after["manifest_sha256"] is None
        assert after["feature_contract_fingerprint"] is None
        assert after["feature_mask_fingerprint"] is None
        listed = store.list_predictions(run_id="run-1")
        assert listed[0]["prediction_id"] == "legacy-pred"
        assert listed[0]["prediction_record"]["keep"] is True
        names = {row["name"] for row in store._db.query_all("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "world_entity_events" in names
        assert "world_graph_snapshots" in names
        assert "world_availability_receipts" in names
    finally:
        store.close()


def test_prediction_cohort_columns_are_indexed_and_override_json(store: WorldModelStore) -> None:
    assert store.append_episode(_episode())
    payload = _prediction("prediction-cohort")
    payload["study_cohort_id"] = "world_cohort:v1:" + "c" * 64
    payload["lane_id"] = "markov.market"
    payload["manifest_sha256"] = "a" * 64
    payload["feature_contract_fingerprint"] = "b" * 64
    payload["feature_mask_fingerprint"] = "c" * 64
    payload["prediction"]["study_cohort_id"] = "json-should-lose"
    payload["prediction"]["lane_id"] = "json-lane"
    assert store.append_legacy_prediction(payload) is True
    row = store.list_predictions(run_id="markov-run-1")[0]
    assert row["study_cohort_id"] == "world_cohort:v1:" + "c" * 64
    assert row["lane_id"] == "markov.market"
    assert row["manifest_sha256"] == "a" * 64
    assert row["feature_contract_fingerprint"] == "b" * 64
    assert row["feature_mask_fingerprint"] == "c" * 64
    assert row["prediction_record"]["study_cohort_id"] == "world_cohort:v1:" + "c" * 64
    indexes = {
        row["name"]
        for row in store._db.query_all(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='world_shadow_predictions'"
        )
    }
    assert "idx_world_predictions_study_cohort" in indexes
    assert "idx_world_predictions_lane" in indexes or "idx_world_predictions_study_cohort" in indexes
    assert any("manifest" in name for name in indexes)
    assert any("feature" in name for name in indexes)


def test_list_collecting_cohort_ids_requires_started_phase(tmp_path: Path) -> None:
    from trader.application.world_model.cohort_service import WorldCohortService
    from trader.domain.world_cohort import RegisterWorldCohort, WorldCohortId
    from tests.application.test_world_cohort_service import (
        START_READY,
        _arm_command,
        _manifest,
        _start_command,
    )

    ready = START_READY
    store = WorldModelStore(tmp_path / "world_model.db", clock=lambda: ready)
    try:
        assert store.list_collecting_cohort_ids() == ()
        service = WorldCohortService(repository=store, query=store)
        manifest = _manifest()
        service.register(RegisterWorldCohort(manifest=manifest))
        assert store.list_collecting_cohort_ids() == ()
        service.arm(_arm_command(manifest))
        assert store.list_collecting_cohort_ids() == ()
        service.start(_start_command(manifest))
        assert store.list_collecting_cohort_ids() == (WorldCohortId(manifest.cohort_id),)
    finally:
        store.close()


def test_append_event_cas_and_restart_keep_conservative_first_seen(tmp_path: Path) -> None:
    from datetime import datetime, timezone

    from trader.domain.world_cohort import WorldCohortSlotAdmitted
    from tests.application.test_world_cohort_service import START_READY, _admit_command
    from tests.application.test_world_context_lanes import _collecting_cohort_service

    boot = datetime(2026, 8, 24, 1, 0, tzinfo=timezone.utc)
    path = tmp_path / "world_model.db"
    store = WorldModelStore(path, clock=lambda: START_READY)
    try:
        service, cohort, _ready = _collecting_cohort_service(store)
        started = store.envelope_for(cohort.started_event)
        assert started.require_proven().first_seen_at == START_READY
        assert started.require_proven().receipt.ready_at == START_READY
        admitted = service.admit_slot(_admit_command(cohort, store))
        assert isinstance(admitted.event, WorldCohortSlotAdmitted)
        assert admitted.sequence == 4
        replayed = store.append_event(admitted.event, expected_sequence=3)
        assert replayed.event.event_id == admitted.event.event_id
        assert replayed.sequence == 4
        store.close()

        restarted = WorldModelStore(path, clock=lambda: boot)
        looked_up = restarted.envelope_for(admitted.event)
        evidence = looked_up.require_proven()
        assert evidence.receipt.ready_at == START_READY
        assert evidence.first_seen_at == boot
        assert evidence.effective_ready_at == boot
        started_again = restarted.envelope_for(cohort.started_event).require_proven()
        assert started_again.receipt.ready_at == START_READY
        assert started_again.first_seen_at == boot
        identical = restarted.append_event(admitted.event, expected_sequence=3)
        assert identical.event.event_id == admitted.event.event_id
        assert identical.sequence == 4
        assert identical.require_proven().first_seen_at == boot
        restarted.close()
    finally:
        store.close()
