from __future__ import annotations

from datetime import datetime, timezone

from trader.domain.world_context import EntityRef, TopologyEdge
from trader.reporting.read_models.world_prediction_integrity import contains_forbidden_trader_feature


UTC = timezone.utc
CUTOFF = datetime(2026, 8, 24, 9, 0, tzinfo=UTC)


def _edge() -> dict[str, object]:
    return TopologyEdge(
        kind="TRADED_ON",
        source=EntityRef(kind="instrument", entity_id="AAPL"),
        target=EntityRef(kind="venue", entity_id="US"),
        effective_from=CUTOFF,
        ready_at=CUTOFF,
        ontology_revision="market_ontology.v1",
        source_refs=("market_metadata",),
    ).to_dict()


def _prediction_with(collection: str, edge: object) -> dict[str, object]:
    return {"input": {"context": {collection: [edge]}}}


def test_canonical_topology_targets_are_not_trader_targets() -> None:
    edge = _edge()

    assert contains_forbidden_trader_feature(_prediction_with("topology_edges", edge)) is False
    assert contains_forbidden_trader_feature(_prediction_with("topology_path", edge)) is False


def test_target_remains_forbidden_outside_exact_topology_paths() -> None:
    edge = _edge()

    assert contains_forbidden_trader_feature({"input": {"target": "UP"}}) is True
    assert contains_forbidden_trader_feature({"context": {"topology_edges": [edge]}}) is True
    assert contains_forbidden_trader_feature({"input": {"context": {"other_edges": [edge]}}}) is True


def test_forged_or_extended_topology_edges_remain_forbidden() -> None:
    extended = {**_edge(), "label": "UP"}
    forged_ref = {**_edge(), "target": {"kind": "venue", "entity_id": "US", "decision_id": "cycle-1"}}
    malformed_collection = {"target": {"kind": "venue", "entity_id": "US"}}

    assert contains_forbidden_trader_feature(_prediction_with("topology_edges", extended)) is True
    assert contains_forbidden_trader_feature(_prediction_with("topology_edges", forged_ref)) is True
    assert contains_forbidden_trader_feature({"input": {"context": {"topology_edges": malformed_collection}}}) is True


def test_other_trader_keys_stay_forbidden_beside_a_canonical_edge() -> None:
    payload = {
        "input": {
            "context": {
                "topology_edges": [_edge()],
                "decision_id": "cycle-1",
            }
        }
    }

    assert contains_forbidden_trader_feature(payload) is True
