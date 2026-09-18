from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from trader.application.world_model.capture import capture_world_episodes
from trader.domain.world_context import (
    ARTIFACT_KINDS,
    CONTEXT_FEATURE_CONTRACT_ID,
    NO_PROVEN_ARTIFACT_REASON,
    POLICY_CONTAMINATED_REASON,
    SCOPE_AMBIGUOUS_REASON,
    SCOPE_UNMAPPED_REASON,
    EntityRef,
    KnowledgeArtifact,
    SensorEvidence,
    TopologyEdge,
    WorldContextSnapshot,
    assert_context_schema,
    bootstrap_instrument_topology,
    freeze_context_mapping,
    model_facing_sensor_evidence,
    temporally_eligible,
    topology_path_for_instrument,
    verified_issuer_entity_id,
)
from trader.domain.world_episode import WorldObservation, canonical_json, world_episode_id


UTC = timezone.utc
CUTOFF = datetime(2026, 8, 22, 10, 5, tzinfo=UTC)

V1_EPISODE_ID = "world-episode:v1:a228bc3d0bffc20d69bacda2edd133c902819de56427afece301a01af3d80809"
V1_PAYLOAD_HASH = "178fe46c21e138c613b7bbf1b20049d39c50374e644cb42f89ec773985969bbf"


def _capture_v1():
    return capture_world_episodes(
        active_symbols=("AAA",),
        tradable_symbols=("AAA",),
        bars_by_symbol={
            "AAA": [
                {
                    "ts": "2026-08-22T09:00:00+00:00",
                    "open": 100.0,
                    "high": 104.0,
                    "low": 99.0,
                    "close": 101.0,
                    "volume": 1000.0,
                    "available_at": "2026-08-22T09:05:00+00:00",
                    "source": "unit-market-bars",
                    "interval": "1h",
                    "timestamp_semantics": "bar_close",
                },
                {
                    "ts": "2026-08-22T10:00:00+00:00",
                    "open": 100.0,
                    "high": 104.0,
                    "low": 99.0,
                    "close": 102.0,
                    "volume": 1000.0,
                    "available_at": "2026-08-22T10:05:00+00:00",
                    "source": "unit-market-bars",
                    "interval": "1h",
                    "timestamp_semantics": "bar_close",
                },
            ]
        },
        market_metadata_by_symbol={
            "AAA": {
                "venue": "XTAI",
                "asset_family": "equity",
                "session_phase": "regular",
                "market_regime": "trending_up",
                "family_regime": "risk_on",
                "freshness": {"status": "fresh", "data_age_minutes": 5.0},
                "available_at": "2026-08-22T10:05:00+00:00",
            }
        },
        source="unit-market-bars",
        interval="1h",
        timestamp_semantics="bar_close",
        captured_at="2026-08-22T10:30:00+00:00",
    )[0]


def _edge(**overrides: object) -> TopologyEdge:
    values: dict[str, object] = {
        "kind": "TRADED_ON",
        "source": EntityRef("instrument", "AAA"),
        "target": EntityRef("venue", "XTAI"),
        "effective_from": CUTOFF,
        "ready_at": CUTOFF,
        "ontology_revision": "semantic_catalog.v1",
        "source_refs": ("semantic_catalog",),
    }
    values.update(overrides)
    return TopologyEdge(**values)  # type: ignore[arg-type]


def _snapshot(**overrides: object) -> WorldContextSnapshot:
    edges = bootstrap_instrument_topology(symbol="AAA", venue="XTAI", family="equity", cutoff_at=CUTOFF)
    values: dict[str, object] = {
        "instrument": EntityRef("instrument", "AAA"),
        "cutoff_at": CUTOFF,
        "ontology_revision": "semantic_catalog.v1",
        "topology_edges": edges,
        "status": "missing",
        "categorical_features": {"context_status": "missing", "macro_status": "missing"},
    }
    values.update(overrides)
    return WorldContextSnapshot(**values)  # type: ignore[arg-type]


def test_topology_edge_canonical_hash_is_stable_and_rejects_causes() -> None:
    first = _edge()
    second = _edge(effective_from="2026-08-22T10:05:00+00:00", ready_at="2026-08-22T10:05:00Z")
    assert first.content_sha256 == second.content_sha256
    assert first.to_dict() == second.to_dict()
    with pytest.raises(ValueError, match="CAUSES"):
        _edge(kind="CAUSES")


def test_context_schema_denylist_rejects_decision_policy_and_prompt_fields() -> None:
    for payload in (
        {"decision": "BUY"},
        {"candidate_rank": 1},
        {"hotlist": ["AAA"]},
        {"prompt": "why"},
        {"llm_score": 0.9},
        {"portfolio_pnl": 1.0},
        {"nested": {"risk_gate": "block"}},
    ):
        with pytest.raises(ValueError, match="forbidden"):
            assert_context_schema(payload)


def test_ready_at_after_cutoff_is_not_temporally_eligible() -> None:
    cutoff = datetime(2026, 8, 22, 10, 5, tzinfo=UTC)
    assert temporally_eligible(cutoff_at=cutoff, ready_at=datetime(2026, 8, 22, 10, 5, tzinfo=UTC))
    assert not temporally_eligible(cutoff_at=cutoff, ready_at=datetime(2026, 8, 22, 10, 6, tzinfo=UTC))
    assert not temporally_eligible(cutoff_at=cutoff, ready_at=None)
    assert not temporally_eligible(
        cutoff_at=cutoff,
        ready_at=datetime(2026, 8, 22, 9, 0, tzinfo=UTC),
        valid_until=datetime(2026, 8, 22, 10, 0, tzinfo=UTC),
    )


def test_knowledge_artifact_requires_ready_at_proof_for_eligibility() -> None:
    artifact = KnowledgeArtifact(
        kind="news_macro",
        artifact_id="macro-1",
        subjects=[EntityRef("venue", "XTAI")],
        schema_version="news_macro_brief.v1",
        content_sha256="abc",
        ready_at="2026-08-22T10:00:00+00:00",
        valid_until="2026-08-23T10:00:00+00:00",
    )
    assert artifact.eligible_at(CUTOFF)
    late = KnowledgeArtifact(
        kind="news_macro",
        artifact_id="macro-2",
        subjects=[EntityRef("venue", "XTAI")],
        schema_version="news_macro_brief.v1",
        content_sha256="def",
        ready_at="2026-08-22T11:00:00+00:00",
    )
    assert not late.eligible_at(CUTOFF)


def test_snapshot_is_non_causal_and_hashes_deterministically() -> None:
    first = _snapshot()
    second = _snapshot()
    assert first.context_id == second.context_id
    assert first.causal_status == "non_causal_association"
    assert "caus" in first.causal_status
    with pytest.raises(ValueError, match="causal"):
        _snapshot(causal_status="causes")
    with pytest.raises(ValueError, match="forbidden"):
        _snapshot(categorical_features={"prompt": "nope"})


def test_bootstrap_topology_does_not_invent_unverified_issuer_joins() -> None:
    unverified = bootstrap_instrument_topology(
        symbol="AAA",
        venue="XTAI",
        family="equity",
        cutoff_at=CUTOFF,
        issuer_id="Issuer SA",
        issuer_verified=False,
    )
    assert {edge.kind for edge in unverified} == {"TRADED_ON", "PART_OF_WORLD", "MEMBER_OF_FAMILY"}
    verified = bootstrap_instrument_topology(
        symbol="AAA",
        venue="XTAI",
        cutoff_at=CUTOFF,
        issuer_id="Issuer SA",
        issuer_verified=True,
    )
    assert any(edge.kind == "ISSUED_BY" for edge in verified)


def test_market_observation_omits_context_and_keeps_frozen_identity() -> None:
    episode = _capture_v1()
    payload = episode.to_dict()
    assert "context" not in payload["observation"]
    assert episode.observation.context is None
    assert episode.episode_id == V1_EPISODE_ID
    assert episode.payload_hash == V1_PAYLOAD_HASH
    assert episode.episode_id == world_episode_id(
        venue="XTAI",
        symbol="AAA",
        bar_interval="1h",
        as_of_bar_ts="2026-08-22T10:00:00+00:00",
        feature_contract_version="world_feature.market.v2",
        sampling_policy_version="active_tradable_completed_bar.v1",
    )
    replayed = WorldObservation.from_dict(episode.observation.to_dict())
    assert replayed.to_dict() == episode.observation.to_dict()
    assert "context" not in canonical_json(replayed.to_dict())


def test_context_observation_identity_includes_context_digest_and_keeps_market_slot() -> None:
    v1 = _capture_v1()
    snapshot = _snapshot(
        status="missing",
        categorical_features={"context_status": "missing", "macro_status": "missing"},
        feature_contract_version=CONTEXT_FEATURE_CONTRACT_ID,
    )
    v2 = WorldObservation(
        venue=v1.observation.venue,
        symbol=v1.observation.symbol,
        bar_interval=v1.observation.bar_interval,
        as_of_bar_ts=v1.observation.as_of_bar_ts,
        feature_contract_version=CONTEXT_FEATURE_CONTRACT_ID,
        sampling_policy_version=v1.observation.sampling_policy_version,
        anchor=v1.observation.anchor,
        available_at=v1.observation.available_at,
        captured_at=v1.observation.captured_at,
        freshness=v1.observation.freshness,
        categorical_features=dict(v1.observation.categorical_features),
        numeric_features=dict(v1.observation.numeric_features),
        context=snapshot,
    )
    assert v2.episode_id != v1.observation.episode_id
    assert v2.episode_id == world_episode_id(
        venue="XTAI",
        symbol="AAA",
        bar_interval="1h",
        as_of_bar_ts=v1.observation.as_of_bar_ts,
        feature_contract_version=CONTEXT_FEATURE_CONTRACT_ID,
        sampling_policy_version=v1.observation.sampling_policy_version,
        context_snapshot_id=snapshot.context_id,
    )
    assert "context" in v2.to_dict()
    frozen = freeze_context_mapping(snapshot)
    assert frozen["context_id"] == snapshot.context_id
    with pytest.raises(TypeError):
        frozen["status"] = "complete"  # type: ignore[index]


def test_freeze_context_mapping_is_deeply_immutable_and_to_dict_is_thawed() -> None:
    snapshot = _snapshot(
        artifact_proofs=(
            {
                "kind": "news_macro",
                "status": "missing",
                "proven": False,
                "reason": "no_proven_artifact_at_cutoff",
            },
        ),
        sensor_statuses={"news_macro": "missing"},
        categorical_features={"context_status": "missing", "macro_status": "missing"},
    )
    frozen = freeze_context_mapping(snapshot)
    with pytest.raises(TypeError):
        frozen["status"] = "complete"  # type: ignore[index]
    with pytest.raises(TypeError):
        frozen["categorical_features"]["context_status"] = "complete"  # type: ignore[index]
    thawed = snapshot.to_dict()
    json.dumps(thawed)
    thawed["status"] = "complete"
    thawed["artifact_proofs"][0]["status"] = "complete"
    thawed["artifact_proofs"][0]["reason"] = "mutated"
    assert snapshot.status == "missing"
    assert snapshot.artifact_proofs[0]["status"] == "missing"
    replayed = WorldContextSnapshot.from_mapping(snapshot.replay_payload())
    assert replayed.context_id == snapshot.context_id


def test_forged_context_id_and_content_hash_are_rejected() -> None:
    snapshot = _snapshot()
    payload = snapshot.to_dict()
    payload["context_id"] = "world-context:v1:" + "0" * 64
    with pytest.raises(ValueError, match="context_id"):
        WorldContextSnapshot.from_mapping(payload)
    payload = snapshot.to_dict()
    payload["content_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="content_sha256"):
        WorldContextSnapshot.from_mapping(payload)


def test_snapshot_rejects_late_and_unproven_sensor_states() -> None:
    with pytest.raises(ValueError, match="late/unproven"):
        _snapshot(
            status="late",
            categorical_features={"context_status": "late", "macro_status": "late"},
            sensor_statuses={"news_macro": "late"},
        )
    with pytest.raises(ValueError, match="late/unproven"):
        _snapshot(
            status="availability_unproven",
            categorical_features={
                "context_status": "availability_unproven",
                "macro_status": "availability_unproven",
            },
            sensor_statuses={"news_macro": "availability_unproven"},
        )


def test_missing_proofs_are_neutral_or_policy_contaminated() -> None:
    with pytest.raises(ValueError, match="neutral missing"):
        _snapshot(
            artifact_proofs=(
                {
                    "kind": "news_macro",
                    "status": "missing",
                    "proven": True,
                    "reason": NO_PROVEN_ARTIFACT_REASON,
                    "artifact_id": "ghost",
                    "content_sha256": "abc",
                    "ready_at": CUTOFF.isoformat(),
                },
            ),
            sensor_statuses={"news_macro": "missing"},
        )
    with pytest.raises(ValueError, match="neutral missing"):
        _snapshot(
            artifact_proofs=(
                {
                    "kind": "news_macro",
                    "status": "missing",
                    "proven": False,
                    "reason": NO_PROVEN_ARTIFACT_REASON,
                    "artifact_id": "ghost",
                },
            ),
            sensor_statuses={"news_macro": "missing"},
        )
    with pytest.raises(ValueError, match="policy_contaminated"):
        _snapshot(
            artifact_proofs=(
                {
                    "kind": "news_macro",
                    "status": "missing",
                    "proven": False,
                    "reason": POLICY_CONTAMINATED_REASON,
                    "artifact_id": "macro-1",
                    "content_sha256": "abc",
                    "ready_at": CUTOFF.isoformat(),
                },
            ),
            sensor_statuses={"news_macro": "missing"},
        )
    with pytest.raises(ValueError, match="id, hash, and ready_at"):
        _snapshot(
            artifact_proofs=(
                {
                    "kind": "news_macro",
                    "status": "missing",
                    "proven": True,
                    "reason": POLICY_CONTAMINATED_REASON,
                    "artifact_id": "macro-1",
                },
            ),
            sensor_statuses={"news_macro": "missing"},
        )
    late = CUTOFF + timedelta(minutes=5)
    with pytest.raises(ValueError, match="ready_at"):
        _snapshot(
            artifact_proofs=(
                {
                    "kind": "news_macro",
                    "status": "missing",
                    "proven": True,
                    "reason": POLICY_CONTAMINATED_REASON,
                    "artifact_id": "macro-1",
                    "content_sha256": "abc",
                    "ready_at": late.isoformat(),
                },
            ),
            sensor_statuses={"news_macro": "missing"},
        )
    with pytest.raises(ValueError, match="artifact_refs"):
        _snapshot(
            artifact_refs=("macro-1",),
            artifact_proofs=(
                {
                    "kind": "news_macro",
                    "status": "missing",
                    "proven": True,
                    "reason": POLICY_CONTAMINATED_REASON,
                    "artifact_id": "macro-1",
                    "content_sha256": "abc",
                    "ready_at": CUTOFF.isoformat(),
                },
            ),
            sensor_statuses={"news_macro": "missing"},
        )
    with pytest.raises(ValueError, match="no_proven_artifact_at_cutoff or policy_contaminated"):
        _snapshot(
            artifact_proofs=(
                {
                    "kind": "news_macro",
                    "status": "missing",
                    "proven": False,
                    "reason": "other_reason",
                },
            ),
            sensor_statuses={"news_macro": "missing"},
        )

    contaminated = _snapshot(
        artifact_proofs=(
            {
                "kind": "news_macro",
                "status": "missing",
                "proven": True,
                "reason": POLICY_CONTAMINATED_REASON,
                "artifact_id": "macro-1",
                "content_sha256": "abc",
                "ready_at": CUTOFF.isoformat(),
            },
        ),
        sensor_statuses={"news_macro": "missing"},
        categorical_features={"context_status": "missing", "macro_status": "missing"},
    )
    assert contaminated.status == "missing"
    assert contaminated.artifact_refs == ()
    replayed = WorldContextSnapshot.from_mapping(contaminated.replay_payload())
    assert replayed.context_id == contaminated.context_id
    assert replayed.artifact_proofs[0]["reason"] == POLICY_CONTAMINATED_REASON


def test_post_cutoff_edge_and_complete_artifact_are_rejected() -> None:
    late = CUTOFF + timedelta(minutes=5)
    with pytest.raises(ValueError, match="ready"):
        _snapshot(topology_edges=(_edge(ready_at=late),))
    with pytest.raises(ValueError, match="ready_at"):
        _snapshot(
            status="complete",
            sensor_statuses={"news_macro": "complete"},
            categorical_features={"context_status": "complete", "macro_status": "complete"},
            artifact_proofs=(
                {
                    "kind": "news_macro",
                    "status": "complete",
                    "proven": True,
                    "artifact_id": "macro-1",
                    "content_sha256": "abc",
                    "ready_at": late.isoformat(),
                },
            ),
            artifact_refs=("macro-1",),
        )


def test_disconnected_path_and_mismatched_instrument_are_rejected() -> None:
    other = TopologyEdge(
        kind="PART_OF_WORLD",
        source={"kind": "family", "entity_id": "other-family"},
        target={"kind": "world", "entity_id": "market"},
        effective_from=CUTOFF,
        ready_at=CUTOFF,
        ontology_revision="semantic_catalog.v1",
        source_refs=("semantic_catalog",),
    )
    edges = bootstrap_instrument_topology(symbol="AAA", venue="XTAI", family="equity", cutoff_at=CUTOFF)
    with pytest.raises(ValueError, match="connected"):
        _snapshot(topology_edges=edges + (other,), topology_path=(other,))
    v1 = _capture_v1()
    snapshot = _snapshot(
        instrument={"kind": "instrument", "entity_id": "BBB"},
        topology_edges=bootstrap_instrument_topology(symbol="BBB", venue="XTAI", cutoff_at=CUTOFF),
    )
    with pytest.raises(ValueError, match="instrument"):
        WorldObservation(
            venue=v1.observation.venue,
            symbol=v1.observation.symbol,
            bar_interval=v1.observation.bar_interval,
            as_of_bar_ts=v1.observation.as_of_bar_ts,
            feature_contract_version=CONTEXT_FEATURE_CONTRACT_ID,
            sampling_policy_version=v1.observation.sampling_policy_version,
            anchor=v1.observation.anchor,
            available_at=v1.observation.available_at,
            captured_at=v1.observation.captured_at,
            freshness=v1.observation.freshness,
            categorical_features=dict(v1.observation.categorical_features),
            numeric_features=dict(v1.observation.numeric_features),
            context=snapshot,
        )


def test_verified_issuer_entity_id_never_uses_display_name() -> None:
    assert verified_issuer_entity_id({"identity_status": "verified", "issuer_name": "Homonym SA"}) is None
    assert (
        verified_issuer_entity_id(
            {
                "identity_status": "verified",
                "issuer_name": "Homonym SA",
                "external_ids": {"isin": "TW0002330008", "lei": "5493001KJTIIGC8Y1R12"},
            }
        )
        == "lei:5493001KJTIIGC8Y1R12"
    )
    assert (
        verified_issuer_entity_id({"identity_status": "verified", "external_ids": {"ticker": "AAA", "perm_id": "1"}})
        == "perm_id:1"
    )
    assert verified_issuer_entity_id(None) is None
    assert verified_issuer_entity_id(object()) is None  # type: ignore[arg-type]
    colon_value = verified_issuer_entity_id({"identity_status": "verified", "external_ids": {"vendor": "scheme:123"}})
    colon_key = verified_issuer_entity_id({"identity_status": "verified", "external_ids": {"vendor:scheme": "123"}})
    assert colon_value != colon_key
    assert colon_value == "vendor:scheme%3A123"
    assert colon_key == "vendor%3Ascheme:123"
    assert (
        verified_issuer_entity_id({"identity_status": "verified", "external_ids": {"LEI": "abc", "lei": "xyz"}}) is None
    )
    assert (
        verified_issuer_entity_id({"identity_status": "verified", "external_ids": {"LEI": "abc", "lei": "abc"}})
        == "lei:abc"
    )
    assert verified_issuer_entity_id({"identity_status": "verified", "external_ids": {1: "abc", "lei": 123}}) is None
    unverified = bootstrap_instrument_topology(
        symbol="AAA",
        venue="XTAI",
        cutoff_at=CUTOFF,
        issuer_id="Homonym SA",
        issuer_verified=True,
    )
    assert any(edge.kind == "ISSUED_BY" and edge.target.entity_id == "Homonym SA" for edge in unverified)
    from trader.application.world_model.context_capture import build_world_context_snapshot
    from trader.domain.world_context import SensorEvidence

    snapshot = build_world_context_snapshot(
        symbol="AAA",
        venue="XTAI",
        cutoff_at=CUTOFF,
        family="equity",
        macro=SensorEvidence(status="missing", reason="no_artifact"),
        company=SensorEvidence(
            status="missing",
            reason="no_artifact",
            payload={
                "issuer_identity": {
                    "issuer_name": "Homonym SA",
                    "identity_status": "verified",
                    "external_ids": {},
                }
            },
        ),
    )
    assert all(edge.kind != "ISSUED_BY" for edge in snapshot.topology_edges)


def test_topology_path_omits_unrelated_part_of_world_hops() -> None:
    edges = bootstrap_instrument_topology(symbol="AAA", venue="XTAI", family="equity", cutoff_at=CUTOFF)
    stranger = TopologyEdge(
        kind="PART_OF_WORLD",
        source={"kind": "family", "entity_id": "stranger"},
        target={"kind": "world", "entity_id": "market"},
        effective_from=CUTOFF,
        ready_at=CUTOFF,
        ontology_revision="semantic_catalog.v1",
        source_refs=("semantic_catalog",),
    )
    path = topology_path_for_instrument(edges + (stranger,), EntityRef("instrument", "AAA"))
    assert all(edge.source.entity_id != "stranger" for edge in path)
    assert any(edge.source.kind == "family" and edge.source.entity_id == "equity" for edge in path)


def test_domain_world_context_is_stdlib_only() -> None:
    from pathlib import Path

    source = Path(__file__).resolve().parents[2] / "trader" / "domain" / "world_context.py"
    text = source.read_text(encoding="utf-8")
    assert "import networkx" not in text
    assert "from networkx" not in text


def _complete_or_partial_proof(status: str, **overrides: object) -> dict[str, object]:
    proof: dict[str, object] = {
        "kind": "news_macro",
        "status": status,
        "proven": True,
        "artifact_id": "macro-1",
        "content_sha256": "abc",
        "ready_at": CUTOFF.isoformat(),
    }
    proof.update(overrides)
    return proof


def _policy_contaminated_proof(**overrides: object) -> dict[str, object]:
    proof: dict[str, object] = {
        "kind": "news_macro",
        "status": "missing",
        "proven": True,
        "reason": POLICY_CONTAMINATED_REASON,
        "artifact_id": "macro-1",
        "content_sha256": "abc",
        "ready_at": CUTOFF.isoformat(),
    }
    proof.update(overrides)
    return proof


def test_complete_and_partial_proofs_reject_malformed_expired_or_equal_valid_until() -> None:
    expired = (CUTOFF - timedelta(minutes=1)).isoformat()
    equal = CUTOFF.isoformat()
    future = (CUTOFF + timedelta(hours=1)).isoformat()
    for status in ("complete", "partial"):
        for valid_until in (expired, equal, "not-a-timestamp"):
            with pytest.raises(ValueError, match="valid_until"):
                _snapshot(
                    status=status if status == "complete" else "partial",
                    sensor_statuses={"news_macro": status},
                    categorical_features={"context_status": status, "macro_status": status},
                    artifact_proofs=(_complete_or_partial_proof(status, valid_until=valid_until),),
                    artifact_refs=("macro-1",),
                )
        accepted = _snapshot(
            status=status if status == "complete" else "partial",
            sensor_statuses={"news_macro": status},
            categorical_features={"context_status": status, "macro_status": status},
            artifact_proofs=(_complete_or_partial_proof(status, valid_until=future),),
            artifact_refs=("macro-1",),
        )
        assert accepted.artifact_refs == ("macro-1",)


def test_policy_contaminated_proofs_reject_malformed_expired_or_equal_valid_until() -> None:
    expired = (CUTOFF - timedelta(minutes=1)).isoformat()
    equal = CUTOFF.isoformat()
    future = (CUTOFF + timedelta(hours=1)).isoformat()
    for valid_until in (expired, equal, "not-a-timestamp"):
        with pytest.raises(ValueError, match="valid_until"):
            _snapshot(
                artifact_proofs=(_policy_contaminated_proof(valid_until=valid_until),),
                sensor_statuses={"news_macro": "missing"},
            )
    contaminated = _snapshot(
        artifact_proofs=(_policy_contaminated_proof(valid_until=future),),
        sensor_statuses={"news_macro": "missing"},
        categorical_features={"context_status": "missing", "macro_status": "missing"},
    )
    assert contaminated.artifact_refs == ()
    assert contaminated.artifact_proofs[0]["valid_until"] == future


def test_neutral_missing_proof_rejects_extra_artifact_metadata_keys() -> None:
    for extra in ({"artifact_ref": "ghost"}, {"source_refs": ("news_macro",)}, {"note": "salt"}):
        proof = {
            "kind": "news_macro",
            "status": "missing",
            "proven": False,
            "reason": NO_PROVEN_ARTIFACT_REASON,
            **extra,
        }
        with pytest.raises(ValueError, match="neutral missing"):
            _snapshot(
                artifact_proofs=(proof,),
                sensor_statuses={"news_macro": "missing"},
            )


def test_model_facing_collapses_expired_malformed_and_unproven_to_neutral_missing() -> None:
    ready = datetime(2026, 8, 22, 10, 0, tzinfo=UTC)
    future = datetime(2026, 8, 23, 10, 0, tzinfo=UTC)
    expired = datetime(2026, 8, 22, 10, 0, tzinfo=UTC)
    complete = KnowledgeArtifact(
        kind="news_macro",
        artifact_id="macro-1",
        subjects=[EntityRef("venue", "XTAI")],
        schema_version="news_macro_brief.v1",
        content_sha256="abc",
        ready_at=ready,
        valid_until=future,
    )
    kept = model_facing_sensor_evidence(
        SensorEvidence(status="complete", proven=True, artifact=complete),
        CUTOFF,
    )
    assert kept.status == "complete"
    expired_complete = KnowledgeArtifact(
        kind="news_macro",
        artifact_id="macro-2",
        subjects=[EntityRef("venue", "XTAI")],
        schema_version="news_macro_brief.v1",
        content_sha256="def",
        ready_at=ready,
        valid_until=expired,
    )
    collapsed = model_facing_sensor_evidence(
        SensorEvidence(status="complete", proven=True, artifact=expired_complete),
        CUTOFF,
    )
    assert collapsed.status == "missing"
    assert collapsed.reason == NO_PROVEN_ARTIFACT_REASON
    assert collapsed.proven is False
    assert collapsed.artifact is None
    equal_partial = KnowledgeArtifact(
        kind="news_macro",
        artifact_id="macro-3",
        subjects=[EntityRef("venue", "XTAI")],
        schema_version="news_macro_brief.v1",
        content_sha256="ghi",
        ready_at=ready,
        valid_until=CUTOFF,
    )
    collapsed_partial = model_facing_sensor_evidence(
        SensorEvidence(status="partial", proven=True, artifact=equal_partial),
        CUTOFF,
    )
    assert collapsed_partial.reason == NO_PROVEN_ARTIFACT_REASON
    contaminated = KnowledgeArtifact(
        kind="news_macro",
        artifact_id="macro-4",
        subjects=[EntityRef("venue", "XTAI")],
        schema_version="news_macro_brief.v1",
        content_sha256="jkl",
        ready_at=ready,
        valid_until=expired,
    )
    collapsed_policy = model_facing_sensor_evidence(
        SensorEvidence(
            status="missing",
            reason=POLICY_CONTAMINATED_REASON,
            proven=True,
            artifact=contaminated,
        ),
        CUTOFF,
    )
    assert collapsed_policy.status == "missing"
    assert collapsed_policy.reason == NO_PROVEN_ARTIFACT_REASON
    assert collapsed_policy.proven is False
    kept_policy = model_facing_sensor_evidence(
        SensorEvidence(
            status="missing",
            reason=POLICY_CONTAMINATED_REASON,
            proven=True,
            artifact=complete,
        ),
        CUTOFF,
    )
    assert kept_policy.reason == POLICY_CONTAMINATED_REASON
    assert kept_policy.proven is True

    class _MalformedArtifact:
        ready_at = ready
        valid_until = "not-a-timestamp"

    malformed = model_facing_sensor_evidence(
        SensorEvidence(
            status="missing",
            reason=POLICY_CONTAMINATED_REASON,
            proven=True,
            artifact=_MalformedArtifact(),  # type: ignore[arg-type]
        ),
        CUTOFF,
    )
    assert malformed.reason == NO_PROVEN_ARTIFACT_REASON
    assert malformed.artifact is None


def _neutral_missing_proof(kind: str = "news_macro") -> dict[str, object]:
    return {
        "kind": kind,
        "status": "missing",
        "proven": False,
        "reason": NO_PROVEN_ARTIFACT_REASON,
    }


def test_snapshot_rejects_artifact_proof_missing_kind() -> None:
    omitted = {
        "status": "missing",
        "proven": False,
        "reason": NO_PROVEN_ARTIFACT_REASON,
    }
    with pytest.raises(ValueError, match="non-empty kind"):
        _snapshot(
            artifact_proofs=(omitted,),
            sensor_statuses={"news_macro": "missing"},
        )
    with pytest.raises(ValueError, match="non-empty kind"):
        _snapshot(
            artifact_proofs=({**omitted, "kind": ""},),
            sensor_statuses={"news_macro": "missing"},
        )


def test_snapshot_rejects_ghost_artifact_proof_kind() -> None:
    with pytest.raises(ValueError, match="sensor_statuses"):
        _snapshot(
            artifact_proofs=(
                _neutral_missing_proof("news_macro"),
                _neutral_missing_proof("ghost_sensor"),
            ),
            sensor_statuses={"news_macro": "missing"},
        )


def test_snapshot_rejects_duplicate_artifact_proof_kind() -> None:
    proof = _neutral_missing_proof()
    with pytest.raises(ValueError, match="duplicate"):
        _snapshot(
            artifact_proofs=(proof, dict(proof)),
            sensor_statuses={"news_macro": "missing"},
        )
    first = _complete_or_partial_proof("complete", artifact_id="macro-1")
    second = _complete_or_partial_proof("complete", artifact_id="macro-2")
    with pytest.raises(ValueError, match="duplicate"):
        _snapshot(
            status="complete",
            sensor_statuses={"news_macro": "complete"},
            categorical_features={"context_status": "complete", "macro_status": "complete"},
            artifact_proofs=(first, second),
            artifact_refs=("macro-1", "macro-2"),
        )


def test_snapshot_rejects_declared_sensor_without_proof() -> None:
    with pytest.raises(ValueError, match="sensor_statuses"):
        _snapshot(
            sensor_statuses={"news_macro": "missing"},
            categorical_features={"context_status": "missing", "macro_status": "missing"},
        )
    with pytest.raises(ValueError, match="sensor_statuses"):
        _snapshot(
            artifact_proofs=(_neutral_missing_proof("news_macro"),),
            sensor_statuses={"news_macro": "missing", "company_intelligence": "missing"},
            categorical_features={"context_status": "missing", "macro_status": "missing"},
        )


def _scope_resolution_fields(
    *,
    status: str = "unmapped",
    mapping_id: str = "world_scope_mapping.v1",
    mapping_sha256: str = "b" * 64,
) -> dict[str, object]:
    return {
        "mapping_id": mapping_id,
        "mapping_sha256": mapping_sha256,
        "resolution_status": status,
        "anchor": {"market_venue": "GM", "instrument": "BMW.DE"},
    }


def test_macro_world_observation_is_an_allowed_artifact_kind() -> None:
    assert "macro_world_observation" in ARTIFACT_KINDS
    artifact = KnowledgeArtifact(
        kind="macro_world_observation",
        artifact_id="macro_world_observation:v1:" + "a" * 64,
        subjects=[EntityRef("venue", "mic:XTAI")],
        schema_version="macro_world_observation.v1",
        content_sha256="abc",
        ready_at="2026-08-22T10:00:00+00:00",
        valid_until="2026-08-23T10:00:00+00:00",
    )
    assert artifact.eligible_at(CUTOFF)
    assert artifact.kind == "macro_world_observation"


def test_unmapped_and_ambiguous_missing_proofs_persist_mapping_identity() -> None:
    for reason, status in (
        (SCOPE_UNMAPPED_REASON, "unmapped"),
        (SCOPE_AMBIGUOUS_REASON, "ambiguous"),
    ):
        snapshot = _snapshot(
            artifact_proofs=(
                {
                    "kind": "macro_world_observation",
                    "status": "missing",
                    "proven": False,
                    "reason": reason,
                    **_scope_resolution_fields(status=status),
                },
            ),
            sensor_statuses={"macro_world_observation": "missing"},
            categorical_features={"context_status": "missing", "macro_status": "missing"},
        )
        proof = snapshot.artifact_proofs[0]
        assert proof["reason"] == reason
        assert proof["mapping_id"] == "world_scope_mapping.v1"
        assert proof["mapping_sha256"] == "b" * 64
        assert proof["resolution_status"] == status
        assert snapshot.artifact_refs == ()


def test_neutral_missing_may_carry_scope_resolution_without_artifact_metadata() -> None:
    snapshot = _snapshot(
        artifact_proofs=(
            {
                "kind": "macro_world_observation",
                "status": "missing",
                "proven": False,
                "reason": NO_PROVEN_ARTIFACT_REASON,
                **_scope_resolution_fields(status="resolved"),
            },
        ),
        sensor_statuses={"macro_world_observation": "missing"},
        categorical_features={"context_status": "missing", "macro_status": "missing"},
    )
    proof = snapshot.artifact_proofs[0]
    assert proof["mapping_id"] == "world_scope_mapping.v1"
    assert proof["resolution_status"] == "resolved"
    assert proof.get("artifact_id") in (None, "")
    with pytest.raises(ValueError, match="neutral missing"):
        _snapshot(
            artifact_proofs=(
                {
                    "kind": "macro_world_observation",
                    "status": "missing",
                    "proven": False,
                    "reason": NO_PROVEN_ARTIFACT_REASON,
                    "artifact_id": "ghost",
                    **_scope_resolution_fields(status="resolved"),
                },
            ),
            sensor_statuses={"macro_world_observation": "missing"},
        )


def test_model_facing_preserves_unmapped_scope_resolution() -> None:
    evidence = SensorEvidence(
        status="missing",
        reason=SCOPE_UNMAPPED_REASON,
        proven=False,
        payload={"scope_resolution": _scope_resolution_fields()},
    )
    kept = model_facing_sensor_evidence(evidence, CUTOFF)
    assert kept.reason == SCOPE_UNMAPPED_REASON
    assert kept.payload is not None
    assert kept.payload["scope_resolution"]["mapping_id"] == "world_scope_mapping.v1"
    collapsed = model_facing_sensor_evidence(
        SensorEvidence(status="missing", reason="other"),
        CUTOFF,
    )
    assert collapsed.reason == NO_PROVEN_ARTIFACT_REASON
    assert collapsed.payload is None
