from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone

import pytest

from trader.application.world_model.baseline import HierarchicalDirichletWorldBaseline, MODEL_ID, MODEL_VERSION
from trader.application.world_model.encoding import (
    FEATURE_CONTRACT_FINGERPRINT,
    FEATURE_CONTRACT_FINGERPRINT_V2,
    FeatureBoundaryError,
)
from trader.application.world_model.context_capture import attach_world_context
from trader.application.world_model.gru import (
    ENCODER_VERSION,
    MODEL_ID as GRU_MODEL_ID,
    MODEL_VERSION as GRU_MODEL_VERSION,
    OnlineGRUWorldChallenger,
)
from trader.application.world_model.service import WorldModelService
from trader.domain.world_context import (
    ALLOWED_CONTEXT_CATEGORICAL_FEATURES,
    CONTEXT_FEATURE_CONTRACT_VERSION,
    SensorEvidence,
)
from trader.domain.world_episode import WorldPrediction

from tests.application.test_world_context_capture import _FakeSource, _v1_episode


NOW = datetime(2026, 8, 22, 10, 30, tzinfo=timezone.utc)


class MemoryStore:
    def __init__(self) -> None:
        self.episodes: dict[str, object] = {}
        self.predictions: list[object] = []
        self.outcomes: list[object] = []

    def append_episode(self, episode):
        identifier = episode.episode_id if hasattr(episode, "episode_id") else episode["episode_id"]
        if identifier in self.episodes:
            return False
        self.episodes[identifier] = episode
        return True

    def append_prediction(self, prediction):
        self.predictions.append(prediction)
        return True

    def append_outcome_event(self, outcome):
        self.outcomes.append(outcome)
        return True

    def get_episode(self, episode_id):
        return self.episodes.get(episode_id)

    def list_eligible_episodes(self):
        return list(self.episodes.values())

    def list_pending_episodes(self, **_kwargs):
        return []

    def list_predictions(self):
        return list(self.predictions)

    def list_observed_outcomes(self, **_kwargs):
        return []

    def list_outcome_events(self, **_kwargs):
        return []


def _contract_version(episode) -> str:
    if hasattr(episode, "observation"):
        return str(episode.observation.feature_contract_version)
    if isinstance(episode, dict):
        observation = episode.get("observation") or episode
        return str((observation or {}).get("feature_contract_version") or "")
    return ""


class RecordingPredictor:
    model_id = "recording_predictor"
    model_version = "v1"

    def __init__(self) -> None:
        self.seen: list[str] = []
        self.updated: list[str] = []

    def accepts_episode(self, episode) -> bool:
        version = _contract_version(episode)
        return version != CONTEXT_FEATURE_CONTRACT_VERSION

    def predict(self, episode, horizon_id, *, prediction_at=None):
        identifier = episode.episode_id if hasattr(episode, "episode_id") else episode["episode_id"]
        self.seen.append(identifier)
        return WorldPrediction(
            episode_id=identifier,
            horizon_id=horizon_id,
            model_id=self.model_id,
            model_version=self.model_version,
            feature_hash="abc",
            created_at=prediction_at or NOW,
            probabilities={"DOWN": 0.2, "FLAT": 0.3, "UP": 0.5},
            status="shadow_only",
        )

    def apply_outcome(self, outcome, episode, *, available_through=None):
        identifier = episode.episode_id if hasattr(episode, "episode_id") else episode["episode_id"]
        self.updated.append(identifier)
        return {"applied": True}


class FilteringPredictor(RecordingPredictor):
    model_id = "filtering_predictor"
    model_version = "context.v2"

    def accepts_episode(self, episode) -> bool:
        return _contract_version(episode) == CONTEXT_FEATURE_CONTRACT_VERSION


def _v2_models() -> tuple[HierarchicalDirichletWorldBaseline, OnlineGRUWorldChallenger]:
    return (
        HierarchicalDirichletWorldBaseline(
            model_version="context.v2",
            include_context=True,
            accepted_feature_contracts=frozenset({CONTEXT_FEATURE_CONTRACT_VERSION}),
        ),
        OnlineGRUWorldChallenger(
            model_version="context.v2",
            encoder_version="world_gru_encoder.v2",
            include_context=True,
            extra_categorical_keys=ALLOWED_CONTEXT_CATEGORICAL_FEATURES,
            accepted_feature_contracts=frozenset({CONTEXT_FEATURE_CONTRACT_VERSION}),
        ),
    )


def test_four_model_identities_are_distinct_and_v1_stays_frozen() -> None:
    markov_v1 = HierarchicalDirichletWorldBaseline()
    gru_v1 = OnlineGRUWorldChallenger()
    markov_v2, gru_v2 = _v2_models()
    identities = {
        (markov_v1.model_id, markov_v1.model_version),
        (gru_v1.model_id, gru_v1.model_version),
        (markov_v2.model_id, markov_v2.model_version),
        (gru_v2.model_id, gru_v2.model_version),
    }
    assert identities == {
        (MODEL_ID, MODEL_VERSION),
        (GRU_MODEL_ID, GRU_MODEL_VERSION),
        (MODEL_ID, "context.v2"),
        (GRU_MODEL_ID, "context.v2"),
    }
    assert gru_v1.encoder_version == ENCODER_VERSION
    assert gru_v2.encoder_version == "world_gru_encoder.v2"
    assert gru_v1.model_fingerprint("elapsed_4h.v1") != gru_v2.model_fingerprint("elapsed_4h.v1")
    v1 = _v1_episode()
    v2 = attach_world_context(
        (v1,),
        _FakeSource(
            SensorEvidence(status="missing", reason="no_artifact"),
            SensorEvidence(status="missing", reason="no_artifact"),
        ),
    )[0]
    v1_audit = markov_v1.predict_audit(v1, "elapsed_4h.v1")
    v2_audit = markov_v2.predict_audit(v2, "elapsed_4h.v1")
    assert v1_audit.as_dict()["feature_contract_fingerprint"] == FEATURE_CONTRACT_FINGERPRINT
    assert v2_audit.as_dict()["feature_contract_fingerprint"] == FEATURE_CONTRACT_FINGERPRINT_V2
    assert FEATURE_CONTRACT_FINGERPRINT == "2b4023b7bab99cd39f3592c45b7b8147ad94a7de18684b7896f6daf1a454603c"


def test_v1_and_v2_predictors_do_not_learn_from_each_other() -> None:
    v1 = _v1_episode()
    v2 = attach_world_context(
        (v1,),
        _FakeSource(
            SensorEvidence(status="missing", reason="no_artifact"),
            SensorEvidence(status="missing", reason="no_artifact"),
        ),
    )[0]
    market = RecordingPredictor()
    context = FilteringPredictor()
    service = WorldModelService(
        store=MemoryStore(),
        predictor=market,
        predictors=(context,),
        labeler=None,
        bar_provider=None,
    )
    service.capture_and_predict((v1, v2), now=NOW)
    assert set(market.seen) == {v1.episode_id}
    assert set(context.seen) == {v2.episode_id}


class LegacyPredictor:
    model_id = "legacy_predictor"
    model_version = "v1"

    def __init__(self) -> None:
        self.seen: list[str] = []

    def predict(self, episode, horizon_id, *, prediction_at=None):
        identifier = episode.episode_id if hasattr(episode, "episode_id") else episode["episode_id"]
        self.seen.append(identifier)
        return WorldPrediction(
            episode_id=identifier,
            horizon_id=horizon_id,
            model_id=self.model_id,
            model_version=self.model_version,
            feature_hash="abc",
            created_at=prediction_at or NOW,
            probabilities={"DOWN": 0.2, "FLAT": 0.3, "UP": 0.5},
            status="shadow_only",
        )


def test_legacy_predictor_without_accepts_episode_still_sees_all_rows() -> None:
    v1 = _v1_episode()
    predictor = LegacyPredictor()
    service = WorldModelService(
        store=MemoryStore(),
        predictor=predictor,
        labeler=None,
        bar_provider=None,
    )
    service.capture_and_predict((v1,), now=NOW)
    assert v1.episode_id in predictor.seen


def test_direct_v1_and_v2_apis_reject_the_other_contract() -> None:
    v1 = _v1_episode()
    v2 = attach_world_context(
        (v1,),
        _FakeSource(
            SensorEvidence(status="missing", reason="no_artifact"),
            SensorEvidence(status="missing", reason="no_artifact"),
        ),
    )[0]
    markov_v1 = HierarchicalDirichletWorldBaseline()
    gru_v1 = OnlineGRUWorldChallenger(hidden_size=4, sequence_len=4)
    markov_v2, gru_v2 = _v2_models()
    with pytest.raises(FeatureBoundaryError):
        markov_v1.predict(v2, "elapsed_4h.v1")
    with pytest.raises(FeatureBoundaryError):
        gru_v1.predict(v2, "elapsed_4h.v1")
    with pytest.raises(FeatureBoundaryError):
        markov_v2.predict(v1, "elapsed_4h.v1")
    with pytest.raises(FeatureBoundaryError):
        gru_v2.observe_episode(v1)
    outcome = {
        "outcome_event_id": "evt-1",
        "horizon_id": "elapsed_4h.v1",
        "status": "observed",
        "training_eligible": True,
        "available_at": "2026-08-22T14:05:00+00:00",
        "direction": "UP",
        "episode_id": v2.episode_id,
    }
    update = markov_v1.apply_outcome(outcome, v2)
    assert update.applied is False
    assert update.reason == "feature_contract_rejected"
    gru_update = gru_v1.apply_outcome(outcome, v2)
    assert gru_update.applied is False
    assert gru_update.reason == "feature_contract_rejected"
    assert gru_v1.support("elapsed_4h.v1") == 0


def _v2_context_pair():
    from trader.domain.world_context import EntityRef, KnowledgeArtifact

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


def test_service_reuses_first_canonical_v2_slot_across_context_ids_and_restart(tmp_path) -> None:
    from pathlib import Path

    from trader.infrastructure.state_db.world_model_store import WorldModelConflictError, WorldModelStore

    first, second = _v2_context_pair()
    assert first.episode_id != second.episode_id
    store = WorldModelStore(Path(tmp_path) / "world_model.db")
    recorder = FilteringPredictor()
    service = WorldModelService(
        store=store,
        predictor=recorder,
        labeler=None,
        bar_provider=None,
        horizons=("elapsed_4h.v1",),
    )
    first_report = service.capture_and_predict((first,), now=NOW)
    assert first_report["episodes_appended"] == 1
    second_report = service.capture_and_predict((second,), now=NOW)
    assert second_report["errors"] == []
    assert second_report["episodes_existing"] == 1
    assert store.counts()["episodes"] == 1
    assert recorder.seen == [first.episode_id]
    with pytest.raises(WorldModelConflictError):
        store.append_episode(second)
    predictions = store.list_predictions()
    assert predictions
    assert {row["episode_id"] for row in predictions} == {first.episode_id}

    restarted_recorder = FilteringPredictor()
    restarted = WorldModelService(
        store=store,
        predictor=restarted_recorder,
        labeler=None,
        bar_provider=None,
        horizons=("elapsed_4h.v1",),
    )
    replay = restarted.capture_and_predict((second,), now=NOW)
    assert replay["errors"] == []
    assert store.counts()["episodes"] == 1
    assert store.get_episode(second.episode_id) is None
    assert store.get_episode(first.episode_id) is not None
    assert {row["episode_id"] for row in store.list_predictions()} == {first.episode_id}


def test_service_fails_closed_on_v2_market_evidence_conflict(tmp_path) -> None:
    from pathlib import Path

    from trader.domain.world_episode import WorldEpisode, WorldObservation
    from trader.infrastructure.state_db.world_model_store import WorldModelStore

    first, _second = _v2_context_pair()
    v1 = _v1_episode()
    observation = v1.observation
    divergent = WorldEpisode(
        WorldObservation(
            venue=observation.venue,
            symbol=observation.symbol,
            bar_interval=observation.bar_interval,
            as_of_bar_ts=observation.as_of_bar_ts,
            feature_contract_version=observation.feature_contract_version,
            sampling_policy_version=observation.sampling_policy_version,
            anchor={**observation.anchor.to_dict(), "high": 200.0, "close": 150.0},
            available_at=observation.available_at,
            captured_at=observation.captured_at,
            freshness=observation.freshness,
            categorical_features=dict(observation.categorical_features),
            numeric_features={**dict(observation.numeric_features), "return": -0.4},
        )
    )
    missing = SensorEvidence(status="missing", reason="no_artifact")
    artifact_second = attach_world_context(
        (divergent,),
        _FakeSource(
            missing,
            SensorEvidence(
                status="complete",
                reason="sidecar_ready",
                proven=True,
                payload={
                    "symbol": "AAA",
                    "company_thesis": {"status": "intact"},
                    "coverage": {"status": "partial"},
                    "source_refs": ["src-b"],
                },
                artifact=_second_company_artifact(),
            ),
        ),
    )[0]
    store = WorldModelStore(Path(tmp_path) / "world_model.db")
    service = WorldModelService(
        store=store,
        predictor=FilteringPredictor(),
        labeler=None,
        bar_provider=None,
        horizons=("elapsed_4h.v1",),
    )
    assert service.capture_and_predict((first,), now=NOW)["errors"] == []
    report = service.capture_and_predict((artifact_second,), now=NOW)
    assert report["errors"]
    assert any("market_evidence_conflict" in str(item.get("error") or "") for item in report["errors"])
    assert store.counts()["episodes"] == 1


def _replace_offset_with_z(value: object) -> object:
    if isinstance(value, dict):
        return {key: _replace_offset_with_z(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_replace_offset_with_z(item) for item in value]
    if isinstance(value, str) and value.endswith("+00:00"):
        return value[: -len("+00:00")] + "Z"
    return value


@pytest.mark.parametrize(
    "zulu_first",
    [True, False],
    ids=["zulu_then_offset", "offset_then_zulu"],
)
def test_service_reuses_v2_slot_across_zulu_and_offset_spellings(tmp_path, zulu_first: bool) -> None:
    from pathlib import Path

    from trader.infrastructure.state_db.world_model_store import WorldModelStore

    first, _second = _v2_context_pair()
    offset_payload = first.to_dict()
    zulu_payload = _replace_offset_with_z(deepcopy(offset_payload))
    assert isinstance(zulu_payload, dict)
    arriving = (zulu_payload, offset_payload) if zulu_first else (offset_payload, zulu_payload)
    store = WorldModelStore(Path(tmp_path) / "world_model.db")
    service = WorldModelService(
        store=store,
        predictor=FilteringPredictor(),
        labeler=None,
        bar_provider=None,
        horizons=("elapsed_4h.v1",),
    )
    first_report = service.capture_and_predict((arriving[0],), now=NOW)
    assert first_report["errors"] == []
    assert first_report["episodes_appended"] == 1
    second_report = service.capture_and_predict((arriving[1],), now=NOW)
    assert second_report["errors"] == []
    assert second_report["episodes_existing"] == 1
    assert second_report["episodes_appended"] == 0
    assert store.counts()["episodes"] == 1

    restarted = WorldModelService(
        store=store,
        predictor=FilteringPredictor(),
        labeler=None,
        bar_provider=None,
        horizons=("elapsed_4h.v1",),
    )
    replay = restarted.capture_and_predict((arriving[1],), now=NOW)
    assert replay["errors"] == []
    assert replay["episodes_existing"] == 1
    assert replay["episodes_appended"] == 0
    assert store.counts()["episodes"] == 1


def _second_company_artifact():
    from trader.domain.world_context import EntityRef, KnowledgeArtifact

    return KnowledgeArtifact(
        kind="company_intelligence",
        artifact_id="aaa-divergent",
        subjects=[EntityRef("instrument", "AAA")],
        schema_version="company_intelligence_brief.v1",
        content_sha256="def",
        ready_at="2026-08-22T09:00:00+00:00",
    )
