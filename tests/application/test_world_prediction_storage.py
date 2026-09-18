from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from trader.application.world_model.baseline import HierarchicalDirichletWorldBaseline
from trader.application.world_model.context_capture import attach_world_context
from trader.application.world_model.service import WorldModelService
from trader.domain.world_context import SensorEvidence
from trader.domain.world_episode import (
    MARKET_FEATURE_CONTRACT_ID,
    AnchorBar,
    WorldEpisode,
    WorldObservation,
)
from trader.infrastructure.state_db.world_model_store import WorldModelStore
from trader.infrastructure.state_db.world_model_query import read_world_model_ledger
from trader.reporting.read_models.world_status import read_world_model_status
from tests.application.test_world_context_capture import _FakeSource, _market_episode


UTC = timezone.utc
NOW = datetime(2026, 8, 28, 8, 0, tzinfo=UTC)
HORIZON = "elapsed_4h.v1"


def _episode() -> WorldEpisode:
    return WorldEpisode(
        observation=WorldObservation(
            venue="US",
            symbol="SPY",
            bar_interval="1h",
            as_of_bar_ts=NOW,
            feature_contract_version=MARKET_FEATURE_CONTRACT_ID,
            sampling_policy_version="active_tradable_completed_bar.v1",
            anchor=AnchorBar(
                ts=NOW,
                open=100.0,
                high=101.0,
                low=99.0,
                close=100.5,
                volume=1_000.0,
                source="fixture",
            ),
            available_at=NOW,
            captured_at=NOW + timedelta(minutes=1),
            freshness="fresh",
            categorical_features={"asset_family": "equities", "venue": "US"},
            numeric_features={"return": 0.01, "atr_pct": 0.02},
        )
    )


def _runtime(store: WorldModelStore) -> WorldModelService:
    return WorldModelService(
        store=store,
        predictor=HierarchicalDirichletWorldBaseline(),
        labeler=None,
        bar_provider=None,
        horizons=(HORIZON,),
    )


def test_prediction_record_references_episode_without_copying_observation_and_restarts_cleanly(
    tmp_path,
) -> None:
    path = tmp_path / "world_model.db"
    episode = _episode()
    store = WorldModelStore(path, clock=lambda: NOW + timedelta(minutes=1))
    first = _runtime(store).capture_and_predict((episode,), now=NOW + timedelta(minutes=1))

    assert first["errors"] == []
    assert first["predictions_appended"] == 1
    rows = store.list_predictions()
    assert len(rows) == 1
    row = rows[0]
    record = row["prediction_record"]
    prediction = row["prediction"]

    assert "input" not in record
    assert "observation" not in record
    assert record["episode_id"] == episode.episode_id
    assert record["feature_hash"] == prediction["feature_hash"]
    assert record["input_sha256"] == prediction["feature_hash"]
    assert row["input_sha256"] == f"sha256:{prediction['feature_hash']}"
    assert prediction["authority"] == "shadow_only"
    assert prediction["decision_effect"] == "none"
    assert prediction["recommendation"] == "NO_GO"
    fingerprint = (
        row["prediction_id"],
        row["input_sha256"],
        row["prediction_sha256"],
        row["payload_sha256"],
    )
    store.close()

    ledger = read_world_model_ledger(path)
    assert ledger["episodes"] == [
        {
            "episode_id": episode.episode_id,
            "venue": "US",
            "symbol": "SPY",
            "bar_interval": "1h",
            "as_of_bar_ts": NOW.isoformat(),
            "feature_contract_version": MARKET_FEATURE_CONTRACT_ID,
            "context_status": None,
        }
    ]

    restarted_store = WorldModelStore(path, clock=lambda: NOW + timedelta(minutes=2))
    replay = _runtime(restarted_store).capture_and_predict((episode,), now=NOW + timedelta(minutes=2))
    replay_rows = restarted_store.list_predictions()

    assert replay["errors"] == []
    assert replay["predictions_appended"] == 0
    assert replay["predictions_existing"] == 1
    assert len(replay_rows) == 1
    replay_row = replay_rows[0]
    assert (
        replay_row["prediction_id"],
        replay_row["input_sha256"],
        replay_row["prediction_sha256"],
        replay_row["payload_sha256"],
    ) == fingerprint
    assert "input" not in replay_row["prediction_record"]
    restarted_store.close()


def test_world_status_keeps_context_ablation_with_compact_persisted_predictions(tmp_path) -> None:
    market = _market_episode()
    missing = SensorEvidence(status="missing", reason="no_artifact")
    context = attach_world_context((market,), _FakeSource(missing, missing))[0]
    path = tmp_path / "world_model.db"
    store = WorldModelStore(path)
    assert store.append_episode(market)
    assert store.append_episode(context)

    for episode, model_version in ((market, "v1"), (context, "context.v1")):
        assert store.append_legacy_prediction(
            {
                "prediction_id": f"compact:{model_version}",
                "run_id": "world_shadow.v1",
                "episode_id": episode.episode_id,
                "horizon_code": HORIZON,
                "model_kind": "hierarchical_dirichlet_world_baseline",
                "model_version": model_version,
                "predicted_at": "2026-08-22T10:30:00+00:00",
                "feature_hash": episode.observation.feature_hash,
                "input_sha256": episode.observation.feature_hash,
                "comparison_batch_id": "batch:compact",
                "comparison_cohort_fingerprint": "cohort:compact",
                "prediction": {
                    "probabilities": {"DOWN": 0.1, "FLAT": 0.2, "UP": 0.7},
                    "predicted_class": "UP",
                    "comparison_batch_id": "batch:compact",
                    "comparison_cohort_fingerprint": "cohort:compact",
                },
            }
        )
        assert store.append_legacy_outcome_event(
            {
                "outcome_event_id": f"outcome:{model_version}",
                "episode_id": episode.episode_id,
                "horizon_code": HORIZON,
                "status": "observed",
                "move_class": "UP",
                "training_eligible": True,
                "label_available_at": "2027-01-01T00:00:00+00:00",
                "target_at": "2027-01-01T00:00:00+00:00",
                "label": {
                    "move_class": "UP",
                    "simple_return": 0.01,
                    "target_at": "2027-01-01T00:00:00+00:00",
                    "source_raw_sha256": "a" * 64,
                },
                "evidence": {"source": "fixture", "source_raw_sha256": "a" * 64},
            }
        )
    store.close()

    result = read_world_model_status(tmp_path)
    ablation = result["evaluation"]["context_ablation"]

    assert ablation["status"] == "insufficient_support"
    assert ablation["coverage"] == {"missing": 1}
    assert ablation["families"][0]["matched_pairs"] == 1
    assert result["authority"] == "shadow_only"
    assert result["decision_effect"] == "none"


def test_live_mapping_cannot_omit_both_input_and_its_fingerprints(tmp_path) -> None:
    episode = _episode()
    store = WorldModelStore(tmp_path / "world_model.db")
    assert store.append_episode(episode)

    with pytest.raises(ValueError, match="requires input_sha256, feature_hash, or input"):
        store.append_prediction(
            {
                "prediction_id": "missing-input-lineage",
                "run_id": "world_shadow.v1",
                "episode_id": episode.episode_id,
                "horizon_code": HORIZON,
                "model_kind": "fixture",
                "model_version": "v1",
                "predicted_at": NOW.isoformat(),
                "prediction": {
                    "probabilities": {"DOWN": 0.2, "FLAT": 0.3, "UP": 0.5},
                    "predicted_class": "UP",
                    "status": "shadow_only",
                    "recommendation": "NO_GO",
                    "authority": "shadow_only",
                    "decision_effect": "none",
                },
            }
        )
    store.close()


def _live_prediction_mapping(episode, **overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "prediction_id": "live-hash",
        "run_id": "world_shadow.v1",
        "episode_id": episode.episode_id,
        "horizon_code": HORIZON,
        "model_kind": "fixture",
        "model_version": "v1",
        "predicted_at": NOW.isoformat(),
        "feature_hash": episode.observation.feature_hash,
        "input_sha256": episode.observation.feature_hash,
        "prediction": {
            "probabilities": {"DOWN": 0.2, "FLAT": 0.3, "UP": 0.5},
            "predicted_class": "UP",
            "status": "shadow_only",
            "recommendation": "NO_GO",
            "authority": "shadow_only",
            "decision_effect": "none",
        },
    }
    payload.update(overrides)
    return payload


def test_live_mapping_rejects_malformed_hash_before_append(tmp_path) -> None:
    episode = _episode()
    store = WorldModelStore(tmp_path / "world_model.db")
    assert store.append_episode(episode)

    with pytest.raises(ValueError, match="sha256"):
        store.append_prediction(_live_prediction_mapping(episode, feature_hash="not-a-digest", input_sha256="nope"))
    with pytest.raises(ValueError, match="sha256"):
        store.append_prediction(
            _live_prediction_mapping(
                episode,
                feature_hash="sha256:" + "z" * 64,
                input_sha256="sha256:" + "z" * 64,
            )
        )
    with pytest.raises(ValueError, match="sha256"):
        store.append_prediction(
            _live_prediction_mapping(episode, feature_hash="abc", input_sha256=episode.observation.feature_hash)
        )
    assert store.list_predictions() == []
    store.close()


def test_live_mapping_accepts_canonical_hex_and_prefixed_digest(tmp_path) -> None:
    episode = _episode()
    store = WorldModelStore(tmp_path / "world_model.db")
    assert store.append_episode(episode)
    digest = episode.observation.feature_hash

    assert store.append_prediction(_live_prediction_mapping(episode, prediction_id="untagged")) is True
    assert (
        store.append_prediction(
            _live_prediction_mapping(
                episode,
                prediction_id="prefixed",
                feature_hash=f"sha256:{digest}",
                input_sha256=f"sha256:{digest}",
            )
        )
        is True
    )
    rows = store.list_predictions()
    assert {row["prediction_id"] for row in rows} == {"untagged", "prefixed"}
    assert all(row["input_sha256"] == f"sha256:{digest}" for row in rows)
    store.close()


def test_live_compact_mapping_rejects_legacy_input_reexpansion(tmp_path) -> None:
    episode = _episode()
    store = WorldModelStore(tmp_path / "world_model.db")
    assert store.append_episode(episode)
    compact = _live_prediction_mapping(
        episode,
        input={"return": 0.01, "ohlcv": "should-not-be-copied"},
    )

    with pytest.raises(ValueError, match="cannot include input"):
        store.append_prediction(compact)
    assert store.list_predictions() == []

    assert store.append_prediction(_live_prediction_mapping(episode)) is True
    row = store.list_predictions()[0]
    assert "input" not in row["prediction_record"]
    assert "observation" not in row["prediction_record"]
    assert row["prediction_record"]["feature_hash"] == episode.observation.feature_hash

    legacy = {
        "prediction_id": "legacy-input",
        "run_id": "world_shadow.v1",
        "episode_id": episode.episode_id,
        "horizon_code": HORIZON,
        "model_kind": "fixture",
        "model_version": "legacy",
        "predicted_at": NOW.isoformat(),
        "input": {"return_1h": 0.01},
        "prediction": {"p_up": 0.6},
    }
    assert store.append_legacy_prediction(legacy) is True
    legacy_row = next(item for item in store.list_predictions() if item["prediction_id"] == "legacy-input")
    assert legacy_row["prediction_record"]["input"] == {"return_1h": 0.01}
    store.close()


def test_prediction_key_hydration_happens_once_then_uses_memory(tmp_path) -> None:
    path = tmp_path / "world_model.db"
    episode = _episode()

    class CountingStore(WorldModelStore):
        def __init__(self, *args, **kwargs) -> None:
            super().__init__(*args, **kwargs)
            self.identity_calls = 0
            self.fail_next_identities = False

        def list_prediction_identities(self):
            self.identity_calls += 1
            if self.fail_next_identities:
                self.fail_next_identities = False
                raise RuntimeError("hydrate-failed")
            return super().list_prediction_identities()

    store = CountingStore(path, clock=lambda: NOW + timedelta(minutes=1))
    store.fail_next_identities = True
    service = _runtime(store)

    failed = service.capture_and_predict((episode,), now=NOW + timedelta(minutes=1))
    assert any(error["stage"] == "list_prediction_identities" for error in failed["errors"])
    assert store.identity_calls == 1
    assert failed["predictions_appended"] == 1

    replay = service.capture_and_predict((episode,), now=NOW + timedelta(minutes=2))
    assert store.identity_calls == 2
    assert replay["predictions_appended"] == 0
    assert replay["predictions_existing"] == 1

    again = service.capture_and_predict((episode,), now=NOW + timedelta(minutes=3))
    assert store.identity_calls == 2
    assert again["predictions_existing"] == 1
    assert again["predictions_appended"] == 0
    store.close()
