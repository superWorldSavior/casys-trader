"""End to end: real pilot config -> cohort predictions -> scored pattern pair.

Regression test for the D20 review (P0-1/P0-2): from the committed
config/world_shadow_pilot.yaml, activation must mint a joint predictor per
cohort (no silent cross-cohort drop), the graph cohort must carry exactly one
context lane, and a hand-matched occurrence must score exactly one matched
set through the real stores and projector.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from tests.application.test_world_cohort_service import (
    _arm_command,
    _slot_for,
    _start_command,
)
from tests.application.test_world_pilot_activation import BOOT, _activate
from tests.domain.test_world_cohort import V2
from tests.domain.test_world_pattern import (
    CUTOFF,
    _evaluating,
    _occurrence,
    _outcome,
    _path,
    _prediction,
    _spec,
)
from tests.read_models.test_world_patterns import _instrument
from trader.application.world_model.cohort_service import WorldCohortService
from trader.application.world_model.service import WorldModelService
from trader.domain.world_cohort import (
    AdmitWorldCohortSlot,
    CohortPhase,
    WorldCohort,
    WorldCohortId,
)
from trader.domain.world_episode import (
    CONTEXT_FEATURE_CONTRACT_ID,
    GRAPH_FEATURE_CONTRACT_ID,
    AnchorBar,
    WorldEpisode,
    WorldObservation,
)
from trader.infrastructure.state_db.world_model_query import read_world_pattern_ledger
from trader.infrastructure.state_db.world_model_store import WorldModelStore
from trader.infrastructure.state_db.world_pattern_store import WorldPatternStore
from trader.reporting.read_models.world_patterns import project_world_pattern_report
from trader.runtime.world_model_runtime import WorldModelRuntime

UTC = timezone.utc
BAR = BOOT + timedelta(hours=2)
MATCH_CUTOFF = BAR + timedelta(minutes=15)


def _bar_episode(*, contract: str) -> WorldEpisode:
    context = None
    if contract == CONTEXT_FEATURE_CONTRACT_ID:
        from trader.application.world_model.context_capture import build_world_context_snapshot
        from trader.domain.world_context import SensorEvidence

        missing = SensorEvidence(status="missing", reason="no_artifact")
        context = build_world_context_snapshot(
            symbol="2301.TW",
            venue="TW",
            cutoff_at=BAR,
            family="equities",
            macro=missing,
            company=missing,
        )
    return WorldEpisode(
        observation=WorldObservation(
            venue="TW",
            symbol="2301.TW",
            bar_interval="15m",
            as_of_bar_ts=BAR,
            feature_contract_version=contract,
            sampling_policy_version="active_tradable_completed_bar.v1",
            anchor=AnchorBar(
                ts=BAR,
                open=100.0,
                high=101.0,
                low=99.0,
                close=100.0,
                volume=1_000.0,
                source="fixture",
            ),
            available_at=BAR,
            captured_at=BAR + timedelta(minutes=1),
            freshness="fresh",
            categorical_features={"asset_family": "equities", "venue": "TW"},
            numeric_features={"return": 0.01, "atr_pct": 0.01},
            context=context,
        )
    )


def test_real_config_scores_one_matched_pair_per_occurrence(tmp_path: Path) -> None:
    db_path = tmp_path / "world_model.db"
    world = WorldModelStore(db_path, clock=lambda: BOOT)
    patterns = WorldPatternStore(world._db, clock=lambda: BOOT)
    try:
        service = WorldCohortService(world, world)
        report = _activate(cohort_service=service)
        assert report.status in {"started", "already_collecting", "blocked"}
        for item in report.cohorts:
            cohort = service.repository.load(WorldCohortId(item["cohort_id"]))
            if cohort.phase is not CohortPhase.COLLECTING:
                from trader.domain.world_cohort import SensorMask

                required = tuple(
                    sensor.sensor_id
                    for sensor in cohort.manifest.sensor_requirements
                    if sensor.mode is SensorMask.REQUIRED
                )
                if cohort.phase is CohortPhase.REGISTERED:
                    service.arm(_arm_command(cohort.manifest, sensors=required))
                    cohort = service.repository.load(WorldCohortId(item["cohort_id"]))
                service.start(_start_command(cohort.manifest))
        cohorts = {
            str(cohort.cohort_id): cohort
            for cohort in (
                service.repository.load(cohort_id)
                for cohort_id in world.list_collecting_cohort_ids()
            )
            if isinstance(cohort, WorldCohort)
        }
        graph = next(
            cohort for cohort in cohorts.values() if cohort.manifest.has_graph_lanes()
        )
        graph_id = str(graph.cohort_id)
        joint_lanes = [
            lane.lane_id
            for lane in graph.manifest.lanes
            if lane.feature_contract_id == CONTEXT_FEATURE_CONTRACT_ID
        ]
        assert joint_lanes == ["markov.joint"]

        runtime = WorldModelRuntime(store=world, cohort_service=service)
        joint_owners = {
            item.lane_identity.study_cohort_id
            for item in runtime.predictors
            if getattr(getattr(item, "lane_identity", None), "lane_id", None) == "markov.joint"
        }
        assert len(joint_owners) == 2
        assert graph_id in joint_owners

        context_episode = _bar_episode(contract=CONTEXT_FEATURE_CONTRACT_ID)
        graph_episode = _bar_episode(contract=GRAPH_FEATURE_CONTRACT_ID)
        assert world.append_episode(context_episode) is True
        assert world.append_episode(graph_episode) is True
        started = graph.started_event
        assert started is not None
        evidence = world.envelope_for(started).require_proven()
        from trader.application.world_model.world_scope_resolver import WorldScopeResolver
        from trader.domain.world_scope import WorldMarketAnchorRef

        from tests.package_layout._helpers import REPO_ROOT

        resolution = WorldScopeResolver.load(REPO_ROOT / "config").mapping.resolve(
            WorldMarketAnchorRef(market_venue="TW", instrument="2301.TW")
        )
        assert resolution.status == "resolved"
        slot = _slot_for(
            graph,
            venue="TW",
            symbol="2301.TW",
            bar_interval="15m",
            as_of_bar_ts=BAR,
            anchor_end_at=BAR,
            episode_refs_by_contract={
                GRAPH_FEATURE_CONTRACT_ID: graph_episode.episode_id,
                CONTEXT_FEATURE_CONTRACT_ID: context_episode.episode_id,
            },
            scope_resolution=resolution,
        )
        service.admit_slot(AdmitWorldCohortSlot(slot=slot, started_evidence=evidence))
        capture = WorldModelService(
            store=world,
            predictor=None,
            labeler=None,
            bar_provider=None,
            predictors=[
                item
                for item in runtime.predictors
                if getattr(getattr(item, "lane_identity", None), "study_cohort_id", None)
                == graph_id
            ],
            horizons=("elapsed_1d.v1",),
            cohort_service=service,
        )
        captured = capture.capture_and_predict(
            (context_episode, graph_episode), now=MATCH_CUTOFF
        )
        assert captured["predictions_appended"] == 3

        hypothesis = _evaluating(
            evaluation_cohort_id=graph_id,
            spec=_spec(
                evaluation_start_not_before=BOOT,
                formation_cutoff=BOOT - timedelta(days=30),
            ),
        )
        for event in hypothesis.events:
            patterns.append_event(event)
        occurrence = _occurrence(
            hypothesis,
            cohort_id=graph_id,
            instrument=_instrument("2301"),
            cutoff_at=MATCH_CUTOFF,
            exact_path=_path(),
            forecast=_prediction(
                episode_id=graph_episode.episode_id,
                created_at=MATCH_CUTOFF,
                feature_hash="graph-2301-e2e",
                probabilities={"DOWN": 0.05, "FLAT": 0.05, "UP": 0.90},
            ),
        )
        outcome = _outcome(episode_id=graph_episode.episode_id, endpoint_close=102.0)
        linked = occurrence.link_outcome(outcome)
        for event in linked.events:
            patterns.append_event(event)
        world.append_outcome_event(outcome)

        ledger = read_world_pattern_ledger(db_path, graph_id)
        assert ledger["status"] == "loaded"
        scored = project_world_pattern_report(ledger)
        assert len(scored["matched_sets"]) == 1
        assert scored["exclusions"] == {}
    finally:
        patterns.close()
        world.close()
