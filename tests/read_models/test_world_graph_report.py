from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from tests.domain.test_world_cohort import COHORT_ID, _lane, _manifest
from trader.application.world_model.cohort_service import WorldCohortService
from trader.domain.world_cohort import (
    ArmWorldCohort,
    RegisterWorldCohort,
    SensorMask,
    StartWorldCohort,
    WorldContrastDefinition,
    WorldContrastTerm,
    WorldSensorRequirement,
)
from trader.domain.world_feature_contract import (
    WORLD_SCOPE_MAPPING_ID,
    WORLD_SCOPE_MAPPING_SHA256,
    world_v3_feature_contract,
    world_v3_graph_content_mask,
    world_v3_topology_status_only_mask,
)
from trader.infrastructure.state_db.world_model_query import read_world_cohort_catalog
from trader.infrastructure.state_db.world_model_store import WorldModelStore
from trader.reporting.read_models.world_graph import read_world_graph_report, read_world_graph_status


GRAPH_COHORT_ID = "world_cohort:v1:" + "d" * 64
V3 = world_v3_feature_contract()
TOPOLOGY = world_v3_topology_status_only_mask()
CONTENT = world_v3_graph_content_mask()

_CLAIM = {
    "authority": "shadow_only",
    "decision_effect": "none",
    "recommendation": "NO_GO",
    "causal_claim": False,
    "pnl_claim": False,
    "actual_trader_contribution": "not_attributable",
}


def _graph_manifest(**overrides: object):
    values: dict[str, object] = {
        "cohort_id": GRAPH_COHORT_ID,
        "context_feature_contract": V3.contract_id,
        "scope_mapping": {
            "mapping_id": WORLD_SCOPE_MAPPING_ID,
            "mapping_sha256": WORLD_SCOPE_MAPPING_SHA256,
        },
        "sensor_requirements": (
            WorldSensorRequirement(
                sensor_id="graph",
                source_contract_id=V3.contract_id,
                projection_contract_id="graph_v3_projection.v1",
                mode="required",
                lane_ids=("markov.graph", "gru.graph"),
            ),
        ),
        "lanes": (
            _lane("markov.graph", contract=V3, mask=TOPOLOGY, role="primary_control"),
            _lane(
                "gru.graph",
                family="gru",
                contract=V3,
                mask=CONTENT,
                role="pilot_treatment",
                sequence_length=4,
            ),
        ),
        "contrasts": (
            WorldContrastDefinition(
                contrast_id="gru.graph_minus_markov.graph.v1",
                terms=(
                    WorldContrastTerm(lane_id="gru.graph", coefficient=1),
                    WorldContrastTerm(lane_id="markov.graph", coefficient=-1),
                ),
                primary_metric="paired_multiclass_log_loss",
                role="pilot_treatment",
            ),
        ),
    }
    values.update(overrides)
    return _manifest(**values)


def _required_sensors(manifest) -> tuple[str, ...]:
    return tuple(item.sensor_id for item in manifest.sensor_requirements if item.mode is SensorMask.REQUIRED)


def _persist(tmp_path: Path, manifest, *, phase: str) -> None:
    store = WorldModelStore(tmp_path / "world_model.db")
    try:
        service = WorldCohortService(repository=store, query=store)
        service.register(RegisterWorldCohort(manifest=manifest))
        if phase in {"armed", "collecting"}:
            service.arm(
                ArmWorldCohort(
                    cohort_id=manifest.cohort_id,
                    manifest_sha256=manifest.manifest_sha256,
                    runtime_identity=manifest.runtime_identity,
                    satisfied_sensor_ids=_required_sensors(manifest),
                )
            )
        if phase == "collecting":
            service.start(
                StartWorldCohort(
                    cohort_id=manifest.cohort_id,
                    manifest_sha256=manifest.manifest_sha256,
                    runtime_identity=manifest.runtime_identity,
                )
            )
    finally:
        store.close()


def _assert_claims(payload: dict[str, Any]) -> None:
    for key, expected in _CLAIM.items():
        assert payload[key] == expected


def test_catalog_missing_database_is_read_only_not_started(tmp_path: Path) -> None:
    db_path = tmp_path / "world_model.db"
    catalog = read_world_cohort_catalog(db_path)

    assert catalog["status"] == "not_started"
    assert catalog["exists"] is False
    assert catalog["cohorts"] == []
    assert not db_path.exists()


def test_catalog_schema_unavailable_when_cohort_tables_are_missing(tmp_path: Path) -> None:
    db_path = tmp_path / "world_model.db"
    connection = sqlite3.connect(db_path)
    try:
        connection.execute("CREATE TABLE world_episodes (episode_id TEXT PRIMARY KEY)")
        connection.execute("CREATE TABLE world_outcome_events (outcome_event_id TEXT PRIMARY KEY)")
        connection.execute("CREATE TABLE world_shadow_predictions (prediction_id TEXT PRIMARY KEY)")
        connection.commit()
    finally:
        connection.close()

    catalog = read_world_cohort_catalog(db_path)
    assert catalog["status"] == "schema_unavailable"
    assert catalog["exists"] is True
    assert "world_cohort_manifests" in catalog["missing_tables"]
    assert catalog["cohorts"] == []


def test_catalog_lists_persisted_graph_and_c1_snapshots(tmp_path: Path) -> None:
    c1 = _manifest()
    graph = _graph_manifest()
    _persist(tmp_path, c1, phase="collecting")
    _persist(tmp_path, graph, phase="collecting")

    catalog = read_world_cohort_catalog(tmp_path / "world_model.db")
    assert catalog["status"] == "loaded"
    assert catalog["exists"] is True
    by_id = {item["cohort_id"]: item for item in catalog["cohorts"]}
    assert set(by_id) == {COHORT_ID, GRAPH_COHORT_ID}
    assert by_id[GRAPH_COHORT_ID]["manifest"]["cohort_id"] == GRAPH_COHORT_ID
    assert [event["event_type"] for event in by_id[GRAPH_COHORT_ID]["events"]] == [
        "world_cohort_registered",
        "world_cohort_armed",
        "world_cohort_started",
    ]


def test_graph_status_absent_db_stays_not_started_and_does_not_create_store(tmp_path: Path) -> None:
    payload = read_world_graph_status(tmp_path)

    assert payload["schema_version"] == "world_graph_status.v1"
    assert payload["status"] == "not_started"
    assert payload["exists"] is False
    assert payload["gaps"]["cohort_activation"] == "not_started"
    _assert_claims(payload)
    assert not (tmp_path / "world_model.db").exists()


def test_graph_status_empty_store_is_no_graph_cohort_not_not_started(tmp_path: Path) -> None:
    store = WorldModelStore(tmp_path / "world_model.db")
    store.close()

    payload = read_world_graph_status(tmp_path)
    assert payload["exists"] is True
    assert payload["gaps"]["cohort_activation"] == "no_graph_cohort"
    _assert_claims(payload)


def test_collecting_c1_without_graph_lanes_is_no_graph_cohort(tmp_path: Path) -> None:
    _persist(tmp_path, _manifest(), phase="collecting")

    payload = read_world_graph_status(tmp_path)
    assert payload["exists"] is True
    assert payload["gaps"]["cohort_activation"] == "no_graph_cohort"
    _assert_claims(payload)


def test_graph_status_derives_collecting_from_persisted_graph_cohort(tmp_path: Path) -> None:
    _persist(tmp_path, _manifest(), phase="collecting")
    _persist(tmp_path, _graph_manifest(), phase="collecting")

    payload = read_world_graph_status(tmp_path)
    assert payload["exists"] is True
    assert payload["gaps"]["cohort_activation"] == "collecting"
    assert payload["gaps"]["graph_cohort_id"] == GRAPH_COHORT_ID
    _assert_claims(payload)


def test_graph_status_does_not_hardcode_collecting_when_graph_is_only_registered(tmp_path: Path) -> None:
    _persist(tmp_path, _manifest(), phase="collecting")
    _persist(tmp_path, _graph_manifest(), phase="registered")

    payload = read_world_graph_status(tmp_path)
    assert payload["gaps"]["cohort_activation"] == "registered"
    assert payload["gaps"]["graph_cohort_id"] == GRAPH_COHORT_ID
    _assert_claims(payload)


def test_graph_report_uses_persisted_graph_activation_without_writing(tmp_path: Path) -> None:
    _persist(tmp_path, _graph_manifest(), phase="collecting")
    before = (tmp_path / "world_model.db").stat().st_mtime_ns

    payload = read_world_graph_report(tmp_path, GRAPH_COHORT_ID)
    assert payload["schema_version"] == "world_graph_report.v1"
    assert payload["gaps"]["cohort_activation"] == "collecting"
    assert payload["gaps"]["graph_cohort_id"] == GRAPH_COHORT_ID
    _assert_claims(payload)
    assert (tmp_path / "world_model.db").stat().st_mtime_ns == before
