from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from trader.application.world_model.context_capture import attach_world_context
from trader.application.world_model.gru import OnlineGRUWorldChallenger
from trader.application.world_model.service import WorldModelService
from trader.domain.world_context import SensorEvidence
from trader.domain.world_episode import SamplingSlotCapture, SamplingSlotCaptureKind, WorldEpisode
from trader.infrastructure.state_db.world_model_store import WorldModelConflictError, WorldModelStore

from tests.application.test_world_context_capture import _FakeSource, _v1_episode
from tests.application.test_world_context_lanes import (
    FilteringPredictor,
    GraphFilteringPredictor,
    MarketFilteringPredictor,
    RecordingPredictor,
    _cohort_market_episode,
    _collecting_cohort_service,
    _v2_context_pair,
    _v3_companion,
)
from tests.application.test_world_cohort_service import ANCHOR_TS, START_READY, V1
from tests.application.test_world_graph_capture import _attach, _unpublished_config
from tests.application.test_world_graph_snapshot import _episode as _graph6_v1_episode


NOW = datetime(2026, 8, 22, 10, 30, tzinfo=timezone.utc)


def _later_poll_episode(episode: WorldEpisode, *, minutes: int = 5, close: float) -> WorldEpisode:
    payload = episode.to_dict()
    observation = payload["observation"]
    available = datetime.fromisoformat(str(observation["available_at"]).replace("Z", "+00:00"))
    captured = datetime.fromisoformat(str(observation["captured_at"]).replace("Z", "+00:00"))
    observation["available_at"] = (available + timedelta(minutes=minutes)).isoformat()
    observation["captured_at"] = (captured + timedelta(minutes=minutes)).isoformat()
    observation["anchor"]["close"] = close
    observation["anchor"]["high"] = max(float(observation["anchor"]["high"]), close)
    observation["anchor"]["low"] = min(float(observation["anchor"]["low"]), close)
    features = dict(observation.get("numeric_features") or {})
    features["return"] = -0.4
    observation["numeric_features"] = features
    return WorldEpisode.from_dict(payload)


def _stored_close(store: WorldModelStore, episode_id: str) -> float:
    stored = store.get_episode(episode_id)
    assert stored is not None
    observation = stored["episode"]["observation"]
    return float(observation["anchor"]["close"])


def _capture_service(store: WorldModelStore, predictor, *extra) -> WorldModelService:
    return WorldModelService(
        store=store,
        predictor=predictor,
        predictors=extra,
        labeler=None,
        bar_provider=None,
        horizons=("elapsed_4h.v1",),
    )


def test_sampling_slot_capture_kinds_are_explicit_lifecycle_values() -> None:
    episode = _v1_episode()
    appended = SamplingSlotCapture.appended(episode)
    reused = SamplingSlotCapture.reused_canonical(episode)
    missing = SamplingSlotCapture.missing()
    assert appended.kind is SamplingSlotCaptureKind.APPENDED
    assert reused.kind is SamplingSlotCaptureKind.REUSED_CANONICAL
    assert missing.kind is SamplingSlotCaptureKind.MISSING
    assert missing.episode is None
    assert isinstance(appended.episode, WorldEpisode)
    assert reused.episode is episode
    with pytest.raises(ValueError, match="requires an episode"):
        SamplingSlotCapture.reused_canonical(None)
    with pytest.raises(ValueError, match="cannot carry an episode"):
        SamplingSlotCapture(kind=SamplingSlotCaptureKind.MISSING, episode=episode)


def test_v1_later_available_at_and_revised_ohlcv_reuse_canonical_episode(tmp_path) -> None:
    first = _v1_episode()
    later = _later_poll_episode(first, close=150.0)
    assert first.episode_id == later.episode_id
    assert later.observation.available_at > first.observation.available_at
    assert later.observation.anchor.close != first.observation.anchor.close
    store = WorldModelStore(Path(tmp_path) / "world_model.db")
    recorder = MarketFilteringPredictor()
    gru = OnlineGRUWorldChallenger(hidden_size=4, sequence_len=4)
    service = _capture_service(store, recorder, gru)
    first_report = service.capture_and_predict((first,), now=NOW)
    assert first_report["errors"] == []
    assert first_report["episodes_appended"] == 1
    prediction_count = store.counts()["predictions"]
    replay = service.capture_and_predict((later,), now=NOW + timedelta(minutes=5))
    assert replay["errors"] == []
    assert replay["episodes_existing"] == 1
    assert replay["episodes_appended"] == 0
    assert replay["predictions_appended"] == 0
    assert replay["model_updates"] == 0
    assert store.counts()["episodes"] == 1
    assert store.counts()["predictions"] == prediction_count
    assert recorder.seen == [first.episode_id]
    assert _stored_close(store, first.episode_id) == first.observation.anchor.close
    with pytest.raises(WorldModelConflictError, match="different canonical content"):
        store.append_episode(later)

    restarted_recorder = MarketFilteringPredictor()
    restarted = _capture_service(
        store,
        restarted_recorder,
        OnlineGRUWorldChallenger(hidden_size=4, sequence_len=4),
    )
    background = restarted.capture_and_predict((later,), now=NOW + timedelta(minutes=6))
    assert background["errors"] == []
    assert background["episodes_existing"] == 1
    assert background["episodes_appended"] == 0
    assert background["predictions_appended"] == 0
    assert background["model_updates"] == 0
    assert restarted_recorder.seen == []
    assert store.counts() == {"episodes": 1, "outcome_events": 0, "predictions": prediction_count}


def test_v2_later_poll_with_revised_ohlcv_reuses_first_canonical_slot(tmp_path) -> None:
    first, divergent_context = _v2_context_pair()
    later = _later_poll_episode(first, close=150.0)
    revised_context = _later_poll_episode(divergent_context, close=175.0)
    store = WorldModelStore(Path(tmp_path) / "world_model.db")
    recorder = FilteringPredictor()
    gru = OnlineGRUWorldChallenger(
        model_version="context.v2",
        encoder_version="world_gru_encoder.v2",
        include_context=True,
        hidden_size=4,
        sequence_len=4,
        accepted_feature_contracts=frozenset({first.observation.feature_contract_version}),
    )
    service = _capture_service(store, recorder, gru)
    assert service.capture_and_predict((first,), now=NOW)["errors"] == []
    prediction_count = store.counts()["predictions"]
    same_context = service.capture_and_predict((later,), now=NOW)
    assert same_context["errors"] == []
    assert same_context["episodes_existing"] == 1
    assert same_context["predictions_appended"] == 0
    other_context = service.capture_and_predict((revised_context,), now=NOW)
    assert other_context["errors"] == []
    assert other_context["episodes_existing"] == 1
    assert other_context["predictions_appended"] == 0
    assert store.counts()["episodes"] == 1
    assert store.counts()["predictions"] == prediction_count
    assert recorder.seen == [first.episode_id]
    assert _stored_close(store, first.episode_id) == first.observation.anchor.close
    with pytest.raises(WorldModelConflictError, match="V2 market slot"):
        store.append_episode(later)
    with pytest.raises(WorldModelConflictError, match="V2 market slot"):
        store.append_episode(revised_context)


def test_v3_later_poll_with_revised_ohlcv_reuses_first_canonical_slot(tmp_path) -> None:
    first = _attach((_graph6_v1_episode(),), _unpublished_config())[0]
    later = _later_poll_episode(first, close=150.0)
    store = WorldModelStore(Path(tmp_path) / "world_model.db")
    recorder = GraphFilteringPredictor()
    service = _capture_service(store, recorder)
    assert service.capture_and_predict((first,), now=NOW)["errors"] == []
    prediction_count = store.counts()["predictions"]
    replay = service.capture_and_predict((later,), now=NOW)
    assert replay["errors"] == []
    assert replay["episodes_existing"] == 1
    assert replay["episodes_appended"] == 0
    assert replay["predictions_appended"] == 0
    assert store.counts()["episodes"] == 1
    assert store.counts()["predictions"] == prediction_count
    assert recorder.seen == [first.episode_id]
    assert _stored_close(store, first.episode_id) == first.observation.anchor.close
    with pytest.raises(WorldModelConflictError, match="V3 market slot"):
        store.append_episode(later)


def test_repeated_background_polls_do_not_duplicate_observations_or_predictions(tmp_path) -> None:
    first = _v1_episode()
    polls = (
        first,
        _later_poll_episode(first, minutes=5, close=150.0),
        _later_poll_episode(first, minutes=8, close=151.0),
    )
    store = WorldModelStore(Path(tmp_path) / "world_model.db")
    recorder = RecordingPredictor()
    gru = OnlineGRUWorldChallenger(hidden_size=4, sequence_len=4)
    service = _capture_service(store, recorder, gru)
    reports = [service.capture_and_predict((poll,), now=NOW + timedelta(minutes=index)) for index, poll in enumerate(polls)]
    assert all(report["errors"] == [] for report in reports)
    assert reports[0]["episodes_appended"] == 1
    assert reports[0]["predictions_appended"] == 2
    assert [report["episodes_existing"] for report in reports[1:]] == [1, 1]
    assert [report["predictions_appended"] for report in reports[1:]] == [0, 0]
    assert [report["model_updates"] for report in reports] == [0, 0, 0]
    assert recorder.seen == [first.episode_id]
    assert store.counts()["episodes"] == 1
    assert store.counts()["predictions"] == 2
    assert _stored_close(store, first.episode_id) == first.observation.anchor.close


def test_canonical_replay_still_admits_the_first_durable_episode_to_a_collecting_cohort(tmp_path) -> None:
    store = WorldModelStore(Path(tmp_path) / "world_model.db", clock=lambda: START_READY)
    cohort_service, cohort, ready = _collecting_cohort_service(store)
    first = _cohort_market_episode(ANCHOR_TS)
    later = _later_poll_episode(first, close=150.0)
    assert first.episode_id == later.episode_id
    from trader.runtime.world_model_runtime import WorldModelRuntime
    from trader.application.world_model.baseline import HierarchicalDirichletWorldBaseline

    runtime = WorldModelRuntime(
        store=store,
        predictor=HierarchicalDirichletWorldBaseline(),
        labeler=None,
        bar_provider=None,
        horizons=("elapsed_4h.v1",),
        cohort_service=cohort_service,
    )
    first_report = runtime.capture_and_predict((first,), now=ANCHOR_TS)
    assert first_report["errors"] == []
    replay = runtime.capture_and_predict((later,), now=ANCHOR_TS + timedelta(minutes=5))
    assert replay["errors"] == []
    assert replay["episodes_existing"] == 1
    assert replay["episodes_appended"] == 0
    slots = cohort_service.list_slots(cohort.cohort_id)
    assert len(slots) == 1
    assert slots[0].episode_refs_by_contract[V1.contract_id] == first.episode_id
    assert slots[0].anchor_end_at > ready
    assert _stored_close(store, first.episode_id) == first.observation.anchor.close
    assert store.counts()["episodes"] == 1


def test_v1_v2_v3_companions_each_reuse_their_own_canonical_slot(tmp_path) -> None:
    v1 = _v1_episode()
    v2 = attach_world_context(
        (v1,),
        _FakeSource(
            SensorEvidence(status="missing", reason="no_artifact"),
            SensorEvidence(status="missing", reason="no_artifact"),
        ),
    )[0]
    v3 = _v3_companion()
    later = (
        _later_poll_episode(v1, close=150.0),
        _later_poll_episode(v2, close=150.0),
        _later_poll_episode(v3, close=150.0),
    )
    store = WorldModelStore(Path(tmp_path) / "world_model.db")
    service = _capture_service(
        store,
        MarketFilteringPredictor(),
        FilteringPredictor(),
        GraphFilteringPredictor(),
    )
    first_report = service.capture_and_predict((v1, v2, v3), now=NOW)
    assert first_report["errors"] == []
    assert first_report["episodes_appended"] == 3
    replay = service.capture_and_predict(later, now=NOW + timedelta(minutes=5))
    assert replay["errors"] == []
    assert replay["episodes_existing"] == 3
    assert replay["episodes_appended"] == 0
    assert replay["predictions_appended"] == 0
    assert store.counts()["episodes"] == 3
    assert _stored_close(store, v1.episode_id) == v1.observation.anchor.close
    assert _stored_close(store, v2.episode_id) == v2.observation.anchor.close
    assert _stored_close(store, v3.episode_id) == v3.observation.anchor.close
