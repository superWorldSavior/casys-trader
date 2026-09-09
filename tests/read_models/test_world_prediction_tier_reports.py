"""Ledger and report parity after the World prediction SQLite -> Parquet cutover.

Query-adapter unit tests live in test_world_prediction_tier_queries.py. This
module checks public ledger and report outputs against a real WorldModelStore
fixture before and after the independent test-side cold activation.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import pytest

from tests.application.test_world_cohort_service import START_READY, _arm_command, _start_command
from tests.domain.test_world_cohort import _manifest, _slot_for, _support
from tests.domain.test_world_pattern import CUTOFF, FORMATION, _evaluating, _outcome
from tests.infrastructure.test_world_prediction_tier_parity import (
    ARCHIVE_NOW,
    RecordClock,
    activate_registered_partitions,
    archive_closed_day_partitions,
    bind_world_record_clock,
    migrate_world,
    open_seeded_world,
    parquet_path,
)
from tests.read_models.test_world_cohort_report import (
    LABEL_AT,
    LINEAGE,
    TARGET_AT,
    _iso,
    _protocol_small,
    _store_market_episode as _cohort_market_episode,
)
from tests.read_models.test_world_patterns import (
    COHORT_ID as PATTERN_COHORT_ID,
    _CONTEXT_POOR,
    _context_prediction,
    _linked_occurrence,
    _store_market_episode as _pattern_market_episode,
)
from trader.application.world_model.cohort_service import WorldCohortService
from trader.domain.world_cohort import AdmitWorldCohortSlot, RegisterWorldCohort, WorldCohortId
from trader.infrastructure.state_db.world_model_query import (
    read_world_cohort_ledger,
    read_world_model_ledger,
    read_world_pattern_ledger,
)
from trader.infrastructure.state_db.world_model_store import WorldModelStore
from trader.infrastructure.state_db.world_pattern_store import WorldPatternStore
from trader.reporting.read_models.world_cohort import read_world_cohort_report
from trader.reporting.read_models.world_patterns import read_world_pattern_report
from trader.reporting.read_models.world_status import read_world_model_status


UTC = timezone.utc
PATTERN_ARCHIVE_NOW = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)
PATTERN_ARCHIVE_BEFORE = date(2026, 9, 7)


def _assert_unavailable_not_partial(ledger: dict[str, Any]) -> None:
    assert ledger["status"] == "unavailable"
    assert ledger.get("error")
    predictions = ledger.get("predictions")
    assert predictions in (None, [])


def _without_resource_budget(payload: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in payload.items() if key != "resource_budget"}


def _assert_status_parity(before: Mapping[str, Any], after: Mapping[str, Any]) -> None:
    assert _without_resource_budget(after) == _without_resource_budget(before)
    assert after["authority"] == "shadow_only"
    assert after["decision_effect"] == "none"


def _assert_report_parity(before: Mapping[str, Any], after: Mapping[str, Any]) -> None:
    assert after == before
    assert after["authority"] == "shadow_only"
    assert after["decision_effect"] == "none"


@pytest.mark.parametrize("mode", ["mixed", "all_cold"])
def test_world_model_ledger_parity_after_mixed_and_all_cold(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    world = open_seeded_world(tmp_path, monkeypatch)
    try:
        before = read_world_model_ledger(world.path)
        before_status = read_world_model_status(world.path.parent, now=ARCHIVE_NOW)
        assert before["status"] == "loaded"
        assert len(before["predictions"]) == world.before_counts["predictions"]
        migrate_world(world, mode=mode)
        after = read_world_model_ledger(world.path)
        after_status = read_world_model_status(world.path.parent, now=ARCHIVE_NOW)
        assert after == before
        assert after["status"] == "loaded"
        assert [row["prediction_id"] for row in after["predictions"]] == [
            row["prediction_id"] for row in before["predictions"]
        ]
        _assert_status_parity(before_status, after_status)
    finally:
        world.close()


def test_world_model_and_pattern_ledgers_unavailable_when_cold_is_corrupt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = open_seeded_world(tmp_path, monkeypatch)
    try:
        before_model = read_world_model_ledger(world.path)
        before_pattern = read_world_pattern_ledger(world.path)
        before_status = read_world_model_status(world.path.parent, now=ARCHIVE_NOW)
        assert before_model["status"] == "loaded"
        assert before_pattern["status"] == "loaded"
        migrate_world(world, mode="all_cold")
        warmed_model = read_world_model_ledger(world.path)
        warmed_pattern = read_world_pattern_ledger(world.path)
        warmed_status = read_world_model_status(world.path.parent, now=ARCHIVE_NOW)
        assert warmed_model == before_model
        assert warmed_pattern == before_pattern
        _assert_status_parity(before_status, warmed_status)
        target = parquet_path(world.path, world.partitions[0][1])
        original = target.read_bytes()
        target.write_bytes(original[:24] + b"corrupt-ledger-bytes")
        broken_model = read_world_model_ledger(world.path)
        broken_pattern = read_world_pattern_ledger(world.path)
        broken_status = read_world_model_status(world.path.parent, now=ARCHIVE_NOW)
        _assert_unavailable_not_partial(broken_model)
        _assert_unavailable_not_partial(broken_pattern)
        assert broken_status["status"] == "unavailable"
        assert broken_status.get("error")
        target.write_bytes(original)
        assert read_world_model_ledger(world.path) == before_model
        assert read_world_pattern_ledger(world.path) == before_pattern
        _assert_status_parity(before_status, read_world_model_status(world.path.parent, now=ARCHIVE_NOW))
    finally:
        world.close()


def test_cohort_ledger_indexed_columns_override_nested_json_after_cold(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = RecordClock(START_READY)
    bind_world_record_clock(monkeypatch, clock)
    db_path = tmp_path / "world_model.db"
    store = WorldModelStore(db_path, clock=clock)
    try:
        service = WorldCohortService(repository=store, query=store)
        manifest = _manifest(statistical_protocol=_protocol_small(), support_gates=_support(descriptive=1))
        service.register(RegisterWorldCohort(manifest=manifest))
        service.arm(_arm_command(manifest))
        service.start(_start_command(manifest))
        cohort = store.load(WorldCohortId(manifest.cohort_id))
        started = cohort.started_event
        assert started is not None
        slot = _slot_for(cohort)
        evidence = store.envelope_for(started).require_proven()
        service.admit_slot(AdmitWorldCohortSlot(slot=slot, started_evidence=evidence))
        stored_episode = _cohort_market_episode(
            venue=slot.venue,
            symbol=slot.symbol,
            as_of=slot.as_of_bar_ts,
            bar_interval=slot.bar_interval,
        )
        episode_id = stored_episode["episode_id"]
        assert store.append_episode(stored_episode)
        lane = cohort.manifest.lane_by_id["markov.market"]
        payload = {
            "prediction_id": "pred-indexed",
            "run_id": "run-1",
            "episode_id": episode_id,
            "horizon_id": "elapsed_1d.v1",
            "model_kind": lane.model_id,
            "model_version": lane.model_version,
            "predicted_at": _iso(slot.as_of_bar_ts),
            "study_cohort_id": cohort.cohort_id,
            "lane_id": "markov.market",
            "manifest_sha256": cohort.manifest.manifest_sha256,
            "feature_contract_fingerprint": lane.feature_contract_fingerprint,
            "feature_mask_fingerprint": lane.feature_mask_fingerprint,
            "input": {"venue": slot.venue, "symbol": slot.symbol, "bar_interval": slot.bar_interval},
            "prediction": {
                "probabilities": {"DOWN": 0.2, "FLAT": 0.2, "UP": 0.6},
                "study_cohort_id": "json-should-lose",
                "lane_id": "json-lane",
                "comparison_batch_id": slot.comparison_batch_id,
                "comparison_cohort_fingerprint": LINEAGE,
                "training_cutoff": _iso(slot.as_of_bar_ts),
            },
        }
        assert store.append_legacy_prediction(payload) is True
        assert store.append_legacy_outcome_event(
            {
                "outcome_event_id": "out-1",
                "episode_id": episode_id,
                "horizon_code": "elapsed_1d.v1",
                "status": "observed",
                "move_class": "UP",
                "training_eligible": True,
                "label_available_at": _iso(LABEL_AT),
                "target_at": _iso(TARGET_AT),
                "label": {"move_class": "UP", "target_at": _iso(TARGET_AT), "simple_return": 0.01},
                "evidence": {"source": "fixture", "source_raw_sha256": "a" * 64},
            }
        )
        before_cohort = read_world_cohort_ledger(db_path, cohort.cohort_id)
        before_model = read_world_model_ledger(db_path)
        before_report = read_world_cohort_report(db_path.parent, cohort.cohort_id)
        before_status = read_world_model_status(db_path.parent, now=ARCHIVE_NOW)
        assert before_cohort["status"] == "loaded"
        assert before_model["status"] == "loaded"
        row = before_cohort["predictions"][0]
        assert row["study_cohort_id"] == cohort.cohort_id
        assert row["lane_id"] == "markov.market"
        assert row["manifest_sha256"] == cohort.manifest.manifest_sha256
        assert "json-should-lose" not in {row["study_cohort_id"], row["lane_id"]}
        report, partitions = archive_closed_day_partitions(db_path, before=date(2026, 8, 28), clock=ARCHIVE_NOW)
        assert report["source_retained"] is True
        activate_registered_partitions(store, partitions)
        after_cohort = read_world_cohort_ledger(db_path, cohort.cohort_id)
        after_model = read_world_model_ledger(db_path)
        after_report = read_world_cohort_report(db_path.parent, cohort.cohort_id)
        after_status = read_world_model_status(db_path.parent, now=ARCHIVE_NOW)
        assert after_cohort == before_cohort
        assert after_model == before_model
        _assert_report_parity(before_report, after_report)
        _assert_status_parity(before_status, after_status)
        hydrated = after_cohort["predictions"][0]
        assert hydrated["study_cohort_id"] == cohort.cohort_id
        assert hydrated["lane_id"] == "markov.market"
        nested = hydrated.get("prediction")
        if isinstance(nested, dict):
            assert nested["study_cohort_id"] == cohort.cohort_id
            assert nested["lane_id"] == "markov.market"
        target = parquet_path(db_path, partitions[0][1])
        original = target.read_bytes()
        target.unlink()
        broken = read_world_cohort_ledger(db_path, cohort.cohort_id)
        _assert_unavailable_not_partial(broken)
        broken_report = read_world_cohort_report(db_path.parent, cohort.cohort_id)
        assert broken_report["status"] == "unavailable"
        target.write_bytes(original)
        assert read_world_cohort_ledger(db_path, cohort.cohort_id) == before_cohort
        _assert_report_parity(before_report, read_world_cohort_report(db_path.parent, cohort.cohort_id))
    finally:
        store.close()


def test_pattern_ledger_indexed_columns_override_nested_json_after_cold(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = RecordClock(CUTOFF)
    bind_world_record_clock(monkeypatch, clock)
    db_path = tmp_path / "world_model.db"
    world = WorldModelStore(db_path, clock=clock)
    pattern = WorldPatternStore(world._db, clock=lambda: FORMATION)
    try:
        hypothesis = _evaluating()
        for event in hypothesis.events:
            pattern.append_event(event)
        occurrence, outcome_payload = _linked_occurrence(
            hypothesis,
            symbol="2330",
            probabilities={"DOWN": 0.05, "FLAT": 0.05, "UP": 0.90},
            endpoint_close=102.0,
        )
        for event in occurrence.events:
            pattern.append_event(event)
        stored_episode = _pattern_market_episode("2330")
        world.append_episode(stored_episode)
        world.append_outcome_event(_outcome(episode_id=stored_episode["episode_id"], endpoint_close=102.0))
        v2 = _context_prediction(symbol="2330", probabilities=_CONTEXT_POOR["2330"])
        world.append_legacy_prediction(
            {
                **v2,
                "episode_id": stored_episode["episode_id"],
                "run_id": "run-1",
                "predicted_at": v2["predicted_at"],
                "prediction": {
                    "probabilities": v2["probabilities"],
                    "study_cohort_id": "json-should-lose",
                    "lane_id": "json-lane",
                },
                "study_cohort_id": PATTERN_COHORT_ID,
                "lane_id": "markov.market",
                "input": {"venue": "XTAI", "symbol": "2330", "bar_interval": "1h", "as_of_bar_ts": v2["as_of_bar_ts"]},
            }
        )
        before = read_world_pattern_ledger(db_path, PATTERN_COHORT_ID)
        before_report = read_world_pattern_report(db_path.parent, PATTERN_COHORT_ID)
        assert before["status"] == "loaded"
        prediction = before["predictions"][0]
        assert prediction["study_cohort_id"] == PATTERN_COHORT_ID
        assert prediction["lane_id"] == "markov.market"
        assert "json-should-lose" not in {prediction["study_cohort_id"], prediction["lane_id"]}
        report, partitions = archive_closed_day_partitions(
            db_path,
            before=PATTERN_ARCHIVE_BEFORE,
            clock=PATTERN_ARCHIVE_NOW,
        )
        assert report["source_retained"] is True
        activate_registered_partitions(world, partitions)
        after = read_world_pattern_ledger(db_path, PATTERN_COHORT_ID)
        after_report = read_world_pattern_report(db_path.parent, PATTERN_COHORT_ID)
        assert after == before
        _assert_report_parity(before_report, after_report)
        hydrated = after["predictions"][0]
        assert hydrated["study_cohort_id"] == PATTERN_COHORT_ID
        assert hydrated["lane_id"] == "markov.market"
        nested = hydrated.get("prediction")
        if isinstance(nested, dict):
            assert nested.get("study_cohort_id") == PATTERN_COHORT_ID
            assert nested.get("lane_id") == "markov.market"
    finally:
        pattern.close()
        world.close()


def test_mixed_then_all_cold_keeps_world_model_ledger(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = open_seeded_world(tmp_path, monkeypatch)
    try:
        before = read_world_model_ledger(world.path)
        before_status = read_world_model_status(world.path.parent, now=ARCHIVE_NOW)
        report, partitions = archive_closed_day_partitions(world.path)
        assert report["source_retained"] is True
        activate_registered_partitions(world.store, partitions, recorded_dates={date(2026, 8, 24)})
        assert read_world_model_ledger(world.path) == before
        _assert_status_parity(before_status, read_world_model_status(world.path.parent, now=ARCHIVE_NOW))
        activate_registered_partitions(
            world.store,
            partitions,
            recorded_dates={date(2026, 8, 25), date(2026, 8, 26)},
        )
        assert read_world_model_ledger(world.path) == before
        _assert_status_parity(before_status, read_world_model_status(world.path.parent, now=ARCHIVE_NOW))
    finally:
        world.close()
