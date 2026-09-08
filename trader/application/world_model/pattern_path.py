"""Shared ID-free graph path projection for discovery and prospective matching.

The walk is the ancestry-forward structural chain plus root branches, then
admitted OBSERVES/ABOUT overlays on visited nodes. The traversal library is
never imported. Evidence refs stay on matched hops and never enter
PatternStep identity.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from trader.application.world_model.pattern_discovery_ports import PatternDriverStateBinding
from trader.domain.world_driver import DriverState
from trader.domain.world_feature_contract import GRAPH_PATH_RULE_VERSION
from trader.domain.world_graph import (
    GEOGRAPHIC_ANCESTRY_WALK,
    GRAPH_OVERLAY_RELATION_KINDS,
    ROOT_BRANCH_WALK,
    KnowledgeArtifactRef,
    KnowledgeWorldRelation,
    StructuralWorldRelation,
    WorldEntityRef,
    WorldGraphSnapshot,
    WorldObservationRef,
    geographic_ancestry_hops,
    geographic_ancestry_is_complete,
)
from trader.domain.world_pattern import PATTERN_FRESHNESS_BUCKETS, PatternMatchedHop, PatternStep


_BUDGET_EXCEEDED = "graph_budget_exceeded"
_ANCESTRY_INCOMPLETE = "incomplete"
_FOUR_HOURS = timedelta(hours=4)
_ONE_DAY = timedelta(hours=24)
_SEVEN_DAYS = timedelta(days=7)


class PatternPathSource(Protocol):
    """Minimal view required to project current paths. No outcome fields."""

    snapshot: WorldGraphSnapshot
    structural_relations: Sequence[StructuralWorldRelation]
    knowledge_relations: Sequence[KnowledgeWorldRelation]

    @property
    def driver_state_by_relation_id(self) -> Mapping[str, PatternDriverStateBinding]: ...


@dataclass(frozen=True)
class ProjectedPatternPath:
    """One projected chain: semantic steps plus occurrence hops with evidence."""

    steps: tuple[PatternStep, ...]
    hops: tuple[PatternMatchedHop, ...]

    def __post_init__(self) -> None:
        if len(self.steps) != len(self.hops):
            raise ValueError("projected path steps and hops must be the same length")
        for step, hop in zip(self.steps, self.hops, strict=True):
            if step.identity_tuple() != hop.identity_tuple():
                raise ValueError("projected hop identity must match the PatternStep identity")

    @property
    def signature(self) -> str:
        return pattern_path_signature(self.steps)


def _signature_part(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, tuple):
        return ":".join(_signature_part(item) for item in value)
    return str(value)


def pattern_path_signature(steps: Sequence[PatternStep]) -> str:
    return "|".join(_signature_part(step.identity_tuple()) for step in steps)


def freshness_bucket(effective_from: datetime, cutoff_at: datetime) -> str:
    if effective_from > cutoff_at:
        raise ValueError("relation effective_from is after snapshot cutoff")
    age = cutoff_at - effective_from
    if age < _FOUR_HOURS:
        bucket = "0-4h"
    elif age < _ONE_DAY:
        bucket = "4-24h"
    elif age < _SEVEN_DAYS:
        bucket = "1-7d"
    else:
        bucket = "older"
    if bucket not in PATTERN_FRESHNESS_BUCKETS:
        raise ValueError("freshness_bucket is not a declared pattern bucket")
    return bucket


def _pattern_step(
    ordinal: int,
    *,
    source_kind: str,
    relation_kind: str,
    direction: str,
    target_kind: str,
    freshness_bucket: str,
    driver_state: DriverState | None = None,
) -> PatternStep:
    return PatternStep(
        ordinal=ordinal,
        source_kind=source_kind,
        relation_kind=relation_kind,
        direction=direction,
        target_kind=target_kind,
        freshness_bucket=freshness_bucket,
        evidence_rule_version=GRAPH_PATH_RULE_VERSION,
        driver_state=driver_state,
    )


def _structural_step(ordinal: int, relation: StructuralWorldRelation, cutoff_at: datetime) -> PatternStep:
    return _pattern_step(
        ordinal,
        source_kind=relation.source.kind,
        relation_kind=relation.kind,
        direction="forward",
        target_kind=relation.target.kind,
        freshness_bucket=freshness_bucket(relation.effective_from, cutoff_at),
    )


def _overlay_step(
    ordinal: int,
    relation: KnowledgeWorldRelation,
    cutoff_at: datetime,
    driver_state: DriverState,
) -> PatternStep:
    if not isinstance(relation.target, WorldEntityRef):
        raise TypeError("overlay target must be a WorldEntityRef")
    if not isinstance(driver_state, DriverState):
        raise TypeError("overlay hop requires a bound DriverState")
    if relation.kind == "OBSERVES":
        if not isinstance(relation.source, WorldObservationRef):
            raise TypeError("OBSERVES source must be a WorldObservationRef")
        target_kind = "world_observation"
    elif relation.kind == "ABOUT":
        if not isinstance(relation.source, KnowledgeArtifactRef):
            raise TypeError("ABOUT source must be a KnowledgeArtifactRef")
        target_kind = "knowledge_artifact"
    else:
        raise ValueError("overlay hop must be OBSERVES or ABOUT")
    return _pattern_step(
        ordinal,
        source_kind=relation.target.kind,
        relation_kind=relation.kind,
        direction="reverse",
        target_kind=target_kind,
        freshness_bucket=freshness_bucket(relation.effective_from, cutoff_at),
        driver_state=driver_state,
    )


def _matched_from_step(step: PatternStep, evidence_refs: Sequence[str]) -> PatternMatchedHop:
    return PatternMatchedHop(
        ordinal=step.ordinal,
        source_kind=step.source_kind,
        relation_kind=step.relation_kind,
        direction=step.direction,
        target_kind=step.target_kind,
        freshness_bucket=step.freshness_bucket,
        evidence_rule_version=step.evidence_rule_version,
        driver_state=step.driver_state,
        evidence_refs=evidence_refs,
    )


def _member_structural(source: PatternPathSource) -> tuple[StructuralWorldRelation, ...]:
    allowed = {ref.relation_id for ref in source.snapshot.structural_relation_refs}
    return tuple(
        relation
        for relation in source.structural_relations
        if relation.relation_id in allowed and relation.effective_at(source.snapshot.cutoff_at)
    )


def _member_knowledge(source: PatternPathSource) -> tuple[KnowledgeWorldRelation, ...]:
    allowed = {ref.relation_id for ref in source.snapshot.knowledge_relation_refs}
    return tuple(
        relation
        for relation in source.knowledge_relations
        if relation.relation_id in allowed and relation.effective_at(source.snapshot.cutoff_at)
    )


def _unique_forward(
    relations: Sequence[StructuralWorldRelation],
    *,
    source: WorldEntityRef,
    kind: str,
    target_kind: str,
) -> StructuralWorldRelation | None:
    matches = [
        item
        for item in relations
        if item.source.node_id == source.node_id and item.kind == kind and item.target.kind == target_kind
    ]
    if len(matches) != 1:
        return None
    return matches[0]


def _capture_is_budget_truncated(snapshot: WorldGraphSnapshot) -> bool:
    if snapshot.status == "partial":
        return True
    missingness = snapshot.missingness or {}
    return missingness.get("budget") == _BUDGET_EXCEEDED or missingness.get("ancestry") == _ANCESTRY_INCOMPLETE


def geographic_ancestry_pattern_eligible(
    snapshot: WorldGraphSnapshot,
    hops: Sequence[StructuralWorldRelation],
) -> bool:
    """True when the geographic chain may be emitted as a pattern path."""

    if not hops:
        return False
    if geographic_ancestry_is_complete(hops):
        return True
    return not _capture_is_budget_truncated(snapshot)


def _walk_ancestry(
    root: WorldEntityRef,
    relations: Sequence[StructuralWorldRelation],
) -> tuple[tuple[StructuralWorldRelation, ...], dict[str, tuple[StructuralWorldRelation, ...]]]:
    hops = geographic_ancestry_hops(root, relations)
    prefixes: dict[str, tuple[StructuralWorldRelation, ...]] = {root.node_id: ()}
    acc: list[StructuralWorldRelation] = []
    for hop in hops:
        acc.append(hop)
        prefixes[hop.target.node_id] = tuple(acc)
    for kind, _direction, target_kind in ROOT_BRANCH_WALK:
        branch = _unique_forward(relations, source=root, kind=kind, target_kind=target_kind)
        if branch is None:
            continue
        prefixes[branch.target.node_id] = (branch,)
    return hops, prefixes


def _path_from_relations(
    relations: Sequence[StructuralWorldRelation],
    *,
    cutoff: datetime,
    overlay: tuple[KnowledgeWorldRelation, PatternDriverStateBinding] | None = None,
) -> ProjectedPatternPath:
    steps: list[PatternStep] = []
    hops: list[PatternMatchedHop] = []
    for index, relation in enumerate(relations):
        step = _structural_step(index, relation, cutoff)
        steps.append(step)
        hops.append(_matched_from_step(step, (relation.relation_id,)))
    if overlay is not None:
        relation, binding = overlay
        step = _overlay_step(len(steps), relation, cutoff, binding.driver_state)
        steps.append(step)
        hops.append(_matched_from_step(step, binding.evidence_refs))
    return ProjectedPatternPath(steps=tuple(steps), hops=tuple(hops))


def project_pattern_path_details(source: PatternPathSource) -> tuple[ProjectedPatternPath, ...]:
    """Project unique current paths. Overlay hops require a bound DriverState."""

    root = source.snapshot.root_entity
    if not isinstance(root, WorldEntityRef) or root.kind != "instrument":
        return ()
    structural = _member_structural(source)
    knowledge = _member_knowledge(source)
    ancestry_hops, prefixes = _walk_ancestry(root, structural)
    cutoff = source.snapshot.cutoff_at
    projected: list[ProjectedPatternPath] = []
    if geographic_ancestry_pattern_eligible(source.snapshot, ancestry_hops):
        projected.append(_path_from_relations(ancestry_hops, cutoff=cutoff))
    for prefix in prefixes.values():
        if len(prefix) == 1 and prefix[0].kind in {"ISSUED_BY", "MEMBER_OF_FAMILY"}:
            projected.append(_path_from_relations(prefix, cutoff=cutoff))
    visited = set(prefixes)
    for relation in knowledge:
        if relation.kind not in GRAPH_OVERLAY_RELATION_KINDS:
            continue
        if not isinstance(relation.target, WorldEntityRef):
            continue
        if relation.target.node_id not in visited:
            continue
        if relation.kind == "OBSERVES" and not isinstance(relation.source, WorldObservationRef):
            continue
        if relation.kind == "ABOUT" and not isinstance(relation.source, KnowledgeArtifactRef):
            continue
        binding = source.driver_state_by_relation_id.get(relation.relation_id)
        if binding is None:
            raise ValueError("overlay hop is missing a DriverState binding")
        prefix = prefixes[relation.target.node_id]
        projected.append(_path_from_relations(prefix, cutoff=cutoff, overlay=(relation, binding)))
    unique: dict[str, ProjectedPatternPath] = {}
    for path in projected:
        unique[path.signature] = path
    return tuple(unique.values())


def project_pattern_paths(source: PatternPathSource) -> tuple[tuple[PatternStep, ...], ...]:
    """Discovery-compatible view: semantic steps only, evidence refs omitted."""

    return tuple(path.steps for path in project_pattern_path_details(source))


def paths_matching_steps(
    source: PatternPathSource,
    steps: Sequence[PatternStep],
) -> tuple[ProjectedPatternPath, ...]:
    """Exact PatternStep identity match, including DriverState. Evidence is ignored."""

    expected = tuple(step.identity_tuple() for step in steps)
    return tuple(
        path
        for path in project_pattern_path_details(source)
        if tuple(step.identity_tuple() for step in path.steps) == expected
    )


__all__ = (
    "GEOGRAPHIC_ANCESTRY_WALK",
    "ROOT_BRANCH_WALK",
    "PatternPathSource",
    "ProjectedPatternPath",
    "freshness_bucket",
    "geographic_ancestry_pattern_eligible",
    "paths_matching_steps",
    "pattern_path_signature",
    "project_pattern_path_details",
    "project_pattern_paths",
)
