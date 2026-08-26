from __future__ import annotations

import ast
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pytest

from tests.package_layout._helpers import REPO_ROOT
from trader.application.world_model.pattern_discovery import PatternDiscoveryService
from trader.application.world_model.pattern_discovery_ports import (
    PatternDriverStateBinding,
    PatternFormationBatch,
    PatternFormationRecord,
    TimeSpecificMarketAnchor,
)
from trader.domain.world_driver import DriverRegimeBundle, DriverState
from trader.application.world_model.pattern_formation_request import PatternFormationRequest
from trader.domain.world_episode import (
    GRAPH_FEATURE_CONTRACT_ID,
    MARKET_FEATURE_CONTRACT_ID,
    AnchorBar,
    WorldEpisode,
    WorldObservation,
    WorldOutcome,
    canonical_sha256,
)
from trader.domain.world_feature_contract import (
    GRAPH_CONTENT_MASK_ID,
    GRAPH_PATH_RULE_VERSION,
    graph_content_mask,
    graph_feature_contract,
)
from trader.domain.world_graph import (
    KnowledgeArtifactRef,
    KnowledgeWorldRelation,
    StructuralWorldRelation,
    WorldEntityRef,
    WorldGraphSnapshot,
    WorldKnowledgeRelationRef,
    WorldObservationRef,
    WorldStructuralRelationRef,
)
from trader.domain.world_macro import MACRO_PRODUCER_VERSION, MACRO_TRANSFORM_VERSION
from trader.domain.world_pattern import (
    EXPLICIT_GRAPH_PATTERN_MODEL_IDENTITY,
    PATTERN_ASSOCIATION_METRIC,
    laplace_smoothed_distribution,
    total_variation_distance,
)


UTC = timezone.utc
SHA = "a" * 64
RECEIPT = f"world-availability-receipt:v1:{'1' * 64}"
FORMATION = datetime(2026, 9, 1, tzinfo=UTC)
EVAL_NOT_BEFORE = datetime(2026, 9, 2, tzinfo=UTC)
T0 = datetime(2026, 1, 1, tzinfo=UTC)

_DISCOVERY = REPO_ROOT / "trader" / "application" / "world_model" / "pattern_discovery.py"
_PORTS = REPO_ROOT / "trader" / "application" / "world_model" / "pattern_discovery_ports.py"
_REQUEST = REPO_ROOT / "trader" / "application" / "world_model" / "pattern_formation_request.py"


def _instrument(symbol: str = "2330") -> WorldEntityRef:
    return WorldEntityRef(kind="instrument", entity_id=f"mic:XTAI:symbol:{symbol}")


def _venue() -> WorldEntityRef:
    return WorldEntityRef(kind="venue", entity_id="mic:XTAI")


def _country(code: str = "TW") -> WorldEntityRef:
    return WorldEntityRef(kind="country", entity_id=f"iso-3166:{code}")


def _region() -> WorldEntityRef:
    return WorldEntityRef(kind="region", entity_id="iso-un-m49:030")


def _world() -> WorldEntityRef:
    return WorldEntityRef(kind="world", entity_id="market")


def _company() -> WorldEntityRef:
    return WorldEntityRef(kind="company", entity_id="lei:549300ABCDEFGHIJKLMN")


def _family() -> WorldEntityRef:
    return WorldEntityRef(kind="family", entity_id="taxonomy:v1:semiconductors")


def _structural(
    kind: str,
    source: WorldEntityRef,
    target: WorldEntityRef,
    *,
    effective_from: datetime = T0,
) -> StructuralWorldRelation:
    return StructuralWorldRelation(
        kind=kind,
        source=source,
        target=target,
        effective_from=effective_from,
        ontology_revision="market_ontology.v1",
        source_refs=("provider:instrument-master:2330",),
    )


def _ancestry(root: WorldEntityRef) -> tuple[StructuralWorldRelation, ...]:
    return (
        _structural("TRADED_ON", root, _venue()),
        _structural("LOCATED_IN", _venue(), _country()),
        _structural("LOCATED_IN", _country(), _region()),
        _structural("PART_OF_WORLD", _region(), _world()),
        _structural("ISSUED_BY", root, _company()),
        _structural("MEMBER_OF_FAMILY", root, _family()),
    )


def _observes(target: WorldEntityRef, *, digest: str, effective_from: datetime) -> KnowledgeWorldRelation:
    return KnowledgeWorldRelation(
        kind="OBSERVES",
        source=WorldObservationRef(observation_id=f"world_observation:v1:{digest}"),
        target=target,
        effective_from=effective_from,
        ontology_revision="market_ontology.v1",
        source_refs=(f"macro_world_observation:v1:{digest}",),
    )


def _regime_driver(*, rates_regime: str = "rising", coverage_status: str = "partial") -> DriverState:
    return DriverState(
        source_family="macro_observation",
        signal_class="regime_bundle",
        regimes=DriverRegimeBundle(
            macro_regime="unknown",
            rates_regime=rates_regime,
            usd_regime="unknown",
            coverage_status=coverage_status,
        ),
        artifact_kind="macro_world_observation",
        until_bound="bounded",
        producer_version=MACRO_PRODUCER_VERSION,
        transform_version=MACRO_TRANSFORM_VERSION,
        missingness="none",
    )


def _binding(
    relation: KnowledgeWorldRelation,
    driver_state: DriverState,
    *,
    evidence_refs: tuple[str, ...] | None = None,
) -> PatternDriverStateBinding:
    return PatternDriverStateBinding(
        relation_id=relation.relation_id,
        driver_state=driver_state,
        evidence_refs=relation.source_refs if evidence_refs is None else evidence_refs,
    )


def _default_bindings(knowledge: tuple[KnowledgeWorldRelation, ...]) -> tuple[PatternDriverStateBinding, ...]:
    bindings: list[PatternDriverStateBinding] = []
    for relation in knowledge:
        if relation.kind == "OBSERVES":
            bindings.append(_binding(relation, _regime_driver()))
        elif relation.kind == "ABOUT":
            bindings.append(_binding(relation, DriverState.unspecified(artifact_kind="news_macro")))
    return tuple(bindings)


def _about(target: WorldEntityRef, *, digest: str, effective_from: datetime) -> KnowledgeWorldRelation:
    return KnowledgeWorldRelation(
        kind="ABOUT",
        source=KnowledgeArtifactRef(artifact_id=f"knowledge_artifact:v1:{digest}", content_sha256=digest),
        target=target,
        effective_from=effective_from,
        ontology_revision="market_ontology.v1",
        source_refs=(f"artifact:{digest}",),
    )


def _observation(*, symbol: str, as_of: datetime, **overrides: object) -> WorldObservation:
    values: dict[str, object] = {
        "venue": "TW",
        "symbol": symbol,
        "bar_interval": "1h",
        "as_of_bar_ts": as_of,
        "feature_contract_version": MARKET_FEATURE_CONTRACT_ID,
        "sampling_policy_version": "active_tradable_completed_bar.v1",
        "anchor": AnchorBar(
            ts=as_of,
            open=100.0,
            high=101.0,
            low=99.0,
            close=100.5,
            volume=1000.0,
            source="unit-pattern-discovery",
            timestamp_semantics="bar_close",
        ),
        "available_at": as_of,
        "captured_at": as_of,
        "freshness": {"status": "fresh", "data_age_minutes": 5.0},
        "categorical_features": {"venue": "TW", "bar_interval": "1h"},
        "numeric_features": {"return": 0.01},
    }
    values.update(overrides)
    return WorldObservation(**values)  # type: ignore[arg-type]


def _market_episode(*, symbol: str, as_of: datetime) -> WorldEpisode:
    return WorldEpisode(observation=_observation(symbol=symbol, as_of=as_of))


def _graph_episode(*, market: WorldEpisode, snapshot: WorldGraphSnapshot) -> WorldEpisode:
    observation = market.observation
    return WorldEpisode(
        observation=_observation(
            symbol=observation.symbol,
            as_of=observation.as_of_bar_ts,
            feature_contract_version=GRAPH_FEATURE_CONTRACT_ID,
            graph=snapshot,
            graph_features={"snapshot": snapshot.to_dict()},
        )
    )


def _close_for(label: str) -> float:
    if label == "UP":
        return 102.0
    if label == "DOWN":
        return 98.0
    return 100.2


def _outcome(*, episode_id: str, as_of: datetime, label: str, digest: str = SHA) -> WorldOutcome:
    target = as_of + timedelta(days=1)
    return WorldOutcome(
        episode_id=episode_id,
        horizon={"horizon_id": "elapsed_1d.v1", "duration_seconds": 24 * 60 * 60},
        status="observed",
        target_at=target,
        available_at=target + timedelta(minutes=5),
        computed_at=target + timedelta(minutes=5),
        anchor_close=100.0,
        endpoint_close=_close_for(label),
        endpoint_bar_ts=target,
        source="analysis_bars",
        source_raw_sha256=digest,
    )


def _snapshot(
    market: WorldEpisode,
    *,
    structural: tuple[StructuralWorldRelation, ...],
    knowledge: tuple[KnowledgeWorldRelation, ...] = (),
    ontology_revision: str = "market_ontology.v1",
    symbol: str = "2330",
) -> WorldGraphSnapshot:
    return WorldGraphSnapshot(
        root_episode_id=market.episode_id,
        root_entity=_instrument(symbol),
        cutoff_at=market.observation.as_of_bar_ts,
        ontology_revision=ontology_revision,
        ontology_hash=SHA,
        identity_map_hash="b" * 64,
        scope_mapping_id="world_scope_mapping.v1",
        scope_mapping_hash="c" * 64,
        structural_relation_refs=frozenset(WorldStructuralRelationRef.from_relation(item) for item in structural),
        knowledge_relation_refs=frozenset(
            WorldKnowledgeRelationRef.from_relation(item, availability_receipt_id=RECEIPT) for item in knowledge
        ),
        status="complete",
    )


def _record(
    *,
    symbol: str = "2330",
    as_of: datetime,
    label: str = "UP",
    structural: tuple[StructuralWorldRelation, ...] | None = None,
    knowledge: tuple[KnowledgeWorldRelation, ...] = (),
    ontology_revision: str = "market_ontology.v1",
    outcome_digest: str | None = None,
    available_at: datetime | None = None,
    extra_structural: tuple[StructuralWorldRelation, ...] = (),
    driver_state_bindings: tuple[PatternDriverStateBinding, ...] | None = None,
) -> PatternFormationRecord:
    market = _market_episode(symbol=symbol, as_of=as_of)
    root = _instrument(symbol)
    members = structural if structural is not None else _ancestry(root)
    members = members + extra_structural
    snapshot = _snapshot(
        market,
        structural=members,
        knowledge=knowledge,
        ontology_revision=ontology_revision,
        symbol=symbol,
    )
    episode = _graph_episode(market=market, snapshot=snapshot)
    digest = outcome_digest or canonical_sha256({"as_of": as_of.isoformat(), "symbol": symbol, "label": label})
    outcome = _outcome(episode_id=episode.episode_id, as_of=as_of, label=label, digest=digest)
    return PatternFormationRecord(
        episode=episode,
        snapshot=snapshot,
        structural_relations=members,
        knowledge_relations=knowledge,
        outcome=outcome,
        recorded_at=as_of,
        available_at=available_at or outcome.available_at,
        driver_state_bindings=_default_bindings(knowledge) if driver_state_bindings is None else driver_state_bindings,
        market_anchor=TimeSpecificMarketAnchor(
            venue=episode.observation.venue,
            symbol=episode.observation.symbol,
            bar_interval=episode.observation.bar_interval,
            as_of_bar_ts=episode.observation.as_of_bar_ts,
            horizon_id=outcome.horizon.horizon_id,
        ),
    )


@dataclass(frozen=True)
class MemorySource:
    records: tuple[PatternFormationRecord, ...]
    rejection_counts: dict[str, int] | None = None

    def load_formation_batch(self, request: PatternFormationRequest) -> PatternFormationBatch:
        return PatternFormationBatch(
            records=self.records,
            rejection_counts=self.rejection_counts or {},
            source_evidence_ids=tuple(dict.fromkeys(record.episode.episode_id for record in self.records)),
        )


def _request(**overrides: object) -> PatternFormationRequest:
    values: dict[str, object] = {
        "formation_cutoff": FORMATION,
        "evaluation_start_not_before": EVAL_NOT_BEFORE,
        "min_support": 1,
        "min_association": 0.0,
        "max_candidates": 20,
    }
    values.update(overrides)
    return PatternFormationRequest(**values)  # type: ignore[arg-type]


def _discover(records: tuple[PatternFormationRecord, ...], **request_overrides: object):
    return PatternDiscoveryService(MemorySource(records)).discover(_request(**request_overrides))


def test_request_defaults_are_explicit_graph_content_and_strict() -> None:
    request = PatternFormationRequest(formation_cutoff=FORMATION, evaluation_start_not_before=EVAL_NOT_BEFORE)
    contract = graph_feature_contract()
    mask = graph_content_mask()
    assert request.horizons == ("elapsed_4h.v1", "elapsed_1d.v1")
    assert request.min_support == 20
    assert request.min_association == 0.10
    assert request.max_candidates == 20
    assert request.smoothing_alpha == 1.0
    assert request.model_identity == EXPLICIT_GRAPH_PATTERN_MODEL_IDENTITY
    assert request.feature_contract_id == GRAPH_FEATURE_CONTRACT_ID == contract.contract_id
    assert request.feature_mask_id == GRAPH_CONTENT_MASK_ID == mask.mask_id
    assert request.feature_contract_fingerprint == contract.fingerprint
    assert request.feature_mask_fingerprint == mask.fingerprint
    with pytest.raises(ValueError, match="strictly later|formation"):
        PatternFormationRequest(formation_cutoff=FORMATION, evaluation_start_not_before=FORMATION)
    with pytest.raises(ValueError, match="explicit_graph_pattern"):
        PatternFormationRequest(
            formation_cutoff=FORMATION,
            evaluation_start_not_before=EVAL_NOT_BEFORE,
            model_identity="online_gru_world_challenger@graph.v1",
        )


def test_projection_excludes_siblings_and_outsiders_and_includes_visited_overlay() -> None:
    as_of = datetime(2026, 8, 1, tzinfo=UTC)
    sibling = _structural("TRADED_ON", _instrument("2317"), _venue())
    outsider = _observes(
        WorldEntityRef(kind="company", entity_id="lei:OTHERCOMPANY000000001"),
        digest="d" * 64,
        effective_from=as_of - timedelta(hours=1),
    )
    overlay = _observes(_country(), digest="e" * 64, effective_from=as_of - timedelta(hours=1))
    about_root = _about(_instrument(), digest="f" * 64, effective_from=as_of - timedelta(hours=2))
    record = _record(
        as_of=as_of,
        knowledge=(overlay, outsider, about_root),
        extra_structural=(sibling,),
    )
    result = _discover((record,))
    signatures = {candidate.semantic_signature for candidate in result.candidates}
    joined = " ".join(signatures)
    assert "venue:TRADED_ON:reverse:instrument" not in joined
    assert "country:OBSERVES:reverse:world_observation" in joined
    assert "company:OBSERVES:reverse:world_observation" not in joined
    assert "instrument:ABOUT:reverse:knowledge_artifact" in joined
    assert any("TRADED_ON:forward:venue" in item and "LOCATED_IN:forward:country" in item for item in signatures)
    for candidate in result.candidates:
        assert all(step.evidence_rule_version == GRAPH_PATH_RULE_VERSION for step in candidate.spec.steps)
        for step in candidate.spec.steps:
            if step.relation_kind in {"OBSERVES", "ABOUT"}:
                assert step.driver_state is not None
                assert "observation_id" not in step.driver_state.to_dict()
            else:
                assert step.driver_state is None


def test_unique_time_specific_anchors_are_deduped_and_same_symbol_times_are_not_collapsed() -> None:
    t1 = datetime(2026, 8, 1, tzinfo=UTC)
    t2 = datetime(2026, 8, 2, tzinfo=UTC)
    overlay_one = _observes(_country(), digest="1" * 64, effective_from=t1 - timedelta(hours=1))
    overlay_two = _observes(_region(), digest="2" * 64, effective_from=t1 - timedelta(hours=1))
    same_slot = (
        _record(as_of=t1, knowledge=(overlay_one, overlay_two)),
        _record(as_of=t1, knowledge=(overlay_one, overlay_two)),
    )
    same_result = _discover(same_slot)
    overlay_candidates = [
        item
        for item in same_result.candidates
        if "country:OBSERVES:reverse:world_observation" in item.semantic_signature
    ]
    assert overlay_candidates
    assert overlay_candidates[0].spec.stats.support == 1
    distinct = (
        _record(as_of=t1, knowledge=(overlay_one,)),
        _record(as_of=t2, knowledge=(_observes(_country(), digest="3" * 64, effective_from=t2 - timedelta(hours=1)),)),
    )
    distinct_result = _discover(distinct)
    country_overlay = [
        item
        for item in distinct_result.candidates
        if "country:OBSERVES:reverse:world_observation" in item.semantic_signature
    ]
    assert country_overlay[0].spec.stats.support == 2


def test_late_labels_are_rejected_defensively() -> None:
    as_of = datetime(2026, 8, 20, tzinfo=UTC)
    late = _record(as_of=as_of, available_at=as_of)
    assert late.outcome.available_at is not None
    late_cutoff = late.outcome.available_at - timedelta(minutes=1)
    result = _discover(
        (late,),
        formation_cutoff=late_cutoff,
        evaluation_start_not_before=late_cutoff + timedelta(days=1),
    )
    assert result.eligible_records == 0
    assert result.rejection_counts.get("late_label", 0) == 1
    assert result.candidates == ()


def test_laplace_and_total_variation_are_exact() -> None:
    overlay_times = [datetime(2026, 8, day, tzinfo=UTC) for day in range(1, 5)]
    pop_times = [datetime(2026, 7, day, tzinfo=UTC) for day in range(1, 21)]
    overlay_records = tuple(
        _record(
            as_of=when,
            label="UP" if index < 3 else "FLAT",
                knowledge=(_observes(_country(), digest=f"{index:064x}", effective_from=when - timedelta(hours=1)),),
        )
        for index, when in enumerate(overlay_times)
    )
    population_only = tuple(
        _record(
            as_of=when,
            label=("DOWN", "FLAT", "UP")[index % 3],
            structural=_ancestry(_instrument())[:1],
        )
        for index, when in enumerate(pop_times)
    )
    result = _discover(overlay_records + population_only)
    overlay = next(
        item for item in result.candidates if "country:OBSERVES:reverse:world_observation" in item.semantic_signature
    )
    class_counts = {"DOWN": 0, "FLAT": 1, "UP": 3}
    population_counts = {"DOWN": 7, "FLAT": 8, "UP": 9}
    # 4 overlay + 20 structural-only = 24 unique anchors; labels: overlay 3 UP 1 FLAT
    # population_only: 20 records cycling DOWN/FLAT/UP → 7 DOWN, 7 FLAT, 6 UP plus overlay 3 UP 1 FLAT
    population_counts = {"DOWN": 7, "FLAT": 8, "UP": 9}
    pattern = laplace_smoothed_distribution(class_counts, alpha=1.0)
    population = laplace_smoothed_distribution(population_counts, alpha=1.0)
    expected = total_variation_distance(pattern, population)
    assert overlay.spec.stats.class_counts == class_counts
    assert overlay.spec.stats.population_class_counts == population_counts
    assert overlay.spec.stats.association_metric == PATTERN_ASSOCIATION_METRIC
    assert overlay.spec.stats.association_score == pytest.approx(expected)
    assert overlay.spec.target.move_distribution["UP"] == pytest.approx(pattern["UP"])


def test_ranking_is_deterministic() -> None:
    records = []
    for day in range(1, 6):
        when = datetime(2026, 8, day, tzinfo=UTC)
        records.append(
            _record(
                as_of=when,
                label="UP",
                knowledge=(
                    _observes(_country(), digest=f"{day:064x}", effective_from=when - timedelta(hours=1)),
                    _about(_instrument(), digest=f"{(day + 10):064x}", effective_from=when - timedelta(hours=10)),
                ),
            )
        )
    first = _discover(tuple(records))
    second = _discover(tuple(reversed(records)))
    assert [item.semantic_signature for item in first.candidates] == [
        item.semantic_signature for item in second.candidates
    ]
    scores = [item.spec.stats.association_score for item in first.candidates]
    assert scores == sorted(scores, reverse=True)
    for left, right in zip(first.candidates, first.candidates[1:], strict=False):
        if left.spec.stats.association_score == right.spec.stats.association_score:
            if left.spec.stats.support == right.spec.stats.support:
                assert (left.semantic_signature, left.spec.target.horizon_id) <= (
                    right.semantic_signature,
                    right.spec.target.horizon_id,
                )


def test_provenance_split_keeps_separate_populations() -> None:
    when = datetime(2026, 8, 1, tzinfo=UTC)
    overlay = _observes(_country(), digest="9" * 64, effective_from=when - timedelta(hours=1))
    current = _record(as_of=when, label="UP", knowledge=(overlay,))
    drifted = _record(
        as_of=when + timedelta(days=1),
        label="DOWN",
        knowledge=(_observes(_country(), digest="8" * 64, effective_from=when - timedelta(hours=1)),),
        ontology_revision="market_ontology.v2",
    )
    result = _discover((current, drifted))
    overlays = [
        item
        for item in result.candidates
        if "country:OBSERVES:reverse:world_observation" in item.semantic_signature
    ]
    assert len(overlays) == 2
    revisions = {item.spec.ontology_revision for item in overlays}
    assert revisions == {"market_ontology.v1", "market_ontology.v2"}
    for item in overlays:
        assert item.spec.stats.population_support == 1
        assert item.spec.stats.support == 1


def test_formation_fingerprint_changes_with_evidence() -> None:
    when = datetime(2026, 8, 1, tzinfo=UTC)
    overlay = _observes(_country(), digest="7" * 64, effective_from=when - timedelta(hours=1))
    first = _record(as_of=when, knowledge=(overlay,), outcome_digest="4" * 64)
    second = _record(as_of=when, knowledge=(overlay,), outcome_digest="5" * 64)
    left = _discover((first,))
    right = _discover((second,))
    assert left.candidates
    assert right.candidates
    assert left.candidates[0].spec.formation_dataset_fingerprint != right.candidates[0].spec.formation_dataset_fingerprint
    assert left.formation_dataset_fingerprint != right.formation_dataset_fingerprint


def test_graph_episode_is_distinct_from_snapshot_market_root() -> None:
    when = datetime(2026, 8, 1, tzinfo=UTC)
    record = _record(as_of=when)
    assert record.episode.observation.feature_contract_version == GRAPH_FEATURE_CONTRACT_ID
    assert record.outcome.episode_id == record.episode.episode_id
    assert record.snapshot.root_episode_id != record.episode.episode_id
    assert record.episode.observation.graph is not None
    assert record.episode.observation.graph.snapshot_id == record.snapshot.snapshot_id
    features = record.episode.observation.graph_features or {}
    nested = features.get("snapshot") if isinstance(features, Mapping) else None
    if isinstance(nested, Mapping):
        assert nested["snapshot_id"] == record.snapshot.snapshot_id
    market = _market_episode(symbol="2330", as_of=when)
    members = _ancestry(_instrument())
    snapshot = _snapshot(market, structural=members)
    with pytest.raises(ValueError, match="graph companion feature contract"):
        PatternFormationRecord(
            episode=market,
            snapshot=snapshot,
            structural_relations=members,
            knowledge_relations=(),
            outcome=_outcome(episode_id=market.episode_id, as_of=when, label="UP"),
            recorded_at=when,
            available_at=when,
            market_anchor=TimeSpecificMarketAnchor(
                venue="TW",
                symbol="2330",
                bar_interval="1h",
                as_of_bar_ts=when,
                horizon_id="elapsed_1d.v1",
            ),
        )
    other = _snapshot(market, structural=members[:1])
    graph = _graph_episode(market=market, snapshot=snapshot)
    with pytest.raises(ValueError, match="embedded graph snapshot_id"):
        PatternFormationRecord(
            episode=graph,
            snapshot=other,
            structural_relations=members[:1],
            knowledge_relations=(),
            outcome=_outcome(episode_id=graph.episode_id, as_of=when, label="UP"),
            recorded_at=when,
            available_at=when,
            market_anchor=TimeSpecificMarketAnchor(
                venue="TW",
                symbol="2330",
                bar_interval="1h",
                as_of_bar_ts=when,
                horizon_id="elapsed_1d.v1",
            ),
        )


def test_conflicting_anchor_labels_are_rejected_and_identical_evidence_dedupes() -> None:
    when = datetime(2026, 8, 1, tzinfo=UTC)
    overlay = _observes(_country(), digest="6" * 64, effective_from=when - timedelta(hours=1))
    up = _record(as_of=when, label="UP", knowledge=(overlay,), outcome_digest="a" * 64)
    down = _record(as_of=when, label="DOWN", knowledge=(overlay,), outcome_digest="b" * 64)
    conflicted = _discover((up, down))
    assert conflicted.candidates == ()
    assert conflicted.eligible_records == 0
    assert conflicted.rejection_counts.get("conflicting_anchor_label") == 2
    duplicate = _discover((up, up))
    overlay_candidates = [
        item
        for item in duplicate.candidates
        if "country:OBSERVES:reverse:world_observation" in item.semantic_signature
    ]
    assert overlay_candidates[0].spec.stats.support == 1
    assert overlay_candidates[0].spec.stats.population_support == 1


def test_zero_candidates_and_import_boundaries() -> None:
    when = datetime(2026, 8, 1, tzinfo=UTC)
    result = _discover((_record(as_of=when),), min_support=20, min_association=0.99)
    assert result.candidates == ()
    assert result.eligible_records == 1
    payload = result.to_dict()
    assert payload["candidates"] == []
    for path in (_DISCOVERY, _PORTS, _REQUEST):
        source = path.read_text(encoding="utf-8")
        assert "trader.infrastructure" not in source
        assert "trader.runtime" not in source
        assert "trader.reporting" not in source
        tree = ast.parse(source, filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                assert not node.module.startswith("trader.application.world_model.gru")
                assert "networkx" not in node.module
                assert not node.module.startswith("trader.infrastructure")
                assert not node.module.startswith("trader.runtime")
            if isinstance(node, ast.Import):
                assert all("gru" not in alias.name.lower() for alias in node.names)
                assert all(alias.name != "networkx" for alias in node.names)
    assert "SemanticPathHop" not in _PORTS.read_text(encoding="utf-8")
    assert "SemanticPathHop" not in _DISCOVERY.read_text(encoding="utf-8")
    assert "class PatternDriverStateBinding" in _PORTS.read_text(encoding="utf-8")


def test_rising_and_falling_regime_bundles_are_distinct_hypotheses() -> None:
    t1 = datetime(2026, 8, 1, tzinfo=UTC)
    t2 = datetime(2026, 8, 2, tzinfo=UTC)
    overlay_one = _observes(_country(), digest="a" * 64, effective_from=t1 - timedelta(hours=1))
    overlay_two = _observes(_country(), digest="b" * 64, effective_from=t2 - timedelta(hours=1))
    rising = _record(
        as_of=t1,
        knowledge=(overlay_one,),
        driver_state_bindings=(_binding(overlay_one, _regime_driver(rates_regime="rising")),),
    )
    falling = _record(
        as_of=t2,
        knowledge=(overlay_two,),
        driver_state_bindings=(_binding(overlay_two, _regime_driver(rates_regime="falling")),),
    )
    result = _discover((rising, falling))
    overlays = [
        item
        for item in result.candidates
        if "country:OBSERVES:reverse:world_observation" in item.semantic_signature
    ]
    assert len(overlays) == 2
    signatures = {item.semantic_signature for item in overlays}
    assert len(signatures) == 2
    hypothesis_ids = {item.spec.hypothesis_id for item in overlays}
    assert len(hypothesis_ids) == 2
    regimes = {
        item.spec.steps[-1].driver_state.regimes.rates_regime
        for item in overlays
        if item.spec.steps[-1].driver_state is not None and item.spec.steps[-1].driver_state.regimes is not None
    }
    assert regimes == {"rising", "falling"}
    for item in overlays:
        payload = item.to_dict()
        assert payload["spec"]["steps"][-1]["driver_state"]["regimes"]["rates_regime"] in {"rising", "falling"}
        assert "observation_id" not in payload["spec"]["steps"][-1]["driver_state"]


def test_evidence_refs_do_not_change_semantic_identity() -> None:
    t1 = datetime(2026, 8, 1, tzinfo=UTC)
    t2 = datetime(2026, 8, 2, tzinfo=UTC)
    first = _observes(_country(), digest="c" * 64, effective_from=t1 - timedelta(hours=1))
    second = _observes(_country(), digest="d" * 64, effective_from=t2 - timedelta(hours=1))
    driver = _regime_driver()
    left = _record(
        as_of=t1,
        knowledge=(first,),
        driver_state_bindings=(_binding(first, driver, evidence_refs=("world_observation:v1:" + "e" * 64,)),),
    )
    right = _record(
        as_of=t2,
        knowledge=(second,),
        driver_state_bindings=(_binding(second, driver, evidence_refs=("world_observation:v1:" + "f" * 64,)),),
    )
    result = _discover((left, right))
    overlays = [
        item
        for item in result.candidates
        if "country:OBSERVES:reverse:world_observation" in item.semantic_signature
    ]
    assert len(overlays) == 1
    assert overlays[0].spec.stats.support == 2
    assert left.driver_state_bindings[0].evidence_refs != right.driver_state_bindings[0].evidence_refs
    assert left.driver_state_bindings[0].driver_state.identity_tuple() == (
        right.driver_state_bindings[0].driver_state.identity_tuple()
    )


def test_missing_and_producer_unknown_stay_distinct() -> None:
    t1 = datetime(2026, 8, 1, tzinfo=UTC)
    t2 = datetime(2026, 8, 2, tzinfo=UTC)
    reported = _observes(_country(), digest="11" * 32, effective_from=t1 - timedelta(hours=1))
    unjoined = _observes(_country(), digest="22" * 32, effective_from=t2 - timedelta(hours=1))
    unknown = _regime_driver(rates_regime="unknown", coverage_status="unknown")
    missing = DriverState.missing(missingness="observation_unjoined", artifact_kind="macro_world_observation")
    left = _record(
        as_of=t1,
        knowledge=(reported,),
        driver_state_bindings=(_binding(reported, unknown),),
    )
    right = _record(
        as_of=t2,
        knowledge=(unjoined,),
        driver_state_bindings=(_binding(unjoined, missing),),
    )
    result = _discover((left, right))
    overlays = [
        item
        for item in result.candidates
        if "country:OBSERVES:reverse:world_observation" in item.semantic_signature
    ]
    assert len(overlays) == 2
    classes = {item.spec.steps[-1].driver_state.signal_class for item in overlays}
    assert classes == {"regime_bundle", "missing"}
    assert unknown.identity_tuple() != missing.identity_tuple()


def test_missing_or_extra_driver_binding_is_rejected() -> None:
    when = datetime(2026, 8, 1, tzinfo=UTC)
    overlay = _observes(_country(), digest="33" * 32, effective_from=when - timedelta(hours=1))
    with pytest.raises(ValueError, match="binding|DriverState"):
        _record(as_of=when, knowledge=(overlay,), driver_state_bindings=())
    extra = PatternDriverStateBinding(
        relation_id=_structural("TRADED_ON", _instrument(), _venue()).relation_id,
        driver_state=_regime_driver(),
    )
    with pytest.raises(ValueError, match="binding|DriverState"):
        _record(as_of=when, knowledge=(overlay,), driver_state_bindings=(_binding(overlay, _regime_driver()), extra))
    with pytest.raises(ValueError, match="duplicate"):
        _record(
            as_of=when,
            knowledge=(overlay,),
            driver_state_bindings=(
                _binding(overlay, _regime_driver()),
                _binding(overlay, _regime_driver(rates_regime="falling")),
            ),
        )
    structural_only = _record(as_of=when)
    assert structural_only.driver_state_bindings == ()
    replayed = PatternDriverStateBinding.from_mapping(_binding(overlay, _regime_driver()).to_dict())
    assert replayed.driver_state == _regime_driver()
