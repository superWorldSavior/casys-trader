"""Shared kernel for logical market anchors and canonical world/region/country/venue scopes.

Resolution is a pure lookup of mapping entries. Logical market venues are
never converted to a MIC by default.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from trader.domain.world_availability import WorldAvailabilitySubjectRef, world_subject_content_sha256


WORLD_SCOPE_MAPPING_SCHEMA = "world_scope_mapping.v1"
SCOPE_KINDS = frozenset({"world", "region", "country", "venue"})
RESOLUTION_STATUSES = frozenset({"resolved", "unmapped", "ambiguous"})
_SCOPE_PREFIXES = {
    "venue": "mic:",
    "country": "iso-3166:",
    "region": "iso-un-m49:",
}


def _required_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a non-empty string")
    text = value.strip()
    if not text:
        raise ValueError(f"{field_name} must be a non-empty string")
    return text


def _immutable_text_tuple(value: Sequence[str] | None, field_name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise TypeError(f"{field_name} must be a sequence of strings")
    return tuple(_required_text(item, f"{field_name}[]") for item in value)


@dataclass(frozen=True)
class WorldCanonicalScopeRef:
    """Namespaced world/region/country/venue identity. Not a logical market venue."""

    kind: str
    entity_id: str

    def __post_init__(self) -> None:
        kind = _required_text(self.kind, "kind").lower()
        if kind not in SCOPE_KINDS:
            allowed = ", ".join(sorted(SCOPE_KINDS))
            raise ValueError(f"scope kind must be one of: {allowed}")
        entity_id = _required_text(self.entity_id, "entity_id")
        if kind == "world":
            if entity_id != "market":
                raise ValueError("world scope entity_id must be 'market'")
        else:
            prefix = _SCOPE_PREFIXES[kind]
            if not entity_id.startswith(prefix):
                raise ValueError(f"{kind} entity_id must start with '{prefix}'")
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "entity_id", entity_id)

    def to_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "entity_id": self.entity_id}

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | WorldCanonicalScopeRef) -> WorldCanonicalScopeRef:
        if isinstance(value, WorldCanonicalScopeRef):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("scope ref must be WorldCanonicalScopeRef or a mapping")
        return cls(kind=value.get("kind"), entity_id=value.get("entity_id"))


@dataclass(frozen=True)
class WorldMarketAnchorRef:
    """Logical market venue plus an explicit instrument selector."""

    market_venue: str
    instrument: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "market_venue", _required_text(self.market_venue, "market_venue"))
        object.__setattr__(self, "instrument", _required_text(self.instrument, "instrument"))

    def to_dict(self) -> dict[str, str]:
        return {"market_venue": self.market_venue, "instrument": self.instrument}

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | WorldMarketAnchorRef) -> WorldMarketAnchorRef:
        if isinstance(value, WorldMarketAnchorRef):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("market anchor must be WorldMarketAnchorRef or a mapping")
        return cls(market_venue=value.get("market_venue"), instrument=value.get("instrument"))


@dataclass(frozen=True)
class WorldScopeMappingEntry:
    """One explicit (market_venue, instrument) → canonical venue/country/region/world row."""

    anchor: WorldMarketAnchorRef | Mapping[str, Any]
    venue: WorldCanonicalScopeRef | Mapping[str, Any]
    country: WorldCanonicalScopeRef | Mapping[str, Any]
    region: WorldCanonicalScopeRef | Mapping[str, Any]
    world: WorldCanonicalScopeRef | Mapping[str, Any]
    provider_proofs: Sequence[str]
    taxonomy_version: str

    def __post_init__(self) -> None:
        anchor = WorldMarketAnchorRef.from_mapping(self.anchor)
        venue = WorldCanonicalScopeRef.from_mapping(self.venue)
        country = WorldCanonicalScopeRef.from_mapping(self.country)
        region = WorldCanonicalScopeRef.from_mapping(self.region)
        world = WorldCanonicalScopeRef.from_mapping(self.world)
        if venue.kind != "venue":
            raise ValueError("venue scope kind must be venue")
        if country.kind != "country":
            raise ValueError("country scope kind must be country")
        if region.kind != "region":
            raise ValueError("region scope kind must be region")
        if world.kind != "world":
            raise ValueError("world scope kind must be world")
        proofs = tuple(sorted(frozenset(_immutable_text_tuple(self.provider_proofs, "provider_proofs"))))
        if not proofs:
            raise ValueError("mapping entry requires at least one provider proof")
        object.__setattr__(self, "anchor", anchor)
        object.__setattr__(self, "venue", venue)
        object.__setattr__(self, "country", country)
        object.__setattr__(self, "region", region)
        object.__setattr__(self, "world", world)
        object.__setattr__(self, "provider_proofs", proofs)
        object.__setattr__(self, "taxonomy_version", _required_text(self.taxonomy_version, "taxonomy_version"))

    def output_key(self) -> tuple[str, str, str, str]:
        return (self.venue.entity_id, self.country.entity_id, self.region.entity_id, self.world.entity_id)

    def to_dict(self) -> dict[str, Any]:
        return {
            "anchor": self.anchor.to_dict(),
            "venue": self.venue.to_dict(),
            "country": self.country.to_dict(),
            "region": self.region.to_dict(),
            "world": self.world.to_dict(),
            "provider_proofs": list(self.provider_proofs),
            "taxonomy_version": self.taxonomy_version,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | WorldScopeMappingEntry) -> WorldScopeMappingEntry:
        if isinstance(value, WorldScopeMappingEntry):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("mapping entry must be WorldScopeMappingEntry or a mapping")
        return cls(
            anchor=value.get("anchor"),
            venue=value.get("venue"),
            country=value.get("country"),
            region=value.get("region"),
            world=value.get("world"),
            provider_proofs=value.get("provider_proofs") or (),
            taxonomy_version=value.get("taxonomy_version"),
        )


def _entry_tuple(value: Sequence[Any] | None) -> tuple[WorldScopeMappingEntry, ...]:
    if value is None:
        return ()
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise TypeError("entries must be a sequence of WorldScopeMappingEntry")
    return tuple(
        item if isinstance(item, WorldScopeMappingEntry) else WorldScopeMappingEntry.from_mapping(item)
        for item in value
    )


def _canonical_entries(entries: Sequence[WorldScopeMappingEntry]) -> tuple[WorldScopeMappingEntry, ...]:
    return tuple(
        sorted(
            entries,
            key=lambda entry: (entry.anchor.market_venue, entry.anchor.instrument, entry.venue.entity_id),
        )
    )


@dataclass(frozen=True)
class WorldScopeResolution:
    """Persisted result of resolving one market anchor against a mapping version."""

    mapping_id: str
    mapping_sha256: str
    anchor: WorldMarketAnchorRef | Mapping[str, Any]
    status: str
    scopes: Sequence[WorldCanonicalScopeRef | Mapping[str, Any]] = ()

    def __post_init__(self) -> None:
        status = _required_text(self.status, "status")
        if status not in RESOLUTION_STATUSES:
            allowed = ", ".join(sorted(RESOLUTION_STATUSES))
            raise ValueError(f"resolution status must be one of: {allowed}")
        anchor = WorldMarketAnchorRef.from_mapping(self.anchor)
        scopes = tuple(
            item if isinstance(item, WorldCanonicalScopeRef) else WorldCanonicalScopeRef.from_mapping(item)
            for item in (self.scopes or ())
        )
        if status == "resolved":
            kinds = tuple(scope.kind for scope in scopes)
            if kinds != ("venue", "country", "region", "world"):
                raise ValueError("resolved scopes must be venue, country, region, world in that order")
        elif scopes:
            raise ValueError("unmapped/ambiguous resolution must not select a scope")
        object.__setattr__(self, "mapping_id", _required_text(self.mapping_id, "mapping_id"))
        object.__setattr__(self, "mapping_sha256", _required_text(self.mapping_sha256, "mapping_sha256"))
        object.__setattr__(self, "anchor", anchor)
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "scopes", scopes)

    def to_dict(self) -> dict[str, Any]:
        return {
            "mapping_id": self.mapping_id,
            "mapping_sha256": self.mapping_sha256,
            "anchor": self.anchor.to_dict(),
            "status": self.status,
            "scopes": [scope.to_dict() for scope in self.scopes],
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | WorldScopeResolution) -> WorldScopeResolution:
        if isinstance(value, WorldScopeResolution):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("scope resolution must be WorldScopeResolution or a mapping")
        return cls(
            mapping_id=value.get("mapping_id"),
            mapping_sha256=value.get("mapping_sha256"),
            anchor=value.get("anchor"),
            status=value.get("status"),
            scopes=value.get("scopes") or (),
        )


def _resolve_world_market_anchor(
    entries: Sequence[WorldScopeMappingEntry],
    anchor: WorldMarketAnchorRef,
    *,
    mapping_id: str,
    mapping_sha256: str,
) -> WorldScopeResolution:
    """Classify exact-anchor matches. No logical-venue fallback is applied."""

    query = WorldMarketAnchorRef.from_mapping(anchor)
    matching = [entry for entry in _entry_tuple(entries) if entry.anchor == query]
    if not matching:
        return WorldScopeResolution(
            mapping_id=mapping_id,
            mapping_sha256=mapping_sha256,
            anchor=query,
            status="unmapped",
        )
    outputs = {entry.output_key() for entry in matching}
    if len(outputs) > 1:
        return WorldScopeResolution(
            mapping_id=mapping_id,
            mapping_sha256=mapping_sha256,
            anchor=query,
            status="ambiguous",
        )
    chosen = matching[0]
    return WorldScopeResolution(
        mapping_id=mapping_id,
        mapping_sha256=mapping_sha256,
        anchor=query,
        status="resolved",
        scopes=(chosen.venue, chosen.country, chosen.region, chosen.world),
    )


@dataclass(frozen=True)
class WorldScopeMapping:
    """Immutable, deeply hashed table of explicit market-anchor → canonical-scope rows."""

    mapping_id: str
    entries: Sequence[WorldScopeMappingEntry | Mapping[str, Any]] = ()
    schema_version: str = WORLD_SCOPE_MAPPING_SCHEMA
    content_sha256: str | None = None

    def __post_init__(self) -> None:
        mapping_id = _required_text(self.mapping_id, "mapping_id")
        schema_version = _required_text(self.schema_version, "schema_version")
        if schema_version != WORLD_SCOPE_MAPPING_SCHEMA:
            raise ValueError(f"schema_version must be {WORLD_SCOPE_MAPPING_SCHEMA}")
        entries = _canonical_entries(_entry_tuple(self.entries))
        seen: dict[tuple[str, str], tuple[str, str, str, str]] = {}
        for entry in entries:
            key = (entry.anchor.market_venue, entry.anchor.instrument)
            output = entry.output_key()
            previous = seen.get(key)
            if previous is not None:
                if previous != output:
                    raise ValueError("conflicting WorldScopeMapping entries for the same market anchor")
                raise ValueError("duplicate WorldScopeMapping entries for the same market anchor")
            seen[key] = output
        content_payload = {
            "schema_version": schema_version,
            "mapping_id": mapping_id,
            "entries": [entry.to_dict() for entry in entries],
        }
        digest = world_subject_content_sha256(content_payload)
        if self.content_sha256 is not None and _required_text(self.content_sha256, "content_sha256") != digest:
            raise ValueError("content_sha256 does not match the canonical WorldScopeMapping")
        object.__setattr__(self, "mapping_id", mapping_id)
        object.__setattr__(self, "schema_version", schema_version)
        object.__setattr__(self, "entries", entries)
        object.__setattr__(self, "content_sha256", digest)

    def resolve(self, anchor: WorldMarketAnchorRef) -> WorldScopeResolution:
        return _resolve_world_market_anchor(
            self.entries,
            anchor,
            mapping_id=self.mapping_id,
            mapping_sha256=self.content_sha256,
        )

    def contains_anchor(self, anchor: WorldMarketAnchorRef) -> bool:
        query = WorldMarketAnchorRef.from_mapping(anchor)
        return any(entry.anchor == query for entry in self.entries)

    def content_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "mapping_id": self.mapping_id,
            "entries": [entry.to_dict() for entry in self.entries],
        }

    def subject_ref(self) -> WorldAvailabilitySubjectRef:
        return WorldAvailabilitySubjectRef(
            kind="world_scope_mapping",
            subject_id=self.mapping_id,
            content_sha256=world_subject_content_sha256(self.content_payload()),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.content_payload(),
            "content_sha256": self.content_sha256,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | WorldScopeMapping) -> WorldScopeMapping:
        if isinstance(value, WorldScopeMapping):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("scope mapping must be WorldScopeMapping or a mapping")
        return cls(
            mapping_id=value.get("mapping_id"),
            entries=value.get("entries") or (),
            schema_version=value.get("schema_version", WORLD_SCOPE_MAPPING_SCHEMA),
            content_sha256=value.get("content_sha256"),
        )


__all__ = [
    "RESOLUTION_STATUSES",
    "SCOPE_KINDS",
    "WORLD_SCOPE_MAPPING_SCHEMA",
    "WorldCanonicalScopeRef",
    "WorldMarketAnchorRef",
    "WorldScopeMapping",
    "WorldScopeMappingEntry",
    "WorldScopeResolution",
]
