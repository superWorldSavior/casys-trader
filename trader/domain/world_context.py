"""Typed temporal ontology for prospective World context snapshots.

This module is the canonical, stdlib-only record of *what was known*, about
*which entity*, from *which source*, at a proven cutoff.  It is provenance and
topology, not a causal-discovery claim: there is no generic ``CAUSES`` edge
and an association is never treated as causal.

The graph library must not be imported here.  An infrastructure adapter
may project these records; it is never the persistence authority.
"""

from __future__ import annotations

import hashlib
import math
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any
from urllib.parse import quote

from trader.domain.world_episode import (
    CONTEXT_FEATURE_CONTRACT_ID,
    canonical_json,
    canonical_payload,
    canonical_sha256,
    parse_utc_timestamp,
)

ONTOLOGY_REVISION = "semantic_catalog.v1"
CONTEXT_SNAPSHOT_SCHEMA_VERSION = "world_context_snapshot.v1"
NON_CAUSAL_STATUS = "non_causal_association"

ENTITY_KINDS = frozenset({"world", "region", "country", "venue", "family", "company", "instrument", "sensor"})
EDGE_KINDS = frozenset(
    {
        "PART_OF_WORLD",
        "TRADED_ON",
        "MEMBER_OF_FAMILY",
        "ISSUED_BY",
        "DESCRIBED_BY",
    }
)
FORBIDDEN_EDGE_KINDS = frozenset({"CAUSES", "CAUSE", "CAUSED_BY", "CAUSAL"})
CONTEXT_STATUSES = frozenset(
    {
        "complete",
        "partial",
        "missing",
        "stale",
        "availability_unproven",
        "late",
    }
)
SNAPSHOT_STATUSES = frozenset({"complete", "partial", "missing", "stale"})
NON_NEUTRAL_UNAVAILABLE_STATUSES = frozenset({"availability_unproven", "late"})
NO_PROVEN_ARTIFACT_REASON = "no_proven_artifact_at_cutoff"
POLICY_CONTAMINATED_REASON = "policy_contaminated"
SCOPE_UNMAPPED_REASON = "scope_unmapped"
SCOPE_AMBIGUOUS_REASON = "scope_ambiguous"
_SCOPE_MISSING_REASONS = frozenset({SCOPE_UNMAPPED_REASON, SCOPE_AMBIGUOUS_REASON})
_SCOPE_RESOLUTION_STATUSES = frozenset({"resolved", "unmapped", "ambiguous"})
_NEUTRAL_MISSING_PROOF_KEYS = frozenset({"kind", "status", "proven", "reason"})
_SCOPE_RESOLUTION_PROOF_KEYS = frozenset({"mapping_id", "mapping_sha256", "resolution_status", "anchor"})
_MISSING_PROOF_ARTIFACT_FIELDS = (
    "artifact_id",
    "content_sha256",
    "ready_at",
    "valid_until",
    "ingested_at",
    "published_at",
)
ARTIFACT_KINDS = frozenset({"news_macro", "company_intelligence", "macro_world_observation"})

ALLOWED_CONTEXT_CATEGORICAL_FEATURES = frozenset(
    {
        "context_status",
        "macro_status",
        "context_macro_regime",
        "context_rates_regime",
        "context_usd_regime",
        "company_status",
        "company_thesis_status",
        "company_coverage_status",
        "company_freshness_status",
        "company_source_count_bucket",
    }
)
ALLOWED_CONTEXT_NUMERIC_FEATURES: frozenset[str] = frozenset()
_PREFERRED_ISSUER_ID_KEYS = ("lei", "cik", "isin")

_CONTEXT_DENYLIST_TOKENS = frozenset(
    {
        "action",
        "intent",
        "decision",
        "order",
        "trade",
        "portfolio",
        "position",
        "pnl",
        "risk",
        "gate",
        "scheduler",
        "schedule",
        "mandate",
        "hotlist",
        "candidate",
        "rank",
        "attractiveness",
        "feedback",
        "outcome",
        "prompt",
        "rationale",
        "hypothesis",
        "confidence",
        "llm",
        "memrl",
        "memory",
        "quantity",
        "qty",
        "fill",
        "broker",
        "watch",
        "score",
    }
)
_CONTEXT_DENYLIST_FRAGMENTS = (
    "decision",
    "action",
    "order",
    "portfolio",
    "position",
    "pnl",
    "risk_gate",
    "scheduler",
    "mandate",
    "hotlist",
    "candidate",
    "attractiveness",
    "feedback",
    "outcome",
    "prompt",
    "rationale",
    "llm",
    "memrl",
    "model_score",
    "forward_return",
)


def _required_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a non-empty string")
    text = value.strip()
    if not text:
        raise ValueError(f"{field_name} must be a non-empty string")
    return text


def _optional_utc(value: datetime | str | None, field_name: str) -> datetime | None:
    return None if value is None else parse_utc_timestamp(value, field_name)


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.astimezone(timezone.utc).isoformat()


def _normalise_key(value: str) -> str:
    separated = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", value)
    return re.sub(r"[^a-z0-9]+", "_", separated.strip().lower()).strip("_")


def _denied_key(key: str) -> bool:
    if key in ALLOWED_CONTEXT_CATEGORICAL_FEATURES or key in ALLOWED_CONTEXT_NUMERIC_FEATURES:
        return False
    tokens = re.findall(r"[a-z0-9]+", _normalise_key(key))
    if any(token in _CONTEXT_DENYLIST_TOKENS for token in tokens):
        return True
    compact = "_".join(tokens)
    return any(fragment in compact for fragment in _CONTEXT_DENYLIST_FRAGMENTS)


def assert_context_schema(value: Any, field_name: str = "context") -> None:
    """Reject decision, policy, critic, and prompt-bearing fields at every level."""

    def visit(current: Any, path: str) -> None:
        if isinstance(current, Mapping):
            for raw_key, nested in current.items():
                if not isinstance(raw_key, str):
                    raise TypeError(f"{path} keys must be strings")
                if _denied_key(raw_key):
                    raise ValueError(f"{path}.{raw_key} is forbidden in a World context record")
                visit(nested, f"{path}.{raw_key}")
        elif isinstance(current, Sequence) and not isinstance(current, (str, bytes, bytearray)):
            for index, nested in enumerate(current):
                visit(nested, f"{path}[{index}]")

    visit(value, field_name)


def temporally_eligible(
    *,
    cutoff_at: datetime,
    ready_at: datetime | None,
    effective_from: datetime | None = None,
    effective_until: datetime | None = None,
    valid_until: datetime | None = None,
) -> bool:
    """Return True only when durability and validity are proven at *cutoff_at*."""

    if ready_at is None or ready_at > cutoff_at:
        return False
    if effective_from is not None and effective_from > cutoff_at:
        return False
    if effective_until is not None and cutoff_at >= effective_until:
        return False
    if valid_until is not None and cutoff_at >= valid_until:
        return False
    return True


def _immutable_text_tuple(value: Sequence[str] | None, field_name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise TypeError(f"{field_name} must be a sequence of strings")
    items = tuple(_required_text(item, f"{field_name}[]") for item in value)
    return items


def _immutable_entity_tuple(value: Sequence[Any] | None, field_name: str) -> tuple["EntityRef", ...]:
    if value is None:
        return ()
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise TypeError(f"{field_name} must be a sequence of entity refs")
    return tuple(item if isinstance(item, EntityRef) else EntityRef.from_mapping(item) for item in value)


def _immutable_categorical(value: Mapping[str, Any] | None) -> Mapping[str, str]:
    if value is None:
        return MappingProxyType({})
    if not isinstance(value, Mapping):
        raise TypeError("categorical_features must be a mapping")
    assert_context_schema(value, "categorical_features")
    result: dict[str, str] = {}
    for raw_key, raw_value in value.items():
        key = _required_text(raw_key, "categorical feature key")
        if key not in ALLOWED_CONTEXT_CATEGORICAL_FEATURES:
            raise ValueError(f"categorical context feature is not whitelisted: {key}")
        if not isinstance(raw_value, str):
            raise TypeError(f"categorical context feature {key} must be a string")
        text = _required_text(raw_value, f"categorical feature {key}")
        if len(text) > 80:
            raise ValueError(f"categorical context feature {key} is too long for a compact projection")
        result[key] = text
    return MappingProxyType(dict(sorted(result.items())))


def _immutable_numeric(value: Mapping[str, Any] | None) -> Mapping[str, float]:
    if value is None:
        return MappingProxyType({})
    if not isinstance(value, Mapping):
        raise TypeError("numeric_features must be a mapping")
    assert_context_schema(value, "numeric_features")
    result: dict[str, float] = {}
    for raw_key, raw_value in value.items():
        key = _required_text(raw_key, "numeric feature key")
        if key not in ALLOWED_CONTEXT_NUMERIC_FEATURES:
            raise ValueError(f"numeric context feature is not whitelisted: {key}")
        if isinstance(raw_value, bool) or not isinstance(raw_value, (int, float)):
            raise TypeError(f"numeric context feature {key} must be a finite number")
        number = float(raw_value)
        if not math.isfinite(number):
            raise ValueError(f"numeric context feature {key} must be finite")
        result[key] = number
    return MappingProxyType(dict(sorted(result.items())))


@dataclass(frozen=True)
class EntityRef:
    """Stable typed reference to one ontology node."""

    kind: str
    entity_id: str

    def __post_init__(self) -> None:
        kind = _required_text(self.kind, "kind").lower()
        if kind not in ENTITY_KINDS:
            allowed = ", ".join(sorted(ENTITY_KINDS))
            raise ValueError(f"entity kind must be one of: {allowed}")
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "entity_id", _required_text(self.entity_id, "entity_id"))

    @property
    def node_id(self) -> str:
        return f"{self.kind}:{self.entity_id}"

    def to_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "entity_id": self.entity_id}

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | EntityRef) -> EntityRef:
        if isinstance(value, EntityRef):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("entity ref must be EntityRef or a mapping")
        return cls(kind=value.get("kind"), entity_id=value.get("entity_id") or value.get("id"))


@dataclass(frozen=True)
class TopologyEdge:
    """Directed, time-bounded ontology relation. Never a causal claim."""

    kind: str
    source: EntityRef | Mapping[str, Any]
    target: EntityRef | Mapping[str, Any]
    effective_from: datetime | str
    ready_at: datetime | str
    ontology_revision: str
    source_refs: Sequence[str] = ()
    effective_until: datetime | str | None = None
    content_sha256: str | None = None

    def __post_init__(self) -> None:
        kind = _required_text(self.kind, "kind").upper()
        if kind in FORBIDDEN_EDGE_KINDS or kind == "CAUSES":
            raise ValueError("generic CAUSES edges are forbidden; the ontology is not a causal graph")
        if kind not in EDGE_KINDS:
            allowed = ", ".join(sorted(EDGE_KINDS))
            raise ValueError(f"topology edge kind must be one of: {allowed}")
        source = EntityRef.from_mapping(self.source)
        target = EntityRef.from_mapping(self.target)
        effective_from = parse_utc_timestamp(self.effective_from, "effective_from")
        ready_at = parse_utc_timestamp(self.ready_at, "ready_at")
        effective_until = _optional_utc(self.effective_until, "effective_until")
        if effective_until is not None and effective_until < effective_from:
            raise ValueError("effective_until must not precede effective_from")
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "source", source)
        object.__setattr__(self, "target", target)
        object.__setattr__(self, "effective_from", effective_from)
        object.__setattr__(self, "ready_at", ready_at)
        object.__setattr__(self, "effective_until", effective_until)
        object.__setattr__(self, "ontology_revision", _required_text(self.ontology_revision, "ontology_revision"))
        object.__setattr__(self, "source_refs", _immutable_text_tuple(self.source_refs, "source_refs"))
        digest = canonical_sha256(self._identity_payload())
        if self.content_sha256 is not None and _required_text(self.content_sha256, "content_sha256") != digest:
            raise ValueError("content_sha256 does not match the canonical topology edge")
        object.__setattr__(self, "content_sha256", digest)

    def _identity_payload(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "source": self.source.to_dict(),
            "target": self.target.to_dict(),
            "effective_from": _iso(self.effective_from),
            "effective_until": _iso(self.effective_until),
            "ready_at": _iso(self.ready_at),
            "ontology_revision": self.ontology_revision,
            "source_refs": list(self.source_refs),
        }

    def to_dict(self) -> dict[str, Any]:
        payload = self._identity_payload()
        payload["content_sha256"] = self.content_sha256
        return payload

    def eligible_at(self, cutoff_at: datetime | str) -> bool:
        cutoff = parse_utc_timestamp(cutoff_at, "cutoff_at")
        return temporally_eligible(
            cutoff_at=cutoff,
            ready_at=self.ready_at,
            effective_from=self.effective_from,
            effective_until=self.effective_until,
        )

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | TopologyEdge) -> TopologyEdge:
        if isinstance(value, TopologyEdge):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("topology edge must be TopologyEdge or a mapping")
        return cls(
            kind=value.get("kind"),
            source=value.get("source"),
            target=value.get("target"),
            effective_from=value.get("effective_from"),
            ready_at=value.get("ready_at"),
            ontology_revision=value.get("ontology_revision"),
            source_refs=value.get("source_refs") or (),
            effective_until=value.get("effective_until"),
            content_sha256=value.get("content_sha256"),
        )


@dataclass(frozen=True)
class KnowledgeArtifact:
    """Provenance envelope for one bounded sensor artifact."""

    kind: str
    artifact_id: str
    subjects: Sequence[EntityRef | Mapping[str, Any]]
    schema_version: str
    content_sha256: str
    occurred_at: datetime | str | None = None
    published_at: datetime | str | None = None
    ingested_at: datetime | str | None = None
    ready_at: datetime | str | None = None
    valid_until: datetime | str | None = None
    source_refs: Sequence[str] = ()
    derived_from: Sequence[str] = ()
    supersedes: Sequence[str] = ()

    def __post_init__(self) -> None:
        kind = _required_text(self.kind, "kind").lower()
        if kind not in ARTIFACT_KINDS:
            allowed = ", ".join(sorted(ARTIFACT_KINDS))
            raise ValueError(f"artifact kind must be one of: {allowed}")
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "artifact_id", _required_text(self.artifact_id, "artifact_id"))
        object.__setattr__(self, "schema_version", _required_text(self.schema_version, "schema_version"))
        object.__setattr__(self, "content_sha256", _required_text(self.content_sha256, "content_sha256"))
        object.__setattr__(self, "subjects", _immutable_entity_tuple(self.subjects, "subjects"))
        if not self.subjects:
            raise ValueError("knowledge artifact requires at least one subject")
        object.__setattr__(self, "occurred_at", _optional_utc(self.occurred_at, "occurred_at"))
        object.__setattr__(self, "published_at", _optional_utc(self.published_at, "published_at"))
        object.__setattr__(self, "ingested_at", _optional_utc(self.ingested_at, "ingested_at"))
        object.__setattr__(self, "ready_at", _optional_utc(self.ready_at, "ready_at"))
        object.__setattr__(self, "valid_until", _optional_utc(self.valid_until, "valid_until"))
        object.__setattr__(self, "source_refs", _immutable_text_tuple(self.source_refs, "source_refs"))
        object.__setattr__(self, "derived_from", _immutable_text_tuple(self.derived_from, "derived_from"))
        object.__setattr__(self, "supersedes", _immutable_text_tuple(self.supersedes, "supersedes"))
        assert_context_schema(self.to_dict(), "knowledge_artifact")

    def eligible_at(self, cutoff_at: datetime | str) -> bool:
        cutoff = parse_utc_timestamp(cutoff_at, "cutoff_at")
        return temporally_eligible(
            cutoff_at=cutoff,
            ready_at=self.ready_at,
            valid_until=self.valid_until,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "artifact_id": self.artifact_id,
            "subjects": [item.to_dict() for item in self.subjects],
            "schema_version": self.schema_version,
            "content_sha256": self.content_sha256,
            "occurred_at": _iso(self.occurred_at),
            "published_at": _iso(self.published_at),
            "ingested_at": _iso(self.ingested_at),
            "ready_at": _iso(self.ready_at),
            "valid_until": _iso(self.valid_until),
            "source_refs": list(self.source_refs),
            "derived_from": list(self.derived_from),
            "supersedes": list(self.supersedes),
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | KnowledgeArtifact) -> KnowledgeArtifact:
        if isinstance(value, KnowledgeArtifact):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("knowledge artifact must be KnowledgeArtifact or a mapping")
        return cls(
            kind=value.get("kind"),
            artifact_id=value.get("artifact_id"),
            subjects=value.get("subjects") or (),
            schema_version=value.get("schema_version"),
            content_sha256=value.get("content_sha256"),
            occurred_at=value.get("occurred_at"),
            published_at=value.get("published_at"),
            ingested_at=value.get("ingested_at"),
            ready_at=value.get("ready_at"),
            valid_until=value.get("valid_until"),
            source_refs=value.get("source_refs") or (),
            derived_from=value.get("derived_from") or (),
            supersedes=value.get("supersedes") or (),
        )


def _edge_tuple(value: Sequence[Any] | None, field_name: str) -> tuple[TopologyEdge, ...]:
    if value is None:
        return ()
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise TypeError(f"{field_name} must be a sequence of topology edges")
    return tuple(item if isinstance(item, TopologyEdge) else TopologyEdge.from_mapping(item) for item in value)


def _deep_freeze(value: Any) -> Any:
    """Freeze nested mappings/sequences/sets into immutable deterministic structures."""

    if isinstance(value, Mapping):
        frozen = {str(key): _deep_freeze(nested) for key, nested in value.items()}
        return MappingProxyType(frozen)
    if isinstance(value, (set, frozenset)):
        items = [_deep_freeze(item) for item in value]
        return tuple(sorted(items, key=lambda item: canonical_json(canonical_payload(item))))
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return tuple(_deep_freeze(item) for item in value)
    return value


def _deep_thaw(value: Any) -> Any:
    """Return a detached JSON-serializable copy of a frozen nested structure."""

    if isinstance(value, Mapping):
        return {str(key): _deep_thaw(nested) for key, nested in value.items()}
    if isinstance(value, (set, frozenset)):
        items = [_deep_thaw(item) for item in value]
        return sorted(items, key=lambda item: canonical_json(canonical_payload(item)))
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_deep_thaw(item) for item in value]
    if isinstance(value, datetime):
        return _iso(value)
    return value


def _artifact_proofs(value: Sequence[Any] | None) -> tuple[Mapping[str, Any], ...]:
    if value is None:
        return ()
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise TypeError("artifact_proofs must be a sequence of mappings")
    proofs: list[Mapping[str, Any]] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise TypeError("artifact proof must be a mapping")
        assert_context_schema(item, "artifact_proofs")
        proofs.append(_deep_freeze(canonical_payload(item)))
    return tuple(proofs)


def _status_mapping(value: Mapping[str, Any] | None) -> Mapping[str, str]:
    if value is None:
        return MappingProxyType({})
    if not isinstance(value, Mapping):
        raise TypeError("sensor_statuses must be a mapping")
    result: dict[str, str] = {}
    for raw_key, raw_value in value.items():
        key = _required_text(raw_key, "sensor status key")
        status = _required_text(raw_value, f"sensor status {key}")
        if status not in CONTEXT_STATUSES:
            allowed = ", ".join(sorted(CONTEXT_STATUSES))
            raise ValueError(f"sensor status must be one of: {allowed}")
        result[key] = status
    return MappingProxyType(dict(sorted(result.items())))


@dataclass(frozen=True)
class WorldContextSnapshot:
    """Immutable point-in-time context attached to one market observation."""

    instrument: EntityRef | Mapping[str, Any]
    cutoff_at: datetime | str
    ontology_revision: str
    topology_edges: Sequence[TopologyEdge | Mapping[str, Any]] = ()
    topology_path: Sequence[TopologyEdge | Mapping[str, Any]] = ()
    artifact_refs: Sequence[str] = ()
    artifact_proofs: Sequence[Mapping[str, Any]] = ()
    categorical_features: Mapping[str, Any] | None = field(default_factory=dict)
    numeric_features: Mapping[str, Any] | None = field(default_factory=dict)
    causal_status: str = NON_CAUSAL_STATUS
    feature_contract_version: str = CONTEXT_FEATURE_CONTRACT_ID
    status: str = "missing"
    sensor_statuses: Mapping[str, Any] | None = field(default_factory=dict)
    schema_version: str = CONTEXT_SNAPSHOT_SCHEMA_VERSION
    context_id: str | None = None

    def __post_init__(self) -> None:
        instrument = EntityRef.from_mapping(self.instrument)
        if instrument.kind != "instrument":
            raise ValueError("snapshot instrument must have kind=instrument")
        cutoff_at = parse_utc_timestamp(self.cutoff_at, "cutoff_at")
        status = _required_text(self.status, "status").lower()
        if status not in CONTEXT_STATUSES:
            allowed = ", ".join(sorted(CONTEXT_STATUSES))
            raise ValueError(f"context status must be one of: {allowed}")
        if status in NON_NEUTRAL_UNAVAILABLE_STATUSES:
            raise ValueError("late/unproven sensor states cannot enter a WorldContextSnapshot")
        causal_status = _required_text(self.causal_status, "causal_status").lower()
        if "caus" in causal_status and causal_status != NON_CAUSAL_STATUS:
            raise ValueError("World context must not claim that an association is causal")
        if causal_status != NON_CAUSAL_STATUS:
            raise ValueError(f"causal_status must be {NON_CAUSAL_STATUS}")
        edges = _edge_tuple(self.topology_edges, "topology_edges")
        path = _edge_tuple(self.topology_path, "topology_path")
        object.__setattr__(self, "instrument", instrument)
        object.__setattr__(self, "cutoff_at", cutoff_at)
        object.__setattr__(self, "ontology_revision", _required_text(self.ontology_revision, "ontology_revision"))
        object.__setattr__(self, "topology_edges", edges)
        object.__setattr__(self, "topology_path", path)
        object.__setattr__(self, "artifact_refs", _immutable_text_tuple(self.artifact_refs, "artifact_refs"))
        object.__setattr__(self, "artifact_proofs", _artifact_proofs(self.artifact_proofs))
        object.__setattr__(self, "categorical_features", _immutable_categorical(self.categorical_features))
        object.__setattr__(self, "numeric_features", _immutable_numeric(self.numeric_features))
        object.__setattr__(self, "causal_status", causal_status)
        feature_contract_version = _required_text(self.feature_contract_version, "feature_contract_version")
        if feature_contract_version != CONTEXT_FEATURE_CONTRACT_ID:
            raise ValueError(f"feature_contract_version must be {CONTEXT_FEATURE_CONTRACT_ID}")
        object.__setattr__(self, "feature_contract_version", feature_contract_version)
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "sensor_statuses", _status_mapping(self.sensor_statuses))
        object.__setattr__(self, "schema_version", _required_text(self.schema_version, "schema_version"))
        digest = canonical_sha256(self._identity_payload())
        context_id = f"world-context:v1:{digest}"
        if self.context_id is not None and _required_text(self.context_id, "context_id") != context_id:
            raise ValueError("context_id does not match the canonical snapshot digest")
        object.__setattr__(self, "context_id", context_id)
        self._validate_invariants()
        assert_context_schema(self.to_dict(), "world_context_snapshot")

    def _validate_invariants(self) -> None:
        cutoff = self.cutoff_at
        for status in self.sensor_statuses.values():
            if status in NON_NEUTRAL_UNAVAILABLE_STATUSES:
                raise ValueError("late/unproven sensor states cannot enter a WorldContextSnapshot")
            if status not in SNAPSHOT_STATUSES:
                allowed = ", ".join(sorted(SNAPSHOT_STATUSES))
                raise ValueError(f"sensor status must be one of: {allowed}")
        for key in ("context_status", "macro_status", "company_status"):
            declared = self.categorical_features.get(key)
            if declared in NON_NEUTRAL_UNAVAILABLE_STATUSES:
                raise ValueError("late/unproven sensor states cannot enter a WorldContextSnapshot")
        edge_by_digest = {edge.content_sha256: edge for edge in self.topology_edges}
        for edge in self.topology_edges:
            if edge.ontology_revision != self.ontology_revision:
                raise ValueError("topology edge ontology_revision must match the snapshot")
            if not edge.eligible_at(cutoff):
                raise ValueError("topology edge must be effective and ready at cutoff")
        reached = {self.instrument.node_id}
        for edge in self.topology_path:
            if edge.content_sha256 not in edge_by_digest:
                raise ValueError("topology_path must be a subset of topology_edges")
            if edge.source.node_id not in reached:
                raise ValueError("topology_path must be a connected traversal rooted at the instrument")
            reached.add(edge.target.node_id)
        declared_status = self.categorical_features.get("context_status")
        if declared_status is not None and declared_status != self.status:
            raise ValueError("categorical context_status must match snapshot status")
        if self.sensor_statuses:
            collapsed = overall_context_status(self.sensor_statuses)
            if collapsed != self.status:
                raise ValueError("context status does not match sensor_statuses")
        proof_ids: list[str] = []
        seen_kinds: set[str] = set()
        for proof in self.artifact_proofs:
            status = str(proof.get("status") or "")
            proven = proof.get("proven") is True
            kind = str(proof.get("kind") or "").strip()
            if not kind:
                raise ValueError("artifact proof requires a non-empty kind")
            if kind not in self.sensor_statuses:
                raise ValueError("artifact proof kind must exist in sensor_statuses")
            if kind in seen_kinds:
                raise ValueError("duplicate artifact proof kind")
            seen_kinds.add(kind)
            if status in NON_NEUTRAL_UNAVAILABLE_STATUSES:
                raise ValueError("late/unproven sensor states cannot enter a WorldContextSnapshot")
            if self.sensor_statuses[kind] != status:
                raise ValueError("artifact proof status must match sensor_statuses")
            ready_raw = proof.get("ready_at")
            if ready_raw not in (None, ""):
                ready_at = parse_utc_timestamp(ready_raw, "artifact_proof.ready_at")
                if ready_at > cutoff:
                    raise ValueError("artifact ready_at must be at or before cutoff")
            if status == "missing":
                self._validate_missing_proof(proof)
                continue
            if status in {"complete", "partial"}:
                if not proven:
                    raise ValueError("complete/partial artifact proof must be proven")
                artifact_id = str(proof.get("artifact_id") or "").strip()
                digest = str(proof.get("content_sha256") or "").strip()
                if not artifact_id or not digest or ready_raw in (None, ""):
                    raise ValueError("complete/partial artifact proof requires id, hash, and ready_at")
                _require_optional_valid_until_after_cutoff(proof, cutoff)
                proof_ids.append(artifact_id)
            elif status == "stale":
                if not proven:
                    raise ValueError("stale artifact proof must be proven")
                artifact_id = str(proof.get("artifact_id") or "").strip()
                digest = str(proof.get("content_sha256") or "").strip()
                valid_until_raw = proof.get("valid_until")
                if not artifact_id or not digest or ready_raw in (None, "") or valid_until_raw in (None, ""):
                    raise ValueError("stale artifact proof requires id, hash, ready_at, and valid_until")
                valid_until = parse_utc_timestamp(valid_until_raw, "artifact_proof.valid_until")
                if valid_until > cutoff:
                    raise ValueError("stale artifact valid_until must be at or before cutoff")
        if self.sensor_statuses or self.artifact_proofs:
            if seen_kinds != set(self.sensor_statuses):
                raise ValueError("artifact proof kinds must match sensor_statuses")
        if set(self.artifact_refs) != set(proof_ids):
            raise ValueError("artifact_refs must match proven complete/partial artifact ids")

    def _validate_missing_proof(self, proof: Mapping[str, Any]) -> None:
        reason = str(proof.get("reason") or "").strip()
        extra_keys = [key for key in proof if key not in _NEUTRAL_MISSING_PROOF_KEYS]
        mapping_keys = [key for key in extra_keys if key in _SCOPE_RESOLUTION_PROOF_KEYS]
        other_keys = [key for key in extra_keys if key not in _SCOPE_RESOLUTION_PROOF_KEYS]
        has_artifact_meta = any(proof.get(name) not in (None, "") for name in _MISSING_PROOF_ARTIFACT_FIELDS)
        if reason == NO_PROVEN_ARTIFACT_REASON:
            if proof.get("proven") is not False or other_keys or has_artifact_meta:
                raise ValueError("neutral missing proof requires proven=False and no artifact metadata")
            if mapping_keys:
                _validate_scope_resolution_fields(proof)
            return
        if reason in _SCOPE_MISSING_REASONS:
            if proof.get("proven") is not False or other_keys or has_artifact_meta:
                raise ValueError("scope resolution missing proof requires proven=False and no artifact metadata")
            expected = "unmapped" if reason == SCOPE_UNMAPPED_REASON else "ambiguous"
            _validate_scope_resolution_fields(proof, expected_status=expected)
            return
        if reason == POLICY_CONTAMINATED_REASON:
            if proof.get("proven") is not True:
                raise ValueError("policy_contaminated proof must be proven")
            artifact_id = str(proof.get("artifact_id") or "").strip()
            digest = str(proof.get("content_sha256") or "").strip()
            ready_raw = proof.get("ready_at")
            if not artifact_id or not digest or ready_raw in (None, ""):
                raise ValueError("policy_contaminated proof requires id, hash, and ready_at")
            ready_at = parse_utc_timestamp(ready_raw, "artifact_proof.ready_at")
            if ready_at > self.cutoff_at:
                raise ValueError("artifact ready_at must be at or before cutoff")
            _require_optional_valid_until_after_cutoff(proof, self.cutoff_at)
            if artifact_id in set(self.artifact_refs):
                raise ValueError("policy_contaminated proof must not enter artifact_refs")
            return
        raise ValueError("missing proof requires no_proven_artifact_at_cutoff or policy_contaminated")

    def _identity_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "instrument": self.instrument.to_dict(),
            "cutoff_at": _iso(self.cutoff_at),
            "ontology_revision": self.ontology_revision,
            "ontology_hash": self.ontology_hash,
            "topology_edges": [edge.to_dict() for edge in self.topology_edges],
            "topology_path": [edge.to_dict() for edge in self.topology_path],
            "artifact_refs": list(self.artifact_refs),
            "artifact_proofs": [dict(item) for item in self.artifact_proofs],
            "categorical_features": dict(self.categorical_features),
            "numeric_features": dict(self.numeric_features),
            "causal_status": self.causal_status,
            "feature_contract_version": self.feature_contract_version,
            "status": self.status,
            "sensor_statuses": dict(self.sensor_statuses),
        }

    @property
    def ontology_hash(self) -> str:
        return canonical_sha256(
            {
                "ontology_revision": self.ontology_revision,
                "edges": [edge.to_dict() for edge in self.topology_edges],
            }
        )

    @property
    def content_sha256(self) -> str:
        return canonical_sha256(self._identity_payload())

    def to_dict(self) -> dict[str, Any]:
        payload = _deep_thaw(self._identity_payload())
        if not isinstance(payload, dict):  # pragma: no cover - defensive invariant
            raise TypeError("world context snapshot to_dict must return an object")
        payload["context_id"] = self.context_id
        payload["content_sha256"] = self.content_sha256
        return payload

    def replay_payload(self) -> dict[str, Any]:
        payload = canonical_payload(self.to_dict())
        if not isinstance(payload, dict):  # pragma: no cover - defensive invariant
            raise TypeError("world context replay payload must be an object")
        return payload

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | WorldContextSnapshot) -> WorldContextSnapshot:
        if isinstance(value, WorldContextSnapshot):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("world context snapshot must be WorldContextSnapshot or a mapping")
        snapshot = cls(
            instrument=value.get("instrument"),
            cutoff_at=value.get("cutoff_at"),
            ontology_revision=value.get("ontology_revision"),
            topology_edges=value.get("topology_edges") or (),
            topology_path=value.get("topology_path") or (),
            artifact_refs=value.get("artifact_refs") or (),
            artifact_proofs=value.get("artifact_proofs") or (),
            categorical_features=value.get("categorical_features") or {},
            numeric_features=value.get("numeric_features") or {},
            causal_status=value.get("causal_status", NON_CAUSAL_STATUS),
            feature_contract_version=value.get("feature_contract_version", CONTEXT_FEATURE_CONTRACT_ID),
            status=value.get("status", "missing"),
            sensor_statuses=value.get("sensor_statuses") or {},
            schema_version=value.get("schema_version", CONTEXT_SNAPSHOT_SCHEMA_VERSION),
            context_id=value.get("context_id"),
        )
        supplied_hash = value.get("content_sha256")
        if supplied_hash is not None and _required_text(supplied_hash, "content_sha256") != snapshot.content_sha256:
            raise ValueError("content_sha256 does not match the canonical snapshot digest")
        return snapshot


def freeze_context_mapping(value: WorldContextSnapshot | Mapping[str, Any]) -> Mapping[str, Any]:
    """Return a deep-frozen canonical snapshot mapping for WorldObservation.context."""

    snapshot = WorldContextSnapshot.from_mapping(value)
    frozen = _deep_freeze(snapshot.to_dict())
    if not isinstance(frozen, Mapping):  # pragma: no cover - defensive invariant
        raise TypeError("frozen world context must be a mapping")
    return frozen


def _normalise_issuer_namespace(key: str) -> str:
    return unicodedata.normalize("NFC", key).strip().casefold()


def _encode_issuer_token(value: str) -> str:
    return quote(value, safe="")


def verified_issuer_entity_id(identity: Mapping[str, Any] | None) -> str | None:
    """Return a namespaced external issuer id, never a display name.

    Preference order: ``lei``, ``cik``, ``isin``, then the lexicographically
    first nonempty verified external id.  Unverified identities and name-only
    records are omitted so homonyms cannot merge.
    """

    if not isinstance(identity, Mapping):
        return None
    status = str(identity.get("identity_status") or "").strip().lower()
    if status != "verified":
        return None
    raw_ids = identity.get("external_ids")
    if not isinstance(raw_ids, Mapping):
        return None
    cleaned: dict[str, str] = {}
    for raw_key, raw_value in raw_ids.items():
        if not isinstance(raw_key, str) or not isinstance(raw_value, str):
            continue
        key = _normalise_issuer_namespace(raw_key)
        value = raw_value.strip()
        if not key or not value:
            continue
        previous = cleaned.get(key)
        if previous is not None and previous != value:
            return None
        cleaned[key] = value
    for key in _PREFERRED_ISSUER_ID_KEYS:
        if cleaned.get(key):
            return f"{_encode_issuer_token(key)}:{_encode_issuer_token(cleaned[key])}"
    rest = [(key, value) for key, value in cleaned.items() if value]
    if not rest:
        return None
    key, value = sorted(rest, key=lambda item: (item[0], item[1]))[0]
    return f"{_encode_issuer_token(key)}:{_encode_issuer_token(value)}"


def bootstrap_instrument_topology(
    *,
    symbol: str,
    venue: str,
    cutoff_at: datetime | str,
    family: str | None = None,
    issuer_id: str | None = None,
    issuer_verified: bool = False,
    ontology_revision: str = ONTOLOGY_REVISION,
    source_refs: Sequence[str] = ("semantic_catalog",),
) -> tuple[TopologyEdge, ...]:
    """Build the safe static topology. Unverified issuers are omitted."""

    cutoff = parse_utc_timestamp(cutoff_at, "cutoff_at")
    instrument = EntityRef(kind="instrument", entity_id=_required_text(symbol, "symbol"))
    venue_ref = EntityRef(kind="venue", entity_id=_required_text(venue, "venue"))
    world = EntityRef(kind="world", entity_id="market")
    refs = tuple(source_refs)
    edges = [
        TopologyEdge(
            kind="TRADED_ON",
            source=instrument,
            target=venue_ref,
            effective_from=cutoff,
            ready_at=cutoff,
            ontology_revision=ontology_revision,
            source_refs=refs,
        ),
        TopologyEdge(
            kind="PART_OF_WORLD",
            source=venue_ref,
            target=world,
            effective_from=cutoff,
            ready_at=cutoff,
            ontology_revision=ontology_revision,
            source_refs=refs,
        ),
    ]
    if family:
        family_ref = EntityRef(kind="family", entity_id=_required_text(family, "family"))
        edges.append(
            TopologyEdge(
                kind="MEMBER_OF_FAMILY",
                source=instrument,
                target=family_ref,
                effective_from=cutoff,
                ready_at=cutoff,
                ontology_revision=ontology_revision,
                source_refs=refs,
            )
        )
        edges.append(
            TopologyEdge(
                kind="PART_OF_WORLD",
                source=family_ref,
                target=world,
                effective_from=cutoff,
                ready_at=cutoff,
                ontology_revision=ontology_revision,
                source_refs=refs,
            )
        )
    if issuer_verified and issuer_id:
        company = EntityRef(kind="company", entity_id=_required_text(issuer_id, "issuer_id"))
        edges.append(
            TopologyEdge(
                kind="ISSUED_BY",
                source=instrument,
                target=company,
                effective_from=cutoff,
                ready_at=cutoff,
                ontology_revision=ontology_revision,
                source_refs=refs,
            )
        )
    return tuple(edges)


def topology_path_for_instrument(edges: Sequence[TopologyEdge], instrument: EntityRef) -> tuple[TopologyEdge, ...]:
    """Return edges reachable from *instrument*, including PART_OF_WORLD only when reached."""

    by_source: dict[str, list[TopologyEdge]] = {}
    for edge in edges:
        by_source.setdefault(edge.source.node_id, []).append(edge)
    for bucket in by_source.values():
        bucket.sort(key=lambda edge: (edge.kind, edge.target.node_id, edge.content_sha256 or ""))
    path: list[TopologyEdge] = []
    seen_edges: set[str] = set()
    seen_nodes = {instrument.node_id}
    queue = [instrument.node_id]
    while queue:
        node = queue.pop(0)
        for edge in by_source.get(node, ()):
            digest = edge.content_sha256 or ""
            if digest in seen_edges:
                continue
            seen_edges.add(digest)
            path.append(edge)
            if edge.target.node_id not in seen_nodes:
                seen_nodes.add(edge.target.node_id)
                queue.append(edge.target.node_id)
    return tuple(path)


def company_source_count_bucket(count: int) -> str:
    if count <= 0:
        return "b0"
    if count == 1:
        return "b1"
    if count <= 3:
        return "b2"
    if count <= 8:
        return "b3"
    return "b4"


def overall_context_status(sensor_statuses: Mapping[str, str]) -> str:
    """Collapse per-sensor statuses without inventing completeness."""

    values = [status for status in sensor_statuses.values() if status in CONTEXT_STATUSES]
    if not values:
        return "missing"
    if all(status == "complete" for status in values):
        return "complete"
    if any(status in {"complete", "partial"} for status in values) and any(status != "complete" for status in values):
        return "partial"
    if all(status == "availability_unproven" for status in values):
        return "availability_unproven"
    if any(status == "availability_unproven" for status in values):
        return "availability_unproven"
    if all(status == "late" for status in values):
        return "late"
    if any(status == "late" for status in values) and not any(status in {"complete", "partial"} for status in values):
        return "late"
    if all(status == "stale" for status in values):
        return "stale"
    if any(status == "stale" for status in values) and not any(status in {"complete", "partial"} for status in values):
        return "stale"
    if all(status == "missing" for status in values):
        return "missing"
    return "partial"


@dataclass(frozen=True)
class SensorEvidence:
    """Point-in-time lookup of one sensor, including ineligible audit rows."""

    status: str
    reason: str | None = None
    artifact: KnowledgeArtifact | None = None
    payload: Mapping[str, Any] | None = None
    proven: bool = False

    def __post_init__(self) -> None:
        status = _required_text(self.status, "status").lower()
        if status not in CONTEXT_STATUSES:
            allowed = ", ".join(sorted(CONTEXT_STATUSES))
            raise ValueError(f"sensor status must be one of: {allowed}")
        object.__setattr__(self, "status", status)
        if self.reason is not None:
            object.__setattr__(self, "reason", _required_text(self.reason, "reason"))
        if self.payload is not None:
            if not isinstance(self.payload, Mapping):
                raise TypeError("sensor payload must be a mapping")
            object.__setattr__(self, "payload", _deep_freeze(dict(self.payload)))
        if self.proven is not True and self.status in {"complete", "partial"}:
            raise ValueError("complete/partial sensor evidence requires a durable readiness receipt")


def neutral_missing_sensor_evidence() -> SensorEvidence:
    """Canonical model-facing state for anything unproven at the cutoff."""

    return SensorEvidence(
        status="missing",
        reason=NO_PROVEN_ARTIFACT_REASON,
        proven=False,
        payload=None,
        artifact=None,
    )


def _require_optional_valid_until_after_cutoff(proof: Mapping[str, Any], cutoff: datetime) -> None:
    raw = proof.get("valid_until")
    if raw in (None, ""):
        return
    valid_until = parse_utc_timestamp(raw, "artifact_proof.valid_until")
    if valid_until <= cutoff:
        raise ValueError("artifact valid_until must be after cutoff")


def _validate_scope_resolution_fields(proof: Mapping[str, Any], *, expected_status: str | None = None) -> None:
    mapping_id = str(proof.get("mapping_id") or "").strip()
    mapping_sha256 = str(proof.get("mapping_sha256") or "").strip()
    status = str(proof.get("resolution_status") or "").strip()
    if not mapping_id or not mapping_sha256 or status not in _SCOPE_RESOLUTION_STATUSES:
        raise ValueError("scope resolution proof requires mapping_id, mapping_sha256, and resolution_status")
    if expected_status is not None and status != expected_status:
        raise ValueError("resolution_status must match the missing reason")
    anchor = proof.get("anchor")
    if anchor is not None and not isinstance(anchor, Mapping):
        raise TypeError("scope resolution anchor must be a mapping")


def payload_has_scope_resolution(payload: Mapping[str, Any] | None) -> bool:
    if not isinstance(payload, Mapping):
        return False
    raw = payload.get("scope_resolution")
    if not isinstance(raw, Mapping):
        return False
    mapping_id = str(raw.get("mapping_id") or "").strip()
    mapping_sha256 = str(raw.get("mapping_sha256") or "").strip()
    status = str(raw.get("resolution_status") or raw.get("status") or "").strip()
    return bool(mapping_id and mapping_sha256 and status in _SCOPE_RESOLUTION_STATUSES)


def _model_facing_temporally_eligible(
    *,
    cutoff_at: datetime,
    ready_at: object,
    valid_until: object,
) -> bool:
    try:
        ready = None if ready_at in (None, "") else parse_utc_timestamp(ready_at, "ready_at")
        until = None if valid_until in (None, "") else parse_utc_timestamp(valid_until, "valid_until")
        return temporally_eligible(cutoff_at=cutoff_at, ready_at=ready, valid_until=until)
    except (TypeError, ValueError):
        return False


def model_facing_sensor_evidence(evidence: SensorEvidence, cutoff_at: datetime | str) -> SensorEvidence:
    """Collapse late/unproven/incomplete proof so it cannot enter a snapshot."""

    cutoff = parse_utc_timestamp(cutoff_at, "cutoff_at")
    artifact = evidence.artifact
    ready_at = None if artifact is None else artifact.ready_at
    valid_until = None if artifact is None else artifact.valid_until
    if evidence.status in {"complete", "partial"}:
        if evidence.proven is not True or artifact is None:
            return neutral_missing_sensor_evidence()
        if not _model_facing_temporally_eligible(cutoff_at=cutoff, ready_at=ready_at, valid_until=valid_until):
            return neutral_missing_sensor_evidence()
        return evidence
    if evidence.status == "stale":
        if evidence.proven is not True or artifact is None or ready_at is None or valid_until is None:
            return neutral_missing_sensor_evidence()
        if ready_at > cutoff or cutoff < valid_until:
            return neutral_missing_sensor_evidence()
        return evidence
    if evidence.status == "missing" and evidence.reason == POLICY_CONTAMINATED_REASON:
        if (
            evidence.proven is True
            and artifact is not None
            and _model_facing_temporally_eligible(
                cutoff_at=cutoff,
                ready_at=ready_at,
                valid_until=valid_until,
            )
        ):
            return evidence
        return neutral_missing_sensor_evidence()
    if evidence.status == "missing" and (
        evidence.reason in _SCOPE_MISSING_REASONS or payload_has_scope_resolution(evidence.payload)
    ):
        if evidence.reason in _SCOPE_MISSING_REASONS and not payload_has_scope_resolution(evidence.payload):
            return neutral_missing_sensor_evidence()
        return evidence
    return neutral_missing_sensor_evidence()


def snapshot_content_hash(value: Mapping[str, Any] | WorldContextSnapshot) -> str:
    payload = value.to_dict() if isinstance(value, WorldContextSnapshot) else dict(value)
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


__all__ = [
    "ALLOWED_CONTEXT_CATEGORICAL_FEATURES",
    "ALLOWED_CONTEXT_NUMERIC_FEATURES",
    "ARTIFACT_KINDS",
    "CONTEXT_FEATURE_CONTRACT_ID",
    "CONTEXT_SNAPSHOT_SCHEMA_VERSION",
    "CONTEXT_STATUSES",
    "EDGE_KINDS",
    "ENTITY_KINDS",
    "NON_CAUSAL_STATUS",
    "NON_NEUTRAL_UNAVAILABLE_STATUSES",
    "NO_PROVEN_ARTIFACT_REASON",
    "POLICY_CONTAMINATED_REASON",
    "SCOPE_AMBIGUOUS_REASON",
    "SCOPE_UNMAPPED_REASON",
    "ONTOLOGY_REVISION",
    "SNAPSHOT_STATUSES",
    "EntityRef",
    "SensorEvidence",
    "KnowledgeArtifact",
    "TopologyEdge",
    "WorldContextSnapshot",
    "assert_context_schema",
    "bootstrap_instrument_topology",
    "company_source_count_bucket",
    "freeze_context_mapping",
    "model_facing_sensor_evidence",
    "neutral_missing_sensor_evidence",
    "overall_context_status",
    "snapshot_content_hash",
    "temporally_eligible",
    "topology_path_for_instrument",
    "verified_issuer_entity_id",
]
