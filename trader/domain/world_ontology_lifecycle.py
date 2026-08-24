"""Append-only market ontology / scope-mapping publication.

A persisted revision id is immutable. Fresh state publishes the current
mapping and ontology. Existing different identity or hash is drift and
fails closed. Same-id hash drift is a conflict, never a rewrite.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from trader.domain.world_feature_contract import (
    WORLD_GRAPH_V3_ONTOLOGY_REVISION,
    WORLD_GRAPH_V3_ONTOLOGY_SHA256,
    WORLD_SCOPE_MAPPING_ID,
    WORLD_SCOPE_MAPPING_SHA256,
)
from trader.domain.world_graph import WorldOntologyRevision
from trader.domain.world_scope import WorldScopeMapping


ONTOLOGY_PUBLICATION_ACTIONS = frozenset({"ready", "publish"})


def _required_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a non-empty string")
    text = value.strip()
    if not text:
        raise ValueError(f"{field_name} must be a non-empty string")
    return text


def _sha256_hex(value: Any, field_name: str) -> str:
    text = _required_text(value, field_name)
    if len(text) != 64 or any(char not in "0123456789abcdef" for char in text):
        raise ValueError(f"{field_name} must be a sha256 hex digest")
    return text


@dataclass(frozen=True)
class WorldOntologyLifecycleSpec:
    """Committed current mapping and ontology identities."""

    revision_id: str
    mapping_id: str
    revision_hash: str
    mapping_sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "revision_id", _required_text(self.revision_id, "revision_id"))
        object.__setattr__(self, "mapping_id", _required_text(self.mapping_id, "mapping_id"))
        object.__setattr__(self, "revision_hash", _sha256_hex(self.revision_hash, "revision_hash"))
        object.__setattr__(self, "mapping_sha256", _sha256_hex(self.mapping_sha256, "mapping_sha256"))

    def to_dict(self) -> dict[str, str]:
        return {
            "revision_id": self.revision_id,
            "mapping_id": self.mapping_id,
            "revision_hash": self.revision_hash,
            "mapping_sha256": self.mapping_sha256,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | WorldOntologyLifecycleSpec) -> WorldOntologyLifecycleSpec:
        if isinstance(value, WorldOntologyLifecycleSpec):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("lifecycle spec must be WorldOntologyLifecycleSpec or a mapping")
        return cls(
            revision_id=value.get("revision_id"),
            mapping_id=value.get("mapping_id"),
            revision_hash=value.get("revision_hash"),
            mapping_sha256=value.get("mapping_sha256"),
        )


@dataclass(frozen=True)
class WorldOntologyPublicationPlan:
    """Deterministic boot action for one expected ontology revision."""

    action: str
    spec: WorldOntologyLifecycleSpec | Mapping[str, Any]
    published_revision_id: str | None
    expected_revision_id: str

    def __post_init__(self) -> None:
        action = _required_text(self.action, "action")
        if action not in ONTOLOGY_PUBLICATION_ACTIONS:
            allowed = ", ".join(sorted(ONTOLOGY_PUBLICATION_ACTIONS))
            raise ValueError(f"publication action must be one of: {allowed}")
        spec = WorldOntologyLifecycleSpec.from_mapping(self.spec)
        expected_revision_id = _required_text(self.expected_revision_id, "expected_revision_id")
        published_revision_id = (
            None
            if self.published_revision_id is None
            else _required_text(self.published_revision_id, "published_revision_id")
        )
        if expected_revision_id != spec.revision_id:
            raise ValueError("expected_revision_id must equal revision_id")
        if action == "ready" and published_revision_id != expected_revision_id:
            raise ValueError("ready plan requires the published revision to equal the current revision")
        if action == "publish" and published_revision_id is not None:
            raise ValueError("publish plan requires no active published revision")
        object.__setattr__(self, "action", action)
        object.__setattr__(self, "spec", spec)
        object.__setattr__(self, "published_revision_id", published_revision_id)
        object.__setattr__(self, "expected_revision_id", expected_revision_id)

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "spec": self.spec.to_dict(),
            "published_revision_id": self.published_revision_id,
            "expected_revision_id": self.expected_revision_id,
        }


def committed_world_ontology_lifecycle_spec() -> WorldOntologyLifecycleSpec:
    return WorldOntologyLifecycleSpec(
        revision_id=WORLD_GRAPH_V3_ONTOLOGY_REVISION,
        mapping_id=WORLD_SCOPE_MAPPING_ID,
        revision_hash=WORLD_GRAPH_V3_ONTOLOGY_SHA256,
        mapping_sha256=WORLD_SCOPE_MAPPING_SHA256,
    )


def require_committed_ontology_revision(
    revision: WorldOntologyRevision,
    mapping: WorldScopeMapping,
) -> WorldOntologyRevision:
    """Fail closed when a derived revision is not the frozen live ontology identity."""

    if not isinstance(revision, WorldOntologyRevision):
        raise TypeError("revision must be WorldOntologyRevision")
    if not isinstance(mapping, WorldScopeMapping):
        raise TypeError("mapping must be WorldScopeMapping")
    spec = committed_world_ontology_lifecycle_spec()
    if mapping.mapping_id != spec.mapping_id:
        raise ValueError("committed ontology requires the live mapping id")
    if mapping.content_sha256 != spec.mapping_sha256:
        raise ValueError("committed mapping hash drifted from frozen identity")
    if revision.revision_id != spec.revision_id:
        raise ValueError("derived ontology revision_id drifted from frozen identity")
    if revision.content_sha256 != spec.revision_hash:
        raise ValueError("derived ontology hash drifted from frozen identity")
    if revision.scope_mapping_id != mapping.mapping_id or revision.scope_mapping_hash != mapping.content_sha256:
        raise ValueError("ontology revision mapping identity does not match mapping")
    return revision


def plan_world_ontology_publication(
    *,
    published: WorldOntologyRevision | None,
    expected: WorldOntologyRevision,
    spec: WorldOntologyLifecycleSpec | Mapping[str, Any] | None = None,
) -> WorldOntologyPublicationPlan:
    """Classify boot publication. Never mutates a persisted revision id in place."""

    if not isinstance(expected, WorldOntologyRevision):
        raise TypeError("expected must be WorldOntologyRevision")
    resolved_spec = (
        committed_world_ontology_lifecycle_spec() if spec is None else WorldOntologyLifecycleSpec.from_mapping(spec)
    )
    if expected.revision_id != resolved_spec.revision_id:
        raise ValueError("expected revision_id must equal the current revision_id")
    if expected.scope_mapping_id != resolved_spec.mapping_id:
        raise ValueError("expected scope_mapping_id must equal the current mapping_id")
    if expected.content_sha256 != resolved_spec.revision_hash:
        raise ValueError("conflict: expected ontology hash drifted from current identity")
    if expected.scope_mapping_hash != resolved_spec.mapping_sha256:
        raise ValueError("conflict: expected mapping hash drifted from current identity")
    if published is None:
        return WorldOntologyPublicationPlan(
            action="publish",
            spec=resolved_spec,
            published_revision_id=None,
            expected_revision_id=expected.revision_id,
        )
    if not isinstance(published, WorldOntologyRevision):
        raise TypeError("published must be WorldOntologyRevision or None")
    if (
        published.revision_id == resolved_spec.revision_id
        and published.content_sha256 == expected.content_sha256
        and published.scope_mapping_id == expected.scope_mapping_id
        and published.scope_mapping_hash == expected.scope_mapping_hash
    ):
        return WorldOntologyPublicationPlan(
            action="ready",
            spec=resolved_spec,
            published_revision_id=published.revision_id,
            expected_revision_id=expected.revision_id,
        )
    raise ValueError("conflict: published ontology revision is not the current committed identity")


__all__ = [
    "ONTOLOGY_PUBLICATION_ACTIONS",
    "WorldOntologyLifecycleSpec",
    "WorldOntologyPublicationPlan",
    "committed_world_ontology_lifecycle_spec",
    "plan_world_ontology_publication",
    "require_committed_ontology_revision",
]
