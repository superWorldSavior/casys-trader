from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone

import pytest

from trader.application.world_model.baseline import HierarchicalDirichletWorldBaseline, MODEL_ID, MODEL_VERSION
from trader.application.world_model.encoding import (
    FEATURE_CONTRACT_FINGERPRINT,
    FEATURE_CONTRACT_FINGERPRINT_CONTEXT,
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
    CONTEXT_FEATURE_CONTRACT_ID,
    SensorEvidence,
)
from trader.domain.world_episode import MARKET_FEATURE_CONTRACT_ID, WorldPrediction

from tests.application.test_world_context_capture import _FakeSource, _market_episode


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

    def list_prediction_identities(self):
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
        return version != CONTEXT_FEATURE_CONTRACT_ID

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
    model_version = "context.v1"

    def accepts_episode(self, episode) -> bool:
        return _contract_version(episode) == CONTEXT_FEATURE_CONTRACT_ID


def _context_models() -> tuple[HierarchicalDirichletWorldBaseline, OnlineGRUWorldChallenger]:
    return (
        HierarchicalDirichletWorldBaseline(
            model_version="context.v1",
            include_context=True,
            accepted_feature_contracts=frozenset({CONTEXT_FEATURE_CONTRACT_ID}),
        ),
        OnlineGRUWorldChallenger(
            model_version="context.v1",
            encoder_version="world_gru_encoder.context.v1",
            include_context=True,
            extra_categorical_keys=ALLOWED_CONTEXT_CATEGORICAL_FEATURES,
            accepted_feature_contracts=frozenset({CONTEXT_FEATURE_CONTRACT_ID}),
        ),
    )


def test_four_model_identities_are_distinct_and_market_stays_frozen() -> None:
    markov_v1 = HierarchicalDirichletWorldBaseline()
    gru_market = OnlineGRUWorldChallenger()
    markov_v2, gru_context = _context_models()
    identities = {
        (markov_v1.model_id, markov_v1.model_version),
        (gru_market.model_id, gru_market.model_version),
        (markov_v2.model_id, markov_v2.model_version),
        (gru_context.model_id, gru_context.model_version),
    }
    assert identities == {
        (MODEL_ID, MODEL_VERSION),
        (GRU_MODEL_ID, GRU_MODEL_VERSION),
        (MODEL_ID, "context.v1"),
        (GRU_MODEL_ID, "context.v1"),
    }
    assert gru_market.encoder_version == ENCODER_VERSION
    assert gru_context.encoder_version == "world_gru_encoder.context.v1"
    assert gru_market.model_fingerprint("elapsed_4h.v1") != gru_context.model_fingerprint("elapsed_4h.v1")
    v1 = _market_episode()
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
    assert v2_audit.as_dict()["feature_contract_fingerprint"] == FEATURE_CONTRACT_FINGERPRINT_CONTEXT
    assert FEATURE_CONTRACT_FINGERPRINT == "4f00c35f1989b02ac504e9cf2c102c9b045f3eff74da22b5a4d31fb0e03738b6"


def test_market_and_context_predictors_do_not_learn_from_each_other() -> None:
    v1 = _market_episode()
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
    v1 = _market_episode()
    predictor = LegacyPredictor()
    service = WorldModelService(
        store=MemoryStore(),
        predictor=predictor,
        labeler=None,
        bar_provider=None,
    )
    service.capture_and_predict((v1,), now=NOW)
    assert v1.episode_id in predictor.seen


def test_direct_market_and_context_apis_reject_the_other_contract() -> None:
    v1 = _market_episode()
    v2 = attach_world_context(
        (v1,),
        _FakeSource(
            SensorEvidence(status="missing", reason="no_artifact"),
            SensorEvidence(status="missing", reason="no_artifact"),
        ),
    )[0]
    markov_v1 = HierarchicalDirichletWorldBaseline()
    gru_market = OnlineGRUWorldChallenger(hidden_size=4, sequence_len=4)
    markov_v2, gru_context = _context_models()
    with pytest.raises(FeatureBoundaryError):
        markov_v1.predict(v2, "elapsed_4h.v1")
    with pytest.raises(FeatureBoundaryError):
        gru_market.predict(v2, "elapsed_4h.v1")
    with pytest.raises(FeatureBoundaryError):
        markov_v2.predict(v1, "elapsed_4h.v1")
    with pytest.raises(FeatureBoundaryError):
        gru_context.observe_episode(v1)
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
    gru_update = gru_market.apply_outcome(outcome, v2)
    assert gru_update.applied is False
    assert gru_update.reason == "feature_contract_rejected"
    assert gru_market.support("elapsed_4h.v1") == 0


def _context_pair():
    from trader.domain.world_context import EntityRef, KnowledgeArtifact

    v1 = _market_episode()
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


def test_service_reuses_first_canonical_context_slot_across_context_ids_and_restart(tmp_path) -> None:
    from pathlib import Path

    from trader.infrastructure.state_db.world_model_store import WorldModelConflictError, WorldModelStore

    first, second = _context_pair()
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


def test_service_reuses_canonical_context_slot_when_later_poll_revises_ohlcv_and_context(tmp_path) -> None:
    from pathlib import Path

    from trader.domain.world_episode import WorldEpisode, WorldObservation
    from trader.infrastructure.state_db.world_model_store import WorldModelConflictError, WorldModelStore

    first, _second = _context_pair()
    v1 = _market_episode()
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
    assert report["errors"] == []
    assert report["episodes_existing"] == 1
    assert report["episodes_appended"] == 0
    assert store.counts()["episodes"] == 1
    stored = store.get_episode(first.episode_id)
    assert stored is not None
    assert float(stored["episode"]["observation"]["anchor"]["close"]) == first.observation.anchor.close
    with pytest.raises(WorldModelConflictError, match="context market slot"):
        store.append_episode(artifact_second)


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
def test_service_reuses_context_slot_across_zulu_and_offset_spellings(tmp_path, zulu_first: bool) -> None:
    from pathlib import Path

    from trader.infrastructure.state_db.world_model_store import WorldModelStore

    first, _second = _context_pair()
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


_COHORT_LINEAGE_FIELDS = (
    "study_cohort_id",
    "lane_id",
    "manifest_sha256",
    "feature_contract_fingerprint",
    "feature_mask_fingerprint",
)


def _collecting_cohort_service(store):
    from trader.application.world_model.cohort_service import WorldCohortService
    from trader.domain.world_cohort import RegisterWorldCohort, WorldCohortId
    from tests.application.test_world_cohort_service import (
        START_READY,
        _arm_command,
        _manifest,
        _start_command,
    )

    service = WorldCohortService(repository=store, query=store)
    manifest = _manifest()
    service.register(RegisterWorldCohort(manifest=manifest))
    service.arm(_arm_command(manifest))
    service.start(_start_command(manifest))
    return service, store.load(WorldCohortId(manifest.cohort_id)), START_READY


def _cohort_market_episode(at, *, symbol: str = "AAPL", market_return: float = 0.01):
    from datetime import timedelta

    from trader.domain.world_episode import (
        MARKET_FEATURE_CONTRACT_ID,
        AnchorBar,
        WorldEpisode,
        WorldObservation,
    )

    return WorldEpisode(
        observation=WorldObservation(
            venue="US",
            symbol=symbol,
            bar_interval="1h",
            as_of_bar_ts=at,
            feature_contract_version=MARKET_FEATURE_CONTRACT_ID,
            sampling_policy_version="active_tradable_completed_bar.v1",
            anchor=AnchorBar(
                ts=at,
                open=100.0,
                high=101.0,
                low=99.0,
                close=100.0,
                volume=1_000.0,
                source="fixture",
            ),
            available_at=at,
            captured_at=at + timedelta(minutes=1),
            freshness="fresh",
            categorical_features={
                "asset_family": "equities",
                "venue": "US",
            },
            numeric_features={"return": market_return, "atr_pct": 0.01},
        )
    )


def test_market_shadow_predictions_omit_cohort_lineage_when_no_cohort_is_active() -> None:
    v1 = _market_episode()
    store = MemoryStore()
    service = WorldModelService(
        store=store,
        predictor=HierarchicalDirichletWorldBaseline(),
        labeler=None,
        bar_provider=None,
        horizons=("elapsed_4h.v1",),
    )
    report = service.capture_and_predict((v1,), now=NOW)
    assert report["errors"] == []
    assert report["predictions_appended"] == 1
    row = store.predictions[0]
    for field in _COHORT_LINEAGE_FIELDS:
        assert not row.get(field)
        nested = row.get("prediction") if isinstance(row.get("prediction"), dict) else {}
        assert not nested.get(field)


def test_collecting_cohort_admits_slots_and_stamps_lane_lineage_on_predictions(tmp_path) -> None:
    from pathlib import Path

    from trader.domain.world_cohort import WorldCohortId
    from trader.infrastructure.state_db.world_model_store import WorldModelStore
    from trader.runtime.world_model_runtime import WorldModelRuntime
    from tests.application.test_world_cohort_service import ANCHOR_TS, START_READY, V1

    store = WorldModelStore(Path(tmp_path) / "world_model.db", clock=lambda: START_READY)
    try:
        cohort_service, cohort, _ready = _collecting_cohort_service(store)
        episode = _cohort_market_episode(ANCHOR_TS)
        runtime = WorldModelRuntime(
            store=store,
            predictor=HierarchicalDirichletWorldBaseline(),
            labeler=None,
            bar_provider=None,
            horizons=("elapsed_4h.v1",),
            cohort_service=cohort_service,
        )
        report = runtime.capture_and_predict((episode,), now=ANCHOR_TS)
        assert report["errors"] == []
        slots = cohort_service.list_slots(WorldCohortId(cohort.cohort_id))
        assert len(slots) == 1
        assert slots[0].started_event_id == cohort.started_event.event_id
        assert slots[0].episode_refs_by_contract[V1.contract_id] == episode.episode_id
        assert slots[0].anchor_end_at > _ready
        rows = store.list_predictions()
        cohort_rows = [row for row in rows if row.get("study_cohort_id")]
        assert cohort_rows
        lane_ids = {row["lane_id"] for row in cohort_rows}
        assert "markov.market" in lane_ids
        for row in cohort_rows:
            for field in _COHORT_LINEAGE_FIELDS:
                assert row[field]
                assert row["prediction_record"][field] == row[field]
                if isinstance(row.get("prediction"), dict) and field in row["prediction"]:
                    assert row["prediction"][field] == row[field]
            assert row["study_cohort_id"] == cohort.cohort_id
            assert row["manifest_sha256"] == cohort.manifest.manifest_sha256
            lane = next(item for item in cohort.manifest.lanes if item.lane_id == row["lane_id"])
            assert row["feature_contract_fingerprint"] == lane.feature_contract_fingerprint
            assert row["feature_mask_fingerprint"] == lane.feature_mask_fingerprint
            nested = row["prediction"] if isinstance(row.get("prediction"), dict) else {}
            record = row["prediction_record"] if isinstance(row.get("prediction_record"), dict) else {}
            assert nested.get("authority") == "shadow_only" or record.get("authority") == "shadow_only"
            assert nested.get("decision_effect", record.get("decision_effect", "none")) == "none"
        v1_rows = [row for row in rows if not row.get("study_cohort_id")]
        assert v1_rows
    finally:
        store.close()


def test_cold_factories_and_lane_predictions_require_proven_world_cohort_started(tmp_path) -> None:
    from pathlib import Path

    from trader.application.world_model.cohort_service import WorldCohortService
    from trader.domain.world_cohort import RegisterWorldCohort, WorldCohortId
    from trader.infrastructure.state_db.world_model_store import WorldModelStore
    from trader.runtime.world_model_runtime import WorldModelRuntime
    from tests.application.test_world_cohort_service import (
        ANCHOR_TS,
        START_READY,
        _arm_command,
        _manifest,
        _start_command,
    )

    store = WorldModelStore(Path(tmp_path) / "world_model.db", clock=lambda: START_READY)
    try:
        service = WorldCohortService(repository=store, query=store)
        manifest = _manifest()
        service.register(RegisterWorldCohort(manifest=manifest))
        service.arm(_arm_command(manifest))

        def boom(*_args, **_kwargs):
            raise OSError("receipt commit failed")

        original = store._commit_availability_receipt
        store._commit_availability_receipt = boom  # type: ignore[method-assign]
        try:
            with pytest.raises(OSError, match="receipt commit failed"):
                service.start(_start_command(manifest))
        finally:
            store._commit_availability_receipt = original  # type: ignore[method-assign]

        loaded = store.load(WorldCohortId(manifest.cohort_id))
        assert loaded.started_event is not None
        unproven = store.envelope_for(loaded.started_event)
        assert unproven.availability_status == "availability_unproven"
        with pytest.raises(ValueError, match="proven|availability_unproven"):
            service.cold_lanes(WorldCohortId(manifest.cohort_id))

        episode = _cohort_market_episode(ANCHOR_TS)
        runtime = WorldModelRuntime(
            store=store,
            predictor=HierarchicalDirichletWorldBaseline(),
            labeler=None,
            bar_provider=None,
            horizons=("elapsed_4h.v1",),
            cohort_service=service,
        )
        report = runtime.capture_and_predict((episode,), now=ANCHOR_TS)
        assert report["errors"] == []
        assert service.list_slots(WorldCohortId(manifest.cohort_id)) == ()
        rows = store.list_predictions()
        assert rows
        assert all(not row.get("study_cohort_id") for row in rows)
        assert all(not row.get("lane_id") for row in rows)
    finally:
        store.close()


def test_pre_start_episodes_are_not_replayed_and_downtime_is_not_backfilled(tmp_path) -> None:
    from datetime import timedelta
    from pathlib import Path

    from trader.application.world_model.baseline import HierarchicalDirichletWorldBaseline
    from trader.infrastructure.state_db.world_model_store import WorldModelStore
    from trader.runtime.world_model_runtime import WorldModelRuntime
    from tests.application.test_world_cohort_service import ANCHOR_TS, LATER_TS, START_READY

    store = WorldModelStore(Path(tmp_path) / "world_model.db", clock=lambda: START_READY)
    try:
        pre_start = _cohort_market_episode(START_READY - timedelta(hours=2), symbol="AAPL")
        shadow = WorldModelService(
            store=store,
            predictor=HierarchicalDirichletWorldBaseline(),
            labeler=None,
            bar_provider=None,
            horizons=("elapsed_4h.v1",),
        )
        assert shadow.capture_and_predict((pre_start,), now=START_READY - timedelta(hours=2))["errors"] == []

        cohort_service, cohort, ready = _collecting_cohort_service(store)
        first = _cohort_market_episode(ANCHOR_TS, symbol="AAPL", market_return=0.02)
        later = _cohort_market_episode(LATER_TS, symbol="AAPL", market_return=-0.01)
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
        later_report = runtime.capture_and_predict((later,), now=LATER_TS)
        assert later_report["errors"] == []

        slots = cohort_service.list_slots(cohort.cohort_id)
        assert len(slots) == 2
        admitted_as_of = {slot.as_of_bar_ts for slot in slots}
        assert pre_start.observation.as_of_bar_ts not in admitted_as_of
        assert first.observation.as_of_bar_ts in admitted_as_of
        assert later.observation.as_of_bar_ts in admitted_as_of
        gap_hours = (LATER_TS - ANCHOR_TS).total_seconds() / 3600
        assert gap_hours == 2
        expected_missing_hours = {ANCHOR_TS + timedelta(hours=1)}
        assert expected_missing_hours.isdisjoint(admitted_as_of)

        market_lane = next(
            predictor
            for predictor in runtime.predictors
            if getattr(getattr(predictor, "lane_identity", None), "lane_id", None) == "markov.market"
        )
        assert market_lane.lane_identity.replay_bound_event_id == cohort.started_event.event_id
        applied = [
            event_id for counts in getattr(market_lane, "_horizons", {}).values() for event_id in counts.applied_events
        ]
        assert pre_start.episode_id not in "".join(applied)
        assert all(slot.anchor_end_at > ready for slot in slots)
    finally:
        store.close()


class GraphFilteringPredictor(RecordingPredictor):
    model_id = "graph_filtering_predictor"
    model_version = "graph.v1"

    def accepts_episode(self, episode) -> bool:
        from trader.domain.world_feature_contract import GRAPH_FEATURE_CONTRACT_ID

        return _contract_version(episode) == GRAPH_FEATURE_CONTRACT_ID


class MarketFilteringPredictor(RecordingPredictor):
    model_id = "market_filtering_predictor"
    model_version = "v1"

    def accepts_episode(self, episode) -> bool:
        return _contract_version(episode) == MARKET_FEATURE_CONTRACT_ID


def _graph_models():
    from trader.application.world_model.encoding import world_lane_encoder_profile
    from trader.domain.world_feature_contract import (
        GRAPH_FEATURE_CONTRACT_ID,
        GRAPH_GRU_MODEL_IDENTITY,
        GRAPH_MARKOV_MODEL_IDENTITY,
        GRAPH_MODEL_VERSION,
    )

    status = world_lane_encoder_profile("topology_status_only")
    content = world_lane_encoder_profile("graph_content")
    markov = HierarchicalDirichletWorldBaseline(
        model_id=GRAPH_MARKOV_MODEL_IDENTITY,
        model_version=GRAPH_MODEL_VERSION,
        feature_contract=status.contract,
        feature_mask=status.mask,
        accepted_feature_contracts=frozenset({GRAPH_FEATURE_CONTRACT_ID}),
    )
    gru = OnlineGRUWorldChallenger(
        model_id=GRAPH_GRU_MODEL_IDENTITY,
        model_version=GRAPH_MODEL_VERSION,
        feature_contract=content.contract,
        feature_mask=content.mask,
        hidden_size=4,
        sequence_len=4,
        accepted_feature_contracts=frozenset({GRAPH_FEATURE_CONTRACT_ID}),
    )
    return markov, gru


def _graph_companion():
    from tests.application.test_world_graph_capture import _attach, _unmapped_config

    return _attach((_market_episode(),), _unmapped_config())[0]


def test_graph_markov_and_gru_identities_are_distinct_and_refuse_other_contracts() -> None:
    from trader.application.world_model.baseline import cold_markov_challenger
    from trader.application.world_model.encoding import world_lane_encoder_profile
    from trader.application.world_model.gru import GRAPH_GRU_ENCODER_IDENTITY, cold_gru_challenger
    from trader.domain.world_cohort import WorldLaneDefinition
    from trader.domain.world_episode import canonical_sha256
    from trader.domain.world_feature_contract import (
        GRAPH_GRU_MODEL_IDENTITY,
        GRAPH_MARKOV_MODEL_IDENTITY,
        GRAPH_MODEL_VERSION,
    )

    v1 = _market_episode()
    v2 = attach_world_context(
        (v1,),
        _FakeSource(
            SensorEvidence(status="missing", reason="no_artifact"),
            SensorEvidence(status="missing", reason="no_artifact"),
        ),
    )[0]
    v3 = _graph_companion()
    markov_graph, gru_graph = _graph_models()
    assert markov_graph.model_id == GRAPH_MARKOV_MODEL_IDENTITY
    assert gru_graph.model_id == GRAPH_GRU_MODEL_IDENTITY
    assert markov_graph.model_version == gru_graph.model_version == GRAPH_MODEL_VERSION
    assert gru_graph.encoder_version == GRAPH_GRU_ENCODER_IDENTITY
    assert markov_graph.accepts_episode(v3) is True
    assert gru_graph.accepts_episode(v3) is True
    assert markov_graph.accepts_episode(v1) is False
    assert gru_graph.accepts_episode(v2) is False
    markov_v1 = HierarchicalDirichletWorldBaseline()
    gru_market = OnlineGRUWorldChallenger(hidden_size=4, sequence_len=4)
    markov_v2, gru_context = _context_models()
    assert markov_v1.accepts_episode(v3) is False
    assert gru_market.accepts_episode(v3) is False
    assert markov_v2.accepts_episode(v3) is False
    assert gru_context.accepts_episode(v3) is False
    with pytest.raises(FeatureBoundaryError):
        markov_v1.predict(v3, "elapsed_4h.v1")
    with pytest.raises(FeatureBoundaryError):
        gru_graph.predict(v1, "elapsed_4h.v1")

    status = world_lane_encoder_profile("topology_status_only")
    lane = WorldLaneDefinition(
        lane_id="markov.topology_status_only",
        model_family="markov",
        model_id=GRAPH_MARKOV_MODEL_IDENTITY,
        model_version=GRAPH_MODEL_VERSION,
        feature_contract_id=status.contract.contract_id,
        feature_contract_fingerprint=status.contract.fingerprint,
        feature_mask_id=status.mask.mask_id,
        feature_mask_fingerprint=status.mask.fingerprint,
        seed=0,
        sequence_length=None,
        hyperparameters_sha256=canonical_sha256({"family": "markov", "lane": "topology_status_only"}),
        role="secondary_challenger",
    )
    cold = cold_markov_challenger(
        lane=lane,
        contract=status.contract,
        mask=status.mask,
        study_cohort_id="world_cohort:v1:" + "c" * 64,
        manifest_sha256="a" * 64,
        started_event_id="world_cohort_started:v1:" + "d" * 64,
        alpha=1.0,
        minimum_global_support=1,
        minimum_coarse_support=1,
        minimum_exact_support=2,
    )
    assert cold.accepts_episode(v3) is True
    assert cold.accepts_episode(v1) is False
    content = world_lane_encoder_profile("graph_content")
    gru_lane = WorldLaneDefinition(
        lane_id="gru.graph_content",
        model_family="gru",
        model_id=GRAPH_GRU_MODEL_IDENTITY,
        model_version=GRAPH_MODEL_VERSION,
        feature_contract_id=content.contract.contract_id,
        feature_contract_fingerprint=content.contract.fingerprint,
        feature_mask_id=content.mask.mask_id,
        feature_mask_fingerprint=content.mask.fingerprint,
        seed=0,
        sequence_length=4,
        hyperparameters_sha256=canonical_sha256({"family": "gru", "lane": "graph_content"}),
        role="secondary_challenger",
    )
    cold_gru = cold_gru_challenger(
        lane=gru_lane,
        contract=content.contract,
        mask=content.mask,
        study_cohort_id="world_cohort:v1:" + "c" * 64,
        manifest_sha256="a" * 64,
        started_event_id="world_cohort_started:v1:" + "d" * 64,
        hidden_size=4,
    )
    assert cold_gru.accepts_episode(v3) is True
    assert cold_gru.accepts_episode(v2) is False


def test_service_dispatches_market_context_graph_companions_on_the_same_market_slot() -> None:
    v1 = _market_episode()
    v2 = attach_world_context(
        (v1,),
        _FakeSource(
            SensorEvidence(status="missing", reason="no_artifact"),
            SensorEvidence(status="missing", reason="no_artifact"),
        ),
    )[0]
    v3 = _graph_companion()
    assert (v1.observation.venue, v1.observation.symbol, v1.observation.bar_interval, v1.observation.as_of_bar_ts) == (
        v2.observation.venue,
        v2.observation.symbol,
        v2.observation.bar_interval,
        v2.observation.as_of_bar_ts,
    )
    assert (v1.observation.venue, v1.observation.symbol, v1.observation.bar_interval, v1.observation.as_of_bar_ts) == (
        v3.observation.venue,
        v3.observation.symbol,
        v3.observation.bar_interval,
        v3.observation.as_of_bar_ts,
    )
    market = MarketFilteringPredictor()
    context = FilteringPredictor()
    graph = GraphFilteringPredictor()
    service = WorldModelService(
        store=MemoryStore(),
        predictor=market,
        predictors=(context, graph),
        labeler=None,
        bar_provider=None,
    )
    report = service.capture_and_predict((v1, v2, v3), now=NOW)
    assert report["errors"] == []
    assert report["episodes_appended"] == 3
    assert set(market.seen) == {v1.episode_id}
    assert set(context.seen) == {v2.episode_id}
    assert set(graph.seen) == {v3.episode_id}


def test_graph_capture_failure_does_not_prevent_market_context_persistence() -> None:
    from trader.domain.world_feature_contract import GRAPH_FEATURE_CONTRACT_ID

    class FailOpenStore(MemoryStore):
        def append_episode(self, episode):
            if _contract_version(episode) == GRAPH_FEATURE_CONTRACT_ID:
                raise RuntimeError("graph capture failed")
            return super().append_episode(episode)

    v1 = _market_episode()
    v2 = attach_world_context(
        (v1,),
        _FakeSource(
            SensorEvidence(status="missing", reason="no_artifact"),
            SensorEvidence(status="missing", reason="no_artifact"),
        ),
    )[0]
    v3 = _graph_companion()
    store = FailOpenStore()
    service = WorldModelService(
        store=store,
        predictor=RecordingPredictor(),
        predictors=(FilteringPredictor(), GraphFilteringPredictor()),
        labeler=None,
        bar_provider=None,
        horizons=("elapsed_4h.v1",),
    )
    report = service.capture_and_predict((v1, v2, v3), now=NOW)
    assert any("graph capture failed" in str(item.get("error") or "") for item in report["errors"])
    assert v1.episode_id in store.episodes
    assert v2.episode_id in store.episodes
    assert v3.episode_id not in store.episodes


def test_service_reuses_first_canonical_graph_slot(tmp_path) -> None:
    from pathlib import Path

    from tests.application.test_world_graph_capture import _attach, _complete_config, _unpublished_config
    from tests.application.test_world_graph_snapshot import _episode as _graph6_market_episode
    from trader.infrastructure.state_db.world_model_store import WorldModelConflictError, WorldModelStore

    v1 = _graph6_market_episode()
    first = _attach((v1,), _unpublished_config())[0]
    second = _attach((v1,), _complete_config())[0]
    assert first.episode_id != second.episode_id
    store = WorldModelStore(Path(tmp_path) / "world_model.db")
    recorder = GraphFilteringPredictor()
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
    assert {row["episode_id"] for row in store.list_predictions()} == {first.episode_id}
