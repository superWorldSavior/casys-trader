"""Typed ports and immutable formation records for explicit graph-pattern discovery."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Protocol

from trader.domain.world_driver import DRIVER_OVERLAY_RELATION_KINDS, DriverState
from trader.domain.world_episode import (
    GRAPH_FEATURE_CONTRACT_ID,
    WorldEpisode,
    WorldOutcome,
    parse_utc_timestamp,
)
from trader.domain.world_graph import KnowledgeWorldRelation, StructuralWorldRelation, WorldGraphSnapshot

if TYPE_CHECKING:
    from trader.application.world_model.pattern_formation_request import PatternFormationRequest


def _required_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a non-empty string")
    text = value.strip()
    if not text:
        raise ValueError(f"{field_name} must be a non-empty string")
    return text


def _iso(value: datetime) -> str:
    return value.isoformat()


def _non_negative_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field_name} must be a nonnegative integer")
    if value < 0:
        raise ValueError(f"{field_name} must be a nonnegative integer")
    return value


def _unique_text_tuple(value: Sequence[str] | None, field_name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise TypeError(f"{field_name} must be a sequence of strings")
    items = tuple(_required_text(item, f"{field_name}[]") for item in value)
    if len(items) != len(set(items)):
        raise ValueError(f"{field_name} must not contain duplicates")
    return items


def _count_mapping(value: Any, field_name: str) -> Mapping[str, int]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{field_name} must be a mapping")
    frozen: dict[str, int] = {}
    for raw_key, raw_count in value.items():
        key = _required_text(raw_key, f"{field_name} key")
        frozen[key] = _non_negative_int(raw_count, f"{field_name}.{key}")
    return MappingProxyType(frozen)


def _embedded_graph_snapshot_id(observation: Any) -> str:
    features = getattr(observation, "graph_features", None)
    if isinstance(features, Mapping):
        nested = features.get("snapshot")
        if isinstance(nested, Mapping):
            raw = nested.get("snapshot_id")
            if isinstance(raw, str) and raw.strip():
                return raw.strip()
        raw = features.get("snapshot_id")
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
    graph = getattr(observation, "graph", None)
    snapshot_id = getattr(graph, "snapshot_id", None)
    if isinstance(snapshot_id, str) and snapshot_id.strip():
        return snapshot_id.strip()
    if isinstance(graph, Mapping):
        raw = graph.get("snapshot_id")
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
    raise ValueError("graph episode must embed snapshot_id on observation.graph or graph_features.snapshot")


_RELATION_ID_PREFIX = "world_relation:v1:"


def _relation_id(value: Any, field_name: str) -> str:
    text = _required_text(value, field_name)
    if not text.startswith(_RELATION_ID_PREFIX):
        raise ValueError(f"{field_name} must start with '{_RELATION_ID_PREFIX}'")
    return text


def _as_driver_state(value: Any) -> DriverState:
    if isinstance(value, DriverState):
        return value
    if isinstance(value, Mapping):
        return DriverState.from_mapping(value)
    raise TypeError("driver_state must be DriverState or a mapping")


@dataclass(frozen=True)
class PatternDriverStateBinding:
    """One admitted overlay relation's closed driver plus occurrence evidence refs."""

    relation_id: str
    driver_state: DriverState | Mapping[str, Any]
    evidence_refs: Sequence[str] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "relation_id", _relation_id(self.relation_id, "relation_id"))
        object.__setattr__(self, "driver_state", _as_driver_state(self.driver_state))
        object.__setattr__(self, "evidence_refs", _unique_text_tuple(self.evidence_refs, "evidence_refs"))

    def identity_tuple(self) -> tuple[object, ...]:
        return (self.relation_id, self.driver_state.identity_tuple())

    def to_dict(self) -> dict[str, Any]:
        return {
            "relation_id": self.relation_id,
            "driver_state": self.driver_state.to_dict(),
            "evidence_refs": list(self.evidence_refs),
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | PatternDriverStateBinding) -> PatternDriverStateBinding:
        if isinstance(value, PatternDriverStateBinding):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("driver state binding must be PatternDriverStateBinding or a mapping")
        return cls(
            relation_id=value.get("relation_id"),
            driver_state=value.get("driver_state"),
            evidence_refs=value.get("evidence_refs") or (),
        )


def _binding_tuple(value: Sequence[Any] | None) -> tuple[PatternDriverStateBinding, ...]:
    if value is None:
        return ()
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise TypeError("driver_state_bindings must be a sequence of PatternDriverStateBinding")
    bindings = tuple(
        item if isinstance(item, PatternDriverStateBinding) else PatternDriverStateBinding.from_mapping(item)
        for item in value
    )
    relation_ids = tuple(item.relation_id for item in bindings)
    if len(relation_ids) != len(set(relation_ids)):
        raise ValueError("driver_state_bindings must not contain duplicate relation ids")
    return bindings


def _admitted_overlay_relation_ids(
    knowledge: Sequence[KnowledgeWorldRelation],
    snapshot: WorldGraphSnapshot,
) -> frozenset[str]:
    admitted = {ref.relation_id for ref in snapshot.knowledge_relation_refs}
    return frozenset(
        relation.relation_id
        for relation in knowledge
        if relation.kind in DRIVER_OVERLAY_RELATION_KINDS and relation.relation_id in admitted
    )


def _typed_tuple(value: Sequence[Any] | None, expected: type, field_name: str) -> tuple[Any, ...]:
    if value is None:
        return ()
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise TypeError(f"{field_name} must be a sequence of {expected.__name__}")
    items = []
    for index, item in enumerate(value):
        if not isinstance(item, expected):
            raise TypeError(f"{field_name}[{index}] must be {expected.__name__}")
        items.append(item)
    return tuple(items)


@dataclass(frozen=True)
class TimeSpecificMarketAnchor:
    """One venue/symbol/interval at one bar close and one labeled horizon."""

    venue: str
    symbol: str
    bar_interval: str
    as_of_bar_ts: datetime | str
    horizon_id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "venue", _required_text(self.venue, "venue"))
        object.__setattr__(self, "symbol", _required_text(self.symbol, "symbol"))
        object.__setattr__(self, "bar_interval", _required_text(self.bar_interval, "bar_interval"))
        object.__setattr__(self, "as_of_bar_ts", parse_utc_timestamp(self.as_of_bar_ts, "as_of_bar_ts"))
        object.__setattr__(self, "horizon_id", _required_text(self.horizon_id, "horizon_id"))

    def identity_tuple(self) -> tuple[str, str, str, str, str]:
        return (self.venue, self.symbol, self.bar_interval, _iso(self.as_of_bar_ts), self.horizon_id)

    def to_dict(self) -> dict[str, str]:
        return {
            "venue": self.venue,
            "symbol": self.symbol,
            "bar_interval": self.bar_interval,
            "as_of_bar_ts": _iso(self.as_of_bar_ts),
            "horizon_id": self.horizon_id,
        }


@dataclass(frozen=True)
class PatternFormationRecord:
    """One labeled episode/snapshot/outcome already bounded by the as-of query."""

    episode: WorldEpisode
    snapshot: WorldGraphSnapshot
    structural_relations: Sequence[StructuralWorldRelation]
    knowledge_relations: Sequence[KnowledgeWorldRelation]
    outcome: WorldOutcome
    recorded_at: datetime | str
    available_at: datetime | str
    market_anchor: TimeSpecificMarketAnchor | Mapping[str, Any]
    driver_state_bindings: Sequence[PatternDriverStateBinding | Mapping[str, Any]] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.episode, WorldEpisode):
            raise TypeError("episode must be a WorldEpisode")
        if not isinstance(self.snapshot, WorldGraphSnapshot):
            raise TypeError("snapshot must be a WorldGraphSnapshot")
        if not isinstance(self.outcome, WorldOutcome):
            raise TypeError("outcome must be a WorldOutcome")
        structural = _typed_tuple(self.structural_relations, StructuralWorldRelation, "structural_relations")
        knowledge = _typed_tuple(self.knowledge_relations, KnowledgeWorldRelation, "knowledge_relations")
        recorded_at = parse_utc_timestamp(self.recorded_at, "recorded_at")
        available_at = parse_utc_timestamp(self.available_at, "available_at")
        anchor = (
            self.market_anchor
            if isinstance(self.market_anchor, TimeSpecificMarketAnchor)
            else TimeSpecificMarketAnchor(**dict(self.market_anchor))
        )
        observation = self.episode.observation
        if observation.feature_contract_version != GRAPH_FEATURE_CONTRACT_ID:
            raise ValueError("formation episode must use the graph companion feature contract")
        if (
            anchor.venue != observation.venue
            or anchor.symbol != observation.symbol
            or anchor.bar_interval != observation.bar_interval
            or anchor.as_of_bar_ts != observation.as_of_bar_ts
        ):
            raise ValueError("market_anchor must match the episode observation slot")
        if anchor.horizon_id != self.outcome.horizon.horizon_id:
            raise ValueError("market_anchor.horizon_id must match the outcome horizon")
        embedded_snapshot_id = _embedded_graph_snapshot_id(observation)
        if embedded_snapshot_id != self.snapshot.snapshot_id:
            raise ValueError("embedded graph snapshot_id must equal snapshot.snapshot_id")
        if self.snapshot.root_episode_id == self.episode.episode_id:
            raise ValueError("snapshot.root_episode_id is the market episode, not the graph companion")
        if self.outcome.episode_id != self.episode.episode_id:
            raise ValueError("outcome.episode_id must match the graph episode")
        bindings = _binding_tuple(self.driver_state_bindings)
        required = _admitted_overlay_relation_ids(knowledge, self.snapshot)
        bound = {item.relation_id for item in bindings}
        if bound != required:
            raise ValueError("admitted OBSERVES/ABOUT relations require exactly one DriverState binding")
        object.__setattr__(self, "structural_relations", structural)
        object.__setattr__(self, "knowledge_relations", knowledge)
        object.__setattr__(self, "recorded_at", recorded_at)
        object.__setattr__(self, "available_at", available_at)
        object.__setattr__(self, "market_anchor", anchor)
        object.__setattr__(self, "driver_state_bindings", bindings)

    @property
    def driver_state_by_relation_id(self) -> Mapping[str, PatternDriverStateBinding]:
        return MappingProxyType({item.relation_id: item for item in self.driver_state_bindings})


@dataclass(frozen=True)
class PatternFormationBatch:
    """Source-owned eligible records plus rejection and evidence accounting."""

    records: Sequence[PatternFormationRecord]
    rejection_counts: Mapping[str, int]
    source_evidence_ids: Sequence[str]

    def __post_init__(self) -> None:
        records = _typed_tuple(self.records, PatternFormationRecord, "records")
        object.__setattr__(self, "records", records)
        object.__setattr__(self, "rejection_counts", _count_mapping(self.rejection_counts, "rejection_counts"))
        object.__setattr__(self, "source_evidence_ids", _unique_text_tuple(self.source_evidence_ids, "source_evidence_ids"))


class PatternFormationSource(Protocol):
    """Loads typed eligible formation records already bounded by the as-of query."""

    def load_formation_batch(self, request: PatternFormationRequest) -> PatternFormationBatch:
        """Return eligible records, source rejections, and evidence identities."""


__all__ = (
    "PatternDriverStateBinding",
    "PatternFormationBatch",
    "PatternFormationRecord",
    "PatternFormationSource",
    "TimeSpecificMarketAnchor",
)
