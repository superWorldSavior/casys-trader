"""Explicit graph-pattern discovery. Application/domain only; no sequence-model coupling."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any

from trader.application.world_model.pattern_discovery_ports import (
    PatternFormationBatch,
    PatternFormationRecord,
    PatternFormationSource,
)
from trader.application.world_model.pattern_formation_request import PatternFormationRequest
from trader.application.world_model.pattern_path import pattern_path_signature, project_pattern_paths
from trader.domain.world_episode import PREDICTION_CLASSES, canonical_sha256
from trader.domain.world_feature_contract import MARKET_ONTOLOGY_REVISION
from trader.domain.world_pattern import (
    PATTERN_ASSOCIATION_METRIC,
    PatternFormationStats,
    PatternHypothesisSpec,
    PatternStep,
    PatternTarget,
    laplace_smoothed_distribution,
    total_variation_distance,
)


def _iso(value: datetime) -> str:
    return value.isoformat()


def _clock_not_after(value: datetime | None, cutoff: datetime) -> bool:
    return value is not None and value <= cutoff


def _defensive_reject(record: PatternFormationRecord, request: PatternFormationRequest) -> str | None:
    cutoff = request.formation_cutoff
    episode = record.episode
    outcome = record.outcome
    observation = episode.observation
    if episode.training_eligible is not True:
        return "episode_not_training_eligible"
    if observation.as_of_bar_ts > cutoff:
        return "as_of_after_formation_cutoff"
    if not _clock_not_after(observation.available_at, cutoff):
        return "episode_available_after_formation_cutoff"
    if record.snapshot.cutoff_at > cutoff:
        return "snapshot_after_formation_cutoff"
    if not _clock_not_after(record.available_at, cutoff):
        return "record_available_after_formation_cutoff"
    if record.recorded_at > cutoff:
        return "record_recorded_after_formation_cutoff"
    if outcome.status != "observed" or outcome.training_eligible is not True:
        return "outcome_not_active_observed"
    if outcome.horizon.horizon_id not in request.horizons:
        return "horizon_not_requested"
    if not _clock_not_after(outcome.available_at, cutoff):
        return "late_label"
    if not _clock_not_after(outcome.computed_at, cutoff):
        return "late_label"
    if outcome.direction not in PREDICTION_CLASSES:
        return "outcome_class_missing"
    if request.ontology_revision is not None and record.snapshot.ontology_revision != request.ontology_revision:
        return "ontology_revision_mismatch"
    return None


def _empty_class_counts() -> dict[str, int]:
    return {label: 0 for label in PREDICTION_CLASSES}


def _build_stats(
    *,
    class_counts: Mapping[str, int],
    population_class_counts: Mapping[str, int],
    smoothing_alpha: float,
) -> PatternFormationStats:
    support = sum(class_counts[label] for label in PREDICTION_CLASSES)
    population_support = sum(population_class_counts[label] for label in PREDICTION_CLASSES)
    pattern = laplace_smoothed_distribution(class_counts, alpha=smoothing_alpha)
    population = laplace_smoothed_distribution(population_class_counts, alpha=smoothing_alpha)
    return PatternFormationStats(
        support=support,
        population_support=population_support,
        class_counts=class_counts,
        population_class_counts=population_class_counts,
        smoothing_alpha=smoothing_alpha,
        association_metric=PATTERN_ASSOCIATION_METRIC,
        association_score=total_variation_distance(pattern, population),
        pattern_distribution=pattern,
        population_distribution=population,
    )


def _evidence_payload(
    record: PatternFormationRecord,
    *,
    path_signature: str,
) -> dict[str, Any]:
    return {
        "episode_id": record.episode.episode_id,
        "snapshot_id": record.snapshot.snapshot_id,
        "snapshot_hash": record.snapshot.content_sha256,
        "outcome_event_id": record.outcome.event_id,
        "outcome_hash": record.outcome.payload_hash,
        "anchor": record.market_anchor.to_dict(),
        "path_signature": path_signature,
    }


def _fingerprint(payloads: Sequence[Mapping[str, Any]]) -> str:
    encoded = [canonical_sha256(dict(item)) for item in payloads]
    return canonical_sha256(sorted(encoded))


@dataclass(frozen=True)
class PatternDiscoveryCandidate:
    spec: PatternHypothesisSpec
    semantic_signature: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "semantic_signature": self.semantic_signature,
            "horizon_id": self.spec.target.horizon_id,
            "support": self.spec.stats.support,
            "association_score": self.spec.stats.association_score,
            "association_metric": self.spec.stats.association_metric,
            "spec": self.spec.to_dict(),
        }


@dataclass(frozen=True)
class PatternDiscoveryGroup:
    """Bounded, traceable explanation of a group, including failed selection gates."""

    semantic_signature: str
    horizon_id: str
    ontology_revision: str
    steps: tuple[PatternStep, ...]
    stats: PatternFormationStats
    rejection_reasons: tuple[str, ...]
    evidence_samples: tuple[Mapping[str, Any], ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "semantic_signature": self.semantic_signature,
            "horizon_id": self.horizon_id,
            "ontology_revision": self.ontology_revision,
            "steps": [step.to_dict() for step in self.steps],
            "stats": self.stats.to_dict(),
            "selected": not self.rejection_reasons,
            "rejection_reasons": list(self.rejection_reasons),
            "evidence_samples": [dict(item) for item in self.evidence_samples],
        }


@dataclass(frozen=True)
class PatternDiscoveryResult:
    candidates: tuple[PatternDiscoveryCandidate, ...]
    formation_dataset_fingerprint: str
    eligible_records: int
    rejection_counts: Mapping[str, int]
    source_evidence_ids: tuple[str, ...]
    considered_records: int = 0
    group_count: int = 0
    groups: tuple[PatternDiscoveryGroup, ...] = ()
    group_rejection_counts: Mapping[str, int] = field(default_factory=dict)
    records_without_paths: int = 0
    records_with_knowledge_relations: int = 0
    records_with_driver_state_bindings: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidates": [item.to_dict() for item in self.candidates],
            "formation_dataset_fingerprint": self.formation_dataset_fingerprint,
            "eligible_records": self.eligible_records,
            "considered_records": self.considered_records,
            "rejection_counts": dict(self.rejection_counts),
            "source_evidence_ids": list(self.source_evidence_ids),
            "group_count": self.group_count,
            "groups": [item.to_dict() for item in self.groups],
            "groups_omitted": self.group_count - len(self.groups),
            "group_rejection_counts": dict(self.group_rejection_counts),
            "records_without_paths": self.records_without_paths,
            "records_with_knowledge_relations": self.records_with_knowledge_relations,
            "records_with_driver_state_bindings": self.records_with_driver_state_bindings,
        }


@dataclass
class _Group:
    steps: tuple[PatternStep, ...]
    horizon_id: str
    ontology_revision: str
    anchors: dict[tuple[str, ...], str] = field(default_factory=dict)
    payloads: list[dict[str, Any]] = field(default_factory=list)
    source_refs: set[str] = field(default_factory=set)


@dataclass(frozen=True)
class PatternDiscoveryService:
    source: PatternFormationSource

    def discover(self, request: PatternFormationRequest) -> PatternDiscoveryResult:
        if not isinstance(request, PatternFormationRequest):
            raise TypeError("PatternFormationRequest is required")
        batch = self.source.load_formation_batch(request)
        if not isinstance(batch, PatternFormationBatch):
            raise TypeError("source must return a PatternFormationBatch")
        rejection_counts: dict[str, int] = dict(batch.rejection_counts)
        eligible: list[PatternFormationRecord] = []
        considered_records = 0
        for record in batch.records:
            if not isinstance(record, PatternFormationRecord):
                raise TypeError("batch records must be PatternFormationRecord")
            if (
                request.ontology_revision is not None
                and record.snapshot.ontology_revision != request.ontology_revision
            ):
                rejection_counts["ontology_revision_mismatch"] = (
                    rejection_counts.get("ontology_revision_mismatch", 0) + 1
                )
                continue
            reason = _defensive_reject(record, request)
            if reason is not None:
                rejection_counts[reason] = rejection_counts.get(reason, 0) + 1
                continue
            considered_records += 1
            eligible.append(record)

        labels_by_anchor: dict[tuple[Any, ...], dict[tuple[str, ...], set[str]]] = defaultdict(
            lambda: defaultdict(set)
        )
        for record in eligible:
            provenance = (
                record.outcome.horizon.horizon_id,
                request.feature_contract_id,
                request.feature_contract_fingerprint,
                request.feature_mask_id,
                request.feature_mask_fingerprint,
                record.snapshot.ontology_revision,
                request.model_identity,
            )
            label = record.outcome.direction
            assert label is not None
            labels_by_anchor[provenance][record.market_anchor.identity_tuple()].add(label)
        conflicted = {
            (provenance, anchor)
            for provenance, anchors in labels_by_anchor.items()
            for anchor, labels in anchors.items()
            if len(labels) > 1
        }

        counted: list[PatternFormationRecord] = []
        for record in eligible:
            provenance = (
                record.outcome.horizon.horizon_id,
                request.feature_contract_id,
                request.feature_contract_fingerprint,
                request.feature_mask_id,
                request.feature_mask_fingerprint,
                record.snapshot.ontology_revision,
                request.model_identity,
            )
            if (provenance, record.market_anchor.identity_tuple()) in conflicted:
                rejection_counts["conflicting_anchor_label"] = rejection_counts.get("conflicting_anchor_label", 0) + 1
                continue
            counted.append(record)
        eligible = counted

        groups: dict[tuple[Any, ...], _Group] = {}
        populations: dict[tuple[Any, ...], dict[tuple[str, ...], str]] = defaultdict(dict)
        all_payloads: list[dict[str, Any]] = []
        seen_payloads: set[str] = set()
        records_without_paths = 0
        for record in eligible:
            paths = project_pattern_paths(record)
            if not paths:
                records_without_paths += 1
            label = record.outcome.direction
            assert label is not None
            provenance = (
                record.outcome.horizon.horizon_id,
                request.feature_contract_id,
                request.feature_contract_fingerprint,
                request.feature_mask_id,
                request.feature_mask_fingerprint,
                record.snapshot.ontology_revision,
                request.model_identity,
            )
            populations[provenance][record.market_anchor.identity_tuple()] = label
            for steps in paths:
                signature = pattern_path_signature(steps)
                key = (signature, *provenance)
                group = groups.get(key)
                if group is None:
                    group = _Group(
                        steps=steps,
                        horizon_id=record.outcome.horizon.horizon_id,
                        ontology_revision=record.snapshot.ontology_revision,
                    )
                    groups[key] = group
                group.anchors[record.market_anchor.identity_tuple()] = label
                payload = _evidence_payload(record, path_signature=signature)
                payload_digest = canonical_sha256(payload)
                if payload_digest in seen_payloads:
                    continue
                seen_payloads.add(payload_digest)
                group.payloads.append(payload)
                all_payloads.append(payload)
                group.source_refs.add(record.episode.episode_id)
                group.source_refs.add(record.snapshot.snapshot_id or "")
                group.source_refs.add(record.outcome.event_id)

        candidates: list[PatternDiscoveryCandidate] = []
        diagnostics: list[PatternDiscoveryGroup] = []
        for (_signature, horizon_id, *_rest), group in groups.items():
            provenance = (
                group.horizon_id,
                request.feature_contract_id,
                request.feature_contract_fingerprint,
                request.feature_mask_id,
                request.feature_mask_fingerprint,
                group.ontology_revision,
                request.model_identity,
            )
            class_counts = _empty_class_counts()
            for label in group.anchors.values():
                class_counts[label] += 1
            population_class_counts = _empty_class_counts()
            for label in populations[provenance].values():
                population_class_counts[label] += 1
            if class_counts["DOWN"] + class_counts["FLAT"] + class_counts["UP"] < 1:
                continue
            stats = _build_stats(
                class_counts=class_counts,
                population_class_counts=population_class_counts,
                smoothing_alpha=request.smoothing_alpha,
            )
            reasons = []
            if stats.support < request.min_support:
                reasons.append("support_below_minimum")
            if stats.association_score < request.min_association:
                reasons.append("association_below_minimum")
            diagnostics.append(
                PatternDiscoveryGroup(
                    semantic_signature=_signature,
                    horizon_id=horizon_id,
                    ontology_revision=group.ontology_revision,
                    steps=group.steps,
                    stats=stats,
                    rejection_reasons=tuple(reasons),
                    evidence_samples=tuple(sorted(group.payloads, key=canonical_sha256)[:3]),
                )
            )
            if reasons:
                continue
            spec = PatternHypothesisSpec(
                evaluation_start_not_before=request.evaluation_start_not_before,
                target=PatternTarget(
                    entity_kind="instrument",
                    horizon_id=horizon_id,
                    move_distribution=stats.pattern_distribution,
                ),
                steps=group.steps,
                formation_cutoff=request.formation_cutoff,
                formation_dataset_fingerprint=_fingerprint(group.payloads),
                feature_contract_id=request.feature_contract_id,
                feature_contract_fingerprint=request.feature_contract_fingerprint,
                feature_mask_id=request.feature_mask_id,
                feature_mask_fingerprint=request.feature_mask_fingerprint,
                model_identity=request.model_identity,
                ontology_revision=group.ontology_revision or MARKET_ONTOLOGY_REVISION,
                stats=stats,
                source_refs=tuple(sorted(item for item in group.source_refs if item)),
                causal_claim=False,
            )
            candidates.append(
                PatternDiscoveryCandidate(spec=spec, semantic_signature=pattern_path_signature(group.steps))
            )

        candidates.sort(
            key=lambda item: (
                -item.spec.stats.association_score,
                -item.spec.stats.support,
                item.semantic_signature,
                item.spec.target.horizon_id,
            )
        )
        selected = tuple(candidates[: request.max_candidates])
        selected_keys = {
            (item.semantic_signature, item.spec.target.horizon_id, item.spec.ontology_revision)
            for item in selected
        }
        diagnostics = [
            replace(item, rejection_reasons=("candidate_limit",))
            if not item.rejection_reasons
            and (item.semantic_signature, item.horizon_id, item.ontology_revision) not in selected_keys
            else item
            for item in diagnostics
        ]
        group_rejections: dict[str, int] = defaultdict(int)
        for item in diagnostics:
            for reason in item.rejection_reasons:
                group_rejections[reason] += 1
        diagnostics.sort(
            key=lambda item: (-item.stats.support, item.semantic_signature, item.horizon_id, item.ontology_revision)
        )
        return PatternDiscoveryResult(
            candidates=selected,
            formation_dataset_fingerprint=_fingerprint(all_payloads) if all_payloads else canonical_sha256([]),
            eligible_records=len(eligible),
            rejection_counts=dict(sorted(rejection_counts.items())),
            source_evidence_ids=batch.source_evidence_ids,
            considered_records=considered_records,
            group_count=len(groups),
            groups=tuple(diagnostics[:20]),
            group_rejection_counts=dict(sorted(group_rejections.items())),
            records_without_paths=records_without_paths,
            records_with_knowledge_relations=sum(bool(record.knowledge_relations) for record in eligible),
            records_with_driver_state_bindings=sum(bool(record.driver_state_bindings) for record in eligible),
        )


__all__ = (
    "PatternDiscoveryCandidate",
    "PatternDiscoveryGroup",
    "PatternDiscoveryResult",
    "PatternDiscoveryService",
)
