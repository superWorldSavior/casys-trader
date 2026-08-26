"""Typed ports and unlabeled evaluation records for prospective pattern matching.

Evaluation records carry graph-companion episodes and attested snapshots only.
They never carry an outcome leaf. Observed leaves belong to the later linker.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Any, Protocol

from trader.application.world_model.pattern_discovery_ports import (
    PatternDriverStateBinding,
    _admitted_overlay_relation_ids,
    _binding_tuple,
    _count_mapping,
    _embedded_graph_snapshot_id,
    _typed_tuple,
    _unique_text_tuple,
)
from trader.domain.world_episode import (
    GRAPH_FEATURE_CONTRACT_ID,
    WorldEpisode,
    WorldPrediction,
    parse_utc_timestamp,
)
from trader.domain.world_graph import KnowledgeWorldRelation, StructuralWorldRelation, WorldGraphSnapshot
from trader.domain.world_pattern import PatternHypothesis, PatternOccurrence


def _required_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a non-empty string")
    text = value.strip()
    if not text:
        raise ValueError(f"{field_name} must be a non-empty string")
    return text


@dataclass(frozen=True)
class PatternEvaluationRecord:
    """One unlabeled graph-companion episode/snapshot already bounded by as-of."""

    episode: WorldEpisode
    snapshot: WorldGraphSnapshot
    structural_relations: Sequence[StructuralWorldRelation]
    knowledge_relations: Sequence[KnowledgeWorldRelation]
    recorded_at: datetime | str
    available_at: datetime | str
    driver_state_bindings: Sequence[PatternDriverStateBinding | Mapping[str, Any]] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.episode, WorldEpisode):
            raise TypeError("episode must be a WorldEpisode")
        if not isinstance(self.snapshot, WorldGraphSnapshot):
            raise TypeError("snapshot must be a WorldGraphSnapshot")
        structural = _typed_tuple(self.structural_relations, StructuralWorldRelation, "structural_relations")
        knowledge = _typed_tuple(self.knowledge_relations, KnowledgeWorldRelation, "knowledge_relations")
        recorded_at = parse_utc_timestamp(self.recorded_at, "recorded_at")
        available_at = parse_utc_timestamp(self.available_at, "available_at")
        observation = self.episode.observation
        if observation.feature_contract_version != GRAPH_FEATURE_CONTRACT_ID:
            raise ValueError("evaluation episode must use the graph companion feature contract")
        embedded_snapshot_id = _embedded_graph_snapshot_id(observation)
        if embedded_snapshot_id != self.snapshot.snapshot_id:
            raise ValueError("embedded graph snapshot_id must equal snapshot.snapshot_id")
        if self.snapshot.root_episode_id == self.episode.episode_id:
            raise ValueError("snapshot.root_episode_id is the market episode, not the graph companion")
        bindings = _binding_tuple(self.driver_state_bindings)
        required = _admitted_overlay_relation_ids(knowledge, self.snapshot)
        bound = {item.relation_id for item in bindings}
        if bound != required:
            raise ValueError("admitted OBSERVES/ABOUT relations require exactly one DriverState binding")
        object.__setattr__(self, "structural_relations", structural)
        object.__setattr__(self, "knowledge_relations", knowledge)
        object.__setattr__(self, "recorded_at", recorded_at)
        object.__setattr__(self, "available_at", available_at)
        object.__setattr__(self, "driver_state_bindings", bindings)

    @property
    def driver_state_by_relation_id(self) -> Mapping[str, PatternDriverStateBinding]:
        return MappingProxyType({item.relation_id: item for item in self.driver_state_bindings})


@dataclass(frozen=True)
class PatternEvaluationBatch:
    """Source-owned unlabeled records plus rejection and evidence accounting."""

    records: Sequence[PatternEvaluationRecord]
    rejection_counts: Mapping[str, int]
    source_evidence_ids: Sequence[str]

    def __post_init__(self) -> None:
        records = _typed_tuple(self.records, PatternEvaluationRecord, "records")
        object.__setattr__(self, "records", records)
        object.__setattr__(self, "rejection_counts", _count_mapping(self.rejection_counts, "rejection_counts"))
        object.__setattr__(
            self, "source_evidence_ids", _unique_text_tuple(self.source_evidence_ids, "source_evidence_ids")
        )


class EvaluatingHypothesisCatalog(Protocol):
    """Reconstructs evaluating hypotheses even when they have zero occurrences."""

    def list_evaluating_hypotheses(
        self,
        *,
        evaluation_cohort_id: str,
        evaluation_dataset_fingerprint: str,
        hypothesis_ids: Sequence[str] | None = None,
    ) -> tuple[PatternHypothesis, ...]:
        """Return evaluating hypotheses that match the cohort and dataset fences."""


class PatternEvaluationSource(Protocol):
    """Loads unlabeled graph companions admitted by the evaluation cohort.

    Must never read outcome rows. An empty or missing cohort must not fall
    back to unrelated episodes.
    """

    def load_evaluation_batch(self, request: Any) -> PatternEvaluationBatch:
        """Return eligible unlabeled records already bounded by the as-of scan."""


class PatternPredictionWriter(Protocol):
    """Append-only shadow prediction persistence. Implemented by WorldModelStore."""

    def append_prediction(self, prediction: WorldPrediction) -> bool:
        """Persist a shadow WorldPrediction. Identical replay is a no-op."""


class PatternOccurrenceCatalog(Protocol):
    """Reconstructs recorded occurrences without inventing outcome links."""

    def list_recorded_occurrences(
        self,
        *,
        evaluation_cohort_id: str | None = None,
        hypothesis_ids: Sequence[str] | None = None,
        occurrence_ids: Sequence[str] | None = None,
    ) -> tuple[PatternOccurrence, ...]:
        """Return reconstructed occurrences matching the optional selectors."""


__all__ = (
    "EvaluatingHypothesisCatalog",
    "PatternEvaluationBatch",
    "PatternEvaluationRecord",
    "PatternEvaluationSource",
    "PatternOccurrenceCatalog",
    "PatternPredictionWriter",
)
