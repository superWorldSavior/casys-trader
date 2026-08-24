from __future__ import annotations

import json
import math
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from tests.domain.test_world_cohort import (
    ANCHOR_TS,
    COHORT_ID,
    V1,
    V2,
    _admit,
    _armed,
    _completion_evidence,
    _manifest,
    _register,
    _slot_for,
    _started,
    _support,
)
from trader.domain.world_cohort import (
    CloseWorldCohort,
    CompleteWorldCohort,
    WorldCohort,
    WorldCohortCompletionHorizonLeaf,
    WorldStatisticalProtocol,
)
from trader.domain.world_episode import (
    AnchorBar,
    WorldEpisode,
    WorldObservation,
    canonical_sha256,
)
from trader.infrastructure.state_db.world_model_query import read_world_cohort_ledger, read_world_model_ledger
from trader.reporting.read_models.world_cohort import project_world_cohort_report, read_world_cohort_report


UTC = timezone.utc
LINEAGE = "1" * 64
LABEL_AT = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)
TARGET_AT = datetime(2026, 8, 25, 1, 0, tzinfo=UTC)
READY_AT = datetime(2026, 8, 24, 1, 5, tzinfo=UTC)

_CLAIM_KEYS = {
    "authority": "shadow_only",
    "decision_effect": "none",
    "recommendation": "NO_GO",
    "causal_claim": False,
    "pnl_claim": False,
    "actual_trader_contribution": "not_attributable",
}


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _store_market_episode(*, venue: str, symbol: str, as_of: datetime, bar_interval: str = "1h") -> dict[str, Any]:
    observation = WorldObservation(
        venue=venue,
        symbol=symbol,
        bar_interval=bar_interval,
        as_of_bar_ts=as_of,
        feature_contract_version=V1.contract_id,
        sampling_policy_version="active_tradable_completed_bar.v1",
        anchor=AnchorBar(
            ts=as_of,
            open=100.0,
            high=102.0,
            low=99.0,
            close=101.0,
            volume=1_000.0,
            source="fixture",
        ),
        available_at=as_of,
        captured_at=as_of,
        freshness="fresh",
        numeric_features={"return": 0.01},
    )
    return WorldEpisode(observation=observation).to_dict()


def _collecting(**manifest_overrides: object) -> WorldCohort:
    return _admit(_started(_armed(_register(_manifest(**manifest_overrides)))))


def _episode(
    episode_id: str,
    *,
    venue: str = "US",
    symbol: str = "AAPL",
    as_of: datetime = ANCHOR_TS,
    contract: str | None = None,
) -> dict[str, Any]:
    return {
        "episode_id": episode_id,
        "venue": venue,
        "symbol": symbol,
        "bar_interval": "1h",
        "as_of_bar_ts": _iso(as_of),
        "feature_contract_version": contract or V1.contract_id,
        "sampling_policy_version": "active_tradable_completed_bar.v1",
    }


def _outcome(
    *,
    episode_id: str,
    horizon_id: str = "elapsed_1d.v1",
    move_class: str = "UP",
    evidence_digest: str | None = None,
    target_at: datetime = TARGET_AT,
    simple_return: float = 0.01,
    status: str = "observed",
    outcome_event_id: str | None = None,
) -> dict[str, Any]:
    evidence = {"source": "fixture", "source_raw_sha256": "a" * 64, "target_at": _iso(target_at)}
    digest = evidence_digest or canonical_sha256(evidence)
    return {
        "outcome_event_id": outcome_event_id or f"outcome:{episode_id}:{horizon_id}",
        "episode_id": episode_id,
        "horizon_code": horizon_id,
        "horizon_id": horizon_id,
        "status": status,
        "move_class": move_class,
        "direction": move_class,
        "training_eligible": True,
        "label_available_at": _iso(LABEL_AT),
        "available_at": _iso(LABEL_AT),
        "target_at": _iso(target_at),
        "simple_return": simple_return,
        "evidence_sha256": digest,
        "label": {"move_class": move_class, "target_at": _iso(target_at), "simple_return": simple_return},
        "evidence": evidence,
    }


def _prediction(
    *,
    lane_id: str,
    episode_id: str,
    cohort: WorldCohort,
    horizon_id: str = "elapsed_1d.v1",
    venue: str = "US",
    symbol: str = "AAPL",
    as_of: datetime = ANCHOR_TS,
    probabilities: dict[str, float] | None = None,
    comparison_batch_id: str = "batch:v1:anchor-aapl",
    predicted_at: datetime = ANCHOR_TS,
    training_cutoff: datetime | None = ANCHOR_TS,
    lineage: str = LINEAGE,
    prediction_id: str | None = None,
    **overrides: object,
) -> dict[str, Any]:
    lane = cohort.manifest.lane_by_id[lane_id]
    payload: dict[str, Any] = {
        "prediction_id": prediction_id or f"pred:{lane_id}:{episode_id}:{horizon_id}",
        "episode_id": episode_id,
        "horizon_code": horizon_id,
        "horizon_id": horizon_id,
        "lane_id": lane_id,
        "study_cohort_id": cohort.cohort_id,
        "manifest_sha256": cohort.manifest.manifest_sha256,
        "feature_contract_fingerprint": lane.feature_contract_fingerprint,
        "feature_mask_fingerprint": lane.feature_mask_fingerprint,
        "model_kind": lane.model_id,
        "model_id": lane.model_id,
        "model_version": lane.model_version,
        "predicted_at": _iso(predicted_at),
        "ready_at": _iso(READY_AT),
        "recorded_at": _iso(READY_AT),
        "comparison_batch_id": comparison_batch_id,
        "comparison_cohort_fingerprint": lineage,
        "training_cutoff": None if training_cutoff is None else _iso(training_cutoff),
        "probabilities": probabilities or {"DOWN": 0.2, "FLAT": 0.2, "UP": 0.6},
        "venue": venue,
        "symbol": symbol,
        "bar_interval": "1h",
        "as_of_bar_ts": _iso(as_of),
        "input": {
            "venue": venue,
            "symbol": symbol,
            "bar_interval": "1h",
            "as_of_bar_ts": _iso(as_of),
            "feature_contract_version": lane.feature_contract_id,
        },
    }
    payload.update(overrides)
    return payload


def _paired_bundle(
    cohort: WorldCohort,
    *,
    symbol: str = "AAPL",
    venue: str = "US",
    as_of: datetime = ANCHOR_TS,
    horizon_id: str = "elapsed_1d.v1",
    batch: str | None = None,
    market_p: dict[str, float] | None = None,
    status_p: dict[str, float] | None = None,
    company_p: dict[str, float] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    v1_id = f"ep-v1:{venue}:{symbol}:{_iso(as_of)}"
    v2_id = f"ep-v2:{venue}:{symbol}:{_iso(as_of)}"
    batch_id = batch or f"batch:v1:anchor-{symbol.lower()}"
    episodes = [
        _episode(v1_id, venue=venue, symbol=symbol, as_of=as_of, contract=V1.contract_id),
        _episode(v2_id, venue=venue, symbol=symbol, as_of=as_of, contract=V2.contract_id),
    ]
    outcomes = [
        _outcome(episode_id=v1_id, horizon_id=horizon_id),
        _outcome(episode_id=v2_id, horizon_id=horizon_id),
    ]
    predictions = [
        _prediction(
            lane_id="markov.market",
            episode_id=v1_id,
            cohort=cohort,
            horizon_id=horizon_id,
            venue=venue,
            symbol=symbol,
            as_of=as_of,
            comparison_batch_id=batch_id,
            probabilities=market_p,
        ),
        _prediction(
            lane_id="markov.status_only",
            episode_id=v2_id,
            cohort=cohort,
            horizon_id=horizon_id,
            venue=venue,
            symbol=symbol,
            as_of=as_of,
            comparison_batch_id=batch_id,
            probabilities=status_p or {"DOWN": 0.05, "FLAT": 0.05, "UP": 0.9},
        ),
        _prediction(
            lane_id="markov.company",
            episode_id=v2_id,
            cohort=cohort,
            horizon_id=horizon_id,
            venue=venue,
            symbol=symbol,
            as_of=as_of,
            comparison_batch_id=batch_id,
            probabilities=company_p or {"DOWN": 0.1, "FLAT": 0.1, "UP": 0.8},
            prediction_id=f"pred:markov.company:{v2_id}:{horizon_id}",
        ),
    ]
    return episodes, outcomes, predictions


def _ledger(
    cohort: WorldCohort,
    *,
    episodes: list[dict[str, Any]],
    outcomes: list[dict[str, Any]],
    predictions: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "status": "loaded",
        "exists": True,
        "manifest": cohort.manifest.to_dict(),
        "events": [event.to_dict() for event in cohort.events],
        "slots": [slot.to_dict() for slot in cohort.admitted_slots],
        "episodes": episodes,
        "outcomes": outcomes,
        "predictions": predictions,
        "receipts": [],
    }


def _assert_bounded_claims(payload: dict[str, Any]) -> None:
    for key, expected in _CLAIM_KEYS.items():
        assert payload[key] == expected
    assert payload.get("winner") is not True


def test_missing_database_is_read_only_not_started(tmp_path: Path) -> None:
    db_path = tmp_path / "world_model.db"
    ledger = read_world_cohort_ledger(db_path, COHORT_ID)
    report = read_world_cohort_report(tmp_path, COHORT_ID)

    assert ledger["status"] == "not_started"
    assert ledger["exists"] is False
    assert report["status"] == "not_started"
    assert report["schema_version"] == "world_cohort_report.v1"
    _assert_bounded_claims(report)
    assert not db_path.exists()


def test_schema_unavailable_when_cohort_tables_are_missing(tmp_path: Path) -> None:
    db_path = tmp_path / "world_model.db"
    connection = sqlite3.connect(db_path)
    try:
        connection.execute("CREATE TABLE world_episodes (episode_id TEXT PRIMARY KEY)")
        connection.execute("CREATE TABLE world_outcome_events (outcome_event_id TEXT PRIMARY KEY)")
        connection.execute("CREATE TABLE world_shadow_predictions (prediction_id TEXT PRIMARY KEY)")
        connection.commit()
    finally:
        connection.close()

    ledger = read_world_cohort_ledger(db_path, COHORT_ID)
    report = read_world_cohort_report(tmp_path, COHORT_ID)
    assert ledger["status"] == "schema_unavailable"
    assert report["status"] == "schema_unavailable"
    _assert_bounded_claims(report)
    assert db_path.exists()


def test_report_schema_keeps_shadow_only_claims_and_no_go() -> None:
    cohort = _collecting(statistical_protocol=_protocol_small(), support_gates=_support(descriptive=1))
    episodes, outcomes, predictions = _paired_bundle(cohort)
    report = project_world_cohort_report(_ledger(cohort, episodes=episodes, outcomes=outcomes, predictions=predictions))

    assert report["schema_version"] == "world_cohort_report.v1"
    assert report["status"] == "loaded"
    assert report["cohort_id"] == cohort.cohort_id
    assert report["manifest_sha256"] == cohort.manifest.manifest_sha256
    assert report["phase"] == "collecting"
    assert report["expected_lanes"] == [lane.lane_id for lane in cohort.manifest.lanes]
    assert set(report["observed_lanes"]) >= {"markov.market", "markov.status_only", "markov.company"}
    _assert_bounded_claims(report)
    assert report["interpretation_limit"] == "coverage_plumbing_preliminary_trends_only"


def test_strict_matching_rejects_cohort_hash_batch_cutoff_lineage_and_label_mismatches() -> None:
    cohort = _collecting(statistical_protocol=_protocol_small(), support_gates=_support(descriptive=1))
    episodes, outcomes, predictions = _paired_bundle(cohort)

    def _report_with(mutated: dict[str, Any]) -> dict[str, Any]:
        rows = [dict(item) for item in predictions]
        rows[1] = {**rows[1], **mutated}
        return project_world_cohort_report(_ledger(cohort, episodes=episodes, outcomes=outcomes, predictions=rows))

    cases = (
        ({"study_cohort_id": "world_cohort:v1:" + "d" * 64}, "study_cohort_id"),
        ({"manifest_sha256": "a" * 64}, "manifest_sha256"),
        ({"comparison_batch_id": "batch:other"}, "comparison_batch_id"),
        ({"predicted_at": _iso(ANCHOR_TS + timedelta(hours=1))}, "predicted_at"),
        ({"training_cutoff": _iso(ANCHOR_TS - timedelta(days=1))}, "training_cutoff"),
        ({"comparison_cohort_fingerprint": "2" * 64}, "training_lineage"),
    )
    for mutation, reason in cases:
        report = _report_with(mutation)
        assert report["exclusions"][f"{reason}_mismatch"] >= 1
        contrast = _primary_status_contrast(report)
        assert contrast["matched_sets"] == 0

    mismatched_label = [dict(item) for item in outcomes]
    mismatched_label[1] = {**mismatched_label[1], "move_class": "DOWN", "direction": "DOWN"}
    label_report = project_world_cohort_report(
        _ledger(cohort, episodes=episodes, outcomes=mismatched_label, predictions=predictions)
    )
    assert label_report["exclusions"]["label_move_class_mismatch"] >= 1
    assert _primary_status_contrast(label_report)["matched_sets"] == 0

    mismatched_target = [dict(item) for item in outcomes]
    mismatched_target[1] = {
        **mismatched_target[1],
        "target_at": _iso(TARGET_AT + timedelta(hours=4)),
        "label": {**mismatched_target[1]["label"], "target_at": _iso(TARGET_AT + timedelta(hours=4))},
    }
    target_report = project_world_cohort_report(
        _ledger(cohort, episodes=episodes, outcomes=mismatched_target, predictions=predictions)
    )
    assert target_report["exclusions"]["label_target_at_mismatch"] >= 1

    mismatched_digest = [dict(item) for item in outcomes]
    mismatched_digest[1] = {**mismatched_digest[1], "evidence_sha256": "b" * 64}
    digest_report = project_world_cohort_report(
        _ledger(cohort, episodes=episodes, outcomes=mismatched_digest, predictions=predictions)
    )
    assert digest_report["exclusions"]["label_evidence_digest_mismatch"] >= 1


def test_duplicates_and_absent_members_are_counted_never_guessed() -> None:
    cohort = _collecting(statistical_protocol=_protocol_small(), support_gates=_support(descriptive=1))
    episodes, outcomes, predictions = _paired_bundle(cohort)
    duplicate = {
        **predictions[0],
        "prediction_id": "pred-duplicate",
        "probabilities": {"DOWN": 0.4, "FLAT": 0.2, "UP": 0.4},
    }
    duplicated = project_world_cohort_report(
        _ledger(cohort, episodes=episodes, outcomes=outcomes, predictions=[*predictions, duplicate])
    )
    assert duplicated["exclusions"]["ambiguous_lane_member"] >= 1
    assert _primary_status_contrast(duplicated)["matched_sets"] == 0
    assert all(set(item["lane_ids"]) == set(item["expected_lane_ids"]) for item in duplicated["matched_sets"])

    missing_market = project_world_cohort_report(
        _ledger(cohort, episodes=episodes, outcomes=outcomes, predictions=predictions[1:])
    )
    assert missing_market["exclusions"]["absent_member"] >= 1
    assert _primary_status_contrast(missing_market)["matched_sets"] == 0
    assert all(len(item["lane_ids"]) == len(item["expected_lane_ids"]) for item in missing_market["matched_sets"])


def test_support_counts_unique_market_anchors_not_rows() -> None:
    cohort = _collecting(statistical_protocol=_protocol_small(), support_gates=_support(descriptive=2))
    later = ANCHOR_TS + timedelta(hours=2)
    first = _paired_bundle(cohort, symbol="AAPL", as_of=ANCHOR_TS, horizon_id="elapsed_1d.v1")
    second = _paired_bundle(
        cohort, symbol="MSFT", as_of=later, horizon_id="elapsed_1d.v1", batch="batch:v1:anchor-msft"
    )
    same_anchor_4h = _paired_bundle(cohort, symbol="AAPL", as_of=ANCHOR_TS, horizon_id="elapsed_4h.v1")
    episodes = first[0] + second[0] + same_anchor_4h[0]
    outcomes = first[1] + second[1] + same_anchor_4h[1]
    predictions = first[2] + second[2] + same_anchor_4h[2]
    report = project_world_cohort_report(_ledger(cohort, episodes=episodes, outcomes=outcomes, predictions=predictions))

    primary = _primary_status_contrast(report, horizon_id="elapsed_1d.v1")
    secondary = _primary_status_contrast(report, horizon_id="elapsed_4h.v1")
    assert primary["matched_sets"] == 2
    assert primary["unique_anchors"] == 2
    assert secondary["matched_sets"] == 1
    assert secondary["unique_anchors"] == 1
    assert report["support"]["pair_unit"] == "unique_market_anchor"
    assert primary["gates"]["descriptive_ready"] is True
    assert secondary["gates"]["descriptive_ready"] is False
    assert primary["winner"] is False


def test_deterministic_block_bootstrap_is_stable_and_uses_venue_session_blocks() -> None:
    protocol = WorldStatisticalProtocol(
        pair_unit="unique_market_anchor",
        block_key="venue_session",
        ci_method="deterministic_block_bootstrap.v1",
        ci_level=0.95,
        bootstrap_resamples=32,
        seed=20260823,
    )
    cohort = _collecting(statistical_protocol=protocol, support_gates=_support(descriptive=1))
    first = _paired_bundle(cohort, venue="US", symbol="AAPL", as_of=ANCHOR_TS)
    second = _paired_bundle(
        cohort,
        venue="EU",
        symbol="AIR",
        as_of=ANCHOR_TS + timedelta(days=1),
        batch="batch:v1:anchor-air",
        status_p={"DOWN": 0.3, "FLAT": 0.3, "UP": 0.4},
    )
    episodes = first[0] + second[0]
    outcomes = first[1] + second[1]
    predictions = first[2] + second[2]
    ledger = _ledger(cohort, episodes=episodes, outcomes=outcomes, predictions=predictions)
    first_report = project_world_cohort_report(ledger)
    second_report = project_world_cohort_report(ledger)
    contrast = _primary_status_contrast(first_report)
    replay = _primary_status_contrast(second_report)
    assert contrast["ci_method"] == "deterministic_block_bootstrap.v1"
    assert contrast["block_key"] == "venue_session"
    assert contrast["mean_delta"] is not None
    assert contrast["median_delta"] is not None
    assert contrast["ci_low"] <= contrast["mean_delta"] <= contrast["ci_high"]
    assert contrast["ci_low"] == replay["ci_low"]
    assert contrast["ci_high"] == replay["ci_high"]
    assert json.dumps(first_report["contrasts"], sort_keys=True) == json.dumps(
        second_report["contrasts"], sort_keys=True
    )
    market_loss = -math.log(0.6)
    status_loss = -math.log(0.9)
    assert contrast["mean_delta"] != status_loss - market_loss or contrast["unique_anchors"] == 2


def test_descriptive_gate_is_not_a_formal_win_and_integrity_failure_forbids_winner() -> None:
    cohort = _collecting(statistical_protocol=_protocol_small(), support_gates=_support(descriptive=1))
    episodes, outcomes, predictions = _paired_bundle(cohort)
    ready = project_world_cohort_report(_ledger(cohort, episodes=episodes, outcomes=outcomes, predictions=predictions))
    contrast = _primary_status_contrast(ready)
    assert contrast["gates"]["descriptive_ready"] is True
    assert contrast["gates"]["formal_ready"] is False
    assert contrast["winner"] is False
    assert contrast["conclusion"] in {"predictive_lift_observed", "inconclusive", "degraded"}
    assert ready["recommendation"] == "NO_GO"

    poisoned = dict(predictions[0])
    poisoned["input"] = {**poisoned["input"], "decision_id": "trader-cycle-1", "pnl": 12.0}
    failed = project_world_cohort_report(
        _ledger(cohort, episodes=episodes, outcomes=outcomes, predictions=[poisoned, *predictions[1:]])
    )
    assert failed["gates"]["integrity"]["passed"] is False
    assert all(item["winner"] is False for item in failed["contrasts"])
    assert all(item["conclusion"] != "predictive_lift_observed" for item in failed["contrasts"])


def test_completion_evidence_is_verified_against_terminal_leaves() -> None:
    collecting = _collecting(statistical_protocol=_protocol_small(), support_gates=_support(descriptive=1))
    closed = collecting.close(CloseWorldCohort(reason="stop"))
    completed = closed.complete(CompleteWorldCohort(evidence=_completion_evidence(closed)))
    episodes, outcomes, predictions = _paired_bundle(completed)
    for episode in episodes:
        outcomes.append(_outcome(episode_id=str(episode["episode_id"]), horizon_id="elapsed_4h.v1"))
    report = project_world_cohort_report(
        _ledger(completed, episodes=episodes, outcomes=outcomes, predictions=predictions)
    )
    evidence = report["completion_evidence"]
    assert evidence["present"] is True
    assert evidence["verified"] is False
    assert evidence["mismatches"]

    slot = completed.admitted_slots[0]
    verified_leaves = []
    for horizon_id in completed.manifest.horizons:
        digest = _leaf_digest_from_outcomes(slot.slot_id, horizon_id, outcomes, episodes, slot)
        verified_leaves.append(
            WorldCohortCompletionHorizonLeaf(horizon_id=horizon_id, status="observed", digest=digest)
        )
    from trader.domain.world_cohort import WorldCohortCompletionEvidence, WorldCohortCompletionSlotEvidence

    matching = WorldCohortCompletionEvidence(
        slots=(WorldCohortCompletionSlotEvidence(slot_id=slot.slot_id, leaves=tuple(verified_leaves)),)
    )
    verified_cohort = closed.complete(CompleteWorldCohort(evidence=matching))
    verified = project_world_cohort_report(
        _ledger(verified_cohort, episodes=episodes, outcomes=outcomes, predictions=predictions)
    )
    assert verified["completion_evidence"]["verified"] is True
    assert verified["completion_evidence"]["mismatches"] == []


def test_one_week_of_data_is_coverage_plumbing_preliminary_trends_only() -> None:
    cohort = _collecting(statistical_protocol=_protocol_small(), support_gates=_support(descriptive=1))
    later = ANCHOR_TS + timedelta(days=6, hours=12)
    first = _paired_bundle(cohort, as_of=ANCHOR_TS)
    second = _paired_bundle(cohort, symbol="MSFT", as_of=later, batch="batch:v1:anchor-msft")
    report = project_world_cohort_report(
        _ledger(
            cohort,
            episodes=first[0] + second[0],
            outcomes=first[1] + second[1],
            predictions=first[2] + second[2],
        )
    )
    assert report["interpretation_limit"] == "coverage_plumbing_preliminary_trends_only"
    assert report["claims_limits"]["one_week_is_coverage_plumbing_preliminary_trends_only"] is True
    _assert_bounded_claims(report)


def test_query_indexed_cohort_columns_override_nested_json(tmp_path: Path) -> None:
    from trader.application.world_model.cohort_service import WorldCohortService
    from trader.domain.world_cohort import AdmitWorldCohortSlot, RegisterWorldCohort, WorldCohortId
    from trader.infrastructure.state_db.world_model_store import WorldModelStore
    from tests.application.test_world_cohort_service import START_READY, _arm_command, _start_command

    db_path = tmp_path / "world_model.db"
    store = WorldModelStore(db_path, clock=lambda: START_READY)
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
        stored_episode = _store_market_episode(
            venue=slot.venue,
            symbol=slot.symbol,
            as_of=slot.as_of_bar_ts,
            bar_interval=slot.bar_interval,
        )
        v1_id = stored_episode["episode_id"]
        assert store.append_episode(stored_episode)
        lane = cohort.manifest.lane_by_id["markov.market"]
        payload = {
            "prediction_id": "pred-indexed",
            "run_id": "run-1",
            "episode_id": v1_id,
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
                "episode_id": v1_id,
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
    finally:
        store.close()

    ledger = read_world_cohort_ledger(db_path, cohort.cohort_id)
    assert ledger["status"] == "loaded"
    row = ledger["predictions"][0]
    assert row["study_cohort_id"] == cohort.cohort_id
    assert row["lane_id"] == "markov.market"
    assert row["manifest_sha256"] == cohort.manifest.manifest_sha256
    assert "json-should-lose" not in {row["study_cohort_id"], row["lane_id"]}

    shadow = read_world_model_ledger(db_path)
    assert shadow["status"] == "loaded"
    assert shadow["predictions"][0]["study_cohort_id"] == cohort.cohort_id
    assert shadow["predictions"][0]["lane_id"] == "markov.market"

    report = read_world_cohort_report(tmp_path, cohort.cohort_id)
    assert report["status"] == "loaded"
    _assert_bounded_claims(report)
    replay = read_world_cohort_report(tmp_path, cohort.cohort_id)
    assert json.dumps(report, sort_keys=True, default=str) == json.dumps(replay, sort_keys=True, default=str)


def _protocol_small() -> WorldStatisticalProtocol:
    return WorldStatisticalProtocol(
        pair_unit="unique_market_anchor",
        block_key="venue_session",
        ci_method="deterministic_block_bootstrap.v1",
        ci_level=0.95,
        bootstrap_resamples=32,
        seed=20260823,
    )


def _primary_status_contrast(report: dict[str, Any], *, horizon_id: str = "elapsed_1d.v1") -> dict[str, Any]:
    matches = [
        item
        for item in report["contrasts"]
        if item["contrast_id"] == "markov.status_only_minus_market.v1" and item["horizon_id"] == horizon_id
    ]
    assert matches, report["contrasts"]
    return matches[0]


def _leaf_digest_from_outcomes(
    slot_id: str,
    horizon_id: str,
    outcomes: list[dict[str, Any]],
    episodes: list[dict[str, Any]],
    slot: Any,
) -> str:
    from trader.reporting.read_models.world_cohort import world_cohort_horizon_leaf_digest

    episode_ids = {
        item["episode_id"]
        for item in episodes
        if item["venue"] == slot.venue
        and item["symbol"] == slot.symbol
        and item["bar_interval"] == slot.bar_interval
        and item["as_of_bar_ts"] == _iso(slot.as_of_bar_ts)
    }
    row = sorted(
        (item for item in outcomes if item["episode_id"] in episode_ids and item["horizon_id"] == horizon_id),
        key=lambda item: str(item["outcome_event_id"]),
    )[0]
    return world_cohort_horizon_leaf_digest(
        slot_id=slot_id,
        horizon_id=horizon_id,
        status=str(row["status"]),
        outcome_event_id=str(row["outcome_event_id"]),
        evidence_sha256=str(row["evidence_sha256"]),
        move_class=str(row["move_class"]),
    )


def test_query_unknown_cohort_does_not_create_rows(tmp_path: Path) -> None:
    from trader.infrastructure.state_db.world_model_store import WorldModelStore

    db_path = tmp_path / "world_model.db"
    store = WorldModelStore(db_path)
    store.close()
    missing = "world_cohort:v1:" + "e" * 64
    ledger = read_world_cohort_ledger(db_path, missing)
    report = read_world_cohort_report(tmp_path, missing)
    assert ledger["status"] == "unknown_cohort"
    assert report["status"] == "unknown_cohort"
    _assert_bounded_claims(report)
    connection = sqlite3.connect(db_path)
    try:
        count = connection.execute("SELECT COUNT(*) FROM world_cohort_manifests").fetchone()[0]
    finally:
        connection.close()
    assert count == 0
