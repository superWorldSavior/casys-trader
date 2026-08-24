from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from trader.application.world_model.baseline import (
    HierarchicalDirichletWorldBaseline,
    cold_markov_challenger,
)
from trader.application.world_model.cohort_service import WorldCohortService
from trader.application.world_model.encoding import world_lane_encoder_profile
from trader.application.world_model.service import WorldModelService
from trader.domain.world_cohort import RegisterWorldCohort, WorldCohortId
from trader.domain.world_episode import (
    MARKET_FEATURE_CONTRACT_ID,
    AnchorBar,
    WorldEpisode,
    WorldObservation,
    completed_bar_cutoff,
)
from trader.infrastructure.state_db.world_model_store import WorldModelStore

from tests.application.test_world_cohort_service import _arm_command, _manifest, _start_command


UTC = timezone.utc
COLLECTION_STARTED_AT = datetime(2026, 8, 24, 5, 25, tzinfo=UTC)
BAR_START_TS = datetime(2026, 8, 24, 5, 15, tzinfo=UTC)
BAR_END_TS = datetime(2026, 8, 24, 5, 30, tzinfo=UTC)
PREVIOUS_BAR_START_TS = datetime(2026, 8, 24, 5, 0, tzinfo=UTC)
PREVIOUS_BAR_END_TS = datetime(2026, 8, 24, 5, 15, tzinfo=UTC)
UNKNOWN_AS_OF_TS = datetime(2026, 8, 24, 5, 45, tzinfo=UTC)
CAPTURE_TS = datetime(2026, 8, 24, 6, 0, tzinfo=UTC)


def _collecting_15m_cohort(tmp_path: Path):
    store = WorldModelStore(tmp_path / "world_model.db", clock=lambda: COLLECTION_STARTED_AT)
    cohort_service = WorldCohortService(repository=store, query=store)
    manifest = _manifest(bar_interval="15m")
    cohort_service.register(RegisterWorldCohort(manifest=manifest))
    cohort_service.arm(_arm_command(manifest))
    cohort_service.start(_start_command(manifest))
    cohort = store.load(WorldCohortId(manifest.cohort_id))
    evidence = store.envelope_for(cohort.started_event).require_proven()
    return store, cohort_service, cohort, evidence


def _capture_service(
    store: WorldModelStore,
    cohort_service: WorldCohortService,
    predictor: object | None = None,
) -> WorldModelService:
    return WorldModelService(
        store=store,
        predictor=HierarchicalDirichletWorldBaseline() if predictor is None else predictor,
        labeler=None,
        bar_provider=None,
        horizons=("elapsed_4h.v1",),
        cohort_service=cohort_service,
    )


def _cohort_bound_market_predictor(cohort) -> HierarchicalDirichletWorldBaseline:
    lane = next(item for item in cohort.manifest.lanes if item.lane_id == "markov.market")
    profile = world_lane_encoder_profile("market")
    started = cohort.started_event
    assert started is not None
    return cold_markov_challenger(
        lane=lane,
        contract=profile.contract,
        mask=profile.mask,
        study_cohort_id=cohort.cohort_id,
        manifest_sha256=cohort.manifest.manifest_sha256,
        started_event_id=started.event_id,
    )


def _episode(
    as_of: datetime,
    *,
    timestamp_semantics: str,
    available_at: datetime | None = None,
    captured_at: datetime | None = None,
    interval: str = "15m",
) -> WorldEpisode:
    if available_at is None:
        if timestamp_semantics == "bar_start":
            available_at = as_of + timedelta(minutes=15)
        else:
            available_at = as_of
    if captured_at is None:
        captured_at = available_at + timedelta(minutes=1)
    return WorldEpisode(
        observation=WorldObservation(
            venue="US",
            symbol="AAPL",
            bar_interval=interval,
            as_of_bar_ts=as_of,
            feature_contract_version=MARKET_FEATURE_CONTRACT_ID,
            sampling_policy_version="active_tradable_completed_bar.v1",
            anchor=AnchorBar(
                ts=as_of,
                open=100.0,
                high=101.0,
                low=99.0,
                close=100.0,
                volume=1_000.0,
                source="fixture",
                timestamp_semantics=timestamp_semantics,
            ),
            available_at=available_at,
            captured_at=captured_at,
            freshness="fresh",
            categorical_features={
                "asset_family": "equities",
                "venue": "US",
                "bar_interval": interval,
                "session_phase": "regular",
                "market_regime": "trend_up",
                "volatility_state": "normal",
            },
            numeric_features={"return": 0.01, "atr_pct": 0.01},
        )
    )


def test_bar_start_before_cohort_start_is_admitted_when_completed_end_is_after(tmp_path: Path) -> None:
    store, cohort_service, cohort, evidence = _collecting_15m_cohort(tmp_path)
    try:
        assert evidence.effective_ready_at == COLLECTION_STARTED_AT
        episode = _episode(BAR_START_TS, timestamp_semantics="bar_start")
        assert episode.observation.as_of_bar_ts == BAR_START_TS
        assert episode.observation.as_of_bar_ts < evidence.effective_ready_at
        proven_end = completed_bar_cutoff(
            as_of_bar_ts=episode.observation.as_of_bar_ts,
            timestamp_semantics=episode.observation.anchor.timestamp_semantics,
            bar_interval=episode.observation.bar_interval,
        )
        assert proven_end == BAR_END_TS
        assert proven_end > evidence.effective_ready_at

        report = _capture_service(store, cohort_service).capture_and_predict((episode,), now=CAPTURE_TS)
        assert report["errors"] == []
        slots = cohort_service.list_slots(cohort.cohort_id)
        assert len(slots) == 1
        assert slots[0].as_of_bar_ts == BAR_START_TS
        assert slots[0].anchor_end_at == BAR_END_TS
        assert slots[0].anchor_end_at > evidence.effective_ready_at
        assert cohort.manifest.authority == "shadow_only"
        assert cohort.manifest.causal_claim is False
        assert cohort.manifest.pnl_claim is False
    finally:
        store.close()


def test_completed_bar_end_at_or_before_cohort_start_is_not_admitted(tmp_path: Path) -> None:
    store, cohort_service, cohort, evidence = _collecting_15m_cohort(tmp_path)
    try:
        earlier = _episode(PREVIOUS_BAR_START_TS, timestamp_semantics="bar_start")
        at_start = _episode(COLLECTION_STARTED_AT, timestamp_semantics="bar_close")
        earlier_end = completed_bar_cutoff(
            as_of_bar_ts=earlier.observation.as_of_bar_ts,
            timestamp_semantics=earlier.observation.anchor.timestamp_semantics,
            bar_interval=earlier.observation.bar_interval,
        )
        at_start_end = completed_bar_cutoff(
            as_of_bar_ts=at_start.observation.as_of_bar_ts,
            timestamp_semantics=at_start.observation.anchor.timestamp_semantics,
            bar_interval=at_start.observation.bar_interval,
        )
        assert earlier_end == PREVIOUS_BAR_END_TS
        assert earlier_end < evidence.effective_ready_at
        assert at_start_end == evidence.effective_ready_at

        report = _capture_service(store, cohort_service).capture_and_predict(
            (earlier, at_start),
            now=CAPTURE_TS,
        )
        assert report["errors"] == []
        assert cohort_service.list_slots(cohort.cohort_id) == ()
    finally:
        store.close()


def test_non_bar_start_semantics_remain_on_as_of_and_unknown_fails_closed(tmp_path: Path) -> None:
    store, cohort_service, cohort, evidence = _collecting_15m_cohort(tmp_path)
    try:
        close_before = _episode(BAR_START_TS, timestamp_semantics="bar_close")
        close_after = _episode(BAR_END_TS, timestamp_semantics="bar_close")
        unknown_after = _episode(UNKNOWN_AS_OF_TS, timestamp_semantics="unknown")
        assert (
            completed_bar_cutoff(
                as_of_bar_ts=close_before.observation.as_of_bar_ts,
                timestamp_semantics=close_before.observation.anchor.timestamp_semantics,
                bar_interval=close_before.observation.bar_interval,
            )
            == BAR_START_TS
        )
        assert (
            completed_bar_cutoff(
                as_of_bar_ts=close_after.observation.as_of_bar_ts,
                timestamp_semantics=close_after.observation.anchor.timestamp_semantics,
                bar_interval=close_after.observation.bar_interval,
            )
            == BAR_END_TS
        )
        assert (
            completed_bar_cutoff(
                as_of_bar_ts=unknown_after.observation.as_of_bar_ts,
                timestamp_semantics=unknown_after.observation.anchor.timestamp_semantics,
                bar_interval=unknown_after.observation.bar_interval,
            )
            is None
        )
        assert close_before.observation.as_of_bar_ts < evidence.effective_ready_at
        assert close_after.observation.as_of_bar_ts > evidence.effective_ready_at
        assert unknown_after.observation.as_of_bar_ts > evidence.effective_ready_at

        report = _capture_service(store, cohort_service).capture_and_predict(
            (close_before, close_after, unknown_after),
            now=CAPTURE_TS,
        )
        assert report["errors"] == []
        slots = cohort_service.list_slots(cohort.cohort_id)
        assert len(slots) == 1
        assert slots[0].as_of_bar_ts == BAR_END_TS
        assert slots[0].anchor_end_at == BAR_END_TS
        admitted_as_of = {slot.as_of_bar_ts for slot in slots}
        admitted_refs = {ref for slot in slots for ref in slot.episode_refs_by_contract.values()}
        assert close_before.observation.as_of_bar_ts not in admitted_as_of
        assert unknown_after.observation.as_of_bar_ts not in admitted_as_of
        assert unknown_after.episode_id not in admitted_refs
    finally:
        store.close()


def test_admitted_bar_start_is_visible_to_cohort_bound_predictor(tmp_path: Path) -> None:
    store, cohort_service, cohort, evidence = _collecting_15m_cohort(tmp_path)
    try:
        live = _episode(BAR_START_TS, timestamp_semantics="bar_start")
        unknown = _episode(UNKNOWN_AS_OF_TS, timestamp_semantics="unknown")
        predictor = _cohort_bound_market_predictor(cohort)
        assert live.observation.as_of_bar_ts == BAR_START_TS
        assert live.observation.as_of_bar_ts < evidence.effective_ready_at
        assert live.observation.completed_bar_end_at == BAR_END_TS
        assert live.observation.completed_bar_end_at > evidence.effective_ready_at
        assert unknown.observation.completed_bar_end_at is None
        assert unknown.observation.as_of_bar_ts > evidence.effective_ready_at
        assert predictor.lane_identity is not None
        assert predictor.lane_identity.study_cohort_id == cohort.cohort_id
        assert predictor.lane_identity.lane_id == "markov.market"
        assert predictor.lane_identity.started_event_id == cohort.started_event.event_id

        service = _capture_service(store, cohort_service, predictor=predictor)
        report = service.capture_and_predict((live, unknown), now=CAPTURE_TS)
        assert report["errors"] == []
        assert report["predictions_appended"] == 1

        slots = cohort_service.list_slots(cohort.cohort_id)
        assert len(slots) == 1
        assert slots[0].as_of_bar_ts == BAR_START_TS
        assert slots[0].anchor_end_at == BAR_END_TS
        admitted_refs = {ref for slot in slots for ref in slot.episode_refs_by_contract.values()}
        assert live.episode_id in admitted_refs
        assert unknown.episode_id not in admitted_refs
        assert service._predictor_sees_episode(predictor, live) is True
        assert service._predictor_sees_episode(predictor, unknown) is False

        rows = store.list_predictions()
        live_rows = [row for row in rows if row.get("episode_id") == live.episode_id]
        unknown_rows = [row for row in rows if row.get("episode_id") == unknown.episode_id]
        assert live_rows
        assert unknown_rows == []
        for row in live_rows:
            assert row["study_cohort_id"] == cohort.cohort_id
            assert row["lane_id"] == "markov.market"
            assert row["manifest_sha256"] == cohort.manifest.manifest_sha256
            lane = next(item for item in cohort.manifest.lanes if item.lane_id == row["lane_id"])
            assert row["feature_contract_fingerprint"] == lane.feature_contract_fingerprint
            assert row["feature_mask_fingerprint"] == lane.feature_mask_fingerprint
            nested = row["prediction"] if isinstance(row.get("prediction"), dict) else {}
            record = row["prediction_record"] if isinstance(row.get("prediction_record"), dict) else {}
            assert nested.get("study_cohort_id") == cohort.cohort_id
            assert nested.get("lane_id") == "markov.market"
            assert record.get("study_cohort_id") == cohort.cohort_id
            assert record.get("lane_id") == "markov.market"
    finally:
        store.close()
