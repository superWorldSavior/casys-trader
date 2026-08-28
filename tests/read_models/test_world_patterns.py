from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.domain.test_world_pattern import (
    CUTOFF,
    FORMATION,
    _evaluating,
    _occurrence,
    _outcome,
    _path,
    _prediction,
    _spec,
    _step,
)
from trader.domain.world_episode import (
    MARKET_FEATURE_CONTRACT_ID,
    AnchorBar,
    WorldEpisode,
    WorldObservation,
    canonical_sha256,
)
from trader.domain.world_feature_contract import WORLD_GRAPH_CONFIG_SHA256
from trader.domain.world_graph import WorldEntityRef
from trader.domain.world_pattern import PatternHypothesis, PatternMatchedHop, PatternOccurrence
from trader.infrastructure.state_db.world_model_query import (
    read_world_cohort_ledger,
    read_world_pattern_ledger,
)
from trader.reporting.read_models.world_patterns import (
    WORLD_PATTERN_STATUS_SCHEMA,
    project_world_pattern_report,
    project_world_pattern_status,
    read_world_pattern_report,
    read_world_pattern_status,
)


UTC = timezone.utc
COHORT_ID = "world_cohort:graph_pilot"
CLOSED_AT = datetime(2026, 9, 4, tzinfo=UTC)
REPO_ROOT = Path(__file__).resolve().parents[2]
GRAPH_CONFIG = yaml.safe_load((REPO_ROOT / "config" / "world_graph.yaml").read_text(encoding="utf-8"))
ASSESSMENT_CONCLUSIONS = frozenset(
    {
        "predictive_association_observed",
        "inconclusive",
        "not_supported",
        "invalidated",
    }
)
YAML_NEGATIVE_CONTROLS = tuple(GRAPH_CONFIG["negative_controls"])
_CLAIM_KEYS = {
    "authority": "shadow_only",
    "decision_effect": "none",
    "recommendation": "NO_GO",
    "causal_claim": False,
    "pnl_claim": False,
    "actual_trader_contribution": "not_attributable",
}

_INSTRUMENTS = (
    ("2330", {"DOWN": 0.05, "FLAT": 0.05, "UP": 0.90}, 102.0),
    ("2454", {"DOWN": 0.90, "FLAT": 0.05, "UP": 0.05}, 97.0),
    ("2303", {"DOWN": 0.05, "FLAT": 0.90, "UP": 0.05}, 100.2),
    ("3711", {"DOWN": 0.05, "FLAT": 0.05, "UP": 0.90}, 103.0),
    ("2308", {"DOWN": 0.90, "FLAT": 0.05, "UP": 0.05}, 96.0),
)
_CONTEXT_POOR = {
    "2330": {"DOWN": 0.80, "FLAT": 0.10, "UP": 0.10},
    "2454": {"DOWN": 0.10, "FLAT": 0.10, "UP": 0.80},
    "2303": {"DOWN": 0.80, "FLAT": 0.10, "UP": 0.10},
    "3711": {"DOWN": 0.80, "FLAT": 0.10, "UP": 0.10},
    "2308": {"DOWN": 0.10, "FLAT": 0.10, "UP": 0.80},
}


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _store_market_episode(symbol: str, *, as_of: datetime = CUTOFF) -> dict[str, Any]:
    observation = WorldObservation(
        venue="XTAI",
        symbol=symbol,
        bar_interval="1h",
        as_of_bar_ts=as_of,
        feature_contract_version=MARKET_FEATURE_CONTRACT_ID,
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


def _episode_id(symbol: str, *, contract: str) -> str:
    return f"world-episode:v1:{canonical_sha256({'symbol': symbol, 'contract': contract})}"


def _instrument(symbol: str) -> WorldEntityRef:
    return WorldEntityRef(kind="instrument", entity_id=f"mic:XTAI:symbol:{symbol}")


def _closed(hypothesis: PatternHypothesis | None = None) -> PatternHypothesis:
    resolved = hypothesis if hypothesis is not None else _evaluating()
    return resolved.close_evaluation(closed_at=CLOSED_AT)


def _short_path() -> tuple[PatternMatchedHop, ...]:
    step = _step(0, source_kind="instrument", relation_kind="TRADED_ON", target_kind="venue")
    return (
        PatternMatchedHop(
            ordinal=step.ordinal,
            source_kind=step.source_kind,
            relation_kind=step.relation_kind,
            direction=step.direction,
            target_kind=step.target_kind,
            freshness_bucket=step.freshness_bucket,
            evidence_rule_version=step.evidence_rule_version,
            evidence_refs=("macro_source_fact_version:v1:" + "d" * 64,),
        ),
    )


def _linked_occurrence(
    hypothesis: PatternHypothesis,
    *,
    symbol: str,
    probabilities: dict[str, float],
    endpoint_close: float,
    cutoff_at: datetime = CUTOFF,
    exact_path: tuple[PatternMatchedHop, ...] | None = None,
    model_id: str | None = None,
) -> tuple[PatternOccurrence, dict[str, Any]]:
    episode_id = _episode_id(symbol, contract="v3")
    forecast_kwargs: dict[str, Any] = {
        "episode_id": episode_id,
        "created_at": cutoff_at,
        "feature_hash": f"graph-{symbol}-{hypothesis.spec.model_identity}",
        "probabilities": probabilities,
    }
    if model_id is not None:
        forecast_kwargs["model_id"] = model_id
    occurrence = _occurrence(
        hypothesis,
        cohort_id=COHORT_ID,
        instrument=_instrument(symbol),
        cutoff_at=cutoff_at,
        exact_path=_path() if exact_path is None else exact_path,
        forecast=_prediction(**forecast_kwargs),
    )
    outcome = _outcome(episode_id=episode_id, endpoint_close=endpoint_close)
    linked = occurrence.link_outcome(outcome)
    return linked, outcome.to_dict()


def _context_prediction(
    *,
    symbol: str,
    probabilities: dict[str, float],
    cutoff_at: datetime = CUTOFF,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    episode_id = _episode_id(symbol, contract="v2")
    payload = {
        "prediction_id": f"world-prediction:v1:{canonical_sha256({'v2': symbol, 'cutoff': _iso(cutoff_at)})}",
        "episode_id": episode_id,
        "horizon_id": "elapsed_1d.v1",
        "horizon_code": "elapsed_1d.v1",
        "feature_contract_id": "world_feature.context.v1",
        "feature_contract_version": "world_feature.context.v1",
        "feature_mask_id": "status_only.v1",
        "model_id": "hierarchical_dirichlet_world_baseline@context.v1",
        "model_kind": "hierarchical_dirichlet_world_baseline@context.v1",
        "model_version": "context.v1",
        "probabilities": probabilities,
        "venue": "XTAI",
        "symbol": symbol,
        "bar_interval": "1h",
        "as_of_bar_ts": _iso(cutoff_at),
        "predicted_at": _iso(cutoff_at),
        "ready_at": _iso(cutoff_at),
        "study_cohort_id": COHORT_ID,
    }
    if extra:
        payload.update(extra)
    return payload


def _topology_prediction(
    *, symbol: str, probabilities: dict[str, float], cutoff_at: datetime = CUTOFF
) -> dict[str, Any]:
    payload = _context_prediction(symbol=symbol, probabilities=probabilities, cutoff_at=cutoff_at)
    payload.update(
        {
            "prediction_id": f"world-prediction:v1:{canonical_sha256({'topo': symbol, 'cutoff': _iso(cutoff_at)})}",
            "episode_id": _episode_id(symbol, contract="v3-status"),
            "feature_contract_id": "world_feature.graph.v1",
            "feature_contract_version": "world_feature.graph.v1",
            "feature_mask_id": "topology_status_only.v1",
            "model_id": "hierarchical_dirichlet_world_baseline@graph.v1",
            "model_kind": "hierarchical_dirichlet_world_baseline@graph.v1",
            "model_version": "graph.v1",
        }
    )
    return payload


def _matched_family(
    *,
    closed: bool = True,
    include_topology: bool = True,
    include_without_event: bool = True,
    second_hypothesis: bool = False,
    invalidate: bool = False,
    mismatch_outcome: bool = False,
    single: bool = False,
) -> dict[str, Any]:
    instruments = _INSTRUMENTS[:1] if single else _INSTRUMENTS
    evaluating = _evaluating()
    hypothesis = evaluating
    if closed and not invalidate:
        hypothesis = _closed(evaluating)
    if invalidate:
        hypothesis = evaluating.invalidate(invalidated_at=CLOSED_AT, reason="protocol_stop")
    hypothesis_events = [event.to_dict() for event in hypothesis.events]
    occurrence_events: list[dict[str, Any]] = []
    outcomes: list[dict[str, Any]] = []
    predictions: list[dict[str, Any]] = []
    record_on = evaluating
    for symbol, probabilities, endpoint in instruments:
        occurrence, outcome_payload = _linked_occurrence(
            record_on,
            symbol=symbol,
            probabilities=probabilities,
            endpoint_close=endpoint,
        )
        occurrence_events.extend(event.to_dict() for event in occurrence.events)
        if mismatch_outcome:
            outcome_payload = {**outcome_payload, "source_raw_sha256": "d" * 64}
        outcomes.append(outcome_payload)
        predictions.append(_context_prediction(symbol=symbol, probabilities=_CONTEXT_POOR[symbol]))
        if include_topology:
            predictions.append(_topology_prediction(symbol=symbol, probabilities=_CONTEXT_POOR[symbol]))
    if include_without_event:
        predictions.append(
            _context_prediction(
                symbol="2317",
                probabilities={"DOWN": 0.33, "FLAT": 0.34, "UP": 0.33},
                extra={"prediction_id": f"world-prediction:v1:{canonical_sha256({'v2': '2317'})}"},
            )
        )
    if second_hypothesis:
        other = _evaluating(
            spec=_spec(
                steps=(_step(0, source_kind="instrument", relation_kind="TRADED_ON", target_kind="venue"),),
                model_identity="hierarchical_dirichlet_world_baseline@graph.v1",
            )
        )
        noise = _linked_occurrence(
            other,
            symbol="2330",
            probabilities=_CONTEXT_POOR["2330"],
            endpoint_close=102.0,
            exact_path=_short_path(),
            model_id="hierarchical_dirichlet_world_baseline@graph.v1",
        )
        hypothesis_events.extend(event.to_dict() for event in _closed(other).events)
        occurrence_events.extend(event.to_dict() for event in noise[0].events)
        outcomes.append(noise[1])
    return {
        "status": "loaded",
        "exists": True,
        "cohort_id": COHORT_ID,
        "hypothesis_events": hypothesis_events,
        "occurrence_events": occurrence_events,
        "outcome_links": [],
        "outcomes": outcomes,
        "predictions": predictions,
        "episodes": [],
    }


def _assert_bounded_claims(payload: dict[str, Any]) -> None:
    for key, expected in _CLAIM_KEYS.items():
        assert payload[key] == expected
    assert payload.get("winner") is not True
    serialized = json.dumps(payload, sort_keys=True, default=str).lower()
    assert "true" not in serialized or '"causal_claim": false' in json.dumps(payload, sort_keys=True).lower()
    assert payload["causal_claim"] is False
    assert payload["pnl_claim"] is False


def _assessment(report: dict[str, Any], hypothesis_id: str | None = None) -> dict[str, Any]:
    rows = report["assessments"]
    assert rows
    if hypothesis_id is None:
        return rows[0]
    matches = [row for row in rows if row["hypothesis_id"] == hypothesis_id]
    assert matches, report["assessments"]
    return matches[0]


def test_missing_database_is_read_only_not_started(tmp_path: Path) -> None:
    db_path = tmp_path / "world_model.db"
    ledger = read_world_pattern_ledger(db_path, COHORT_ID)
    report = read_world_pattern_report(tmp_path, COHORT_ID)
    status = read_world_pattern_status(tmp_path, COHORT_ID)

    assert ledger["status"] == "not_started"
    assert ledger["exists"] is False
    assert report["status"] == "not_started"
    assert report["schema_version"] == "world_pattern_report.v1"
    assert status["status"] == "not_started"
    assert status["schema_version"] == WORLD_PATTERN_STATUS_SCHEMA
    _assert_bounded_claims(report)
    _assert_bounded_claims(status)
    assert not db_path.exists()


def test_status_lists_evaluating_hypothesis_with_zero_occurrences() -> None:
    hypothesis = _evaluating()
    payload = project_world_pattern_status(
        {
            "status": "loaded",
            "exists": True,
            "cohort_id": COHORT_ID,
            "hypothesis_events": [event.to_dict() for event in hypothesis.events],
            "occurrence_events": [],
            "outcomes": [],
        }
    )
    assert payload["schema_version"] == WORLD_PATTERN_STATUS_SCHEMA
    assert payload["hypothesis_count"] == 1
    assert payload["occurrence_count"] == 0
    row = payload["hypotheses"][0]
    assert row["hypothesis_id"] == hypothesis.hypothesis_id
    assert row["status"] == "evaluating"
    assert row["occurrence_count"] == 0
    assert row["expected_horizon_ids"] == ["elapsed_4h.v1", "elapsed_1d.v1", "elapsed_3d.v1"]
    _assert_bounded_claims(payload)


def test_schema_unavailable_when_pattern_tables_are_missing(tmp_path: Path) -> None:
    db_path = tmp_path / "world_model.db"
    connection = sqlite3.connect(db_path)
    try:
        connection.execute("CREATE TABLE world_episodes (episode_id TEXT PRIMARY KEY)")
        connection.execute("CREATE TABLE world_outcome_events (outcome_event_id TEXT PRIMARY KEY)")
        connection.execute("CREATE TABLE world_shadow_predictions (prediction_id TEXT PRIMARY KEY)")
        connection.commit()
    finally:
        connection.close()

    ledger = read_world_pattern_ledger(db_path, COHORT_ID)
    report = read_world_pattern_report(tmp_path, COHORT_ID)
    assert ledger["status"] == "schema_unavailable"
    assert report["status"] == "schema_unavailable"
    _assert_bounded_claims(report)
    assert db_path.exists()
    assert "world_pattern_hypothesis_events" in ledger["missing_tables"]


def test_report_schema_keeps_shadow_only_claims_and_closed_conclusions() -> None:
    report = project_world_pattern_report(_matched_family())
    assert report["schema_version"] == "world_pattern_report.v1"
    assert report["status"] == "loaded"
    assert report["cohort_id"] == COHORT_ID
    _assert_bounded_claims(report)
    assert report["conclusion"] in ASSESSMENT_CONCLUSIONS
    for assessment in report["assessments"]:
        assert assessment["schema_version"] == "pattern_assessment.v1"
        assert assessment["conclusion"] in ASSESSMENT_CONCLUSIONS
        assert assessment["causal_claim"] is False
        assert assessment["pnl_claim"] is False
        assert assessment["winner"] is False
        assert "move_class" not in assessment.get("outcome_links", [{}])[0] if assessment.get("outcome_links") else True


def test_report_rejoins_compact_predictions_to_their_canonical_episodes() -> None:
    expanded = _matched_family()
    compact = dict(expanded)
    episodes: list[dict[str, Any]] = []
    predictions: list[dict[str, Any]] = []
    for source in expanded["predictions"]:
        observation = {
            "venue": source["venue"],
            "symbol": source["symbol"],
            "bar_interval": source["bar_interval"],
            "as_of_bar_ts": source["as_of_bar_ts"],
            "feature_contract_version": source["feature_contract_version"],
        }
        episodes.append({"episode_id": source["episode_id"], "observation": observation})
        prediction = dict(source)
        for field in (
            "input",
            "venue",
            "symbol",
            "bar_interval",
            "as_of_bar_ts",
            "feature_contract_id",
            "feature_contract_version",
        ):
            prediction.pop(field, None)
        predictions.append(prediction)
    compact["episodes"] = episodes
    compact["predictions"] = predictions

    expected = project_world_pattern_report(expanded)
    actual = project_world_pattern_report(compact)

    assert actual["matched_sets"] == expected["matched_sets"]
    assert actual["assessments"] == expected["assessments"]
    _assert_bounded_claims(actual)


def test_matched_context_graph_sets_pair_same_anchor_and_horizon() -> None:
    report = project_world_pattern_report(_matched_family())
    assert report["matched_sets"]
    for row in report["matched_sets"]:
        assert row["venue"] == "XTAI"
        assert row["horizon_id"] == "elapsed_1d.v1"
        assert row["context_feature_contract_id"] == "world_feature.context.v1"
        assert row["graph_feature_contract_id"] == "world_feature.graph.v1"
        assert row["graph_feature_mask_id"] == "graph_content.v1"
        assert row["context_prediction_id"]
        assert row["occurrence_id"].startswith("pattern_occurrence:v1:")
        assert set(row["lane_ids"]) >= {"context", "graph"}
    symbols = {row["symbol"] for row in report["matched_sets"]}
    assert symbols == {"2330", "2454", "2303", "3711", "2308"}
    assessment = _assessment(report)
    assert assessment["unique_support"] == 5
    assert assessment["lift_vs_context"]["mean_delta"] < 0.0
    assert assessment["log_loss"]["graph"] < assessment["log_loss"]["context"]


def test_canonical_world_outcome_is_verified_id_digest_horizon() -> None:
    ledger = _matched_family(include_topology=False, include_without_event=False, single=True)
    report = project_world_pattern_report(ledger)
    assessment = _assessment(report)
    verified = assessment["canonical_outcomes"]
    assert verified
    leaf = verified[0]
    assert leaf["verified"] is True
    assert leaf["world_outcome_event_id"].startswith("world-outcome:v1:")
    assert len(leaf["world_outcome_content_sha256"]) == 64
    assert leaf["horizon_id"] == "elapsed_1d.v1"
    reconstructed = _outcome(
        episode_id=_episode_id("2330", contract="v3"),
        endpoint_close=102.0,
    )
    assert leaf["world_outcome_event_id"] == reconstructed.event_id
    assert leaf["world_outcome_content_sha256"] == reconstructed.payload_hash
    assert "move_class" not in leaf
    assert "simple_return" not in leaf


def test_outcome_mismatch_is_excluded_never_second_label_authority() -> None:
    report = project_world_pattern_report(
        _matched_family(mismatch_outcome=True, include_topology=False, include_without_event=False)
    )
    assert report["exclusions"]["canonical_outcome_mismatch"] >= 1
    assert report["matched_sets"] == []
    assessment = _assessment(report)
    assert assessment["conclusion"] == "inconclusive"
    assert assessment["unique_support"] == 0


def test_missing_hypothesis_excludes_occurrence_and_is_not_scored() -> None:
    ledger = _matched_family(include_topology=False, include_without_event=False, single=True)
    ledger["hypothesis_events"] = []
    report = project_world_pattern_report(ledger)
    assert report["exclusions"]["hypothesis_missing"] >= 1
    assert report["matched_sets"] == []
    assert report["assessments"] == []
    assert report["conclusion"] == "inconclusive"
    _assert_bounded_claims(report)


def test_invalidated_hypothesis_conclusion_is_invalidated() -> None:
    report = project_world_pattern_report(_matched_family(invalidate=True, include_topology=False))
    assessment = _assessment(report)
    assert assessment["conclusion"] == "invalidated"
    assert report["conclusion"] == "invalidated"
    assert assessment["winner"] is False


def test_sparse_support_is_inconclusive() -> None:
    report = project_world_pattern_report(
        _matched_family(single=True, include_topology=False, include_without_event=False)
    )
    assessment = _assessment(report)
    assert assessment["unique_support"] == 1
    assert assessment["conclusion"] == "inconclusive"


def test_negative_controls_come_from_world_graph_yaml() -> None:
    assert YAML_NEGATIVE_CONTROLS == (
        "topology_status_only",
        "entity_permutation_within_venue_session",
        "block_respecting_time_shift",
        "truncated_chain",
        "comparable_population_without_event",
    )
    report = project_world_pattern_report(_matched_family())
    controls = report["negative_controls"]
    assert tuple(controls) == YAML_NEGATIVE_CONTROLS or set(controls) == set(YAML_NEGATIVE_CONTROLS)
    for control_id in YAML_NEGATIVE_CONTROLS:
        row = controls[control_id]
        assert row["control_id"] == control_id
        assert row["status"] in {"evaluated", "unavailable"}
        assert row.get("causal_claim") is not True
    permutation = controls["entity_permutation_within_venue_session"]
    assert permutation["p_value"] is not None
    assert 0.0 <= permutation["p_value"] <= 1.0
    assert permutation["block_key"] == "venue_session"
    time_shift = controls["block_respecting_time_shift"]
    assert time_shift["shifted_mean_delta"] is not None
    truncated = controls["truncated_chain"]
    assert truncated["truncated_signature"]
    without_event = controls["comparable_population_without_event"]
    assert without_event["without_event_count"] >= 1
    topology = controls["topology_status_only"]
    assert topology["mask_id"] == "topology_status_only.v1"
    assessment = _assessment(report)
    assert set(assessment["permutation_results"]) >= {
        "entity_permutation_within_venue_session",
        "block_respecting_time_shift",
    }
    assert isinstance(assessment["counter_examples"], list)
    assert all("occurrence_id" in item for item in assessment["counter_examples"])


def test_holm_bonferroni_multiplicity_uses_frozen_yaml_family() -> None:
    assert GRAPH_CONFIG["multiplicity"]["method"] == "holm_bonferroni"
    assert GRAPH_CONFIG["multiplicity"]["family_wise_alpha"] == 0.05
    assert GRAPH_CONFIG["multiplicity"]["frozen_before"] == "GRAPH-7"
    assert GRAPH_CONFIG["content_sha256"] == WORLD_GRAPH_CONFIG_SHA256

    alone = project_world_pattern_report(_matched_family(include_without_event=False))
    family = project_world_pattern_report(_matched_family(second_hypothesis=True, include_without_event=False))
    multiplicity = family["multiplicity"]
    assert multiplicity["method"] == "holm_bonferroni"
    assert multiplicity["family_wise_alpha"] == 0.05
    assert multiplicity["frozen_before"] == "GRAPH-7"
    assert multiplicity["config_sha256"] == WORLD_GRAPH_CONFIG_SHA256
    assert multiplicity["family_size"] == 2
    assert len(multiplicity["adjusted"]) == 2
    alone_assessment = _assessment(alone)
    assert alone_assessment["conclusion"] == "predictive_association_observed"
    assert alone["multiplicity"]["family_size"] == 1
    assert alone["multiplicity"]["adjusted"][0]["holm_rejected"] is True
    # The same raw association is not a family-wise win once a second hypothesis is tested.
    assert family["conclusion"] in {"inconclusive", "not_supported"}
    assert (
        all(item["holm_rejected"] is False for item in multiplicity["adjusted"])
        or family["conclusion"] != "predictive_association_observed"
    )


def test_not_supported_when_graph_does_not_beat_context() -> None:
    hypothesis = _evaluating()
    occurrence, outcome_payload = _linked_occurrence(
        hypothesis,
        symbol="2330",
        probabilities=_CONTEXT_POOR["2330"],
        endpoint_close=102.0,
    )
    second, second_outcome = _linked_occurrence(
        hypothesis,
        symbol="2454",
        probabilities=_CONTEXT_POOR["2454"],
        endpoint_close=97.0,
    )
    ledger = {
        "status": "loaded",
        "exists": True,
        "cohort_id": COHORT_ID,
        "hypothesis_events": [event.to_dict() for event in _closed(hypothesis).events],
        "occurrence_events": [event.to_dict() for event in occurrence.events]
        + [event.to_dict() for event in second.events],
        "outcome_links": [],
        "outcomes": [outcome_payload, second_outcome],
        "predictions": [
            _context_prediction(symbol="2330", probabilities=_CONTEXT_POOR["2330"]),
            _context_prediction(symbol="2454", probabilities=_CONTEXT_POOR["2454"]),
        ],
        "episodes": [],
    }
    report = project_world_pattern_report(ledger)
    assessment = _assessment(report)
    assert assessment["lift_vs_context"]["mean_delta"] == 0.0 or assessment["lift_vs_context"]["mean_delta"] >= 0.0
    assert assessment["conclusion"] in {"not_supported", "inconclusive"}
    if assessment["unique_support"] >= 2 and assessment["lift_vs_context"]["mean_delta"] >= 0.0:
        assert assessment["conclusion"] == "not_supported"


def test_report_is_reproducible_and_reporting_has_no_sql() -> None:
    ledger = _matched_family()
    first = project_world_pattern_report(ledger)
    second = project_world_pattern_report(ledger)
    assert json.dumps(first, sort_keys=True, default=str) == json.dumps(second, sort_keys=True, default=str)
    source = (REPO_ROOT / "trader" / "reporting" / "read_models" / "world_patterns.py").read_text(encoding="utf-8")
    assert "sqlite3" not in source
    assert "SELECT " not in source
    assert "SELECT\n" not in source
    assert "payload_json" not in source


def test_query_indexed_pattern_columns_override_nested_json(tmp_path: Path) -> None:
    from trader.infrastructure.state_db.world_model_store import WorldModelStore
    from trader.infrastructure.state_db.world_pattern_store import WorldPatternStore

    db_path = tmp_path / "world_model.db"
    world = WorldModelStore(db_path)
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
        stored_episode = _store_market_episode("2330")
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
                "study_cohort_id": COHORT_ID,
                "lane_id": "markov.market",
                "input": {"venue": "XTAI", "symbol": "2330", "bar_interval": "1h", "as_of_bar_ts": v2["as_of_bar_ts"]},
            }
        )
        connection = sqlite3.connect(db_path)
        try:
            nested = json.loads(
                connection.execute(
                    "SELECT payload_json FROM world_pattern_occurrence_events "
                    "WHERE event_type='pattern_occurrence_recorded'"
                ).fetchone()[0]
            )
            nested["cohort_id"] = "json-should-lose"
            nested["horizon_id"] = "json-horizon"
            connection.execute("DROP TRIGGER world_pattern_occurrence_events_no_update")
            connection.execute(
                "UPDATE world_pattern_occurrence_events SET payload_json=? WHERE event_type='pattern_occurrence_recorded'",
                (json.dumps(nested),),
            )
            connection.execute(
                """
                CREATE TRIGGER world_pattern_occurrence_events_no_update
                BEFORE UPDATE ON world_pattern_occurrence_events
                BEGIN
                    SELECT RAISE(ABORT, 'world_pattern_occurrence_events are append-only');
                END
                """
            )
            connection.commit()
        finally:
            connection.close()
    finally:
        pattern.close()
        world.close()

    ledger = read_world_pattern_ledger(db_path, COHORT_ID)
    assert ledger["status"] == "loaded"
    recorded = next(
        row for row in ledger["occurrence_events"] if row.get("event_type") == "pattern_occurrence_recorded"
    )
    assert recorded["cohort_id"] == COHORT_ID
    assert recorded["horizon_id"] == "elapsed_1d.v1"
    assert recorded["cohort_id"] != "json-should-lose"
    prediction = ledger["predictions"][0]
    assert prediction["study_cohort_id"] == COHORT_ID
    report = read_world_pattern_report(tmp_path, COHORT_ID)
    assert report["status"] == "loaded"
    _assert_bounded_claims(report)
    replay = read_world_pattern_report(tmp_path, COHORT_ID)
    assert json.dumps(report, sort_keys=True, default=str) == json.dumps(replay, sort_keys=True, default=str)


def test_cohort_query_adapter_is_unchanged_for_missing_db(tmp_path: Path) -> None:
    ledger = read_world_cohort_ledger(tmp_path / "world_model.db", COHORT_ID)
    assert ledger["status"] == "not_started"
    assert ledger["exists"] is False
    assert not (tmp_path / "world_model.db").exists()


def test_status_surfaces_durable_lifecycle_marker_without_labels_or_causality(tmp_path: Path) -> None:
    from tests.state_db.test_world_pattern_store import (
        COHORT_ID as PATTERN_COHORT,
        EVAL_FP,
        EVAL_NOT_BEFORE,
        FORMATION,
        FORMATION_FP,
        _discovery_completed,
        _evaluating,
        _store,
    )

    store = _store(tmp_path)
    hypothesis = _evaluating()
    store.append_event(hypothesis.registered)
    store.append_event(next(event for event in hypothesis.events if event.event_type == "pattern_evaluation_started"))
    assert hypothesis.status == "evaluating"
    marker = _discovery_completed(
        selected_hypothesis_ids=(hypothesis.hypothesis_id,),
        selected_count=1,
    )
    assert store.append_discovery_completed(marker) is True
    store.close()

    status = read_world_pattern_status(tmp_path, PATTERN_COHORT)
    ledger = read_world_pattern_ledger(tmp_path / "world_model.db", PATTERN_COHORT)
    assert ledger["status"] == "loaded"
    assert ledger["lifecycle_events"]
    assert status["lifecycle"]["replay_authority"] == "world_pattern_lifecycle_events"
    assert status["lifecycle"]["causal_claim"] is False
    row = status["lifecycle"]["markers"][0]
    assert row["formation_cutoff"] == FORMATION.isoformat()
    assert row["evaluation_start_not_before"] == EVAL_NOT_BEFORE.isoformat()
    assert row["evaluation_dataset_fingerprint"] == EVAL_FP
    assert row["formation_dataset_fingerprint"] == FORMATION_FP
    assert row["selected_hypothesis_ids"] == [hypothesis.hypothesis_id]
    assert row["selected_count"] == 1
    assert row["event_id"] == marker.event_id
    assert row["started_event_id"] == marker.started_event_id
    assert row["replay_authority"] == "world_pattern_lifecycle_events"
    assert row["shadow_only"] is True
    assert row["decision_effect"] == "none"
    assert row["causal_claim"] is False
    serialized = json.dumps(row, sort_keys=True)
    assert "move_class" not in serialized
    assert "label_class" not in serialized
    _assert_bounded_claims(status)


def test_pattern_ledger_in_lists_are_chunked_beyond_sqlite_variable_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.state_db.test_world_pattern_store import COHORT_ID as PATTERN_COHORT, _store
    from trader.infrastructure.state_db import world_model_query as query_mod
    from urllib.parse import quote

    store = _store(tmp_path)
    db_path = store.path
    store.close()
    dummy_count = 1100
    with sqlite3.connect(db_path) as connection:
        for index in range(dummy_count):
            hypothesis_id = f"pattern_hypothesis:v1:{index:064x}"
            payload = json.dumps({"evaluation_cohort_id": PATTERN_COHORT})
            connection.execute(
                """
                INSERT INTO world_pattern_hypothesis_events (
                    event_id, hypothesis_id, event_type, sequence, payload_json, payload_sha256, recorded_at
                ) VALUES (?, ?, 'pattern_evaluation_started', 1, ?, ?, ?)
                """,
                (
                    f"pattern_hypothesis_event:v1:{index:064x}",
                    hypothesis_id,
                    payload,
                    "a" * 64,
                    "2026-09-02T00:00:00+00:00",
                ),
            )
        connection.commit()

    statements: list[str] = []

    def traced(path: Path):
        uri = f"file:{quote(str(path.resolve()), safe='/')}?mode=ro"
        connection = sqlite3.connect(uri, uri=True)
        connection.row_factory = sqlite3.Row
        connection.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 999)
        connection.set_trace_callback(lambda sql: statements.append(sql))
        return connection

    monkeypatch.setattr(query_mod, "_readonly_connection", traced)
    ledger = read_world_pattern_ledger(db_path, PATTERN_COHORT)
    assert ledger["status"] == "loaded"
    widths: list[int] = []
    for sql in statements:
        compact = " ".join(sql.split())
        if "hypothesis_id IN" not in compact:
            continue
        match = re.search(r"hypothesis_id IN \(([^)]*)\)", compact)
        assert match is not None
        body = match.group(1).strip()
        widths.append(0 if not body else body.count(",") + 1)
    assert widths
    assert all(width <= 500 for width in widths)
    assert max(widths) == 500
    assert sum(widths) == dummy_count
