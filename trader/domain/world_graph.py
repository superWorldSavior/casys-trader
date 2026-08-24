"""Typed V3 temporal graph records: namespaced entities, relations, identity map, revisions.

Stdlib-only. Structural and knowledge overlays are distinct; generic ``CAUSES``
edges are forbidden. Availability is store-assigned and is never a caller field
on a relation. The graph library must not be imported here.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any

from trader.domain.world_availability import (
    AvailabilityEvidence,
    PointInTimeEligibility,
    PointInTimeEligibilityPolicy,
)
from trader.domain.world_context import EntityRef
from trader.domain.world_episode import canonical_payload, canonical_sha256, parse_utc_timestamp
from trader.domain.world_macro import (
    MACRO_PRODUCER_VERSION,
    MacroObservationEnvelope,
    MacroObservationProvenance,
    MacroScope,
    MacroSourceFactVersionId,
    is_admitted_macro_producer,
    macro_observes_producer_ref,
)
from trader.domain.world_scope import (
    SCOPE_KINDS,
    WorldCanonicalScopeRef,
    WorldScopeMapping,
    WorldScopeResolution,
)


WORLD_ENTITY_KINDS = frozenset(
    {
        "world",
        "region",
        "country",
        "venue",
        "family",
        "company",
        "instrument",
        "macro_indicator",
        "event",
    }
)
STRUCTURAL_RELATION_KINDS = frozenset(
    {
        "PART_OF_WORLD",
        "LOCATED_IN",
        "TRADED_ON",
        "ISSUED_BY",
        "MEMBER_OF_FAMILY",
    }
)
KNOWLEDGE_RELATION_KINDS = frozenset(
    {
        "ABOUT",
        "OBSERVES",
        "DERIVED_FROM",
        "SUPERSEDES",
        "USES",
    }
)
FORBIDDEN_RELATION_KINDS = frozenset(
    {
        "CAUSES",
        "CAUSE",
        "CAUSED_BY",
        "CAUSAL",
        "HYPOTHESIZED_INFLUENCE",
        "HYPOTHESIZEDINFLUENCE",
    }
)
SNAPSHOT_STATUSES = frozenset({"complete", "partial", "missing", "stale"})
IDENTITY_MAPPABLE_V2_KINDS = frozenset({"world", "region", "country", "venue", "family", "company", "instrument"})
_COMPANY_PREFIXES = ("lei:", "cik:", "issuer:")
_INSTRUMENT_ID_RE = re.compile(r"^mic:[A-Z0-9]{4}:symbol:.+$")
_LOCATED_IN_PAIRS = frozenset({("country", "region"), ("venue", "country"), ("venue", "region")})
_STRUCTURAL_ENDPOINTS = MappingProxyType(
    {
        "PART_OF_WORLD": (frozenset({"region", "country", "venue"}), frozenset({"world"})),
        "TRADED_ON": (frozenset({"instrument"}), frozenset({"venue"})),
        "ISSUED_BY": (frozenset({"instrument"}), frozenset({"company"})),
        "MEMBER_OF_FAMILY": (frozenset({"instrument"}), frozenset({"family"})),
    }
)

WORLD_RELATION_SCHEMA = "world_relation.v1"
WORLD_ENTITY_EVENT_SCHEMA = "world_entity_event.v1"
WORLD_IDENTITY_EVENT_SCHEMA = "world_entity_identity_event.v1"
WORLD_RELATION_EVENT_SCHEMA = "world_relation_event.v1"
WORLD_REVISION_SCHEMA = "world_ontology_revision.v1"
WORLD_REVISION_EVENT_SCHEMA = "world_ontology_revision_event.v1"
WORLD_IDENTITY_LINK_SCHEMA = "world_entity_identity_link.v1"
WORLD_IDENTITY_MAP_SCHEMA = "world_entity_identity_map.v1"
WORLD_GRAPH_SNAPSHOT_SCHEMA = "world_graph_snapshot.v1"
GRAPH_TRAVERSAL_POLICY_VERSION = "graph_traversal.v1"

_RELATION_ID_PREFIX = "world_relation:v1"
_LINK_ID_PREFIX = "world_entity_identity_link:v1"
_ENTITY_EVENT_PREFIX = "world_entity_event:v1"
_IDENTITY_EVENT_PREFIX = "world_entity_identity_event:v1"
_RELATION_EVENT_PREFIX = "world_relation_event:v1"
_REVISION_EVENT_PREFIX = "world_ontology_revision_event:v1"
_SNAPSHOT_ID_PREFIX = "world_graph_snapshot:v1"
_OBSERVATION_REF_PREFIX = "world_observation:v1"
_ARTIFACT_REF_PREFIX = "knowledge_artifact:v1"
_HYPOTHESIS_REF_PREFIX = "pattern_hypothesis:v1"
_EPISODE_ID_PREFIX = "world-episode:v1"
_RECEIPT_ID_PREFIX = "world-availability-receipt:v1"
_MACRO_OBSERVATION_ID_PREFIX = "macro_world_observation:v1"
_BRIDGE_EVENT_PREFIX = "macro_graph_bridge_event:v1"
_BRIDGE_RUN_PREFIX = "macro_graph_bridge_run:v1"
_RESERVATION_PREFIX = "macro_observation_cursor_reservation:v1"
_REQUEST_ID_PREFIX = "macro_graph_bridge_request:v1"
MACRO_GRAPH_BRIDGE_EVENT_SCHEMA = "macro_graph_bridge_event.v1"
MACRO_OBSERVATION_CURSOR_SCHEMA = "macro_observation_cursor.v1"
MACRO_OBSERVATION_CURSOR_RESERVATION_SCHEMA = "macro_observation_cursor_reservation.v1"
MACRO_GRAPH_BRIDGE_RUN_SPEC_SCHEMA = "macro_graph_bridge_run_spec.v1"
_COLLECTION_PLAN_ID_PREFIX = "macro_collection_plan:v1"
MACRO_GRAPH_SKIP_REASONS = frozenset({"scope_not_registered", "invalid_envelope", "producer_not_admitted"})
MACRO_GRAPH_BLOCK_REASONS = frozenset({"config_drift"})
MACRO_GRAPH_RUN_STATUSES = frozenset({"active", "blocked", "superseded"})
MACRO_GRAPH_LINK_STATUSES = frozenset({"linked", "skipped"})


def _required_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a non-empty string")
    text = value.strip()
    if not text:
        raise ValueError(f"{field_name} must be a non-empty string")
    return text


def _required_mapping_text(value: Mapping[str, Any], key: str) -> str:
    if key not in value or value[key] is None:
        raise ValueError(f"{key} is required")
    return _required_text(value[key], key)


def _optional_utc(value: datetime | str | None, field_name: str) -> datetime | None:
    return None if value is None else parse_utc_timestamp(value, field_name)


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.astimezone(timezone.utc).isoformat()


def _non_negative_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field_name} must be an integer")
    if value < 0:
        raise ValueError(f"{field_name} must be non-negative")
    return value


def _positive_int(value: Any, field_name: str) -> int:
    number = _non_negative_int(value, field_name)
    if number < 1:
        raise ValueError(f"{field_name} must be >= 1")
    return number


def _sha256_hex(value: Any, field_name: str) -> str:
    text = _required_text(value, field_name)
    if len(text) != 64 or any(char not in "0123456789abcdef" for char in text):
        raise ValueError(f"{field_name} must be a sha256 hex digest")
    return text


def _validate_prefixed_id(value: Any, prefix: str, field_name: str) -> str:
    text = _required_text(value, field_name)
    expected = f"{prefix}:"
    if not text.startswith(expected):
        raise ValueError(f"{field_name} must start with '{expected}'")
    _sha256_hex(text[len(expected) :], field_name)
    return text


def _prefixed_id(prefix: str, payload: Any) -> str:
    return f"{prefix}:{canonical_sha256(payload)}"


def _immutable_text_tuple(value: Sequence[str] | None, field_name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise TypeError(f"{field_name} must be a sequence of strings")
    return tuple(_required_text(item, f"{field_name}[]") for item in value)


def _require_source_refs(value: Sequence[str] | None, field_name: str = "source_refs") -> tuple[str, ...]:
    items = _immutable_text_tuple(value, field_name)
    if not items:
        raise ValueError(f"{field_name} must not be empty")
    return items


def _mapping_proxy_text(value: Mapping[str, Any] | None, field_name: str) -> Mapping[str, str]:
    if value is None:
        return MappingProxyType({})
    if not isinstance(value, Mapping):
        raise TypeError(f"{field_name} must be a mapping")
    result: dict[str, str] = {}
    for raw_key, raw_value in value.items():
        key = _required_text(raw_key, f"{field_name} key")
        result[key] = _required_text(raw_value, f"{field_name}.{key}")
    return MappingProxyType(dict(sorted(result.items())))


def _deep_freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _deep_freeze(nested) for key, nested in value.items()})
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return tuple(_deep_freeze(item) for item in value)
    return value


def _deep_thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _deep_thaw(nested) for key, nested in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_deep_thaw(item) for item in value]
    return value


def _freeze_mapping(value: Mapping[str, Any] | None, field_name: str) -> Mapping[str, Any]:
    if value is None:
        return MappingProxyType({})
    if not isinstance(value, Mapping):
        raise TypeError(f"{field_name} must be a mapping")
    return _deep_freeze(canonical_payload(value))


def _intervals_overlap(
    left_from: datetime,
    left_until: datetime | None,
    right_from: datetime,
    right_until: datetime | None,
) -> bool:
    if left_until is not None and left_until <= right_from:
        return False
    if right_until is not None and right_until <= left_from:
        return False
    return True


def _effective_at(cutoff: datetime, *, effective_from: datetime, effective_until: datetime | None) -> bool:
    if effective_from > cutoff:
        return False
    if effective_until is not None and cutoff >= effective_until:
        return False
    return True


def _reject_forbidden_relation_kind(kind: str) -> None:
    compact = kind.upper().replace("-", "_")
    if compact in FORBIDDEN_RELATION_KINDS or "CAUS" in compact:
        raise ValueError("generic CAUSES edges are forbidden; the ontology is not a causal graph")


def parse_structural_relation_kind(value: Any) -> str:
    kind = _required_text(value, "kind").upper()
    _reject_forbidden_relation_kind(kind)
    if kind in KNOWLEDGE_RELATION_KINDS:
        raise ValueError(f"{kind} is a knowledge relation kind, not a structural topology edge")
    if kind not in STRUCTURAL_RELATION_KINDS:
        allowed = ", ".join(sorted(STRUCTURAL_RELATION_KINDS))
        raise ValueError(f"structural relation kind must be one of: {allowed}")
    return kind


def parse_knowledge_relation_kind(value: Any) -> str:
    kind = _required_text(value, "kind").upper()
    _reject_forbidden_relation_kind(kind)
    if kind in STRUCTURAL_RELATION_KINDS:
        raise ValueError(f"{kind} is a structural relation kind, not a knowledge overlay")
    if kind not in KNOWLEDGE_RELATION_KINDS:
        allowed = ", ".join(sorted(KNOWLEDGE_RELATION_KINDS))
        raise ValueError(f"knowledge relation kind must be one of: {allowed}")
    return kind


def evaluate_world_graph_point_in_time(
    *,
    evidence: AvailabilityEvidence | None,
    cutoff_at: datetime | str,
    effective_from: datetime | str,
    valid_until: datetime | str | None = None,
    superseded: bool = False,
    version: str | None = None,
    admitted_versions: frozenset[str] | None = None,
):
    """Domain PIT gate: ready evidence, then cutoff in ``[effective_from, valid_until)``."""

    cutoff = parse_utc_timestamp(cutoff_at, "cutoff_at")
    start = parse_utc_timestamp(effective_from, "effective_from")
    result = PointInTimeEligibilityPolicy(admitted_versions=admitted_versions).evaluate(
        evidence=evidence,
        cutoff_at=cutoff,
        valid_until=valid_until,
        superseded=superseded,
        version=version,
    )
    if result.status != "eligible":
        return result
    until = None if valid_until is None else parse_utc_timestamp(valid_until, "valid_until")
    if not _effective_at(cutoff, effective_from=start, effective_until=until):
        return PointInTimeEligibility(status="stale", effective_ready_at=result.effective_ready_at)
    return result


@dataclass(frozen=True)
class WorldEntityRef:
    """Namespaced V3 world-entity identity. Not a knowledge overlay node."""

    kind: str
    entity_id: str

    def __post_init__(self) -> None:
        kind = _required_text(self.kind, "kind").lower()
        if kind not in WORLD_ENTITY_KINDS:
            allowed = ", ".join(sorted(WORLD_ENTITY_KINDS))
            raise ValueError(f"entity kind must be one of: {allowed}")
        entity_id = _required_text(self.entity_id, "entity_id")
        if kind in SCOPE_KINDS:
            canonical = WorldCanonicalScopeRef(kind=kind, entity_id=entity_id)
            entity_id = canonical.entity_id
        elif kind == "instrument":
            if not _INSTRUMENT_ID_RE.fullmatch(entity_id):
                raise ValueError("instrument entity_id must be namespaced as mic:<MIC>:symbol:<symbol>")
        elif kind == "family":
            if not entity_id.startswith("taxonomy:"):
                raise ValueError("family entity_id must start with 'taxonomy:'")
        elif kind == "company":
            if entity_id.startswith("isin:") or entity_id.startswith("ISIN:"):
                raise ValueError("ISIN identifies an instrument, never a company")
            if not entity_id.startswith(_COMPANY_PREFIXES):
                raise ValueError("company entity_id must start with lei:, cik:, or issuer:")
        elif kind in {"macro_indicator", "event"}:
            if ":" not in entity_id:
                raise ValueError(f"{kind} entity_id must be namespaced")
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "entity_id", entity_id)

    @property
    def node_kind(self) -> str:
        return "world_entity"

    @property
    def node_id(self) -> str:
        return f"{self.kind}:{self.entity_id}"

    def to_dict(self) -> dict[str, str]:
        return {"node_kind": "world_entity", "kind": self.kind, "entity_id": self.entity_id}

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | WorldEntityRef) -> WorldEntityRef:
        if isinstance(value, WorldEntityRef):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("entity ref must be WorldEntityRef or a mapping")
        node_kind = value.get("node_kind")
        if node_kind not in (None, "", "world_entity"):
            raise ValueError("entity ref node_kind must be world_entity")
        return cls(kind=value.get("kind"), entity_id=value.get("entity_id") or value.get("id"))


@dataclass(frozen=True)
class SensorRef:
    sensor_id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "sensor_id", _required_text(self.sensor_id, "sensor_id"))

    @property
    def node_kind(self) -> str:
        return "sensor"

    @property
    def node_id(self) -> str:
        return f"sensor:{self.sensor_id}"

    def to_dict(self) -> dict[str, str]:
        return {"node_kind": "sensor", "sensor_id": self.sensor_id}

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | SensorRef) -> SensorRef:
        if isinstance(value, SensorRef):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("sensor ref must be SensorRef or a mapping")
        return cls(sensor_id=value.get("sensor_id"))


@dataclass(frozen=True)
class KnowledgeArtifactRef:
    artifact_id: str
    content_sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "artifact_id",
            _validate_prefixed_id(self.artifact_id, _ARTIFACT_REF_PREFIX, "artifact_id"),
        )
        object.__setattr__(self, "content_sha256", _sha256_hex(self.content_sha256, "content_sha256"))

    @property
    def node_kind(self) -> str:
        return "knowledge_artifact"

    def to_dict(self) -> dict[str, str]:
        return {
            "node_kind": "knowledge_artifact",
            "artifact_id": self.artifact_id,
            "content_sha256": self.content_sha256,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | KnowledgeArtifactRef) -> KnowledgeArtifactRef:
        if isinstance(value, KnowledgeArtifactRef):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("artifact ref must be KnowledgeArtifactRef or a mapping")
        return cls(artifact_id=value.get("artifact_id"), content_sha256=value.get("content_sha256"))


@dataclass(frozen=True)
class WorldObservationRef:
    observation_id: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "observation_id",
            _validate_prefixed_id(self.observation_id, _OBSERVATION_REF_PREFIX, "observation_id"),
        )

    @property
    def node_kind(self) -> str:
        return "world_observation"

    def to_dict(self) -> dict[str, str]:
        return {"node_kind": "world_observation", "observation_id": self.observation_id}

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | WorldObservationRef) -> WorldObservationRef:
        if isinstance(value, WorldObservationRef):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("observation ref must be WorldObservationRef or a mapping")
        return cls(observation_id=value.get("observation_id"))


@dataclass(frozen=True)
class WorldGraphSnapshotRef:
    snapshot_id: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "snapshot_id",
            _validate_prefixed_id(self.snapshot_id, _SNAPSHOT_ID_PREFIX, "snapshot_id"),
        )

    @property
    def node_kind(self) -> str:
        return "world_graph_snapshot"

    def to_dict(self) -> dict[str, str]:
        return {"node_kind": "world_graph_snapshot", "snapshot_id": self.snapshot_id}

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | WorldGraphSnapshotRef) -> WorldGraphSnapshotRef:
        if isinstance(value, WorldGraphSnapshotRef):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("snapshot ref must be WorldGraphSnapshotRef or a mapping")
        return cls(snapshot_id=value.get("snapshot_id"))


@dataclass(frozen=True)
class PatternHypothesisRef:
    hypothesis_id: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "hypothesis_id",
            _validate_prefixed_id(self.hypothesis_id, _HYPOTHESIS_REF_PREFIX, "hypothesis_id"),
        )

    @property
    def node_kind(self) -> str:
        return "pattern_hypothesis"

    def to_dict(self) -> dict[str, str]:
        return {"node_kind": "pattern_hypothesis", "hypothesis_id": self.hypothesis_id}

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | PatternHypothesisRef) -> PatternHypothesisRef:
        if isinstance(value, PatternHypothesisRef):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("hypothesis ref must be PatternHypothesisRef or a mapping")
        return cls(hypothesis_id=value.get("hypothesis_id"))


@dataclass(frozen=True)
class MacroSourceFactVersionRef:
    fact_version_id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "fact_version_id", MacroSourceFactVersionId(self.fact_version_id).value)

    @property
    def node_kind(self) -> str:
        return "macro_source_fact_version"

    def to_dict(self) -> dict[str, str]:
        return {"node_kind": "macro_source_fact_version", "fact_version_id": self.fact_version_id}

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | MacroSourceFactVersionRef) -> MacroSourceFactVersionRef:
        if isinstance(value, MacroSourceFactVersionRef):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("fact version ref must be MacroSourceFactVersionRef or a mapping")
        return cls(fact_version_id=value.get("fact_version_id"))


WorldGraphNodeRef = (
    WorldEntityRef
    | SensorRef
    | KnowledgeArtifactRef
    | WorldObservationRef
    | WorldGraphSnapshotRef
    | PatternHypothesisRef
    | MacroSourceFactVersionRef
)

_NODE_REF_CLASSES = (
    WorldEntityRef,
    SensorRef,
    KnowledgeArtifactRef,
    WorldObservationRef,
    WorldGraphSnapshotRef,
    PatternHypothesisRef,
    MacroSourceFactVersionRef,
)
_NODE_PARSERS = {
    "world_entity": WorldEntityRef.from_mapping,
    "sensor": SensorRef.from_mapping,
    "knowledge_artifact": KnowledgeArtifactRef.from_mapping,
    "world_observation": WorldObservationRef.from_mapping,
    "world_graph_snapshot": WorldGraphSnapshotRef.from_mapping,
    "pattern_hypothesis": PatternHypothesisRef.from_mapping,
    "macro_source_fact_version": MacroSourceFactVersionRef.from_mapping,
}


def parse_world_graph_node_ref(value: Mapping[str, Any] | WorldGraphNodeRef) -> WorldGraphNodeRef:
    if isinstance(value, _NODE_REF_CLASSES):
        return value
    if not isinstance(value, Mapping):
        raise TypeError("graph node ref must be a mapping or typed ref")
    node_kind = value.get("node_kind")
    if node_kind in (None, "") and value.get("kind") in WORLD_ENTITY_KINDS:
        return WorldEntityRef.from_mapping(value)
    try:
        parser = _NODE_PARSERS[_required_text(node_kind, "node_kind")]
    except KeyError as exc:
        raise ValueError(f"unknown graph node_kind: {node_kind}") from exc
    return parser(value)


def _require_node_types(node: WorldGraphNodeRef, allowed: tuple[type, ...], *, role: str, kind: str) -> None:
    if not isinstance(node, allowed):
        names = ", ".join(item.__name__ for item in allowed)
        raise ValueError(f"{kind} {role} must be {names}")


def _validate_knowledge_endpoints(kind: str, source: WorldGraphNodeRef, target: WorldGraphNodeRef) -> None:
    if kind == "ABOUT":
        _require_node_types(source, (KnowledgeArtifactRef,), role="source", kind=kind)
        _require_node_types(target, (WorldEntityRef,), role="target", kind=kind)
        return
    if kind == "OBSERVES":
        _require_node_types(source, (WorldObservationRef,), role="source", kind=kind)
        _require_node_types(target, (WorldEntityRef,), role="target", kind=kind)
        return
    if kind == "DERIVED_FROM":
        _require_node_types(source, (KnowledgeArtifactRef, WorldObservationRef), role="source", kind=kind)
        _require_node_types(target, (KnowledgeArtifactRef, MacroSourceFactVersionRef), role="target", kind=kind)
        return
    if kind == "USES":
        _require_node_types(source, (WorldGraphSnapshotRef, PatternHypothesisRef), role="source", kind=kind)
        _require_node_types(target, (KnowledgeArtifactRef, WorldObservationRef), role="target", kind=kind)
        return
    if kind == "SUPERSEDES":
        version_types = (
            KnowledgeArtifactRef,
            WorldObservationRef,
            MacroSourceFactVersionRef,
            WorldGraphSnapshotRef,
            PatternHypothesisRef,
        )
        _require_node_types(source, version_types, role="source", kind=kind)
        _require_node_types(target, version_types, role="target", kind=kind)
        if type(source) is not type(target):
            raise ValueError("SUPERSEDES source and target must be the same version ref type")
        return
    raise ValueError(f"unsupported knowledge relation kind: {kind}")


def _validate_structural_endpoints(kind: str, source: WorldEntityRef, target: WorldEntityRef) -> None:
    if kind == "LOCATED_IN":
        if (source.kind, target.kind) not in _LOCATED_IN_PAIRS:
            raise ValueError("LOCATED_IN source/target kinds are not an allowed pair")
        return
    allowed = _STRUCTURAL_ENDPOINTS.get(kind)
    if allowed is None:
        raise ValueError(f"unsupported structural relation kind: {kind}")
    sources, targets = allowed
    if source.kind not in sources or target.kind not in targets:
        raise ValueError(f"{kind} source/target kinds are not allowed")


def _relation_identity_payload(
    *,
    kind: str,
    source: Mapping[str, Any],
    target: Mapping[str, Any],
    effective_from: datetime,
    effective_until: datetime | None,
    ontology_revision: str,
    source_refs: Sequence[str],
    supersedes: str | None,
) -> dict[str, Any]:
    return {
        "schema_version": WORLD_RELATION_SCHEMA,
        "kind": kind,
        "source": dict(source),
        "target": dict(target),
        "effective_from": _iso(effective_from),
        "effective_until": _iso(effective_until),
        "ontology_revision": ontology_revision,
        "source_refs": list(source_refs),
        "supersedes": supersedes,
    }


@dataclass(frozen=True)
class StructuralWorldRelation:
    """Factual topology edge between two WorldEntityRef nodes."""

    kind: str
    source: WorldEntityRef | Mapping[str, Any]
    target: WorldEntityRef | Mapping[str, Any]
    effective_from: datetime | str
    ontology_revision: str
    source_refs: Sequence[str]
    effective_until: datetime | str | None = None
    supersedes: str | None = None
    schema_version: str = WORLD_RELATION_SCHEMA
    relation_id: str | None = None
    content_sha256: str | None = None

    def __post_init__(self) -> None:
        schema_version = _required_text(self.schema_version, "schema_version")
        if schema_version != WORLD_RELATION_SCHEMA:
            raise ValueError(f"schema_version must be {WORLD_RELATION_SCHEMA}")
        kind = parse_structural_relation_kind(self.kind)
        source = WorldEntityRef.from_mapping(self.source)
        target = WorldEntityRef.from_mapping(self.target)
        _validate_structural_endpoints(kind, source, target)
        effective_from = parse_utc_timestamp(self.effective_from, "effective_from")
        effective_until = _optional_utc(self.effective_until, "effective_until")
        if effective_until is not None and effective_until < effective_from:
            raise ValueError("effective_until must not precede effective_from")
        ontology_revision = _required_text(self.ontology_revision, "ontology_revision")
        source_refs = _require_source_refs(self.source_refs)
        supersedes = (
            None
            if self.supersedes is None
            else _validate_prefixed_id(self.supersedes, _RELATION_ID_PREFIX, "supersedes")
        )
        identity = _relation_identity_payload(
            kind=kind,
            source=source.to_dict(),
            target=target.to_dict(),
            effective_from=effective_from,
            effective_until=effective_until,
            ontology_revision=ontology_revision,
            source_refs=source_refs,
            supersedes=supersedes,
        )
        relation_id = _prefixed_id(_RELATION_ID_PREFIX, identity)
        if self.relation_id is not None and _required_text(self.relation_id, "relation_id") != relation_id:
            raise ValueError("relation_id does not match the canonical structural relation")
        digest = canonical_sha256({**identity, "relation_id": relation_id})
        if self.content_sha256 is not None and _required_text(self.content_sha256, "content_sha256") != digest:
            raise ValueError("content_sha256 does not match the canonical structural relation")
        object.__setattr__(self, "schema_version", schema_version)
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "source", source)
        object.__setattr__(self, "target", target)
        object.__setattr__(self, "effective_from", effective_from)
        object.__setattr__(self, "effective_until", effective_until)
        object.__setattr__(self, "ontology_revision", ontology_revision)
        object.__setattr__(self, "source_refs", source_refs)
        object.__setattr__(self, "supersedes", supersedes)
        object.__setattr__(self, "relation_id", relation_id)
        object.__setattr__(self, "content_sha256", digest)

    @property
    def family(self) -> str:
        return "structural"

    def effective_at(self, cutoff_at: datetime | str) -> bool:
        cutoff = parse_utc_timestamp(cutoff_at, "cutoff_at")
        return _effective_at(cutoff, effective_from=self.effective_from, effective_until=self.effective_until)

    def corrected(
        self,
        *,
        effective_from: datetime | str,
        source_refs: Sequence[str],
        source: WorldEntityRef | Mapping[str, Any] | None = None,
        target: WorldEntityRef | Mapping[str, Any] | None = None,
        effective_until: datetime | str | None = None,
        ontology_revision: str | None = None,
    ) -> StructuralWorldRelation:
        return StructuralWorldRelation(
            kind=self.kind,
            source=self.source if source is None else source,
            target=self.target if target is None else target,
            effective_from=effective_from,
            effective_until=effective_until,
            ontology_revision=self.ontology_revision if ontology_revision is None else ontology_revision,
            source_refs=source_refs,
            supersedes=self.relation_id,
        )

    def to_dict(self) -> dict[str, Any]:
        payload = _relation_identity_payload(
            kind=self.kind,
            source=self.source.to_dict(),
            target=self.target.to_dict(),
            effective_from=self.effective_from,
            effective_until=self.effective_until,
            ontology_revision=self.ontology_revision,
            source_refs=self.source_refs,
            supersedes=self.supersedes,
        )
        payload["relation_id"] = self.relation_id
        payload["content_sha256"] = self.content_sha256
        payload["family"] = self.family
        return payload

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | StructuralWorldRelation) -> StructuralWorldRelation:
        if isinstance(value, StructuralWorldRelation):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("structural relation must be StructuralWorldRelation or a mapping")
        return cls(
            kind=value.get("kind"),
            source=value.get("source"),
            target=value.get("target"),
            effective_from=value.get("effective_from"),
            ontology_revision=value.get("ontology_revision"),
            source_refs=value.get("source_refs") or (),
            effective_until=value.get("effective_until"),
            supersedes=value.get("supersedes"),
            schema_version=value.get("schema_version", WORLD_RELATION_SCHEMA),
            relation_id=value.get("relation_id"),
            content_sha256=value.get("content_sha256"),
        )


@dataclass(frozen=True)
class KnowledgeWorldRelation:
    """Knowledge overlay edge. Never a structural topology head."""

    kind: str
    source: WorldGraphNodeRef | Mapping[str, Any]
    target: WorldGraphNodeRef | Mapping[str, Any]
    effective_from: datetime | str
    ontology_revision: str
    source_refs: Sequence[str]
    effective_until: datetime | str | None = None
    supersedes: str | None = None
    schema_version: str = WORLD_RELATION_SCHEMA
    relation_id: str | None = None
    content_sha256: str | None = None

    def __post_init__(self) -> None:
        schema_version = _required_text(self.schema_version, "schema_version")
        if schema_version != WORLD_RELATION_SCHEMA:
            raise ValueError(f"schema_version must be {WORLD_RELATION_SCHEMA}")
        kind = parse_knowledge_relation_kind(self.kind)
        source = parse_world_graph_node_ref(self.source)
        target = parse_world_graph_node_ref(self.target)
        _validate_knowledge_endpoints(kind, source, target)
        effective_from = parse_utc_timestamp(self.effective_from, "effective_from")
        effective_until = _optional_utc(self.effective_until, "effective_until")
        if effective_until is not None and effective_until < effective_from:
            raise ValueError("effective_until must not precede effective_from")
        ontology_revision = _required_text(self.ontology_revision, "ontology_revision")
        source_refs = _require_source_refs(self.source_refs)
        supersedes = (
            None
            if self.supersedes is None
            else _validate_prefixed_id(self.supersedes, _RELATION_ID_PREFIX, "supersedes")
        )
        identity = _relation_identity_payload(
            kind=kind,
            source=source.to_dict(),
            target=target.to_dict(),
            effective_from=effective_from,
            effective_until=effective_until,
            ontology_revision=ontology_revision,
            source_refs=source_refs,
            supersedes=supersedes,
        )
        relation_id = _prefixed_id(_RELATION_ID_PREFIX, identity)
        if self.relation_id is not None and _required_text(self.relation_id, "relation_id") != relation_id:
            raise ValueError("relation_id does not match the canonical knowledge relation")
        digest = canonical_sha256({**identity, "relation_id": relation_id})
        if self.content_sha256 is not None and _required_text(self.content_sha256, "content_sha256") != digest:
            raise ValueError("content_sha256 does not match the canonical knowledge relation")
        object.__setattr__(self, "schema_version", schema_version)
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "source", source)
        object.__setattr__(self, "target", target)
        object.__setattr__(self, "effective_from", effective_from)
        object.__setattr__(self, "effective_until", effective_until)
        object.__setattr__(self, "ontology_revision", ontology_revision)
        object.__setattr__(self, "source_refs", source_refs)
        object.__setattr__(self, "supersedes", supersedes)
        object.__setattr__(self, "relation_id", relation_id)
        object.__setattr__(self, "content_sha256", digest)

    @property
    def family(self) -> str:
        return "knowledge"

    def effective_at(self, cutoff_at: datetime | str) -> bool:
        cutoff = parse_utc_timestamp(cutoff_at, "cutoff_at")
        return _effective_at(cutoff, effective_from=self.effective_from, effective_until=self.effective_until)

    def to_dict(self) -> dict[str, Any]:
        payload = _relation_identity_payload(
            kind=self.kind,
            source=self.source.to_dict(),
            target=self.target.to_dict(),
            effective_from=self.effective_from,
            effective_until=self.effective_until,
            ontology_revision=self.ontology_revision,
            source_refs=self.source_refs,
            supersedes=self.supersedes,
        )
        payload["relation_id"] = self.relation_id
        payload["content_sha256"] = self.content_sha256
        payload["family"] = self.family
        return payload

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | KnowledgeWorldRelation) -> KnowledgeWorldRelation:
        if isinstance(value, KnowledgeWorldRelation):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("knowledge relation must be KnowledgeWorldRelation or a mapping")
        return cls(
            kind=value.get("kind"),
            source=value.get("source"),
            target=value.get("target"),
            effective_from=value.get("effective_from"),
            ontology_revision=value.get("ontology_revision"),
            source_refs=value.get("source_refs") or (),
            effective_until=value.get("effective_until"),
            supersedes=value.get("supersedes"),
            schema_version=value.get("schema_version", WORLD_RELATION_SCHEMA),
            relation_id=value.get("relation_id"),
            content_sha256=value.get("content_sha256"),
        )


WorldRelation = StructuralWorldRelation | KnowledgeWorldRelation


@dataclass(frozen=True)
class WorldStructuralRelationRef:
    relation_id: str
    content_sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "relation_id",
            _validate_prefixed_id(self.relation_id, _RELATION_ID_PREFIX, "relation_id"),
        )
        object.__setattr__(self, "content_sha256", _sha256_hex(self.content_sha256, "content_sha256"))

    def to_dict(self) -> dict[str, str]:
        return {"relation_id": self.relation_id, "content_sha256": self.content_sha256}

    @classmethod
    def from_relation(cls, relation: StructuralWorldRelation) -> WorldStructuralRelationRef:
        if not isinstance(relation, StructuralWorldRelation):
            raise TypeError("structural relation ref requires StructuralWorldRelation")
        return cls(relation_id=relation.relation_id, content_sha256=relation.content_sha256)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | WorldStructuralRelationRef) -> WorldStructuralRelationRef:
        if isinstance(value, WorldStructuralRelationRef):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("structural relation ref must be a mapping")
        return cls(relation_id=value.get("relation_id"), content_sha256=value.get("content_sha256"))


@dataclass(frozen=True)
class WorldKnowledgeRelationRef:
    relation_id: str
    content_sha256: str
    ontology_revision: str
    availability_receipt_id: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "relation_id",
            _validate_prefixed_id(self.relation_id, _RELATION_ID_PREFIX, "relation_id"),
        )
        object.__setattr__(self, "content_sha256", _sha256_hex(self.content_sha256, "content_sha256"))
        object.__setattr__(self, "ontology_revision", _required_text(self.ontology_revision, "ontology_revision"))
        object.__setattr__(
            self,
            "availability_receipt_id",
            _validate_prefixed_id(self.availability_receipt_id, _RECEIPT_ID_PREFIX, "availability_receipt_id"),
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "relation_id": self.relation_id,
            "content_sha256": self.content_sha256,
            "ontology_revision": self.ontology_revision,
            "availability_receipt_id": self.availability_receipt_id,
        }

    @classmethod
    def from_relation(
        cls,
        relation: KnowledgeWorldRelation,
        *,
        availability_receipt_id: str,
    ) -> WorldKnowledgeRelationRef:
        if not isinstance(relation, KnowledgeWorldRelation):
            raise TypeError("knowledge relation ref requires KnowledgeWorldRelation")
        return cls(
            relation_id=relation.relation_id,
            content_sha256=relation.content_sha256,
            ontology_revision=relation.ontology_revision,
            availability_receipt_id=availability_receipt_id,
        )

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | WorldKnowledgeRelationRef) -> WorldKnowledgeRelationRef:
        if isinstance(value, WorldKnowledgeRelationRef):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("knowledge relation ref must be a mapping")
        return cls(
            relation_id=value.get("relation_id"),
            content_sha256=value.get("content_sha256"),
            ontology_revision=value.get("ontology_revision"),
            availability_receipt_id=value.get("availability_receipt_id"),
        )


@dataclass(frozen=True)
class WorldEntityRevisionRef:
    """Entity identity plus the hashed assertion that was valid at a cutoff."""

    entity: WorldEntityRef | Mapping[str, Any]
    content_sha256: str

    def __post_init__(self) -> None:
        entity = WorldEntityRef.from_mapping(self.entity)
        object.__setattr__(self, "entity", entity)
        object.__setattr__(self, "content_sha256", _sha256_hex(self.content_sha256, "content_sha256"))

    def to_dict(self) -> dict[str, Any]:
        return {"entity": self.entity.to_dict(), "content_sha256": self.content_sha256}

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | WorldEntityRevisionRef) -> WorldEntityRevisionRef:
        if isinstance(value, WorldEntityRevisionRef):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("entity revision ref must be an identity/hash mapping, not a bare string")
        if "content_sha256" not in value:
            raise ValueError("entity revision ref requires identity and content hash")
        return cls(entity=value.get("entity") or value, content_sha256=value.get("content_sha256"))


def _set_event_id(event: Any, prefix: str, payload: Mapping[str, Any], *, schema_version: str) -> None:
    provided_schema = _required_text(getattr(event, "schema_version"), "schema_version")
    if provided_schema != schema_version:
        raise ValueError(f"schema_version must be {schema_version}")
    computed = _prefixed_id(prefix, payload)
    provided = getattr(event, "event_id")
    if provided is not None and _required_text(provided, "event_id") != computed:
        raise ValueError("event_id does not match the canonical event identity")
    object.__setattr__(event, "schema_version", provided_schema)
    object.__setattr__(event, "event_id", computed)


@dataclass(frozen=True)
class WorldEntityAsserted:
    entity: WorldEntityRef | Mapping[str, Any]
    source_refs: Sequence[str]
    effective_from: datetime | str
    effective_until: datetime | str | None = None
    event_id: str | None = None
    schema_version: str = WORLD_ENTITY_EVENT_SCHEMA
    event_type: str = "world_entity_asserted"

    def __post_init__(self) -> None:
        entity = WorldEntityRef.from_mapping(self.entity)
        source_refs = _require_source_refs(self.source_refs)
        effective_from = parse_utc_timestamp(self.effective_from, "effective_from")
        effective_until = _optional_utc(self.effective_until, "effective_until")
        object.__setattr__(self, "entity", entity)
        object.__setattr__(self, "source_refs", source_refs)
        object.__setattr__(self, "effective_from", effective_from)
        object.__setattr__(self, "effective_until", effective_until)
        object.__setattr__(self, "event_type", "world_entity_asserted")
        _set_event_id(
            self,
            _ENTITY_EVENT_PREFIX,
            {
                "event_type": "world_entity_asserted",
                "schema_version": WORLD_ENTITY_EVENT_SCHEMA,
                "entity": entity.to_dict(),
                "source_refs": list(source_refs),
                "effective_from": _iso(effective_from),
                "effective_until": _iso(effective_until),
            },
            schema_version=WORLD_ENTITY_EVENT_SCHEMA,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "event_type": self.event_type,
            "event_id": self.event_id,
            "entity": self.entity.to_dict(),
            "source_refs": list(self.source_refs),
            "effective_from": _iso(self.effective_from),
            "effective_until": _iso(self.effective_until),
        }

    def as_ref(self) -> WorldEntityRevisionRef:
        digest = canonical_sha256(
            {
                "entity": self.entity.to_dict(),
                "source_refs": list(self.source_refs),
                "effective_from": _iso(self.effective_from),
                "effective_until": _iso(self.effective_until),
            }
        )
        return WorldEntityRevisionRef(entity=self.entity, content_sha256=digest)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | WorldEntityAsserted) -> WorldEntityAsserted:
        if isinstance(value, WorldEntityAsserted):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("entity asserted event must be a mapping")
        return cls(
            entity=value.get("entity"),
            source_refs=value.get("source_refs") or (),
            effective_from=value.get("effective_from"),
            effective_until=value.get("effective_until"),
            event_id=value.get("event_id"),
            schema_version=value.get("schema_version", WORLD_ENTITY_EVENT_SCHEMA),
        )


@dataclass(frozen=True)
class WorldEntityRetired:
    entity: WorldEntityRef | Mapping[str, Any]
    retired_at: datetime | str
    source_refs: Sequence[str]
    event_id: str | None = None
    schema_version: str = WORLD_ENTITY_EVENT_SCHEMA
    event_type: str = "world_entity_retired"

    def __post_init__(self) -> None:
        entity = WorldEntityRef.from_mapping(self.entity)
        retired_at = parse_utc_timestamp(self.retired_at, "retired_at")
        source_refs = _require_source_refs(self.source_refs)
        object.__setattr__(self, "entity", entity)
        object.__setattr__(self, "retired_at", retired_at)
        object.__setattr__(self, "source_refs", source_refs)
        object.__setattr__(self, "event_type", "world_entity_retired")
        _set_event_id(
            self,
            _ENTITY_EVENT_PREFIX,
            {
                "event_type": "world_entity_retired",
                "schema_version": WORLD_ENTITY_EVENT_SCHEMA,
                "entity": entity.to_dict(),
                "retired_at": _iso(retired_at),
                "source_refs": list(source_refs),
            },
            schema_version=WORLD_ENTITY_EVENT_SCHEMA,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "event_type": self.event_type,
            "event_id": self.event_id,
            "entity": self.entity.to_dict(),
            "retired_at": _iso(self.retired_at),
            "source_refs": list(self.source_refs),
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | WorldEntityRetired) -> WorldEntityRetired:
        if isinstance(value, WorldEntityRetired):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("entity retired event must be a mapping")
        return cls(
            entity=value.get("entity"),
            retired_at=value.get("retired_at"),
            source_refs=value.get("source_refs") or (),
            event_id=value.get("event_id"),
            schema_version=value.get("schema_version", WORLD_ENTITY_EVENT_SCHEMA),
        )


@dataclass(frozen=True)
class WorldEntitySuperseded:
    entity: WorldEntityRef | Mapping[str, Any]
    successor: WorldEntityRef | Mapping[str, Any]
    superseded_at: datetime | str
    source_refs: Sequence[str]
    event_id: str | None = None
    schema_version: str = WORLD_ENTITY_EVENT_SCHEMA
    event_type: str = "world_entity_superseded"

    def __post_init__(self) -> None:
        entity = WorldEntityRef.from_mapping(self.entity)
        successor = WorldEntityRef.from_mapping(self.successor)
        if entity.kind != successor.kind:
            raise ValueError("superseding entity must keep the same kind")
        superseded_at = parse_utc_timestamp(self.superseded_at, "superseded_at")
        source_refs = _require_source_refs(self.source_refs)
        object.__setattr__(self, "entity", entity)
        object.__setattr__(self, "successor", successor)
        object.__setattr__(self, "superseded_at", superseded_at)
        object.__setattr__(self, "source_refs", source_refs)
        object.__setattr__(self, "event_type", "world_entity_superseded")
        _set_event_id(
            self,
            _ENTITY_EVENT_PREFIX,
            {
                "event_type": "world_entity_superseded",
                "schema_version": WORLD_ENTITY_EVENT_SCHEMA,
                "entity": entity.to_dict(),
                "successor": successor.to_dict(),
                "superseded_at": _iso(superseded_at),
                "source_refs": list(source_refs),
            },
            schema_version=WORLD_ENTITY_EVENT_SCHEMA,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "event_type": self.event_type,
            "event_id": self.event_id,
            "entity": self.entity.to_dict(),
            "successor": self.successor.to_dict(),
            "superseded_at": _iso(self.superseded_at),
            "source_refs": list(self.source_refs),
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | WorldEntitySuperseded) -> WorldEntitySuperseded:
        if isinstance(value, WorldEntitySuperseded):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("entity superseded event must be a mapping")
        return cls(
            entity=value.get("entity"),
            successor=value.get("successor"),
            superseded_at=value.get("superseded_at"),
            source_refs=value.get("source_refs") or (),
            event_id=value.get("event_id"),
            schema_version=value.get("schema_version", WORLD_ENTITY_EVENT_SCHEMA),
        )


WorldEntityEvent = WorldEntityAsserted | WorldEntityRetired | WorldEntitySuperseded
_ENTITY_EVENT_CLASSES = (WorldEntityAsserted, WorldEntityRetired, WorldEntitySuperseded)
_ENTITY_EVENT_PARSERS = {
    "world_entity_asserted": WorldEntityAsserted.from_mapping,
    "world_entity_retired": WorldEntityRetired.from_mapping,
    "world_entity_superseded": WorldEntitySuperseded.from_mapping,
}


def parse_world_entity_event(value: Mapping[str, Any] | WorldEntityEvent) -> WorldEntityEvent:
    if isinstance(value, _ENTITY_EVENT_CLASSES):
        return value
    if not isinstance(value, Mapping):
        raise TypeError("entity event must be a mapping or typed event")
    event_type = _required_text(value.get("event_type"), "event_type")
    try:
        parser = _ENTITY_EVENT_PARSERS[event_type]
    except KeyError as exc:
        raise ValueError(f"unknown world entity event_type: {event_type}") from exc
    return parser(value)


def _as_v2_entity(value: EntityRef | Mapping[str, Any]) -> EntityRef:
    return value if isinstance(value, EntityRef) else EntityRef.from_mapping(value)


@dataclass(frozen=True)
class WorldEntityIdentityLinkRef:
    link_id: str
    content_sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "link_id", _validate_prefixed_id(self.link_id, _LINK_ID_PREFIX, "link_id"))
        object.__setattr__(self, "content_sha256", _sha256_hex(self.content_sha256, "content_sha256"))

    def to_dict(self) -> dict[str, str]:
        return {"link_id": self.link_id, "content_sha256": self.content_sha256}

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | WorldEntityIdentityLinkRef) -> WorldEntityIdentityLinkRef:
        if isinstance(value, WorldEntityIdentityLinkRef):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("identity link ref must be a mapping")
        return cls(link_id=value.get("link_id"), content_sha256=value.get("content_sha256"))


@dataclass(frozen=True)
class WorldEntityIdentityLink:
    """Proven V2 EntityRef → V3 WorldEntityRef correspondence. Never rewritten in place."""

    v2_ref: EntityRef | Mapping[str, Any]
    v3_ref: WorldEntityRef | Mapping[str, Any]
    source_refs: Sequence[str]
    effective_from: datetime | str
    effective_until: datetime | str | None = None
    supersedes: str | None = None
    schema_version: str = WORLD_IDENTITY_LINK_SCHEMA
    link_id: str | None = None
    content_sha256: str | None = None

    def __post_init__(self) -> None:
        schema_version = _required_text(self.schema_version, "schema_version")
        if schema_version != WORLD_IDENTITY_LINK_SCHEMA:
            raise ValueError(f"schema_version must be {WORLD_IDENTITY_LINK_SCHEMA}")
        v2_ref = _as_v2_entity(self.v2_ref)
        v3_ref = WorldEntityRef.from_mapping(self.v3_ref)
        if v2_ref.kind == "sensor":
            raise ValueError("sensor is not a V2 world entity that can be linked to V3")
        if v2_ref.kind not in IDENTITY_MAPPABLE_V2_KINDS:
            raise ValueError(f"V2 identity kind {v2_ref.kind} cannot be linked to V3")
        if v2_ref.kind != v3_ref.kind:
            raise ValueError("identity link kind mismatch between V2 and V3 refs")
        source_refs = _require_source_refs(self.source_refs)
        effective_from = parse_utc_timestamp(self.effective_from, "effective_from")
        effective_until = _optional_utc(self.effective_until, "effective_until")
        if effective_until is not None and effective_until < effective_from:
            raise ValueError("effective_until must not precede effective_from")
        supersedes = (
            None if self.supersedes is None else _validate_prefixed_id(self.supersedes, _LINK_ID_PREFIX, "supersedes")
        )
        identity = {
            "schema_version": schema_version,
            "v2_ref": v2_ref.to_dict(),
            "v3_ref": v3_ref.to_dict(),
            "source_refs": list(source_refs),
            "effective_from": _iso(effective_from),
            "effective_until": _iso(effective_until),
            "supersedes": supersedes,
        }
        link_id = _prefixed_id(_LINK_ID_PREFIX, identity)
        if self.link_id is not None and _required_text(self.link_id, "link_id") != link_id:
            raise ValueError("link_id does not match the canonical identity link")
        digest = canonical_sha256({**identity, "link_id": link_id})
        if self.content_sha256 is not None and _required_text(self.content_sha256, "content_sha256") != digest:
            raise ValueError("content_sha256 does not match the canonical identity link")
        object.__setattr__(self, "schema_version", schema_version)
        object.__setattr__(self, "v2_ref", v2_ref)
        object.__setattr__(self, "v3_ref", v3_ref)
        object.__setattr__(self, "source_refs", source_refs)
        object.__setattr__(self, "effective_from", effective_from)
        object.__setattr__(self, "effective_until", effective_until)
        object.__setattr__(self, "supersedes", supersedes)
        object.__setattr__(self, "link_id", link_id)
        object.__setattr__(self, "content_sha256", digest)

    def as_ref(self) -> WorldEntityIdentityLinkRef:
        return WorldEntityIdentityLinkRef(link_id=self.link_id, content_sha256=self.content_sha256)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "link_id": self.link_id,
            "v2_ref": self.v2_ref.to_dict(),
            "v3_ref": self.v3_ref.to_dict(),
            "source_refs": list(self.source_refs),
            "effective_from": _iso(self.effective_from),
            "effective_until": _iso(self.effective_until),
            "supersedes": self.supersedes,
            "content_sha256": self.content_sha256,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | WorldEntityIdentityLink) -> WorldEntityIdentityLink:
        if isinstance(value, WorldEntityIdentityLink):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("identity link must be WorldEntityIdentityLink or a mapping")
        return cls(
            v2_ref=value.get("v2_ref"),
            v3_ref=value.get("v3_ref"),
            source_refs=value.get("source_refs") or (),
            effective_from=value.get("effective_from"),
            effective_until=value.get("effective_until"),
            supersedes=value.get("supersedes"),
            schema_version=value.get("schema_version", WORLD_IDENTITY_LINK_SCHEMA),
            link_id=value.get("link_id"),
            content_sha256=value.get("content_sha256"),
        )


@dataclass(frozen=True)
class WorldEntityIdentityLinked:
    link: WorldEntityIdentityLink | Mapping[str, Any]
    event_id: str | None = None
    schema_version: str = WORLD_IDENTITY_EVENT_SCHEMA
    event_type: str = "world_entity_identity_linked"

    def __post_init__(self) -> None:
        link = WorldEntityIdentityLink.from_mapping(self.link)
        object.__setattr__(self, "link", link)
        object.__setattr__(self, "event_type", "world_entity_identity_linked")
        _set_event_id(
            self,
            _IDENTITY_EVENT_PREFIX,
            {
                "event_type": "world_entity_identity_linked",
                "schema_version": WORLD_IDENTITY_EVENT_SCHEMA,
                "link_id": link.link_id,
                "content_sha256": link.content_sha256,
            },
            schema_version=WORLD_IDENTITY_EVENT_SCHEMA,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "event_type": self.event_type,
            "event_id": self.event_id,
            "link": self.link.to_dict(),
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | WorldEntityIdentityLinked) -> WorldEntityIdentityLinked:
        if isinstance(value, WorldEntityIdentityLinked):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("identity linked event must be a mapping")
        return cls(
            link=value.get("link"),
            event_id=value.get("event_id"),
            schema_version=value.get("schema_version", WORLD_IDENTITY_EVENT_SCHEMA),
        )


@dataclass(frozen=True)
class WorldEntityIdentityUnlinked:
    link_id: str
    retired_at: datetime | str
    source_refs: Sequence[str]
    event_id: str | None = None
    schema_version: str = WORLD_IDENTITY_EVENT_SCHEMA
    event_type: str = "world_entity_identity_unlinked"

    def __post_init__(self) -> None:
        link_id = _validate_prefixed_id(self.link_id, _LINK_ID_PREFIX, "link_id")
        retired_at = parse_utc_timestamp(self.retired_at, "retired_at")
        source_refs = _require_source_refs(self.source_refs)
        object.__setattr__(self, "link_id", link_id)
        object.__setattr__(self, "retired_at", retired_at)
        object.__setattr__(self, "source_refs", source_refs)
        object.__setattr__(self, "event_type", "world_entity_identity_unlinked")
        _set_event_id(
            self,
            _IDENTITY_EVENT_PREFIX,
            {
                "event_type": "world_entity_identity_unlinked",
                "schema_version": WORLD_IDENTITY_EVENT_SCHEMA,
                "link_id": link_id,
                "retired_at": _iso(retired_at),
                "source_refs": list(source_refs),
            },
            schema_version=WORLD_IDENTITY_EVENT_SCHEMA,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "event_type": self.event_type,
            "event_id": self.event_id,
            "link_id": self.link_id,
            "retired_at": _iso(self.retired_at),
            "source_refs": list(self.source_refs),
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | WorldEntityIdentityUnlinked) -> WorldEntityIdentityUnlinked:
        if isinstance(value, WorldEntityIdentityUnlinked):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("identity unlinked event must be a mapping")
        return cls(
            link_id=value.get("link_id"),
            retired_at=value.get("retired_at"),
            source_refs=value.get("source_refs") or (),
            event_id=value.get("event_id"),
            schema_version=value.get("schema_version", WORLD_IDENTITY_EVENT_SCHEMA),
        )


@dataclass(frozen=True)
class WorldEntityIdentityLinkSuperseded:
    predecessor_link_id: str
    successor: WorldEntityIdentityLink | Mapping[str, Any]
    event_id: str | None = None
    schema_version: str = WORLD_IDENTITY_EVENT_SCHEMA
    event_type: str = "world_entity_identity_link_superseded"

    def __post_init__(self) -> None:
        predecessor_link_id = _validate_prefixed_id(self.predecessor_link_id, _LINK_ID_PREFIX, "predecessor_link_id")
        successor = WorldEntityIdentityLink.from_mapping(self.successor)
        if successor.supersedes != predecessor_link_id:
            raise ValueError("successor.supersedes must equal predecessor_link_id")
        object.__setattr__(self, "predecessor_link_id", predecessor_link_id)
        object.__setattr__(self, "successor", successor)
        object.__setattr__(self, "event_type", "world_entity_identity_link_superseded")
        _set_event_id(
            self,
            _IDENTITY_EVENT_PREFIX,
            {
                "event_type": "world_entity_identity_link_superseded",
                "schema_version": WORLD_IDENTITY_EVENT_SCHEMA,
                "predecessor_link_id": predecessor_link_id,
                "successor_link_id": successor.link_id,
                "content_sha256": successor.content_sha256,
            },
            schema_version=WORLD_IDENTITY_EVENT_SCHEMA,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "event_type": self.event_type,
            "event_id": self.event_id,
            "predecessor_link_id": self.predecessor_link_id,
            "successor": self.successor.to_dict(),
        }

    @classmethod
    def from_mapping(
        cls, value: Mapping[str, Any] | WorldEntityIdentityLinkSuperseded
    ) -> WorldEntityIdentityLinkSuperseded:
        if isinstance(value, WorldEntityIdentityLinkSuperseded):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("identity superseded event must be a mapping")
        return cls(
            predecessor_link_id=value.get("predecessor_link_id"),
            successor=value.get("successor"),
            event_id=value.get("event_id"),
            schema_version=value.get("schema_version", WORLD_IDENTITY_EVENT_SCHEMA),
        )


WorldEntityIdentityEvent = WorldEntityIdentityLinked | WorldEntityIdentityUnlinked | WorldEntityIdentityLinkSuperseded
_IDENTITY_EVENT_CLASSES = (WorldEntityIdentityLinked, WorldEntityIdentityUnlinked, WorldEntityIdentityLinkSuperseded)
_IDENTITY_EVENT_PARSERS = {
    "world_entity_identity_linked": WorldEntityIdentityLinked.from_mapping,
    "world_entity_identity_unlinked": WorldEntityIdentityUnlinked.from_mapping,
    "world_entity_identity_link_superseded": WorldEntityIdentityLinkSuperseded.from_mapping,
}


def parse_world_entity_identity_event(value: Mapping[str, Any] | WorldEntityIdentityEvent) -> WorldEntityIdentityEvent:
    if isinstance(value, _IDENTITY_EVENT_CLASSES):
        return value
    if not isinstance(value, Mapping):
        raise TypeError("identity event must be a mapping or typed event")
    event_type = _required_text(value.get("event_type"), "event_type")
    try:
        parser = _IDENTITY_EVENT_PARSERS[event_type]
    except KeyError as exc:
        raise ValueError(f"unknown world entity identity event_type: {event_type}") from exc
    return parser(value)


def _assert_v2_uniqueness(
    states: Mapping[str, tuple[WorldEntityIdentityLink, datetime | None]],
    incoming: WorldEntityIdentityLink,
) -> None:
    incoming_until = incoming.effective_until
    for link, until in states.values():
        if link.link_id == incoming.link_id:
            if link.content_sha256 != incoming.content_sha256:
                raise ValueError("conflict: same identity link id with different content")
            continue
        if link.v2_ref != incoming.v2_ref:
            continue
        if not _intervals_overlap(link.effective_from, until, incoming.effective_from, incoming_until):
            continue
        if link.v3_ref != incoming.v3_ref:
            raise ValueError("ambiguous active V2 identity already linked to a different V3 entity")
        raise ValueError("ambiguous duplicate V2 identity link")


def _fold_identity_events(
    events: Sequence[WorldEntityIdentityEvent],
) -> dict[str, tuple[WorldEntityIdentityLink, datetime | None]]:
    states: dict[str, tuple[WorldEntityIdentityLink, datetime | None]] = {}
    for event in events:
        if isinstance(event, WorldEntityIdentityLinked):
            link = event.link
            existing = states.get(link.link_id)
            if existing is not None:
                if existing[0].content_sha256 != link.content_sha256:
                    raise ValueError("conflict: same identity link id with different content")
                continue
            _assert_v2_uniqueness(states, link)
            states[link.link_id] = (link, link.effective_until)
            continue
        if isinstance(event, WorldEntityIdentityUnlinked):
            current = states.get(event.link_id)
            if current is None:
                raise ValueError("unknown identity link")
            link, until = current
            if event.retired_at <= link.effective_from:
                raise ValueError("retired_at must be later than effective_from")
            if until is not None and until != event.retired_at and until <= event.retired_at:
                raise ValueError("identity link already retired")
            states[event.link_id] = (link, event.retired_at)
            continue
        if isinstance(event, WorldEntityIdentityLinkSuperseded):
            current = states.get(event.predecessor_link_id)
            if current is None:
                raise ValueError("unknown identity link")
            predecessor, until = current
            successor = event.successor
            if successor.v2_ref != predecessor.v2_ref:
                raise ValueError("identity supersession must keep the V2 ref")
            if successor.effective_from <= predecessor.effective_from:
                raise ValueError("successor effective_from must be later than the predecessor")
            if until is not None and until <= successor.effective_from:
                raise ValueError("predecessor identity link is not active")
            states[event.predecessor_link_id] = (predecessor, successor.effective_from)
            existing_successor = states.get(successor.link_id)
            if existing_successor is not None:
                if existing_successor[0].content_sha256 != successor.content_sha256:
                    raise ValueError("conflict: same identity link id with different content")
                continue
            _assert_v2_uniqueness(states, successor)
            states[successor.link_id] = (successor, successor.effective_until)
            continue
        raise TypeError(f"unsupported identity event: {type(event).__name__}")
    return states


@dataclass(frozen=True)
class WorldEntityIdentityMap:
    """Event-sourced V2→V3 identity aggregate. Corrections append; they never rewrite."""

    events: Sequence[WorldEntityIdentityEvent | Mapping[str, Any]] = ()
    schema_version: str = WORLD_IDENTITY_MAP_SCHEMA

    def __post_init__(self) -> None:
        schema_version = _required_text(self.schema_version, "schema_version")
        if schema_version != WORLD_IDENTITY_MAP_SCHEMA:
            raise ValueError(f"schema_version must be {WORLD_IDENTITY_MAP_SCHEMA}")
        events = tuple(parse_world_entity_identity_event(item) for item in self.events)
        _fold_identity_events(events)
        object.__setattr__(self, "schema_version", schema_version)
        object.__setattr__(self, "events", events)

    @classmethod
    def empty(cls) -> WorldEntityIdentityMap:
        return cls(events=())

    @classmethod
    def from_events(cls, events: Sequence[WorldEntityIdentityEvent | Mapping[str, Any]]) -> WorldEntityIdentityMap:
        return cls(events=events)

    def _states(self) -> dict[str, tuple[WorldEntityIdentityLink, datetime | None]]:
        return _fold_identity_events(self.events)

    def link(self, link: WorldEntityIdentityLink | Mapping[str, Any]) -> WorldEntityIdentityMap:
        resolved = WorldEntityIdentityLink.from_mapping(link)
        states = self._states()
        existing = states.get(resolved.link_id)
        if existing is not None:
            if existing[0].content_sha256 != resolved.content_sha256:
                raise ValueError("conflict: same identity link id with different content")
            return self
        _assert_v2_uniqueness(states, resolved)
        return WorldEntityIdentityMap(events=(*self.events, WorldEntityIdentityLinked(link=resolved)))

    def unlink(
        self,
        link_id: str,
        *,
        retired_at: datetime | str,
        source_refs: Sequence[str],
    ) -> WorldEntityIdentityMap:
        event = WorldEntityIdentityUnlinked(link_id=link_id, retired_at=retired_at, source_refs=source_refs)
        states = self._states()
        current = states.get(event.link_id)
        if current is None:
            raise ValueError("unknown identity link")
        link, until = current
        if until == event.retired_at:
            return self
        if event.retired_at <= link.effective_from:
            raise ValueError("retired_at must be later than effective_from")
        if until is not None and until <= event.retired_at:
            raise ValueError("identity link already retired")
        return WorldEntityIdentityMap(events=(*self.events, event))

    def supersede(self, successor: WorldEntityIdentityLink | Mapping[str, Any]) -> WorldEntityIdentityMap:
        resolved = WorldEntityIdentityLink.from_mapping(successor)
        if resolved.supersedes is None:
            raise ValueError("successor identity link must point at the predecessor via supersedes")
        event = WorldEntityIdentityLinkSuperseded(predecessor_link_id=resolved.supersedes, successor=resolved)
        states = self._states()
        current = states.get(event.predecessor_link_id)
        if current is None:
            raise ValueError("unknown identity link")
        predecessor, until = current
        if resolved.v2_ref != predecessor.v2_ref:
            raise ValueError("identity supersession must keep the V2 ref")
        if resolved.effective_from <= predecessor.effective_from:
            raise ValueError("successor effective_from must be later than the predecessor")
        if until is not None and until <= resolved.effective_from:
            raise ValueError("predecessor identity link is not active")
        existing = states.get(resolved.link_id)
        if existing is not None and existing[0].content_sha256 == resolved.content_sha256:
            return self
        return WorldEntityIdentityMap(events=(*self.events, event))

    def active_links_at(self, cutoff_at: datetime | str) -> tuple[WorldEntityIdentityLink, ...]:
        cutoff = parse_utc_timestamp(cutoff_at, "cutoff_at")
        active = [
            link
            for link, until in self._states().values()
            if _effective_at(cutoff, effective_from=link.effective_from, effective_until=until)
        ]
        return tuple(sorted(active, key=lambda item: item.link_id))

    def heads_hash_at(self, cutoff_at: datetime | str) -> str:
        refs = [link.as_ref().to_dict() for link in self.active_links_at(cutoff_at)]
        return canonical_sha256(refs)


def _relation_event_payload(event_type: str, family: str, extra: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "event_type": event_type,
        "schema_version": WORLD_RELATION_EVENT_SCHEMA,
        "family": family,
        **extra,
    }


@dataclass(frozen=True)
class StructuralWorldRelationAsserted:
    relation: StructuralWorldRelation | Mapping[str, Any]
    event_id: str | None = None
    schema_version: str = WORLD_RELATION_EVENT_SCHEMA
    event_type: str = "world_relation_asserted"
    family: str = "structural"

    def __post_init__(self) -> None:
        if isinstance(self.relation, KnowledgeWorldRelation):
            raise TypeError("structural relation event cannot wrap a knowledge relation")
        relation = StructuralWorldRelation.from_mapping(self.relation)
        object.__setattr__(self, "relation", relation)
        object.__setattr__(self, "event_type", "world_relation_asserted")
        object.__setattr__(self, "family", "structural")
        _set_event_id(
            self,
            _RELATION_EVENT_PREFIX,
            _relation_event_payload(
                "world_relation_asserted",
                "structural",
                {"relation_id": relation.relation_id, "content_sha256": relation.content_sha256},
            ),
            schema_version=WORLD_RELATION_EVENT_SCHEMA,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "event_type": self.event_type,
            "event_id": self.event_id,
            "family": self.family,
            "relation": self.relation.to_dict(),
        }

    @classmethod
    def from_mapping(
        cls, value: Mapping[str, Any] | StructuralWorldRelationAsserted
    ) -> StructuralWorldRelationAsserted:
        if isinstance(value, StructuralWorldRelationAsserted):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("structural asserted event must be a mapping")
        return cls(
            relation=value.get("relation"),
            event_id=value.get("event_id"),
            schema_version=value.get("schema_version", WORLD_RELATION_EVENT_SCHEMA),
        )


@dataclass(frozen=True)
class StructuralWorldRelationRetired:
    relation_id: str
    retired_at: datetime | str
    source_refs: Sequence[str]
    event_id: str | None = None
    schema_version: str = WORLD_RELATION_EVENT_SCHEMA
    event_type: str = "world_relation_retired"
    family: str = "structural"

    def __post_init__(self) -> None:
        relation_id = _validate_prefixed_id(self.relation_id, _RELATION_ID_PREFIX, "relation_id")
        retired_at = parse_utc_timestamp(self.retired_at, "retired_at")
        source_refs = _require_source_refs(self.source_refs)
        object.__setattr__(self, "relation_id", relation_id)
        object.__setattr__(self, "retired_at", retired_at)
        object.__setattr__(self, "source_refs", source_refs)
        object.__setattr__(self, "event_type", "world_relation_retired")
        object.__setattr__(self, "family", "structural")
        _set_event_id(
            self,
            _RELATION_EVENT_PREFIX,
            _relation_event_payload(
                "world_relation_retired",
                "structural",
                {
                    "relation_id": relation_id,
                    "retired_at": _iso(retired_at),
                    "source_refs": list(source_refs),
                },
            ),
            schema_version=WORLD_RELATION_EVENT_SCHEMA,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "event_type": self.event_type,
            "event_id": self.event_id,
            "family": self.family,
            "relation_id": self.relation_id,
            "retired_at": _iso(self.retired_at),
            "source_refs": list(self.source_refs),
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | StructuralWorldRelationRetired) -> StructuralWorldRelationRetired:
        if isinstance(value, StructuralWorldRelationRetired):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("structural retired event must be a mapping")
        return cls(
            relation_id=value.get("relation_id"),
            retired_at=value.get("retired_at"),
            source_refs=value.get("source_refs") or (),
            event_id=value.get("event_id"),
            schema_version=value.get("schema_version", WORLD_RELATION_EVENT_SCHEMA),
        )


@dataclass(frozen=True)
class KnowledgeWorldRelationAsserted:
    relation: KnowledgeWorldRelation | Mapping[str, Any]
    event_id: str | None = None
    schema_version: str = WORLD_RELATION_EVENT_SCHEMA
    event_type: str = "world_relation_asserted"
    family: str = "knowledge"

    def __post_init__(self) -> None:
        if isinstance(self.relation, StructuralWorldRelation):
            raise TypeError("knowledge relation event cannot wrap a structural relation")
        relation = KnowledgeWorldRelation.from_mapping(self.relation)
        object.__setattr__(self, "relation", relation)
        object.__setattr__(self, "event_type", "world_relation_asserted")
        object.__setattr__(self, "family", "knowledge")
        _set_event_id(
            self,
            _RELATION_EVENT_PREFIX,
            _relation_event_payload(
                "world_relation_asserted",
                "knowledge",
                {"relation_id": relation.relation_id, "content_sha256": relation.content_sha256},
            ),
            schema_version=WORLD_RELATION_EVENT_SCHEMA,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "event_type": self.event_type,
            "event_id": self.event_id,
            "family": self.family,
            "relation": self.relation.to_dict(),
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | KnowledgeWorldRelationAsserted) -> KnowledgeWorldRelationAsserted:
        if isinstance(value, KnowledgeWorldRelationAsserted):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("knowledge asserted event must be a mapping")
        return cls(
            relation=value.get("relation"),
            event_id=value.get("event_id"),
            schema_version=value.get("schema_version", WORLD_RELATION_EVENT_SCHEMA),
        )


@dataclass(frozen=True)
class KnowledgeWorldRelationRetired:
    relation_id: str
    retired_at: datetime | str
    source_refs: Sequence[str]
    event_id: str | None = None
    schema_version: str = WORLD_RELATION_EVENT_SCHEMA
    event_type: str = "world_relation_retired"
    family: str = "knowledge"

    def __post_init__(self) -> None:
        relation_id = _validate_prefixed_id(self.relation_id, _RELATION_ID_PREFIX, "relation_id")
        retired_at = parse_utc_timestamp(self.retired_at, "retired_at")
        source_refs = _require_source_refs(self.source_refs)
        object.__setattr__(self, "relation_id", relation_id)
        object.__setattr__(self, "retired_at", retired_at)
        object.__setattr__(self, "source_refs", source_refs)
        object.__setattr__(self, "event_type", "world_relation_retired")
        object.__setattr__(self, "family", "knowledge")
        _set_event_id(
            self,
            _RELATION_EVENT_PREFIX,
            _relation_event_payload(
                "world_relation_retired",
                "knowledge",
                {
                    "relation_id": relation_id,
                    "retired_at": _iso(retired_at),
                    "source_refs": list(source_refs),
                },
            ),
            schema_version=WORLD_RELATION_EVENT_SCHEMA,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "event_type": self.event_type,
            "event_id": self.event_id,
            "family": self.family,
            "relation_id": self.relation_id,
            "retired_at": _iso(self.retired_at),
            "source_refs": list(self.source_refs),
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | KnowledgeWorldRelationRetired) -> KnowledgeWorldRelationRetired:
        if isinstance(value, KnowledgeWorldRelationRetired):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("knowledge retired event must be a mapping")
        return cls(
            relation_id=value.get("relation_id"),
            retired_at=value.get("retired_at"),
            source_refs=value.get("source_refs") or (),
            event_id=value.get("event_id"),
            schema_version=value.get("schema_version", WORLD_RELATION_EVENT_SCHEMA),
        )


StructuralWorldRelationEvent = StructuralWorldRelationAsserted | StructuralWorldRelationRetired
KnowledgeWorldRelationEvent = KnowledgeWorldRelationAsserted | KnowledgeWorldRelationRetired
WorldRelationEvent = StructuralWorldRelationEvent | KnowledgeWorldRelationEvent
_RELATION_EVENT_CLASSES = (
    StructuralWorldRelationAsserted,
    StructuralWorldRelationRetired,
    KnowledgeWorldRelationAsserted,
    KnowledgeWorldRelationRetired,
)
_RELATION_EVENT_PARSERS = {
    ("structural", "world_relation_asserted"): StructuralWorldRelationAsserted.from_mapping,
    ("structural", "world_relation_retired"): StructuralWorldRelationRetired.from_mapping,
    ("knowledge", "world_relation_asserted"): KnowledgeWorldRelationAsserted.from_mapping,
    ("knowledge", "world_relation_retired"): KnowledgeWorldRelationRetired.from_mapping,
}


def parse_world_relation_event(value: Mapping[str, Any] | WorldRelationEvent) -> WorldRelationEvent:
    if isinstance(value, _RELATION_EVENT_CLASSES):
        return value
    if not isinstance(value, Mapping):
        raise TypeError("relation event must be a mapping or typed event")
    family = _required_text(value.get("family"), "family")
    event_type = _required_text(value.get("event_type"), "event_type")
    try:
        parser = _RELATION_EVENT_PARSERS[(family, event_type)]
    except KeyError as exc:
        raise ValueError(f"unknown world relation event {family}/{event_type}") from exc
    return parser(value)


def _fold_relation_events_at_cutoff(
    events: Sequence[Any],
    *,
    cutoff_at: datetime | str,
    evidence_by_relation_id: Mapping[str, AvailabilityEvidence | None] | None,
    asserted_cls: type,
    retired_cls: type,
) -> tuple[Any, ...]:
    cutoff = parse_utc_timestamp(cutoff_at, "cutoff_at")
    parsed = tuple(parse_world_relation_event(item) for item in events)
    states: dict[str, tuple[Any, datetime | None]] = {}
    for event in parsed:
        if isinstance(event, asserted_cls):
            relation = event.relation
            existing = states.get(relation.relation_id)
            if existing is not None and existing[0].content_sha256 != relation.content_sha256:
                raise ValueError("conflict: same relation id with different content")
            if existing is None:
                states[relation.relation_id] = (relation, relation.effective_until)
            continue
        if isinstance(event, retired_cls):
            current = states.get(event.relation_id)
            if current is None:
                raise ValueError("unknown relation")
            relation, until = current
            if event.retired_at <= relation.effective_from:
                raise ValueError("retired_at must be later than effective_from")
            tighter = event.retired_at if until is None or event.retired_at < until else until
            states[event.relation_id] = (relation, tighter)
            continue
        raise TypeError(f"unsupported relation event: {type(event).__name__}")
    evidence_map = evidence_by_relation_id or {}
    admitted = [
        relation
        for relation, until in states.values()
        if evaluate_world_graph_point_in_time(
            evidence=evidence_map.get(relation.relation_id),
            cutoff_at=cutoff,
            effective_from=relation.effective_from,
            valid_until=until,
        ).status
        == "eligible"
    ]
    return tuple(sorted(admitted, key=lambda item: item.relation_id))


def fold_structural_relation_events_at_cutoff(
    events: Sequence[StructuralWorldRelationEvent | Mapping[str, Any]],
    *,
    cutoff_at: datetime | str,
    evidence_by_relation_id: Mapping[str, AvailabilityEvidence | None] | None = None,
) -> tuple[StructuralWorldRelation, ...]:
    return _fold_relation_events_at_cutoff(
        events,
        cutoff_at=cutoff_at,
        evidence_by_relation_id=evidence_by_relation_id,
        asserted_cls=StructuralWorldRelationAsserted,
        retired_cls=StructuralWorldRelationRetired,
    )


def fold_knowledge_relation_events_at_cutoff(
    events: Sequence[KnowledgeWorldRelationEvent | Mapping[str, Any]],
    *,
    cutoff_at: datetime | str,
    evidence_by_relation_id: Mapping[str, AvailabilityEvidence | None] | None = None,
) -> tuple[KnowledgeWorldRelation, ...]:
    return _fold_relation_events_at_cutoff(
        events,
        cutoff_at=cutoff_at,
        evidence_by_relation_id=evidence_by_relation_id,
        asserted_cls=KnowledgeWorldRelationAsserted,
        retired_cls=KnowledgeWorldRelationRetired,
    )


def _entity_tuple(value: Sequence[Any] | None) -> tuple[WorldEntityRef, ...]:
    if value is None:
        return ()
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise TypeError("entities must be a sequence of WorldEntityRef")
    items = tuple(item if isinstance(item, WorldEntityRef) else WorldEntityRef.from_mapping(item) for item in value)
    return tuple(sorted(items, key=lambda item: item.node_id))


def _structural_ref_tuple(value: Sequence[Any] | frozenset[Any] | None) -> tuple[WorldStructuralRelationRef, ...]:
    if value is None:
        return ()
    if isinstance(value, (str, bytes, bytearray)):
        raise TypeError("structural_relation_refs must be a sequence")
    items = tuple(
        item if isinstance(item, WorldStructuralRelationRef) else WorldStructuralRelationRef.from_mapping(item)
        for item in value
    )
    return tuple(sorted(items, key=lambda item: (item.relation_id, item.content_sha256)))


def _identity_link_ref_tuple(value: Sequence[Any] | None) -> tuple[WorldEntityIdentityLinkRef, ...]:
    if value is None:
        return ()
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise TypeError("identity_link_refs must be a sequence")
    parsed: list[WorldEntityIdentityLinkRef] = []
    for item in value:
        if isinstance(item, str):
            raise TypeError("identity_link_refs must be identity/hash refs, not bare strings")
        parsed.append(
            item if isinstance(item, WorldEntityIdentityLinkRef) else WorldEntityIdentityLinkRef.from_mapping(item)
        )
    return tuple(sorted(parsed, key=lambda item: item.link_id))


def _knowledge_ref_frozenset(value: Sequence[Any] | frozenset[Any] | None) -> frozenset[WorldKnowledgeRelationRef]:
    if value is None:
        return frozenset()
    if isinstance(value, (str, bytes, bytearray)):
        raise TypeError("knowledge_relation_refs must be a set of refs")
    return frozenset(
        item if isinstance(item, WorldKnowledgeRelationRef) else WorldKnowledgeRelationRef.from_mapping(item)
        for item in value
    )


def _structural_ref_frozenset(value: Sequence[Any] | frozenset[Any] | None) -> frozenset[WorldStructuralRelationRef]:
    return frozenset(_structural_ref_tuple(value))


def _entity_revision_ref_tuple(value: Sequence[Any] | None) -> tuple[WorldEntityRevisionRef, ...]:
    if value is None:
        return ()
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise TypeError("entity_revision_refs must be a sequence of identity/hash refs")
    parsed: list[WorldEntityRevisionRef] = []
    for item in value:
        if isinstance(item, str):
            raise TypeError("entity_revision_refs must be identity/hash refs, not bare strings")
        parsed.append(item if isinstance(item, WorldEntityRevisionRef) else WorldEntityRevisionRef.from_mapping(item))
    return tuple(sorted(parsed, key=lambda item: (item.entity.node_id, item.content_sha256)))


@dataclass(frozen=True)
class WorldOntologyRevision:
    """Frozen structural heads. Knowledge overlay is never part of this hash."""

    revision_id: str
    entities: Sequence[WorldEntityRef | Mapping[str, Any]]
    structural_relation_refs: Sequence[WorldStructuralRelationRef | Mapping[str, Any]]
    identity_link_refs: Sequence[WorldEntityIdentityLinkRef | Mapping[str, Any]]
    scope_mapping_id: str
    scope_mapping_hash: str
    schema_version: str = WORLD_REVISION_SCHEMA
    entity_heads_hash: str | None = None
    structural_heads_hash: str | None = None
    identity_map_hash: str | None = None
    content_sha256: str | None = None

    def __post_init__(self) -> None:
        schema_version = _required_text(self.schema_version, "schema_version")
        if schema_version != WORLD_REVISION_SCHEMA:
            raise ValueError(f"schema_version must be {WORLD_REVISION_SCHEMA}")
        revision_id = _required_text(self.revision_id, "revision_id")
        entities = _entity_tuple(self.entities)
        structural_relation_refs = _structural_ref_tuple(self.structural_relation_refs)
        identity_link_refs = _identity_link_ref_tuple(self.identity_link_refs)
        scope_mapping_id = _required_text(self.scope_mapping_id, "scope_mapping_id")
        scope_mapping_hash = _sha256_hex(self.scope_mapping_hash, "scope_mapping_hash")
        entity_heads_hash = canonical_sha256([item.to_dict() for item in entities])
        structural_heads_hash = canonical_sha256([item.to_dict() for item in structural_relation_refs])
        identity_map_hash = canonical_sha256([item.to_dict() for item in identity_link_refs])
        if (
            self.entity_heads_hash is not None
            and _required_text(self.entity_heads_hash, "entity_heads_hash") != entity_heads_hash
        ):
            raise ValueError("entity_heads_hash does not match the canonical entity heads")
        if (
            self.structural_heads_hash is not None
            and _required_text(self.structural_heads_hash, "structural_heads_hash") != structural_heads_hash
        ):
            raise ValueError("structural_heads_hash does not match the canonical structural heads")
        if (
            self.identity_map_hash is not None
            and _required_text(self.identity_map_hash, "identity_map_hash") != identity_map_hash
        ):
            raise ValueError("identity_map_hash does not match the canonical identity-map heads")
        payload = {
            "schema_version": schema_version,
            "revision_id": revision_id,
            "entity_heads_hash": entity_heads_hash,
            "structural_heads_hash": structural_heads_hash,
            "identity_map_hash": identity_map_hash,
            "scope_mapping_id": scope_mapping_id,
            "scope_mapping_hash": scope_mapping_hash,
            "entities": [item.to_dict() for item in entities],
            "structural_relation_refs": [item.to_dict() for item in structural_relation_refs],
            "identity_link_refs": [item.to_dict() for item in identity_link_refs],
        }
        digest = canonical_sha256(payload)
        if self.content_sha256 is not None and _required_text(self.content_sha256, "content_sha256") != digest:
            raise ValueError("content_sha256 does not match the canonical ontology revision")
        object.__setattr__(self, "schema_version", schema_version)
        object.__setattr__(self, "revision_id", revision_id)
        object.__setattr__(self, "entities", entities)
        object.__setattr__(self, "structural_relation_refs", structural_relation_refs)
        object.__setattr__(self, "identity_link_refs", identity_link_refs)
        object.__setattr__(self, "scope_mapping_id", scope_mapping_id)
        object.__setattr__(self, "scope_mapping_hash", scope_mapping_hash)
        object.__setattr__(self, "entity_heads_hash", entity_heads_hash)
        object.__setattr__(self, "structural_heads_hash", structural_heads_hash)
        object.__setattr__(self, "identity_map_hash", identity_map_hash)
        object.__setattr__(self, "content_sha256", digest)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "revision_id": self.revision_id,
            "entities": [item.to_dict() for item in self.entities],
            "structural_relation_refs": [item.to_dict() for item in self.structural_relation_refs],
            "identity_link_refs": [item.to_dict() for item in self.identity_link_refs],
            "scope_mapping_id": self.scope_mapping_id,
            "scope_mapping_hash": self.scope_mapping_hash,
            "entity_heads_hash": self.entity_heads_hash,
            "structural_heads_hash": self.structural_heads_hash,
            "identity_map_hash": self.identity_map_hash,
            "content_sha256": self.content_sha256,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | WorldOntologyRevision) -> WorldOntologyRevision:
        if isinstance(value, WorldOntologyRevision):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("ontology revision must be WorldOntologyRevision or a mapping")
        overlay_keys = [key for key in value if "knowledge" in str(key)]
        if overlay_keys or value.get("knowledge_relation_refs") not in (None, (), []):
            raise ValueError("knowledge overlay cannot enter a WorldOntologyRevision")
        return cls(
            revision_id=value.get("revision_id"),
            entities=value.get("entities") or (),
            structural_relation_refs=value.get("structural_relation_refs") or (),
            identity_link_refs=value.get("identity_link_refs") or (),
            scope_mapping_id=value.get("scope_mapping_id"),
            scope_mapping_hash=value.get("scope_mapping_hash"),
            schema_version=value.get("schema_version", WORLD_REVISION_SCHEMA),
            entity_heads_hash=value.get("entity_heads_hash"),
            structural_heads_hash=value.get("structural_heads_hash"),
            identity_map_hash=value.get("identity_map_hash"),
            content_sha256=value.get("content_sha256"),
        )


def reconcile_world_ontology_revision(
    existing: WorldOntologyRevision,
    incoming: WorldOntologyRevision,
) -> WorldOntologyRevision:
    if existing.revision_id != incoming.revision_id:
        raise ValueError("revision_id mismatch")
    if existing.content_sha256 != incoming.content_sha256:
        raise ValueError("conflict: same revision id with a different identity-map or mapping hash")
    return existing


@dataclass(frozen=True)
class WorldOntologyRevisionPublished:
    revision: WorldOntologyRevision | Mapping[str, Any]
    event_id: str | None = None
    schema_version: str = WORLD_REVISION_EVENT_SCHEMA
    event_type: str = "world_ontology_revision_published"

    def __post_init__(self) -> None:
        revision = WorldOntologyRevision.from_mapping(self.revision)
        object.__setattr__(self, "revision", revision)
        object.__setattr__(self, "event_type", "world_ontology_revision_published")
        _set_event_id(
            self,
            _REVISION_EVENT_PREFIX,
            {
                "event_type": "world_ontology_revision_published",
                "schema_version": WORLD_REVISION_EVENT_SCHEMA,
                "revision_id": revision.revision_id,
                "content_sha256": revision.content_sha256,
            },
            schema_version=WORLD_REVISION_EVENT_SCHEMA,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "event_type": self.event_type,
            "event_id": self.event_id,
            "revision": self.revision.to_dict(),
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | WorldOntologyRevisionPublished) -> WorldOntologyRevisionPublished:
        if isinstance(value, WorldOntologyRevisionPublished):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("revision published event must be a mapping")
        return cls(
            revision=value.get("revision"),
            event_id=value.get("event_id"),
            schema_version=value.get("schema_version", WORLD_REVISION_EVENT_SCHEMA),
        )


@dataclass(frozen=True)
class WorldOntologyRevisionSuperseded:
    revision_id: str
    successor_revision_id: str
    event_id: str | None = None
    schema_version: str = WORLD_REVISION_EVENT_SCHEMA
    event_type: str = "world_ontology_revision_superseded"

    def __post_init__(self) -> None:
        revision_id = _required_text(self.revision_id, "revision_id")
        successor_revision_id = _required_text(self.successor_revision_id, "successor_revision_id")
        if revision_id == successor_revision_id:
            raise ValueError("successor_revision_id must differ from revision_id")
        object.__setattr__(self, "revision_id", revision_id)
        object.__setattr__(self, "successor_revision_id", successor_revision_id)
        object.__setattr__(self, "event_type", "world_ontology_revision_superseded")
        _set_event_id(
            self,
            _REVISION_EVENT_PREFIX,
            {
                "event_type": "world_ontology_revision_superseded",
                "schema_version": WORLD_REVISION_EVENT_SCHEMA,
                "revision_id": revision_id,
                "successor_revision_id": successor_revision_id,
            },
            schema_version=WORLD_REVISION_EVENT_SCHEMA,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "event_type": self.event_type,
            "event_id": self.event_id,
            "revision_id": self.revision_id,
            "successor_revision_id": self.successor_revision_id,
        }

    @classmethod
    def from_mapping(
        cls, value: Mapping[str, Any] | WorldOntologyRevisionSuperseded
    ) -> WorldOntologyRevisionSuperseded:
        if isinstance(value, WorldOntologyRevisionSuperseded):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("revision superseded event must be a mapping")
        return cls(
            revision_id=value.get("revision_id"),
            successor_revision_id=value.get("successor_revision_id"),
            event_id=value.get("event_id"),
            schema_version=value.get("schema_version", WORLD_REVISION_EVENT_SCHEMA),
        )


WorldOntologyRevisionEvent = WorldOntologyRevisionPublished | WorldOntologyRevisionSuperseded
_REVISION_EVENT_CLASSES = (WorldOntologyRevisionPublished, WorldOntologyRevisionSuperseded)
_REVISION_EVENT_PARSERS = {
    "world_ontology_revision_published": WorldOntologyRevisionPublished.from_mapping,
    "world_ontology_revision_superseded": WorldOntologyRevisionSuperseded.from_mapping,
}


def parse_world_ontology_revision_event(
    value: Mapping[str, Any] | WorldOntologyRevisionEvent,
) -> WorldOntologyRevisionEvent:
    if isinstance(value, _REVISION_EVENT_CLASSES):
        return value
    if not isinstance(value, Mapping):
        raise TypeError("revision event must be a mapping or typed event")
    event_type = _required_text(value.get("event_type"), "event_type")
    try:
        parser = _REVISION_EVENT_PARSERS[event_type]
    except KeyError as exc:
        raise ValueError(f"unknown world ontology revision event_type: {event_type}") from exc
    return parser(value)


def world_instrument_root_for_resolution(
    resolution: WorldScopeResolution,
    *,
    instrument: str,
) -> WorldEntityRef | None:
    """Exact-anchor instrument root. Unmapped/ambiguous never invent a MIC."""

    if not isinstance(resolution, WorldScopeResolution):
        raise TypeError("resolution must be WorldScopeResolution")
    instrument_id = _required_text(instrument, "instrument")
    if resolution.status != "resolved":
        return None
    if instrument_id != resolution.anchor.instrument:
        raise ValueError("instrument must match the resolved market anchor")
    venue = next(scope for scope in resolution.scopes if scope.kind == "venue")
    return WorldEntityRef(kind="instrument", entity_id=f"{venue.entity_id}:symbol:{instrument_id}")


@dataclass(frozen=True)
class WorldGraphSnapshot:
    """Immutable V3 subgraph at a cutoff. Structural and knowledge refs stay two sets.

    Unmapped/ambiguous missingness forbids a world-entity root and graph members.
    ``root_entity=None`` is a backward-compatible widening of
    ``world_graph_snapshot.v1``: historical instrument-root payloads stay valid,
    and JSON null is admitted only for unmapped/ambiguous missing snapshots.
    Persistence adapters project null onto empty TEXT columns; rehydration reads
    the canonical payload, never those columns.
    """

    root_episode_id: str
    root_entity: WorldEntityRef | Mapping[str, Any] | None
    cutoff_at: datetime | str
    ontology_revision: str
    ontology_hash: str
    identity_map_hash: str
    scope_mapping_id: str
    scope_mapping_hash: str
    entity_revision_refs: Sequence[WorldEntityRevisionRef | Mapping[str, Any]] = ()
    identity_link_refs: Sequence[WorldEntityIdentityLinkRef | Mapping[str, Any]] = ()
    structural_relation_refs: Sequence[WorldStructuralRelationRef | Mapping[str, Any]] | frozenset[Any] = ()
    knowledge_relation_refs: Sequence[WorldKnowledgeRelationRef | Mapping[str, Any]] | frozenset[Any] = ()
    artifact_refs: Sequence[str] = ()
    producer_versions: Mapping[str, Any] | None = None
    traversal_policy_version: str = GRAPH_TRAVERSAL_POLICY_VERSION
    status: str = "missing"
    missingness: Mapping[str, Any] | None = None
    schema_version: str = WORLD_GRAPH_SNAPSHOT_SCHEMA
    snapshot_id: str | None = None
    content_sha256: str | None = None

    def __post_init__(self) -> None:
        schema_version = _required_text(self.schema_version, "schema_version")
        if schema_version != WORLD_GRAPH_SNAPSHOT_SCHEMA:
            raise ValueError(f"schema_version must be {WORLD_GRAPH_SNAPSHOT_SCHEMA}")
        root_episode_id = _validate_prefixed_id(self.root_episode_id, _EPISODE_ID_PREFIX, "root_episode_id")
        root_entity = None if self.root_entity is None else WorldEntityRef.from_mapping(self.root_entity)
        cutoff_at = parse_utc_timestamp(self.cutoff_at, "cutoff_at")
        ontology_revision = _required_text(self.ontology_revision, "ontology_revision")
        ontology_hash = _sha256_hex(self.ontology_hash, "ontology_hash")
        identity_map_hash = _sha256_hex(self.identity_map_hash, "identity_map_hash")
        scope_mapping_id = _required_text(self.scope_mapping_id, "scope_mapping_id")
        scope_mapping_hash = _sha256_hex(self.scope_mapping_hash, "scope_mapping_hash")
        status = _required_text(self.status, "status").lower()
        if status not in SNAPSHOT_STATUSES:
            allowed = ", ".join(sorted(SNAPSHOT_STATUSES))
            raise ValueError(f"snapshot status must be one of: {allowed}")
        entity_revision_refs = _entity_revision_ref_tuple(self.entity_revision_refs)
        identity_link_refs = _identity_link_ref_tuple(self.identity_link_refs)
        artifact_refs = tuple(sorted(frozenset(_immutable_text_tuple(self.artifact_refs, "artifact_refs"))))
        structural_relation_refs = _structural_ref_frozenset(self.structural_relation_refs)
        knowledge_relation_refs = _knowledge_ref_frozenset(self.knowledge_relation_refs)
        structural_ids = {item.relation_id for item in structural_relation_refs}
        knowledge_ids = {item.relation_id for item in knowledge_relation_refs}
        if structural_ids & knowledge_ids:
            raise ValueError(
                "structural and knowledge relation refs must be disjoint; a relation cannot be in both sets"
            )
        producer_versions = _mapping_proxy_text(self.producer_versions, "producer_versions")
        missingness = _freeze_mapping(self.missingness, "missingness")
        scope_status = missingness.get("scope") if isinstance(missingness, Mapping) else None
        if scope_status in {"unmapped", "ambiguous"}:
            if root_entity is not None:
                raise ValueError("unmapped/ambiguous snapshot must not select a world entity root")
            if status != "missing":
                raise ValueError("unmapped/ambiguous snapshot status must be missing")
            if (
                entity_revision_refs
                or identity_link_refs
                or structural_relation_refs
                or knowledge_relation_refs
                or artifact_refs
            ):
                raise ValueError("unmapped/ambiguous snapshot must not carry graph members")
        else:
            if root_entity is None:
                raise ValueError("snapshot root_entity is required when scope is not unmapped/ambiguous")
            if root_entity.kind != "instrument":
                raise ValueError("snapshot root_entity must have kind=instrument")
        traversal_policy_version = _required_text(self.traversal_policy_version, "traversal_policy_version")
        identity = {
            "schema_version": schema_version,
            "root_episode_id": root_episode_id,
            "root_entity": None if root_entity is None else root_entity.to_dict(),
            "cutoff_at": _iso(cutoff_at),
            "ontology_revision": ontology_revision,
            "ontology_hash": ontology_hash,
            "identity_map_hash": identity_map_hash,
            "scope_mapping_id": scope_mapping_id,
            "scope_mapping_hash": scope_mapping_hash,
            "entity_revision_refs": [item.to_dict() for item in entity_revision_refs],
            "identity_link_refs": [item.to_dict() for item in identity_link_refs],
            "structural_relation_refs": [
                item.to_dict() for item in sorted(structural_relation_refs, key=lambda ref: ref.relation_id)
            ],
            "knowledge_relation_refs": [
                item.to_dict() for item in sorted(knowledge_relation_refs, key=lambda ref: ref.relation_id)
            ],
            "artifact_refs": list(artifact_refs),
            "producer_versions": dict(producer_versions),
            "traversal_policy_version": traversal_policy_version,
            "status": status,
            "missingness": _deep_thaw(missingness),
        }
        digest = canonical_sha256(identity)
        snapshot_id = _prefixed_id(_SNAPSHOT_ID_PREFIX, identity)
        if self.snapshot_id is not None and _required_text(self.snapshot_id, "snapshot_id") != snapshot_id:
            raise ValueError("snapshot_id does not match the canonical graph snapshot")
        if self.content_sha256 is not None and _required_text(self.content_sha256, "content_sha256") != digest:
            raise ValueError("content_sha256 does not match the canonical graph snapshot")
        object.__setattr__(self, "schema_version", schema_version)
        object.__setattr__(self, "root_episode_id", root_episode_id)
        object.__setattr__(self, "root_entity", root_entity)
        object.__setattr__(self, "cutoff_at", cutoff_at)
        object.__setattr__(self, "ontology_revision", ontology_revision)
        object.__setattr__(self, "ontology_hash", ontology_hash)
        object.__setattr__(self, "identity_map_hash", identity_map_hash)
        object.__setattr__(self, "scope_mapping_id", scope_mapping_id)
        object.__setattr__(self, "scope_mapping_hash", scope_mapping_hash)
        object.__setattr__(self, "entity_revision_refs", entity_revision_refs)
        object.__setattr__(self, "identity_link_refs", identity_link_refs)
        object.__setattr__(self, "structural_relation_refs", structural_relation_refs)
        object.__setattr__(self, "knowledge_relation_refs", knowledge_relation_refs)
        object.__setattr__(self, "artifact_refs", artifact_refs)
        object.__setattr__(self, "producer_versions", producer_versions)
        object.__setattr__(self, "traversal_policy_version", traversal_policy_version)
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "missingness", missingness)
        object.__setattr__(self, "snapshot_id", snapshot_id)
        object.__setattr__(self, "content_sha256", digest)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "snapshot_id": self.snapshot_id,
            "root_episode_id": self.root_episode_id,
            "root_entity": None if self.root_entity is None else self.root_entity.to_dict(),
            "cutoff_at": _iso(self.cutoff_at),
            "ontology_revision": self.ontology_revision,
            "ontology_hash": self.ontology_hash,
            "identity_map_hash": self.identity_map_hash,
            "scope_mapping_id": self.scope_mapping_id,
            "scope_mapping_hash": self.scope_mapping_hash,
            "entity_revision_refs": [item.to_dict() for item in self.entity_revision_refs],
            "identity_link_refs": [item.to_dict() for item in self.identity_link_refs],
            "structural_relation_refs": [
                item.to_dict() for item in sorted(self.structural_relation_refs, key=lambda ref: ref.relation_id)
            ],
            "knowledge_relation_refs": [
                item.to_dict() for item in sorted(self.knowledge_relation_refs, key=lambda ref: ref.relation_id)
            ],
            "artifact_refs": list(self.artifact_refs),
            "producer_versions": dict(self.producer_versions),
            "traversal_policy_version": self.traversal_policy_version,
            "status": self.status,
            "missingness": _deep_thaw(self.missingness),
            "content_sha256": self.content_sha256,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | WorldGraphSnapshot) -> WorldGraphSnapshot:
        if isinstance(value, WorldGraphSnapshot):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("graph snapshot must be WorldGraphSnapshot or a mapping")
        return cls(
            root_episode_id=value.get("root_episode_id"),
            root_entity=value.get("root_entity"),
            cutoff_at=value.get("cutoff_at"),
            ontology_revision=value.get("ontology_revision"),
            ontology_hash=value.get("ontology_hash"),
            identity_map_hash=value.get("identity_map_hash"),
            scope_mapping_id=value.get("scope_mapping_id"),
            scope_mapping_hash=value.get("scope_mapping_hash"),
            entity_revision_refs=value.get("entity_revision_refs") or (),
            identity_link_refs=value.get("identity_link_refs") or (),
            structural_relation_refs=value.get("structural_relation_refs") or (),
            knowledge_relation_refs=value.get("knowledge_relation_refs") or (),
            artifact_refs=value.get("artifact_refs") or (),
            producer_versions=value.get("producer_versions") or {},
            traversal_policy_version=value.get("traversal_policy_version", GRAPH_TRAVERSAL_POLICY_VERSION),
            status=value.get("status", "missing"),
            missingness=value.get("missingness") or {},
            schema_version=value.get("schema_version", WORLD_GRAPH_SNAPSHOT_SCHEMA),
            snapshot_id=value.get("snapshot_id"),
            content_sha256=value.get("content_sha256"),
        )


def observation_graph_snapshot(value: Mapping[str, Any] | WorldGraphSnapshot) -> WorldGraphSnapshot:
    """Hydrate a snapshot for observation. Unmapped/ambiguous never select a root.

    Canonical construction stays fail-closed: ``from_mapping`` still rejects a
    fabricated root. Observation replay projects that contamination to honest
    missingness instead of raising or borrowing another MIC.
    """

    if isinstance(value, WorldGraphSnapshot):
        return value
    if not isinstance(value, Mapping):
        raise TypeError("graph snapshot must be WorldGraphSnapshot or a mapping")
    missingness = value.get("missingness") or {}
    scope = missingness.get("scope") if isinstance(missingness, Mapping) else None
    if scope not in {"unmapped", "ambiguous"}:
        return WorldGraphSnapshot.from_mapping(value)
    projected = dict(value)
    projected["root_entity"] = None
    projected["entity_revision_refs"] = ()
    projected["identity_link_refs"] = ()
    projected["structural_relation_refs"] = ()
    projected["knowledge_relation_refs"] = ()
    projected["artifact_refs"] = ()
    projected["status"] = "missing"
    frozen_missing = dict(missingness)
    frozen_missing["scope"] = scope
    projected["missingness"] = frozen_missing
    projected.pop("snapshot_id", None)
    projected.pop("content_sha256", None)
    return WorldGraphSnapshot.from_mapping(projected)


def reconcile_world_graph_snapshot(existing: WorldGraphSnapshot, incoming: WorldGraphSnapshot) -> WorldGraphSnapshot:
    if existing.snapshot_id != incoming.snapshot_id or existing.content_sha256 != incoming.content_sha256:
        raise ValueError("conflict: graph snapshot identity/content mismatch")
    return existing


class StaleBridgeEpoch(ValueError):
    """Fence no longer matches the active unblocked registry generation."""


def world_observation_ref_for_observation_id(observation_id: str) -> WorldObservationRef:
    identity = _validate_prefixed_id(observation_id, _MACRO_OBSERVATION_ID_PREFIX, "observation_id")
    return WorldObservationRef(observation_id=_prefixed_id(_OBSERVATION_REF_PREFIX, {"observation_id": identity}))


@dataclass(frozen=True)
class MacroObservationCursor:
    """Opaque store-assigned position in the observation receipt journal."""

    receipt_log_generation: int
    ordinal: int
    receipt_id: str | None = None
    observation_id: str | None = None
    schema_version: str = MACRO_OBSERVATION_CURSOR_SCHEMA

    def __post_init__(self) -> None:
        schema_version = _required_text(self.schema_version, "schema_version")
        if schema_version != MACRO_OBSERVATION_CURSOR_SCHEMA:
            raise ValueError(f"schema_version must be {MACRO_OBSERVATION_CURSOR_SCHEMA}")
        generation = _positive_int(self.receipt_log_generation, "receipt_log_generation")
        ordinal = _non_negative_int(self.ordinal, "ordinal")
        if ordinal == 0:
            if self.receipt_id is not None or self.observation_id is not None:
                raise ValueError("origin cursor has no receipt")
            receipt_id = None
            observation_id = None
        else:
            receipt_id = _validate_prefixed_id(self.receipt_id, _RECEIPT_ID_PREFIX, "receipt_id")
            observation_id = _validate_prefixed_id(self.observation_id, _MACRO_OBSERVATION_ID_PREFIX, "observation_id")
        object.__setattr__(self, "schema_version", schema_version)
        object.__setattr__(self, "receipt_log_generation", generation)
        object.__setattr__(self, "ordinal", ordinal)
        object.__setattr__(self, "receipt_id", receipt_id)
        object.__setattr__(self, "observation_id", observation_id)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "receipt_log_generation": self.receipt_log_generation,
            "ordinal": self.ordinal,
            "receipt_id": self.receipt_id,
            "observation_id": self.observation_id,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | MacroObservationCursor) -> MacroObservationCursor:
        if isinstance(value, MacroObservationCursor):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("cursor must be MacroObservationCursor or a mapping")
        return cls(
            receipt_log_generation=value.get("receipt_log_generation"),
            ordinal=value.get("ordinal"),
            receipt_id=value.get("receipt_id"),
            observation_id=value.get("observation_id"),
            schema_version=value.get("schema_version", MACRO_OBSERVATION_CURSOR_SCHEMA),
        )


@dataclass(frozen=True)
class MacroObservationCursorReservation:
    bridge_key: str
    request_id: str
    cursor: MacroObservationCursor | Mapping[str, Any]
    schema_version: str = MACRO_OBSERVATION_CURSOR_RESERVATION_SCHEMA
    reservation_id: str | None = None
    content_sha256: str | None = None

    def __post_init__(self) -> None:
        schema_version = _required_text(self.schema_version, "schema_version")
        if schema_version != MACRO_OBSERVATION_CURSOR_RESERVATION_SCHEMA:
            raise ValueError(f"schema_version must be {MACRO_OBSERVATION_CURSOR_RESERVATION_SCHEMA}")
        bridge_key = _required_text(self.bridge_key, "bridge_key")
        request_id = _validate_prefixed_id(self.request_id, _REQUEST_ID_PREFIX, "request_id")
        cursor = MacroObservationCursor.from_mapping(self.cursor)
        identity = {
            "schema_version": schema_version,
            "bridge_key": bridge_key,
            "request_id": request_id,
            "cursor": cursor.to_dict(),
        }
        reservation_id = _prefixed_id(_RESERVATION_PREFIX, identity)
        if self.reservation_id is not None and _required_text(self.reservation_id, "reservation_id") != reservation_id:
            raise ValueError("reservation_id does not match the canonical cursor reservation")
        digest = canonical_sha256({**identity, "reservation_id": reservation_id})
        if self.content_sha256 is not None and _required_text(self.content_sha256, "content_sha256") != digest:
            raise ValueError("content_sha256 does not match the canonical cursor reservation")
        object.__setattr__(self, "schema_version", schema_version)
        object.__setattr__(self, "bridge_key", bridge_key)
        object.__setattr__(self, "request_id", request_id)
        object.__setattr__(self, "cursor", cursor)
        object.__setattr__(self, "reservation_id", reservation_id)
        object.__setattr__(self, "content_sha256", digest)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "reservation_id": self.reservation_id,
            "bridge_key": self.bridge_key,
            "request_id": self.request_id,
            "cursor": self.cursor.to_dict(),
            "content_sha256": self.content_sha256,
        }

    @classmethod
    def from_mapping(
        cls, value: Mapping[str, Any] | MacroObservationCursorReservation
    ) -> MacroObservationCursorReservation:
        if isinstance(value, MacroObservationCursorReservation):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("cursor reservation must be MacroObservationCursorReservation or a mapping")
        return cls(
            bridge_key=value.get("bridge_key"),
            request_id=value.get("request_id"),
            cursor=value.get("cursor"),
            schema_version=value.get("schema_version", MACRO_OBSERVATION_CURSOR_RESERVATION_SCHEMA),
            reservation_id=value.get("reservation_id"),
            content_sha256=value.get("content_sha256"),
        )


@dataclass(frozen=True)
class MacroGraphBridgeFence:
    bridge_key: str
    run_id: str
    epoch: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "bridge_key", _required_text(self.bridge_key, "bridge_key"))
        object.__setattr__(self, "run_id", _validate_prefixed_id(self.run_id, _BRIDGE_RUN_PREFIX, "run_id"))
        object.__setattr__(self, "epoch", _positive_int(self.epoch, "epoch"))

    def to_dict(self) -> dict[str, Any]:
        return {"bridge_key": self.bridge_key, "run_id": self.run_id, "epoch": self.epoch}

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | MacroGraphBridgeFence) -> MacroGraphBridgeFence:
        if isinstance(value, MacroGraphBridgeFence):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("fence must be MacroGraphBridgeFence or a mapping")
        return cls(bridge_key=value.get("bridge_key"), run_id=value.get("run_id"), epoch=value.get("epoch"))


@dataclass(frozen=True)
class MacroGraphBridgeRunSpec:
    scope_mapping_id: str
    scope_mapping_hash: str
    ontology_revision_id: str
    ontology_revision_hash: str
    collection_plan_id: str
    collection_plan_hash: str
    producer_version: str
    schema_version: str = MACRO_GRAPH_BRIDGE_RUN_SPEC_SCHEMA

    def __post_init__(self) -> None:
        schema_version = _required_text(self.schema_version, "schema_version")
        if schema_version != MACRO_GRAPH_BRIDGE_RUN_SPEC_SCHEMA:
            raise ValueError(f"schema_version must be {MACRO_GRAPH_BRIDGE_RUN_SPEC_SCHEMA}")
        object.__setattr__(self, "schema_version", schema_version)
        object.__setattr__(self, "scope_mapping_id", _required_text(self.scope_mapping_id, "scope_mapping_id"))
        object.__setattr__(self, "scope_mapping_hash", _sha256_hex(self.scope_mapping_hash, "scope_mapping_hash"))
        object.__setattr__(
            self, "ontology_revision_id", _required_text(self.ontology_revision_id, "ontology_revision_id")
        )
        object.__setattr__(
            self, "ontology_revision_hash", _sha256_hex(self.ontology_revision_hash, "ontology_revision_hash")
        )
        object.__setattr__(
            self,
            "collection_plan_id",
            _validate_prefixed_id(self.collection_plan_id, _COLLECTION_PLAN_ID_PREFIX, "collection_plan_id"),
        )
        object.__setattr__(self, "collection_plan_hash", _sha256_hex(self.collection_plan_hash, "collection_plan_hash"))
        producer_version = _required_text(self.producer_version, "producer_version")
        if producer_version != MACRO_PRODUCER_VERSION:
            raise ValueError(f"producer_version must be {MACRO_PRODUCER_VERSION}")
        object.__setattr__(self, "producer_version", producer_version)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "scope_mapping_id": self.scope_mapping_id,
            "scope_mapping_hash": self.scope_mapping_hash,
            "ontology_revision_id": self.ontology_revision_id,
            "ontology_revision_hash": self.ontology_revision_hash,
            "collection_plan_id": self.collection_plan_id,
            "collection_plan_hash": self.collection_plan_hash,
            "producer_version": self.producer_version,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | MacroGraphBridgeRunSpec) -> MacroGraphBridgeRunSpec:
        if isinstance(value, MacroGraphBridgeRunSpec):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("run spec must be MacroGraphBridgeRunSpec or a mapping")
        return cls(
            scope_mapping_id=_required_mapping_text(value, "scope_mapping_id"),
            scope_mapping_hash=_required_mapping_text(value, "scope_mapping_hash"),
            ontology_revision_id=_required_mapping_text(value, "ontology_revision_id"),
            ontology_revision_hash=_required_mapping_text(value, "ontology_revision_hash"),
            collection_plan_id=_required_mapping_text(value, "collection_plan_id"),
            collection_plan_hash=_required_mapping_text(value, "collection_plan_hash"),
            producer_version=_required_mapping_text(value, "producer_version"),
            schema_version=_required_mapping_text(value, "schema_version"),
        )


@dataclass(frozen=True)
class MacroGraphBridgeRun:
    run_id: str
    epoch: int
    status: str
    spec: MacroGraphBridgeRunSpec | Mapping[str, Any]
    activation_cursor: MacroObservationCursor | Mapping[str, Any]
    cursor: MacroObservationCursor | Mapping[str, Any]
    block_reason: str | None = None

    def __post_init__(self) -> None:
        status = _required_text(self.status, "status")
        if status not in MACRO_GRAPH_RUN_STATUSES:
            allowed = ", ".join(sorted(MACRO_GRAPH_RUN_STATUSES))
            raise ValueError(f"run status must be one of: {allowed}")
        reason = None if self.block_reason is None else _required_text(self.block_reason, "block_reason")
        if status == "blocked":
            if reason not in MACRO_GRAPH_BLOCK_REASONS:
                raise ValueError("blocked run requires reason=config_drift")
        elif reason is not None:
            raise ValueError("block_reason is only valid on a blocked run")
        object.__setattr__(self, "run_id", _validate_prefixed_id(self.run_id, _BRIDGE_RUN_PREFIX, "run_id"))
        object.__setattr__(self, "epoch", _positive_int(self.epoch, "epoch"))
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "spec", MacroGraphBridgeRunSpec.from_mapping(self.spec))
        object.__setattr__(self, "activation_cursor", MacroObservationCursor.from_mapping(self.activation_cursor))
        object.__setattr__(self, "cursor", MacroObservationCursor.from_mapping(self.cursor))
        object.__setattr__(self, "block_reason", reason)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "epoch": self.epoch,
            "status": self.status,
            "spec": self.spec.to_dict(),
            "activation_cursor": self.activation_cursor.to_dict(),
            "cursor": self.cursor.to_dict(),
            "block_reason": self.block_reason,
        }


def _canonical_scope_outputs(mapping: WorldScopeMapping) -> frozenset[tuple[str, str]]:
    outputs = {("world", "market")}
    for entry in mapping.entries:
        outputs.add((entry.venue.kind, entry.venue.entity_id))
        outputs.add((entry.country.kind, entry.country.entity_id))
        outputs.add((entry.region.kind, entry.region.entity_id))
        outputs.add((entry.world.kind, entry.world.entity_id))
    return frozenset(outputs)


@dataclass(frozen=True)
class MacroObservationKnowledgeLink:
    """Pure OBSERVES factory. Never invents a relation for an unknown scope."""

    status: str
    relation: KnowledgeWorldRelation | None = None
    skip_reason: str | None = None
    observation_ref: WorldObservationRef | None = None

    def __post_init__(self) -> None:
        status = _required_text(self.status, "status")
        if status not in MACRO_GRAPH_LINK_STATUSES:
            allowed = ", ".join(sorted(MACRO_GRAPH_LINK_STATUSES))
            raise ValueError(f"link status must be one of: {allowed}")
        if status == "linked":
            if not isinstance(self.relation, KnowledgeWorldRelation):
                raise TypeError("linked result requires KnowledgeWorldRelation")
            if self.skip_reason is not None:
                raise ValueError("linked result must not carry a skip_reason")
            if not isinstance(self.observation_ref, WorldObservationRef):
                raise TypeError("linked result requires WorldObservationRef")
        else:
            if self.relation is not None:
                raise ValueError("skipped result must not carry a relation")
            reason = _required_text(self.skip_reason, "skip_reason")
            if reason not in MACRO_GRAPH_SKIP_REASONS:
                allowed = ", ".join(sorted(MACRO_GRAPH_SKIP_REASONS))
                raise ValueError(f"skip_reason must be one of: {allowed}")
            object.__setattr__(self, "skip_reason", reason)
        object.__setattr__(self, "status", status)

    @classmethod
    def from_envelope(
        cls,
        envelope: Any,
        scope_mapping: WorldScopeMapping,
        structural_revision: WorldOntologyRevision,
    ) -> MacroObservationKnowledgeLink:
        if not isinstance(scope_mapping, WorldScopeMapping):
            raise TypeError("scope_mapping must be WorldScopeMapping")
        if not isinstance(structural_revision, WorldOntologyRevision):
            raise TypeError("structural_revision must be WorldOntologyRevision")
        if (
            structural_revision.scope_mapping_id != scope_mapping.mapping_id
            or structural_revision.scope_mapping_hash != scope_mapping.content_sha256
        ):
            raise ValueError("scope mapping hash does not match structural revision")
        try:
            if not isinstance(envelope, MacroObservationEnvelope):
                raise TypeError("envelope must be MacroObservationEnvelope")
            observation = envelope.observation
            WorldCanonicalScopeRef(kind=observation.scope.kind, entity_id=observation.scope.entity_id)
            key = (observation.scope.kind, observation.scope.entity_id)
            if key not in _canonical_scope_outputs(scope_mapping):
                return cls(status="skipped", skip_reason="scope_not_registered")
            if not is_admitted_macro_producer(observation.producer_version):
                return cls(status="skipped", skip_reason="producer_not_admitted")
            observation_ref = world_observation_ref_for_observation_id(observation.observation_id)
            receipt = envelope.persisted.receipt
            provenance = MacroObservationProvenance(
                producer_version=observation.producer_version,
                origin_scope=observation.scope,
                observation_id=observation.observation_id,
                observation_sha256=observation.content_sha256,
                fact_refs=observation.fact_refs,
                mapping_id=scope_mapping.mapping_id,
                mapping_sha256=scope_mapping.content_sha256,
            )
            relation = KnowledgeWorldRelation(
                kind="OBSERVES",
                source=observation_ref,
                target=WorldEntityRef(kind=observation.scope.kind, entity_id=observation.scope.entity_id),
                effective_from=observation.cutoff_at,
                effective_until=observation.valid_until,
                ontology_revision=structural_revision.revision_id,
                source_refs=(
                    *provenance.to_source_refs(),
                    f"{receipt.receipt_id}/{receipt.receipt_sha256}",
                ),
            )
            return cls(status="linked", relation=relation, observation_ref=observation_ref)
        except (TypeError, ValueError):
            return cls(status="skipped", skip_reason="invalid_envelope")


def admits_macro_observes_relation(
    relation: KnowledgeWorldRelation,
    *,
    admitted_producer_version: str = MACRO_PRODUCER_VERSION,
) -> bool:
    """Keep non-OBSERVES knowledge; admit OBSERVES only with the live producer token."""

    if not isinstance(relation, KnowledgeWorldRelation):
        raise TypeError("relation must be KnowledgeWorldRelation")
    if relation.kind != "OBSERVES":
        return True
    return macro_observes_producer_ref(admitted_producer_version) in relation.source_refs


_ANCESTRY_WALK = (
    ("TRADED_ON", "venue"),
    ("LOCATED_IN", "country"),
    ("LOCATED_IN", "region"),
    ("PART_OF_WORLD", "world"),
)


def ancestry_distance_from_instrument_root(
    *,
    root: WorldEntityRef,
    origin: WorldEntityRef,
    relations: Sequence[StructuralWorldRelation],
) -> int | None:
    """Venue=0 … world=3 along unique TRADED_ON/LOCATED_IN/PART_OF_WORLD hops.

    Returns None when the origin is not on that unique ancestry. Never invents a hop.
    """

    if not isinstance(root, WorldEntityRef) or not isinstance(origin, WorldEntityRef):
        raise TypeError("root and origin must be WorldEntityRef")
    if root.kind != "instrument":
        raise ValueError("ancestry distance requires an instrument root")
    outgoing: dict[str, list[StructuralWorldRelation]] = {}
    for relation in relations:
        if not isinstance(relation, StructuralWorldRelation):
            raise TypeError("relations must be StructuralWorldRelation")
        outgoing.setdefault(relation.source.node_id, []).append(relation)
    current = root
    for distance, (kind, target_kind) in enumerate(_ANCESTRY_WALK):
        matches = [
            item for item in outgoing.get(current.node_id, ()) if item.kind == kind and item.target.kind == target_kind
        ]
        if len(matches) != 1:
            return None
        nxt = matches[0].target
        if nxt.node_id == origin.node_id:
            return distance
        current = nxt
    return None


def reconstruct_macro_observes_provenance(
    relation: KnowledgeWorldRelation,
    *,
    root: WorldEntityRef | None = None,
    structural_relations: Sequence[StructuralWorldRelation] = (),
) -> MacroObservationProvenance:
    """Rebuild producer, origin scope, lineage, and ancestry distance without fabricating them."""

    if not isinstance(relation, KnowledgeWorldRelation):
        raise TypeError("relation must be KnowledgeWorldRelation")
    if relation.kind != "OBSERVES":
        raise ValueError("provenance reconstruction is limited to OBSERVES relations")
    if not isinstance(relation.target, WorldEntityRef):
        raise TypeError("OBSERVES target must be WorldEntityRef")
    origin = MacroScope(kind=relation.target.kind, entity_id=relation.target.entity_id)
    distance = None
    if root is not None:
        distance = ancestry_distance_from_instrument_root(
            root=root,
            origin=relation.target,
            relations=structural_relations,
        )
    return MacroObservationProvenance.from_source_refs(
        relation.source_refs,
        origin_scope=origin,
        ancestry_distance=distance,
    )


def _set_bridge_event_id(event: Any, payload: Mapping[str, Any]) -> None:
    provided_schema = _required_text(getattr(event, "schema_version"), "schema_version")
    if provided_schema != MACRO_GRAPH_BRIDGE_EVENT_SCHEMA:
        raise ValueError(f"schema_version must be {MACRO_GRAPH_BRIDGE_EVENT_SCHEMA}")
    computed = _prefixed_id(_BRIDGE_EVENT_PREFIX, payload)
    provided = getattr(event, "event_id")
    if provided is not None and _required_text(provided, "event_id") != computed:
        raise ValueError("event_id does not match the canonical event identity")
    object.__setattr__(event, "schema_version", provided_schema)
    object.__setattr__(event, "event_id", computed)


def _bridge_run_id(payload: Mapping[str, Any]) -> str:
    return _prefixed_id(_BRIDGE_RUN_PREFIX, payload)


@dataclass(frozen=True)
class MacroGraphBridgeActivated:
    bridge_key: str
    run_id: str
    epoch: int
    spec: MacroGraphBridgeRunSpec | Mapping[str, Any]
    activation_cursor: MacroObservationCursor | Mapping[str, Any]
    reservation_id: str
    event_id: str | None = None
    schema_version: str = MACRO_GRAPH_BRIDGE_EVENT_SCHEMA
    event_type: str = "macro_graph_bridge_activated"

    def __post_init__(self) -> None:
        spec = MacroGraphBridgeRunSpec.from_mapping(self.spec)
        cursor = MacroObservationCursor.from_mapping(self.activation_cursor)
        bridge_key = _required_text(self.bridge_key, "bridge_key")
        reservation_id = _validate_prefixed_id(self.reservation_id, _RESERVATION_PREFIX, "reservation_id")
        epoch = _positive_int(self.epoch, "epoch")
        run_id = _validate_prefixed_id(self.run_id, _BRIDGE_RUN_PREFIX, "run_id")
        object.__setattr__(self, "bridge_key", bridge_key)
        object.__setattr__(self, "run_id", run_id)
        object.__setattr__(self, "epoch", epoch)
        object.__setattr__(self, "spec", spec)
        object.__setattr__(self, "activation_cursor", cursor)
        object.__setattr__(self, "reservation_id", reservation_id)
        object.__setattr__(self, "event_type", "macro_graph_bridge_activated")
        _set_bridge_event_id(
            self,
            {
                "event_type": "macro_graph_bridge_activated",
                "schema_version": MACRO_GRAPH_BRIDGE_EVENT_SCHEMA,
                "bridge_key": bridge_key,
                "run_id": run_id,
                "epoch": epoch,
                "spec": spec.to_dict(),
                "activation_cursor": cursor.to_dict(),
                "reservation_id": reservation_id,
            },
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "event_type": self.event_type,
            "event_id": self.event_id,
            "bridge_key": self.bridge_key,
            "run_id": self.run_id,
            "epoch": self.epoch,
            "spec": self.spec.to_dict(),
            "activation_cursor": self.activation_cursor.to_dict(),
            "reservation_id": self.reservation_id,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | MacroGraphBridgeActivated) -> MacroGraphBridgeActivated:
        if isinstance(value, MacroGraphBridgeActivated):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("activated event must be a mapping")
        return cls(
            bridge_key=value.get("bridge_key"),
            run_id=value.get("run_id"),
            epoch=value.get("epoch"),
            spec=value.get("spec"),
            activation_cursor=value.get("activation_cursor"),
            reservation_id=value.get("reservation_id"),
            event_id=value.get("event_id"),
            schema_version=value.get("schema_version", MACRO_GRAPH_BRIDGE_EVENT_SCHEMA),
        )


@dataclass(frozen=True)
class MacroGraphObservationLinked:
    bridge_key: str
    run_id: str
    epoch: int
    observation_id: str
    relation_id: str
    relation_event_id: str
    cursor: MacroObservationCursor | Mapping[str, Any]
    event_id: str | None = None
    schema_version: str = MACRO_GRAPH_BRIDGE_EVENT_SCHEMA
    event_type: str = "macro_graph_observation_linked"

    def __post_init__(self) -> None:
        cursor = MacroObservationCursor.from_mapping(self.cursor)
        if cursor.ordinal < 1:
            raise ValueError("linked observation cursor must be after the origin")
        object.__setattr__(self, "bridge_key", _required_text(self.bridge_key, "bridge_key"))
        object.__setattr__(self, "run_id", _validate_prefixed_id(self.run_id, _BRIDGE_RUN_PREFIX, "run_id"))
        object.__setattr__(self, "epoch", _positive_int(self.epoch, "epoch"))
        object.__setattr__(
            self,
            "observation_id",
            _validate_prefixed_id(self.observation_id, _MACRO_OBSERVATION_ID_PREFIX, "observation_id"),
        )
        object.__setattr__(
            self, "relation_id", _validate_prefixed_id(self.relation_id, _RELATION_ID_PREFIX, "relation_id")
        )
        object.__setattr__(
            self,
            "relation_event_id",
            _validate_prefixed_id(self.relation_event_id, _RELATION_EVENT_PREFIX, "relation_event_id"),
        )
        object.__setattr__(self, "cursor", cursor)
        object.__setattr__(self, "event_type", "macro_graph_observation_linked")
        _set_bridge_event_id(
            self,
            {
                "event_type": "macro_graph_observation_linked",
                "schema_version": MACRO_GRAPH_BRIDGE_EVENT_SCHEMA,
                "bridge_key": self.bridge_key,
                "run_id": self.run_id,
                "epoch": self.epoch,
                "observation_id": self.observation_id,
                "relation_id": self.relation_id,
                "relation_event_id": self.relation_event_id,
                "cursor": cursor.to_dict(),
            },
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "event_type": self.event_type,
            "event_id": self.event_id,
            "bridge_key": self.bridge_key,
            "run_id": self.run_id,
            "epoch": self.epoch,
            "observation_id": self.observation_id,
            "relation_id": self.relation_id,
            "relation_event_id": self.relation_event_id,
            "cursor": self.cursor.to_dict(),
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | MacroGraphObservationLinked) -> MacroGraphObservationLinked:
        if isinstance(value, MacroGraphObservationLinked):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("linked event must be a mapping")
        return cls(
            bridge_key=value.get("bridge_key"),
            run_id=value.get("run_id"),
            epoch=value.get("epoch"),
            observation_id=value.get("observation_id"),
            relation_id=value.get("relation_id"),
            relation_event_id=value.get("relation_event_id"),
            cursor=value.get("cursor"),
            event_id=value.get("event_id"),
            schema_version=value.get("schema_version", MACRO_GRAPH_BRIDGE_EVENT_SCHEMA),
        )


@dataclass(frozen=True)
class MacroGraphObservationSkipped:
    bridge_key: str
    run_id: str
    epoch: int
    observation_id: str
    reason: str
    cursor: MacroObservationCursor | Mapping[str, Any]
    event_id: str | None = None
    schema_version: str = MACRO_GRAPH_BRIDGE_EVENT_SCHEMA
    event_type: str = "macro_graph_observation_skipped"

    def __post_init__(self) -> None:
        reason = _required_text(self.reason, "reason")
        if reason not in MACRO_GRAPH_SKIP_REASONS:
            allowed = ", ".join(sorted(MACRO_GRAPH_SKIP_REASONS))
            raise ValueError(f"skip reason must be one of: {allowed}")
        cursor = MacroObservationCursor.from_mapping(self.cursor)
        if cursor.ordinal < 1:
            raise ValueError("skipped observation cursor must be after the origin")
        object.__setattr__(self, "bridge_key", _required_text(self.bridge_key, "bridge_key"))
        object.__setattr__(self, "run_id", _validate_prefixed_id(self.run_id, _BRIDGE_RUN_PREFIX, "run_id"))
        object.__setattr__(self, "epoch", _positive_int(self.epoch, "epoch"))
        object.__setattr__(
            self,
            "observation_id",
            _validate_prefixed_id(self.observation_id, _MACRO_OBSERVATION_ID_PREFIX, "observation_id"),
        )
        object.__setattr__(self, "reason", reason)
        object.__setattr__(self, "cursor", cursor)
        object.__setattr__(self, "event_type", "macro_graph_observation_skipped")
        _set_bridge_event_id(
            self,
            {
                "event_type": "macro_graph_observation_skipped",
                "schema_version": MACRO_GRAPH_BRIDGE_EVENT_SCHEMA,
                "bridge_key": self.bridge_key,
                "run_id": self.run_id,
                "epoch": self.epoch,
                "observation_id": self.observation_id,
                "reason": reason,
                "cursor": cursor.to_dict(),
            },
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "event_type": self.event_type,
            "event_id": self.event_id,
            "bridge_key": self.bridge_key,
            "run_id": self.run_id,
            "epoch": self.epoch,
            "observation_id": self.observation_id,
            "reason": self.reason,
            "cursor": self.cursor.to_dict(),
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | MacroGraphObservationSkipped) -> MacroGraphObservationSkipped:
        if isinstance(value, MacroGraphObservationSkipped):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("skipped event must be a mapping")
        return cls(
            bridge_key=value.get("bridge_key"),
            run_id=value.get("run_id"),
            epoch=value.get("epoch"),
            observation_id=value.get("observation_id"),
            reason=value.get("reason"),
            cursor=value.get("cursor"),
            event_id=value.get("event_id"),
            schema_version=value.get("schema_version", MACRO_GRAPH_BRIDGE_EVENT_SCHEMA),
        )


@dataclass(frozen=True)
class MacroGraphBridgeCursorAdvanced:
    bridge_key: str
    run_id: str
    epoch: int
    cursor: MacroObservationCursor | Mapping[str, Any]
    event_id: str | None = None
    schema_version: str = MACRO_GRAPH_BRIDGE_EVENT_SCHEMA
    event_type: str = "macro_graph_bridge_cursor_advanced"

    def __post_init__(self) -> None:
        cursor = MacroObservationCursor.from_mapping(self.cursor)
        object.__setattr__(self, "bridge_key", _required_text(self.bridge_key, "bridge_key"))
        object.__setattr__(self, "run_id", _validate_prefixed_id(self.run_id, _BRIDGE_RUN_PREFIX, "run_id"))
        object.__setattr__(self, "epoch", _positive_int(self.epoch, "epoch"))
        object.__setattr__(self, "cursor", cursor)
        object.__setattr__(self, "event_type", "macro_graph_bridge_cursor_advanced")
        _set_bridge_event_id(
            self,
            {
                "event_type": "macro_graph_bridge_cursor_advanced",
                "schema_version": MACRO_GRAPH_BRIDGE_EVENT_SCHEMA,
                "bridge_key": self.bridge_key,
                "run_id": self.run_id,
                "epoch": self.epoch,
                "cursor": cursor.to_dict(),
            },
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "event_type": self.event_type,
            "event_id": self.event_id,
            "bridge_key": self.bridge_key,
            "run_id": self.run_id,
            "epoch": self.epoch,
            "cursor": self.cursor.to_dict(),
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | MacroGraphBridgeCursorAdvanced) -> MacroGraphBridgeCursorAdvanced:
        if isinstance(value, MacroGraphBridgeCursorAdvanced):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("cursor advanced event must be a mapping")
        return cls(
            bridge_key=value.get("bridge_key"),
            run_id=value.get("run_id"),
            epoch=value.get("epoch"),
            cursor=value.get("cursor"),
            event_id=value.get("event_id"),
            schema_version=value.get("schema_version", MACRO_GRAPH_BRIDGE_EVENT_SCHEMA),
        )


@dataclass(frozen=True)
class MacroGraphBridgeBlocked:
    bridge_key: str
    run_id: str
    epoch: int
    reason: str
    event_id: str | None = None
    schema_version: str = MACRO_GRAPH_BRIDGE_EVENT_SCHEMA
    event_type: str = "macro_graph_bridge_blocked"

    def __post_init__(self) -> None:
        reason = _required_text(self.reason, "reason")
        if reason not in MACRO_GRAPH_BLOCK_REASONS:
            raise ValueError("block reason must be config_drift")
        object.__setattr__(self, "bridge_key", _required_text(self.bridge_key, "bridge_key"))
        object.__setattr__(self, "run_id", _validate_prefixed_id(self.run_id, _BRIDGE_RUN_PREFIX, "run_id"))
        object.__setattr__(self, "epoch", _positive_int(self.epoch, "epoch"))
        object.__setattr__(self, "reason", reason)
        object.__setattr__(self, "event_type", "macro_graph_bridge_blocked")
        _set_bridge_event_id(
            self,
            {
                "event_type": "macro_graph_bridge_blocked",
                "schema_version": MACRO_GRAPH_BRIDGE_EVENT_SCHEMA,
                "bridge_key": self.bridge_key,
                "run_id": self.run_id,
                "epoch": self.epoch,
                "reason": reason,
            },
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "event_type": self.event_type,
            "event_id": self.event_id,
            "bridge_key": self.bridge_key,
            "run_id": self.run_id,
            "epoch": self.epoch,
            "reason": self.reason,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | MacroGraphBridgeBlocked) -> MacroGraphBridgeBlocked:
        if isinstance(value, MacroGraphBridgeBlocked):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("blocked event must be a mapping")
        return cls(
            bridge_key=value.get("bridge_key"),
            run_id=value.get("run_id"),
            epoch=value.get("epoch"),
            reason=value.get("reason"),
            event_id=value.get("event_id"),
            schema_version=value.get("schema_version", MACRO_GRAPH_BRIDGE_EVENT_SCHEMA),
        )


@dataclass(frozen=True)
class MacroGraphBridgeResumed:
    bridge_key: str
    run_id: str
    epoch: int
    spec: MacroGraphBridgeRunSpec | Mapping[str, Any]
    event_id: str | None = None
    schema_version: str = MACRO_GRAPH_BRIDGE_EVENT_SCHEMA
    event_type: str = "macro_graph_bridge_resumed"

    def __post_init__(self) -> None:
        spec = MacroGraphBridgeRunSpec.from_mapping(self.spec)
        object.__setattr__(self, "bridge_key", _required_text(self.bridge_key, "bridge_key"))
        object.__setattr__(self, "run_id", _validate_prefixed_id(self.run_id, _BRIDGE_RUN_PREFIX, "run_id"))
        object.__setattr__(self, "epoch", _positive_int(self.epoch, "epoch"))
        object.__setattr__(self, "spec", spec)
        object.__setattr__(self, "event_type", "macro_graph_bridge_resumed")
        _set_bridge_event_id(
            self,
            {
                "event_type": "macro_graph_bridge_resumed",
                "schema_version": MACRO_GRAPH_BRIDGE_EVENT_SCHEMA,
                "bridge_key": self.bridge_key,
                "run_id": self.run_id,
                "epoch": self.epoch,
                "spec": spec.to_dict(),
            },
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "event_type": self.event_type,
            "event_id": self.event_id,
            "bridge_key": self.bridge_key,
            "run_id": self.run_id,
            "epoch": self.epoch,
            "spec": self.spec.to_dict(),
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | MacroGraphBridgeResumed) -> MacroGraphBridgeResumed:
        if isinstance(value, MacroGraphBridgeResumed):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("resumed event must be a mapping")
        return cls(
            bridge_key=value.get("bridge_key"),
            run_id=value.get("run_id"),
            epoch=value.get("epoch"),
            spec=value.get("spec"),
            event_id=value.get("event_id"),
            schema_version=value.get("schema_version", MACRO_GRAPH_BRIDGE_EVENT_SCHEMA),
        )


@dataclass(frozen=True)
class MacroGraphBridgeRunHandedOff:
    bridge_key: str
    predecessor_run_id: str
    successor_run_id: str
    epoch: int
    predecessor_epoch: int
    spec: MacroGraphBridgeRunSpec | Mapping[str, Any]
    cursor: MacroObservationCursor | Mapping[str, Any]
    event_id: str | None = None
    schema_version: str = MACRO_GRAPH_BRIDGE_EVENT_SCHEMA
    event_type: str = "macro_graph_bridge_run_handed_off"

    def __post_init__(self) -> None:
        spec = MacroGraphBridgeRunSpec.from_mapping(self.spec)
        cursor = MacroObservationCursor.from_mapping(self.cursor)
        predecessor_run_id = _validate_prefixed_id(self.predecessor_run_id, _BRIDGE_RUN_PREFIX, "predecessor_run_id")
        successor_run_id = _validate_prefixed_id(self.successor_run_id, _BRIDGE_RUN_PREFIX, "successor_run_id")
        if predecessor_run_id == successor_run_id:
            raise ValueError("successor_run_id must differ from predecessor_run_id")
        object.__setattr__(self, "bridge_key", _required_text(self.bridge_key, "bridge_key"))
        object.__setattr__(self, "predecessor_run_id", predecessor_run_id)
        object.__setattr__(self, "successor_run_id", successor_run_id)
        object.__setattr__(self, "epoch", _positive_int(self.epoch, "epoch"))
        object.__setattr__(self, "predecessor_epoch", _positive_int(self.predecessor_epoch, "predecessor_epoch"))
        object.__setattr__(self, "spec", spec)
        object.__setattr__(self, "cursor", cursor)
        object.__setattr__(self, "event_type", "macro_graph_bridge_run_handed_off")
        _set_bridge_event_id(
            self,
            {
                "event_type": "macro_graph_bridge_run_handed_off",
                "schema_version": MACRO_GRAPH_BRIDGE_EVENT_SCHEMA,
                "bridge_key": self.bridge_key,
                "predecessor_run_id": predecessor_run_id,
                "successor_run_id": successor_run_id,
                "epoch": self.epoch,
                "predecessor_epoch": self.predecessor_epoch,
                "spec": spec.to_dict(),
                "cursor": cursor.to_dict(),
            },
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "event_type": self.event_type,
            "event_id": self.event_id,
            "bridge_key": self.bridge_key,
            "predecessor_run_id": self.predecessor_run_id,
            "successor_run_id": self.successor_run_id,
            "epoch": self.epoch,
            "predecessor_epoch": self.predecessor_epoch,
            "spec": self.spec.to_dict(),
            "cursor": self.cursor.to_dict(),
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | MacroGraphBridgeRunHandedOff) -> MacroGraphBridgeRunHandedOff:
        if isinstance(value, MacroGraphBridgeRunHandedOff):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("handoff event must be a mapping")
        return cls(
            bridge_key=value.get("bridge_key"),
            predecessor_run_id=value.get("predecessor_run_id"),
            successor_run_id=value.get("successor_run_id"),
            epoch=value.get("epoch"),
            predecessor_epoch=value.get("predecessor_epoch"),
            spec=value.get("spec"),
            cursor=value.get("cursor"),
            event_id=value.get("event_id"),
            schema_version=value.get("schema_version", MACRO_GRAPH_BRIDGE_EVENT_SCHEMA),
        )


MacroGraphBridgeEvent = (
    MacroGraphBridgeActivated
    | MacroGraphObservationLinked
    | MacroGraphObservationSkipped
    | MacroGraphBridgeCursorAdvanced
    | MacroGraphBridgeBlocked
    | MacroGraphBridgeResumed
    | MacroGraphBridgeRunHandedOff
)
_BRIDGE_EVENT_CLASSES = (
    MacroGraphBridgeActivated,
    MacroGraphObservationLinked,
    MacroGraphObservationSkipped,
    MacroGraphBridgeCursorAdvanced,
    MacroGraphBridgeBlocked,
    MacroGraphBridgeResumed,
    MacroGraphBridgeRunHandedOff,
)
_BRIDGE_EVENT_PARSERS = {
    "macro_graph_bridge_activated": MacroGraphBridgeActivated.from_mapping,
    "macro_graph_observation_linked": MacroGraphObservationLinked.from_mapping,
    "macro_graph_observation_skipped": MacroGraphObservationSkipped.from_mapping,
    "macro_graph_bridge_cursor_advanced": MacroGraphBridgeCursorAdvanced.from_mapping,
    "macro_graph_bridge_blocked": MacroGraphBridgeBlocked.from_mapping,
    "macro_graph_bridge_resumed": MacroGraphBridgeResumed.from_mapping,
    "macro_graph_bridge_run_handed_off": MacroGraphBridgeRunHandedOff.from_mapping,
}


def parse_macro_graph_bridge_event(
    value: Mapping[str, Any] | MacroGraphBridgeEvent,
) -> MacroGraphBridgeEvent:
    if isinstance(value, _BRIDGE_EVENT_CLASSES):
        return value
    if not isinstance(value, Mapping):
        raise TypeError("bridge event must be a mapping or typed event")
    event_type = _required_text(value.get("event_type"), "event_type")
    try:
        parser = _BRIDGE_EVENT_PARSERS[event_type]
    except KeyError as exc:
        raise ValueError(f"unknown macro graph bridge event_type: {event_type}") from exc
    return parser(value)


@dataclass(frozen=True)
class _BridgeTerminal:
    kind: str
    observation_id: str
    ordinal: int
    reason: str | None = None
    relation_id: str | None = None
    relation_event_id: str | None = None


def _fold_bridge_events(
    bridge_key: str,
    events: Sequence[MacroGraphBridgeEvent],
) -> tuple[tuple[MacroGraphBridgeRun, ...], MacroGraphBridgeRun | None, dict[str, _BridgeTerminal]]:
    runs: dict[str, MacroGraphBridgeRun] = {}
    active: MacroGraphBridgeRun | None = None
    terminals: dict[str, _BridgeTerminal] = {}
    for event in events:
        if event.bridge_key != bridge_key:
            raise ValueError("event bridge_key mismatch")
        if isinstance(event, MacroGraphBridgeActivated):
            if active is not None and active.status == "active":
                raise ValueError("active generation already exists")
            run = MacroGraphBridgeRun(
                run_id=event.run_id,
                epoch=event.epoch,
                status="active",
                spec=event.spec,
                activation_cursor=event.activation_cursor,
                cursor=event.activation_cursor,
            )
            runs[run.run_id] = run
            active = run
            continue
        if active is None:
            raise ValueError("bridge event requires an active generation")
        if isinstance(event, MacroGraphBridgeRunHandedOff):
            if active.run_id != event.predecessor_run_id:
                raise ValueError("handoff predecessor is not the active run")
            if active.status != "blocked":
                raise ValueError("handoff requires a blocked active run")
            runs[active.run_id] = MacroGraphBridgeRun(
                run_id=active.run_id,
                epoch=active.epoch,
                status="superseded",
                spec=active.spec,
                activation_cursor=active.activation_cursor,
                cursor=active.cursor,
            )
            successor = MacroGraphBridgeRun(
                run_id=event.successor_run_id,
                epoch=event.epoch,
                status="active",
                spec=event.spec,
                activation_cursor=event.cursor,
                cursor=event.cursor,
            )
            runs[successor.run_id] = successor
            active = successor
            continue
        if event.run_id != active.run_id or event.epoch != active.epoch:
            raise ValueError("event does not target the active generation")
        if isinstance(event, MacroGraphObservationSkipped):
            terminal = _BridgeTerminal(
                kind="skipped",
                observation_id=event.observation_id,
                ordinal=event.cursor.ordinal,
                reason=event.reason,
            )
            existing = terminals.get(event.observation_id)
            if existing is not None and existing != terminal:
                raise ValueError("conflict: same observation with different terminal content")
            terminals[event.observation_id] = terminal
            continue
        if isinstance(event, MacroGraphObservationLinked):
            terminal = _BridgeTerminal(
                kind="linked",
                observation_id=event.observation_id,
                ordinal=event.cursor.ordinal,
                relation_id=event.relation_id,
                relation_event_id=event.relation_event_id,
            )
            existing = terminals.get(event.observation_id)
            if existing is not None and existing != terminal:
                raise ValueError("conflict: same observation with different terminal content")
            terminals[event.observation_id] = terminal
            continue
        if isinstance(event, MacroGraphBridgeCursorAdvanced):
            if event.cursor.receipt_log_generation != active.cursor.receipt_log_generation:
                raise ValueError("cursor generation mismatch")
            if event.cursor.ordinal < active.cursor.ordinal:
                raise ValueError("cursor cannot move backwards")
            by_ordinal = {item.ordinal: item for item in terminals.values()}
            for ordinal in range(active.cursor.ordinal + 1, event.cursor.ordinal + 1):
                if ordinal not in by_ordinal:
                    raise ValueError("cursor cannot skip a non-terminal observation")
            active = MacroGraphBridgeRun(
                run_id=active.run_id,
                epoch=active.epoch,
                status=active.status,
                spec=active.spec,
                activation_cursor=active.activation_cursor,
                cursor=event.cursor,
                block_reason=active.block_reason,
            )
            runs[active.run_id] = active
            continue
        if isinstance(event, MacroGraphBridgeBlocked):
            if active.status == "blocked":
                continue
            active = MacroGraphBridgeRun(
                run_id=active.run_id,
                epoch=active.epoch,
                status="blocked",
                spec=active.spec,
                activation_cursor=active.activation_cursor,
                cursor=active.cursor,
                block_reason=event.reason,
            )
            runs[active.run_id] = active
            continue
        if isinstance(event, MacroGraphBridgeResumed):
            if active.status != "blocked":
                raise ValueError("resume requires a blocked run")
            if event.spec != active.spec:
                raise ValueError("resume requires the same frozen spec hashes")
            active = MacroGraphBridgeRun(
                run_id=active.run_id,
                epoch=active.epoch,
                status="active",
                spec=active.spec,
                activation_cursor=active.activation_cursor,
                cursor=active.cursor,
            )
            runs[active.run_id] = active
            continue
        raise TypeError(f"unsupported bridge event: {type(event).__name__}")
    ordered = tuple(sorted(runs.values(), key=lambda item: (item.epoch, item.run_id)))
    return ordered, active, terminals


@dataclass(frozen=True)
class MacroGraphBridgeRegistry:
    """Append-only single-active coordinator for the macro→graph bridge."""

    bridge_key: str
    events: Sequence[MacroGraphBridgeEvent | Mapping[str, Any]] = ()

    def __post_init__(self) -> None:
        bridge_key = _required_text(self.bridge_key, "bridge_key")
        events = tuple(parse_macro_graph_bridge_event(item) for item in self.events)
        _fold_bridge_events(bridge_key, events)
        object.__setattr__(self, "bridge_key", bridge_key)
        object.__setattr__(self, "events", events)

    @classmethod
    def empty(cls, bridge_key: str) -> MacroGraphBridgeRegistry:
        return cls(bridge_key=bridge_key, events=())

    @property
    def version(self) -> int:
        return len(self.events)

    def _state(
        self,
    ) -> tuple[tuple[MacroGraphBridgeRun, ...], MacroGraphBridgeRun | None, dict[str, _BridgeTerminal]]:
        return _fold_bridge_events(self.bridge_key, self.events)

    @property
    def runs(self) -> tuple[MacroGraphBridgeRun, ...]:
        return self._state()[0]

    @property
    def active_run(self) -> MacroGraphBridgeRun | None:
        return self._state()[1]

    @property
    def fence(self) -> MacroGraphBridgeFence | None:
        run = self.active_run
        if run is None:
            return None
        return MacroGraphBridgeFence(bridge_key=self.bridge_key, run_id=run.run_id, epoch=run.epoch)

    def _require_version(self, expected_version: int) -> None:
        expected = _non_negative_int(expected_version, "expected_version")
        if expected != self.version:
            raise ValueError("expected_version does not match the registry version")

    def _append(self, event: MacroGraphBridgeEvent) -> MacroGraphBridgeRegistry:
        return MacroGraphBridgeRegistry(bridge_key=self.bridge_key, events=(*self.events, event))

    def _require_active(self) -> MacroGraphBridgeRun:
        run = self.active_run
        if run is None:
            raise ValueError("bridge has no active generation")
        return run

    def _require_active_unblocked(self) -> MacroGraphBridgeRun:
        run = self._require_active()
        if run.status == "blocked":
            raise ValueError("active generation is blocked")
        if run.status != "active":
            raise ValueError("bridge has no active generation")
        return run

    def activate(
        self,
        *,
        reservation: MacroObservationCursorReservation,
        spec: MacroGraphBridgeRunSpec,
        expected_version: int,
    ) -> MacroGraphBridgeRegistry:
        self._require_version(expected_version)
        if not isinstance(reservation, MacroObservationCursorReservation):
            raise TypeError("reservation must be MacroObservationCursorReservation")
        if not isinstance(spec, MacroGraphBridgeRunSpec):
            raise TypeError("spec must be MacroGraphBridgeRunSpec")
        if reservation.bridge_key != self.bridge_key:
            raise ValueError("reservation bridge_key mismatch")
        run = self.active_run
        if run is not None:
            activated = self.events[0] if self.events else None
            if (
                isinstance(activated, MacroGraphBridgeActivated)
                and activated.reservation_id == reservation.reservation_id
                and run.spec == spec
                and run.activation_cursor == reservation.cursor
            ):
                return self
            raise ValueError("active generation already exists")
        run_id = _bridge_run_id(
            {
                "bridge_key": self.bridge_key,
                "epoch": 1,
                "spec": spec.to_dict(),
                "activation_cursor": reservation.cursor.to_dict(),
                "reservation_id": reservation.reservation_id,
            }
        )
        event = MacroGraphBridgeActivated(
            bridge_key=self.bridge_key,
            run_id=run_id,
            epoch=1,
            spec=spec,
            activation_cursor=reservation.cursor,
            reservation_id=reservation.reservation_id,
        )
        return self._append(event)

    def skip_observation(
        self,
        *,
        observation_id: str,
        reason: str,
        cursor: MacroObservationCursor,
        expected_version: int,
    ) -> MacroGraphBridgeRegistry:
        self._require_version(expected_version)
        run = self._require_active()
        terminals = self._state()[2]
        existing = terminals.get(observation_id)
        event = MacroGraphObservationSkipped(
            bridge_key=self.bridge_key,
            run_id=run.run_id,
            epoch=run.epoch,
            observation_id=observation_id,
            reason=reason,
            cursor=cursor,
        )
        if existing is not None:
            candidate = _BridgeTerminal(
                kind="skipped",
                observation_id=event.observation_id,
                ordinal=event.cursor.ordinal,
                reason=event.reason,
            )
            if existing != candidate:
                raise ValueError("conflict: same observation with different terminal content")
            return self
        if run.status == "blocked":
            raise ValueError("active generation is blocked")
        self._assert_observation_cursor(run, cursor, observation_id)
        return self._append(event)

    def link_observation(
        self,
        *,
        observation_id: str,
        relation: KnowledgeWorldRelation,
        relation_event_id: str,
        cursor: MacroObservationCursor,
        expected_version: int,
    ) -> MacroGraphBridgeRegistry:
        self._require_version(expected_version)
        if not isinstance(relation, KnowledgeWorldRelation):
            raise TypeError("relation must be KnowledgeWorldRelation")
        run = self._require_active()
        terminals = self._state()[2]
        existing = terminals.get(observation_id)
        event = MacroGraphObservationLinked(
            bridge_key=self.bridge_key,
            run_id=run.run_id,
            epoch=run.epoch,
            observation_id=observation_id,
            relation_id=relation.relation_id,
            relation_event_id=relation_event_id,
            cursor=cursor,
        )
        if existing is not None:
            candidate = _BridgeTerminal(
                kind="linked",
                observation_id=event.observation_id,
                ordinal=event.cursor.ordinal,
                relation_id=event.relation_id,
                relation_event_id=event.relation_event_id,
            )
            if existing != candidate:
                raise ValueError("conflict: same observation with different terminal content")
            return self
        if run.status == "blocked":
            raise ValueError("active generation is blocked")
        self._assert_observation_cursor(run, cursor, observation_id)
        return self._append(event)

    def advance_cursor(self, cursor: MacroObservationCursor, expected_version: int) -> MacroGraphBridgeRegistry:
        self._require_version(expected_version)
        run = self._require_active_unblocked()
        if not isinstance(cursor, MacroObservationCursor):
            raise TypeError("cursor must be MacroObservationCursor")
        if cursor == run.cursor:
            return self
        event = MacroGraphBridgeCursorAdvanced(
            bridge_key=self.bridge_key,
            run_id=run.run_id,
            epoch=run.epoch,
            cursor=cursor,
        )
        return self._append(event)

    def block(self, *, reason: str, expected_version: int) -> MacroGraphBridgeRegistry:
        self._require_version(expected_version)
        run = self._require_active()
        if run.status == "blocked" and run.block_reason == reason:
            return self
        if run.status != "active":
            raise ValueError("only an active generation can be blocked")
        event = MacroGraphBridgeBlocked(
            bridge_key=self.bridge_key,
            run_id=run.run_id,
            epoch=run.epoch,
            reason=reason,
        )
        return self._append(event)

    def resume(self, *, spec: MacroGraphBridgeRunSpec, expected_version: int) -> MacroGraphBridgeRegistry:
        self._require_version(expected_version)
        if not isinstance(spec, MacroGraphBridgeRunSpec):
            raise TypeError("spec must be MacroGraphBridgeRunSpec")
        run = self._require_active()
        if run.status == "active" and run.spec == spec:
            return self
        if run.status != "blocked":
            raise ValueError("resume requires a blocked run")
        if spec != run.spec:
            raise ValueError("resume requires the same frozen spec hashes")
        event = MacroGraphBridgeResumed(
            bridge_key=self.bridge_key,
            run_id=run.run_id,
            epoch=run.epoch,
            spec=spec,
        )
        return self._append(event)

    def handoff(
        self,
        *,
        active_run_id: str,
        next_run_spec: MacroGraphBridgeRunSpec,
        expected_version: int,
    ) -> MacroGraphBridgeRegistry:
        self._require_version(expected_version)
        if not isinstance(next_run_spec, MacroGraphBridgeRunSpec):
            raise TypeError("next_run_spec must be MacroGraphBridgeRunSpec")
        run = self._require_active()
        if run.status != "blocked":
            raise ValueError("handoff requires a blocked active run")
        if run.run_id != _validate_prefixed_id(active_run_id, _BRIDGE_RUN_PREFIX, "active_run_id"):
            raise ValueError("handoff active_run_id does not match the blocked run")
        successor_run_id = _bridge_run_id(
            {
                "bridge_key": self.bridge_key,
                "epoch": run.epoch + 1,
                "spec": next_run_spec.to_dict(),
                "activation_cursor": run.cursor.to_dict(),
                "predecessor_run_id": run.run_id,
            }
        )
        event = MacroGraphBridgeRunHandedOff(
            bridge_key=self.bridge_key,
            predecessor_run_id=run.run_id,
            successor_run_id=successor_run_id,
            epoch=run.epoch + 1,
            predecessor_epoch=run.epoch,
            spec=next_run_spec,
            cursor=run.cursor,
        )
        return self._append(event)

    def _assert_observation_cursor(
        self,
        run: MacroGraphBridgeRun,
        cursor: MacroObservationCursor,
        observation_id: str,
    ) -> None:
        if not isinstance(cursor, MacroObservationCursor):
            raise TypeError("cursor must be MacroObservationCursor")
        if cursor.receipt_log_generation != run.activation_cursor.receipt_log_generation:
            raise ValueError("cursor generation mismatch")
        if cursor.ordinal <= run.activation_cursor.ordinal:
            raise ValueError("observation cursor is before the activation cursor")
        if cursor.observation_id != observation_id:
            raise ValueError("cursor observation_id mismatch")


__all__ = [
    "FORBIDDEN_RELATION_KINDS",
    "GRAPH_TRAVERSAL_POLICY_VERSION",
    "KNOWLEDGE_RELATION_KINDS",
    "MACRO_GRAPH_BLOCK_REASONS",
    "MACRO_GRAPH_BRIDGE_RUN_SPEC_SCHEMA",
    "MACRO_GRAPH_SKIP_REASONS",
    "STRUCTURAL_RELATION_KINDS",
    "WORLD_ENTITY_KINDS",
    "WORLD_GRAPH_SNAPSHOT_SCHEMA",
    "ancestry_distance_from_instrument_root",
    "reconstruct_macro_observes_provenance",
    "KnowledgeArtifactRef",
    "KnowledgeWorldRelation",
    "KnowledgeWorldRelationAsserted",
    "KnowledgeWorldRelationEvent",
    "KnowledgeWorldRelationRetired",
    "MacroGraphBridgeActivated",
    "MacroGraphBridgeBlocked",
    "MacroGraphBridgeCursorAdvanced",
    "MacroGraphBridgeEvent",
    "MacroGraphBridgeFence",
    "MacroGraphBridgeRegistry",
    "MacroGraphBridgeResumed",
    "MacroGraphBridgeRun",
    "MacroGraphBridgeRunHandedOff",
    "MacroGraphBridgeRunSpec",
    "MacroGraphObservationLinked",
    "MacroGraphObservationSkipped",
    "MacroObservationCursor",
    "MacroObservationCursorReservation",
    "MacroObservationKnowledgeLink",
    "MacroSourceFactVersionRef",
    "PatternHypothesisRef",
    "SensorRef",
    "StaleBridgeEpoch",
    "StructuralWorldRelation",
    "StructuralWorldRelationAsserted",
    "StructuralWorldRelationEvent",
    "StructuralWorldRelationRetired",
    "WorldEntityAsserted",
    "WorldEntityEvent",
    "WorldEntityIdentityEvent",
    "WorldEntityIdentityLink",
    "WorldEntityIdentityLinked",
    "WorldEntityIdentityLinkRef",
    "WorldEntityIdentityLinkSuperseded",
    "WorldEntityIdentityMap",
    "WorldEntityIdentityUnlinked",
    "WorldEntityRef",
    "WorldEntityRetired",
    "WorldEntityRevisionRef",
    "WorldEntitySuperseded",
    "WorldGraphNodeRef",
    "WorldGraphSnapshot",
    "WorldGraphSnapshotRef",
    "WorldKnowledgeRelationRef",
    "WorldObservationRef",
    "WorldOntologyRevision",
    "WorldOntologyRevisionEvent",
    "WorldOntologyRevisionPublished",
    "WorldOntologyRevisionSuperseded",
    "WorldRelation",
    "WorldRelationEvent",
    "WorldStructuralRelationRef",
    "admits_macro_observes_relation",
    "evaluate_world_graph_point_in_time",
    "fold_knowledge_relation_events_at_cutoff",
    "fold_structural_relation_events_at_cutoff",
    "parse_knowledge_relation_kind",
    "parse_macro_graph_bridge_event",
    "parse_structural_relation_kind",
    "parse_world_entity_event",
    "parse_world_entity_identity_event",
    "parse_world_graph_node_ref",
    "parse_world_ontology_revision_event",
    "parse_world_relation_event",
    "reconcile_world_graph_snapshot",
    "reconcile_world_ontology_revision",
    "observation_graph_snapshot",
    "world_instrument_root_for_resolution",
    "world_observation_ref_for_observation_id",
]
