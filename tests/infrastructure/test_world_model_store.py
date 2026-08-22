"""Contract tests for the isolated immutable world-model SQLite ledger."""

from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path

import pytest

from trader.domain.world_episode import (
    AnchorBar,
    OutcomeHorizon,
    WorldEpisode,
    WorldObservation,
    WorldOutcome,
    WorldPrediction,
)
from trader.infrastructure.state_db.connection import StateDb
from trader.infrastructure.state_db.world_model_store import (
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
        row["outcome_event_id"]
        for row in store.list_observed_outcomes(horizon_id="4h", include_superseded=True)
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
    assert [row["episode_id"] for row in store.list_pending_episodes(horizon_code="4h")] == [
        "episode-1"
    ]


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
    assert [row["prediction_id"] for row in store.list_predictions(horizon_id="elapsed_4h.v1")] == [
        "prediction-1"
    ]

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
