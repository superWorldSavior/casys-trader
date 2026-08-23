"""Bounded deterministic V3 graph features with per-feature provenance.

Computed only from a PIT snapshot bundle. No network, LLM, or graph-library I/O.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from types import MappingProxyType
from typing import Any

from trader.application.world_model.graph_snapshot import WorldGraphSnapshotBundle
from trader.domain.world_episode import canonical_sha256
from trader.domain.world_feature_contract import (
    GRAPH_CONTENT_CATEGORICAL_FEATURES,
    GRAPH_FEATURE_CONTRACT_VERSION,
    GRAPH_FEATURE_GROUP_ID,
    GRAPH_STATUS_CATEGORICAL_FEATURES,
    GRAPH_STATUS_FEATURE_GROUP_ID,
    WORLD_V3_ENCODER_IDENTITY,
    WorldFeatureContract,
    WorldFeatureMask,
    world_v3_feature_contract,
    world_v3_graph_content_mask,
)
from trader.domain.world_graph import KnowledgeWorldRelation, StructuralWorldRelation


_FRESHNESS_ORDER = ("0-4h", "4-24h", "1-7d", "older")
_FRESHNESS_4H = timedelta(hours=4)
_FRESHNESS_24H = timedelta(hours=24)
_FRESHNESS_7D = timedelta(days=7)
_GRAPH_FEATURE_NAMES = GRAPH_STATUS_CATEGORICAL_FEATURES | GRAPH_CONTENT_CATEGORICAL_FEATURES


@dataclass(frozen=True)
class WorldGraphFeatureProvenance:
    feature_name: str
    group_id: str
    source_kind: str
    source_refs: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "feature_name": self.feature_name,
            "group_id": self.group_id,
            "source_kind": self.source_kind,
            "source_refs": list(self.source_refs),
        }


@dataclass(frozen=True)
class WorldGraphFeatureVector:
    snapshot_id: str
    cutoff_at: datetime
    contract_id: str
    contract_fingerprint: str
    mask_id: str
    mask_fingerprint: str
    encoder_identity: str
    categorical_features: Mapping[str, str]
    numeric_features: Mapping[str, float]
    provenance: tuple[WorldGraphFeatureProvenance, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "categorical_features", MappingProxyType(dict(self.categorical_features)))
        object.__setattr__(self, "numeric_features", MappingProxyType(dict(self.numeric_features)))

    def to_observation_payload(self) -> dict[str, dict[str, object]]:
        return {
            "categorical_features": dict(self.categorical_features),
            "numeric_features": dict(self.numeric_features),
        }


def _count_bucket(count: int) -> str:
    if count <= 0:
        return "b0"
    if count == 1:
        return "b1"
    if count <= 3:
        return "b2"
    if count <= 8:
        return "b3"
    return "b4"


def _depth_bucket(depth: int) -> str:
    if depth <= 0:
        return "b0"
    if depth == 1:
        return "b1"
    if depth == 2:
        return "b2"
    if depth == 3:
        return "b3"
    return "b4"


def _freshness_bucket(effective_from: datetime, cutoff: datetime) -> str:
    age = cutoff - effective_from
    if age < _FRESHNESS_4H:
        return "0-4h"
    if age < _FRESHNESS_24H:
        return "4-24h"
    if age < _FRESHNESS_7D:
        return "1-7d"
    return "older"


def _freshness_rank(bucket: str) -> int:
    try:
        return _FRESHNESS_ORDER.index(bucket)
    except ValueError:
        return len(_FRESHNESS_ORDER)


def _extreme_freshness(buckets: Sequence[str], *, newest: bool) -> str:
    if not buckets:
        return "missing"
    ranked = sorted(buckets, key=_freshness_rank)
    return ranked[0] if newest else ranked[-1]


def _source_refs(
    structural: Sequence[StructuralWorldRelation],
    knowledge: Sequence[KnowledgeWorldRelation],
) -> tuple[str, ...]:
    refs: set[str] = set()
    for relation in (*structural, *knowledge):
        refs.update(relation.source_refs)
    return tuple(sorted(refs))


def _macro_agreement(scope_status: str, knowledge: Sequence[KnowledgeWorldRelation]) -> str:
    if scope_status in {"unmapped", "ambiguous"}:
        return "missing"
    observes = [relation for relation in knowledge if relation.kind == "OBSERVES"]
    if not observes:
        return "none"
    targets = {relation.target.node_id for relation in observes}
    sources = {item for relation in observes for item in relation.source_refs}
    if len(targets) > 1:
        return "disagree"
    if len(sources) >= 2:
        return "agree"
    return "single"


def _missingness_status(missingness: Mapping[str, Any]) -> str:
    if not missingness:
        return "none"
    if missingness.get("budget") == "graph_budget_exceeded":
        return "budget"
    scope = missingness.get("scope")
    if scope in {"unmapped", "ambiguous"}:
        return str(scope)
    ontology = missingness.get("ontology")
    if ontology == "unpublished":
        return "unpublished"
    return "present"


def _path_signature(signatures: Sequence[str]) -> str:
    if not signatures:
        return "none"
    digest = canonical_sha256(list(signatures))
    return f"sig:{digest[:16]}"


def _group_for(name: str) -> str:
    if name in GRAPH_STATUS_CATEGORICAL_FEATURES:
        return GRAPH_STATUS_FEATURE_GROUP_ID
    return GRAPH_FEATURE_GROUP_ID


def _provenance(
    name: str,
    *,
    source_kind: str,
    source_refs: Sequence[str],
    snapshot_id: str,
) -> WorldGraphFeatureProvenance:
    refs = tuple(source_refs) if source_refs else (snapshot_id,)
    return WorldGraphFeatureProvenance(
        feature_name=name,
        group_id=_group_for(name),
        source_kind=source_kind,
        source_refs=refs,
    )


def encode_world_graph_features(
    bundle: WorldGraphSnapshotBundle,
    *,
    contract: WorldFeatureContract | None = None,
    mask: WorldFeatureMask | None = None,
) -> WorldGraphFeatureVector:
    if not isinstance(bundle, WorldGraphSnapshotBundle):
        raise TypeError("graph features require a WorldGraphSnapshotBundle")
    resolved_contract = world_v3_feature_contract() if contract is None else contract
    resolved_mask = world_v3_graph_content_mask() if mask is None else mask
    if not isinstance(resolved_contract, WorldFeatureContract):
        raise TypeError("contract must be WorldFeatureContract")
    if not isinstance(resolved_mask, WorldFeatureMask):
        raise TypeError("mask must be WorldFeatureMask")
    if resolved_contract.contract_id != GRAPH_FEATURE_CONTRACT_VERSION:
        raise ValueError("graph feature contract must be market_ohlcv_graph.v3")
    resolved_mask.assert_compatible_with(resolved_contract)

    snapshot = bundle.snapshot
    cutoff = snapshot.cutoff_at
    scope_status = str(snapshot.missingness["scope"]) if "scope" in snapshot.missingness else "resolved"

    path_signatures = tuple(sorted({path.signature for path in bundle.paths.paths}))
    path_relation_ids = tuple(sorted({step.relation_id for path in bundle.paths.paths for step in path.steps}))
    depths = [path.depth for path in bundle.paths.paths]
    path_buckets = [step.freshness_bucket for path in bundle.paths.paths for step in path.steps]
    member_buckets = [
        _freshness_bucket(relation.effective_from, cutoff)
        for relation in (*bundle.structural_relations, *bundle.knowledge_relations)
    ]
    window_ids: dict[str, tuple[str, ...]] = {}
    window_counts: dict[str, int] = {}
    for bucket in _FRESHNESS_ORDER:
        ids = tuple(
            sorted(
                relation.relation_id
                for relation in bundle.knowledge_relations
                if _freshness_bucket(relation.effective_from, cutoff) == bucket
            )
        )
        window_ids[bucket] = ids
        window_counts[bucket] = len(ids)
    sources = _source_refs(bundle.structural_relations, bundle.knowledge_relations)
    values: dict[str, str] = {
        "graph_status": snapshot.status,
        "graph_scope_status": scope_status,
        "graph_coverage_status": snapshot.status,
        "graph_freshness_status": _extreme_freshness(member_buckets, newest=True),
        "graph_source_count_bucket": _count_bucket(len(sources)),
        "graph_artifact_count_bucket": _count_bucket(len(snapshot.artifact_refs)),
        "graph_missingness_status": _missingness_status(snapshot.missingness),
        "graph_path_count_bucket": _count_bucket(len(bundle.paths.paths)),
        "graph_depth_min_bucket": _depth_bucket(min(depths) if depths else 0),
        "graph_depth_max_bucket": _depth_bucket(max(depths) if depths else 0),
        "graph_path_freshness_min_bucket": _extreme_freshness(path_buckets, newest=True),
        "graph_path_freshness_max_bucket": _extreme_freshness(path_buckets, newest=False),
        "graph_path_signature": _path_signature(path_signatures),
        "graph_macro_agreement_status": _macro_agreement(scope_status, bundle.knowledge_relations),
        "graph_window_0_4h_count_bucket": _count_bucket(window_counts["0-4h"]),
        "graph_window_4_24h_count_bucket": _count_bucket(window_counts["4-24h"]),
        "graph_window_1_7d_count_bucket": _count_bucket(window_counts["1-7d"]),
        "graph_window_older_count_bucket": _count_bucket(window_counts["older"]),
    }
    provenance_by_name = {
        "graph_status": _provenance("graph_status", source_kind="snapshot", source_refs=(snapshot.snapshot_id,), snapshot_id=snapshot.snapshot_id),
        "graph_scope_status": _provenance(
            "graph_scope_status", source_kind="snapshot", source_refs=(snapshot.snapshot_id,), snapshot_id=snapshot.snapshot_id
        ),
        "graph_coverage_status": _provenance(
            "graph_coverage_status", source_kind="snapshot", source_refs=(snapshot.snapshot_id,), snapshot_id=snapshot.snapshot_id
        ),
        "graph_freshness_status": _provenance(
            "graph_freshness_status",
            source_kind="relation",
            source_refs=path_relation_ids,
            snapshot_id=snapshot.snapshot_id,
        ),
        "graph_source_count_bucket": _provenance(
            "graph_source_count_bucket", source_kind="source_ref", source_refs=sources, snapshot_id=snapshot.snapshot_id
        ),
        "graph_artifact_count_bucket": _provenance(
            "graph_artifact_count_bucket",
            source_kind="artifact",
            source_refs=tuple(snapshot.artifact_refs),
            snapshot_id=snapshot.snapshot_id,
        ),
        "graph_missingness_status": _provenance(
            "graph_missingness_status", source_kind="snapshot", source_refs=(snapshot.snapshot_id,), snapshot_id=snapshot.snapshot_id
        ),
        "graph_path_count_bucket": _provenance(
            "graph_path_count_bucket", source_kind="path", source_refs=path_relation_ids, snapshot_id=snapshot.snapshot_id
        ),
        "graph_depth_min_bucket": _provenance(
            "graph_depth_min_bucket", source_kind="path", source_refs=path_relation_ids, snapshot_id=snapshot.snapshot_id
        ),
        "graph_depth_max_bucket": _provenance(
            "graph_depth_max_bucket", source_kind="path", source_refs=path_relation_ids, snapshot_id=snapshot.snapshot_id
        ),
        "graph_path_freshness_min_bucket": _provenance(
            "graph_path_freshness_min_bucket", source_kind="path", source_refs=path_relation_ids, snapshot_id=snapshot.snapshot_id
        ),
        "graph_path_freshness_max_bucket": _provenance(
            "graph_path_freshness_max_bucket", source_kind="path", source_refs=path_relation_ids, snapshot_id=snapshot.snapshot_id
        ),
        "graph_path_signature": _provenance(
            "graph_path_signature", source_kind="path", source_refs=path_relation_ids, snapshot_id=snapshot.snapshot_id
        ),
        "graph_macro_agreement_status": _provenance(
            "graph_macro_agreement_status",
            source_kind="knowledge_relation",
            source_refs=tuple(item.relation_id for item in bundle.knowledge_relations if item.kind == "OBSERVES"),
            snapshot_id=snapshot.snapshot_id,
        ),
        "graph_window_0_4h_count_bucket": _provenance(
            "graph_window_0_4h_count_bucket",
            source_kind="window",
            source_refs=window_ids["0-4h"],
            snapshot_id=snapshot.snapshot_id,
        ),
        "graph_window_4_24h_count_bucket": _provenance(
            "graph_window_4_24h_count_bucket",
            source_kind="window",
            source_refs=window_ids["4-24h"],
            snapshot_id=snapshot.snapshot_id,
        ),
        "graph_window_1_7d_count_bucket": _provenance(
            "graph_window_1_7d_count_bucket",
            source_kind="window",
            source_refs=window_ids["1-7d"],
            snapshot_id=snapshot.snapshot_id,
        ),
        "graph_window_older_count_bucket": _provenance(
            "graph_window_older_count_bucket",
            source_kind="window",
            source_refs=window_ids["older"],
            snapshot_id=snapshot.snapshot_id,
        ),
    }

    selected = resolved_mask.selected_categorical_features(resolved_contract) & _GRAPH_FEATURE_NAMES
    categorical = {name: values[name] for name in sorted(selected)}
    provenance = tuple(provenance_by_name[name] for name in sorted(selected))
    return WorldGraphFeatureVector(
        snapshot_id=snapshot.snapshot_id,
        cutoff_at=cutoff,
        contract_id=resolved_contract.contract_id,
        contract_fingerprint=resolved_contract.fingerprint,
        mask_id=resolved_mask.mask_id,
        mask_fingerprint=resolved_mask.fingerprint,
        encoder_identity=WORLD_V3_ENCODER_IDENTITY,
        categorical_features=categorical,
        numeric_features={},
        provenance=provenance,
    )


__all__ = [
    "WorldGraphFeatureProvenance",
    "WorldGraphFeatureVector",
    "encode_world_graph_features",
]
