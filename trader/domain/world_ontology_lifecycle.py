"""Append-only market ontology / scope-mapping publication.

``market_ontology.v1`` / ``world_scope_mapping.v1`` are schema families, not
aggregate instance ids. A mapping content hash is a generation. The ontology
revision instance id is derived from that hash. Fresh state publishes. A new
generation appends a new revision and supersedes the prior active one. Same
instance id is never rewritten. Domain code does not read files or frozen
SHA literals as live authorities.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from trader.domain.world_episode import canonical_sha256
from trader.domain.world_feature_contract import MARKET_ONTOLOGY_REVISION, WORLD_SCOPE_MAPPING_ID
from trader.domain.world_graph import WorldOntologyRevision
from trader.domain.world_scope import WorldScopeMapping


ONTOLOGY_PUBLICATION_ACTIONS = frozenset({"ready", "publish", "supersede"})
MARKET_ONTOLOGY_REVISION_FAMILY = MARKET_ONTOLOGY_REVISION
MARKET_ONTOLOGY_REVISION_PREFIX = "market_ontology:v1:"
# Extended derivation scheme. v1 embedded refresh-varying brief refs in derived
# edges (stable id, drifting content -> unpublishable conflict); v2 derives
# from identity-stable inputs only. Bump to force one clean supersede.
MARKET_ONTOLOGY_EXTENDED_DERIVATION_VERSION = "2"


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


def market_ontology_revision_id_for_mapping_hash(mapping_sha256: str) -> str:
    """Deterministic ontology instance id for one mapping generation."""

    return f"{MARKET_ONTOLOGY_REVISION_PREFIX}{_sha256_hex(mapping_sha256, 'mapping_sha256')}"


def market_ontology_revision_id(mapping: WorldScopeMapping) -> str:
    if not isinstance(mapping, WorldScopeMapping):
        raise TypeError("mapping must be WorldScopeMapping")
    if mapping.mapping_id != WORLD_SCOPE_MAPPING_ID:
        raise ValueError(f"mapping_id must be {WORLD_SCOPE_MAPPING_ID}")
    return market_ontology_revision_id_for_mapping_hash(mapping.content_sha256)


def market_ontology_extended_revision_id(
    mapping: WorldScopeMapping,
    *,
    registry_sha256: str,
    catalog_sha256: str,
) -> str:
    """Deterministic revision id for mapping + issuer registry + family catalog."""

    if not isinstance(mapping, WorldScopeMapping):
        raise TypeError("mapping must be WorldScopeMapping")
    if mapping.mapping_id != WORLD_SCOPE_MAPPING_ID:
        raise ValueError(f"mapping_id must be {WORLD_SCOPE_MAPPING_ID}")
    digest = canonical_sha256(
        {
            "derivation_version": MARKET_ONTOLOGY_EXTENDED_DERIVATION_VERSION,
            "mapping_sha256": _sha256_hex(mapping.content_sha256, "mapping_sha256"),
            "registry_sha256": _sha256_hex(registry_sha256, "registry_sha256"),
            "catalog_sha256": _sha256_hex(catalog_sha256, "catalog_sha256"),
        }
    )
    return f"{MARKET_ONTOLOGY_REVISION_PREFIX}{digest}"


def admits_market_ontology_family(revision_id: str) -> bool:
    text = _required_text(revision_id, "revision_id")
    return text == MARKET_ONTOLOGY_REVISION_FAMILY or text.startswith(MARKET_ONTOLOGY_REVISION_PREFIX)


@dataclass(frozen=True)
class WorldOntologyLifecycleSpec:
    """One mapping generation's ontology identities."""

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
        if action == "supersede":
            if published_revision_id is None:
                raise ValueError("supersede plan requires a published predecessor revision")
            if published_revision_id == expected_revision_id:
                raise ValueError("supersede plan requires a distinct successor revision id")
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


def world_ontology_lifecycle_spec(
    mapping: WorldScopeMapping,
    revision: WorldOntologyRevision,
) -> WorldOntologyLifecycleSpec:
    require_mapping_aligned_ontology_revision(revision, mapping)
    return WorldOntologyLifecycleSpec(
        revision_id=revision.revision_id,
        mapping_id=mapping.mapping_id,
        revision_hash=revision.content_sha256,
        mapping_sha256=mapping.content_sha256,
    )


def committed_world_ontology_lifecycle_spec(
    mapping: WorldScopeMapping | None = None,
    revision: WorldOntologyRevision | None = None,
) -> WorldOntologyLifecycleSpec:
    """Derive the live spec from a mapping generation. No frozen SHA authority."""

    if mapping is None or revision is None:
        raise TypeError("lifecycle spec must be derived from mapping and revision")
    return world_ontology_lifecycle_spec(mapping, revision)


def require_mapping_aligned_ontology_revision(
    revision: WorldOntologyRevision,
    mapping: WorldScopeMapping,
) -> WorldOntologyRevision:
    """Fail closed when a derived revision is not this mapping generation."""

    if not isinstance(revision, WorldOntologyRevision):
        raise TypeError("revision must be WorldOntologyRevision")
    if not isinstance(mapping, WorldScopeMapping):
        raise TypeError("mapping must be WorldScopeMapping")
    if mapping.mapping_id != WORLD_SCOPE_MAPPING_ID:
        raise ValueError("committed ontology requires the live mapping id")
    expected_id = market_ontology_revision_id(mapping)
    if revision.revision_id != expected_id:
        raise ValueError("derived ontology revision_id drifted from mapping generation")
    if revision.scope_mapping_id != mapping.mapping_id or revision.scope_mapping_hash != mapping.content_sha256:
        raise ValueError("ontology revision mapping identity does not match mapping")
    return revision


def require_committed_ontology_revision(
    revision: WorldOntologyRevision,
    mapping: WorldScopeMapping,
) -> WorldOntologyRevision:
    return require_mapping_aligned_ontology_revision(revision, mapping)


def require_extended_ontology_revision(
    revision: WorldOntologyRevision,
    mapping: WorldScopeMapping,
    *,
    registry_sha256: str,
    catalog_sha256: str,
) -> WorldOntologyRevision:
    """Fail closed when a revision is not this mapping + registry + catalog generation."""

    if not isinstance(revision, WorldOntologyRevision):
        raise TypeError("revision must be WorldOntologyRevision")
    if not isinstance(mapping, WorldScopeMapping):
        raise TypeError("mapping must be WorldScopeMapping")
    if mapping.mapping_id != WORLD_SCOPE_MAPPING_ID:
        raise ValueError("committed ontology requires the live mapping id")
    expected_id = market_ontology_extended_revision_id(
        mapping, registry_sha256=registry_sha256, catalog_sha256=catalog_sha256
    )
    if revision.revision_id != expected_id:
        raise ValueError("derived ontology revision_id drifted from extended inputs")
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
        WorldOntologyLifecycleSpec(
            revision_id=expected.revision_id,
            mapping_id=expected.scope_mapping_id,
            revision_hash=expected.content_sha256,
            mapping_sha256=expected.scope_mapping_hash,
        )
        if spec is None
        else WorldOntologyLifecycleSpec.from_mapping(spec)
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
    if (
        published.scope_mapping_id == resolved_spec.mapping_id
        and admits_market_ontology_family(published.revision_id)
        and admits_market_ontology_family(expected.revision_id)
        and published.revision_id != expected.revision_id
    ):
        return WorldOntologyPublicationPlan(
            action="supersede",
            spec=resolved_spec,
            published_revision_id=published.revision_id,
            expected_revision_id=expected.revision_id,
        )
    raise ValueError("conflict: published ontology revision is not the current committed identity")


__all__ = [
    "MARKET_ONTOLOGY_REVISION_FAMILY",
    "MARKET_ONTOLOGY_REVISION_PREFIX",
    "ONTOLOGY_PUBLICATION_ACTIONS",
    "WorldOntologyLifecycleSpec",
    "WorldOntologyPublicationPlan",
    "admits_market_ontology_family",
    "committed_world_ontology_lifecycle_spec",
    "market_ontology_extended_revision_id",
    "market_ontology_revision_id",
    "market_ontology_revision_id_for_mapping_hash",
    "plan_world_ontology_publication",
    "require_committed_ontology_revision",
    "require_extended_ontology_revision",
    "require_mapping_aligned_ontology_revision",
    "world_ontology_lifecycle_spec",
]
