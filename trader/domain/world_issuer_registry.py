"""Provisional Yahoo-sourced issuer registry for company derivation.

Each entry binds one mapped instrument (``mic:…:symbol:…``) to a provisional
company identity ``issuer:yahoo:v1:<MIC>:<symbol>``, sourced from a verified
company brief. Only ``identity_status=verified`` briefs enter; LEI/CIK/MOPS
reconciliation arrives later through the identity map (supersede, never
rewrite). Multi-venue symbols get one entry per listing: no invented grouping.

Refresh safety: ``content_sha256`` covers identities only
``(instrument node id, issuer id)``. Brief refreshes (new ``as_of``) with
unchanged identity keep the same hash, so ontology revisions do not churn.
``brief_as_of`` and ``source_refs`` travel as audit metadata.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from trader.domain.world_episode import canonical_sha256

ISSUER_REGISTRY_SCHEMA = "issuer_registry.v1"
ISSUER_ID_PREFIX = "issuer:yahoo:v1:"
ISSUER_RESOLUTION_METHODS = frozenset({"mapping_plus_exchange", "mapping_only"})

_MIC_RE = re.compile(r"^[A-Z0-9]{4}$")
_REGISTRY_KEYS = frozenset({"registry_id", "entries", "content_sha256"})
_ENTRY_KEYS = frozenset(
    {
        "instrument_node_id",
        "market_venue",
        "symbol",
        "mic",
        "issuer_entity_id",
        "identity_status",
        "brief_as_of",
        "source_refs",
        "resolution_method",
    }
)


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


def issuer_entity_id_for_listing(mic: Any, symbol: Any) -> str:
    """Deterministic provisional company id for one (MIC, symbol) listing."""

    mic_text = _required_text(mic, "mic").upper()
    if not _MIC_RE.fullmatch(mic_text):
        raise ValueError("mic must be a 4-character MIC")
    symbol_text = _required_text(symbol, "symbol")
    if any(char.isspace() for char in symbol_text):
        raise ValueError("symbol must not contain whitespace")
    return f"{ISSUER_ID_PREFIX}{mic_text}:{symbol_text}"


def _immutable_source_refs(value: Sequence[str] | None) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise TypeError("source_refs must be a sequence of strings")
    return tuple(_required_text(item, "source_refs[]") for item in value)


@dataclass(frozen=True)
class IssuerEntry:
    """One verified listing → provisional company binding."""

    instrument_node_id: str
    market_venue: str
    symbol: str
    mic: str
    issuer_entity_id: str
    identity_status: str
    brief_as_of: str
    source_refs: Sequence[str]
    resolution_method: str

    def __post_init__(self) -> None:
        node_id = _required_text(self.instrument_node_id, "instrument_node_id")
        venue = _required_text(self.market_venue, "market_venue")
        symbol = _required_text(self.symbol, "symbol")
        mic = _required_text(self.mic, "mic").upper()
        if not _MIC_RE.fullmatch(mic):
            raise ValueError("mic must be a 4-character MIC")
        expected_node = f"instrument:mic:{mic}:symbol:{symbol}"
        if node_id != expected_node:
            raise ValueError("instrument_node_id must equal instrument:mic:<MIC>:symbol:<symbol>")
        issuer_id = _required_text(self.issuer_entity_id, "issuer_entity_id")
        if issuer_id != issuer_entity_id_for_listing(mic, symbol):
            raise ValueError("issuer_entity_id must equal issuer:yahoo:v1:<MIC>:<symbol>")
        status = _required_text(self.identity_status, "identity_status")
        if status != "verified":
            raise ValueError("issuer entries require identity_status=verified")
        as_of = _required_text(self.brief_as_of, "brief_as_of")
        source_refs = _immutable_source_refs(self.source_refs)
        if not source_refs:
            raise ValueError("source_refs must not be empty")
        method = _required_text(self.resolution_method, "resolution_method")
        if method not in ISSUER_RESOLUTION_METHODS:
            allowed = ", ".join(sorted(ISSUER_RESOLUTION_METHODS))
            raise ValueError(f"resolution_method must be one of: {allowed}")
        object.__setattr__(self, "instrument_node_id", node_id)
        object.__setattr__(self, "market_venue", venue)
        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(self, "mic", mic)
        object.__setattr__(self, "issuer_entity_id", issuer_id)
        object.__setattr__(self, "identity_status", status)
        object.__setattr__(self, "brief_as_of", as_of)
        object.__setattr__(self, "source_refs", source_refs)
        object.__setattr__(self, "resolution_method", method)

    def to_dict(self) -> dict[str, Any]:
        return {
            "instrument_node_id": self.instrument_node_id,
            "market_venue": self.market_venue,
            "symbol": self.symbol,
            "mic": self.mic,
            "issuer_entity_id": self.issuer_entity_id,
            "identity_status": self.identity_status,
            "brief_as_of": self.brief_as_of,
            "source_refs": list(self.source_refs),
            "resolution_method": self.resolution_method,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | IssuerEntry) -> IssuerEntry:
        if isinstance(value, IssuerEntry):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("issuer entry must be IssuerEntry or a mapping")
        unknown = sorted(str(key) for key in value if key not in _ENTRY_KEYS)
        if unknown:
            raise ValueError(f"issuer entry forbids unknown fields: {', '.join(unknown)}")
        return cls(
            instrument_node_id=value.get("instrument_node_id"),
            market_venue=value.get("market_venue"),
            symbol=value.get("symbol"),
            mic=value.get("mic"),
            issuer_entity_id=value.get("issuer_entity_id"),
            identity_status=value.get("identity_status"),
            brief_as_of=value.get("brief_as_of"),
            source_refs=value.get("source_refs"),
            resolution_method=value.get("resolution_method"),
        )


def _freeze_entries(value: Mapping[str, Any] | None) -> Mapping[str, IssuerEntry]:
    if value is None:
        return MappingProxyType({})
    if not isinstance(value, Mapping):
        raise TypeError("entries must be a mapping of instrument node id to issuer entry")
    frozen: dict[str, IssuerEntry] = {}
    for key, raw in value.items():
        node_id = _required_text(key, "entries key")
        entry = IssuerEntry.from_mapping(raw)
        if entry.instrument_node_id != node_id:
            raise ValueError("registry key must equal the entry instrument_node_id")
        frozen[node_id] = entry
    return MappingProxyType(dict(sorted(frozen.items())))


@dataclass(frozen=True)
class IssuerRegistry:
    """One immutable issuer-registry generation. Hash covers identities only."""

    registry_id: str
    entries: Mapping[str, IssuerEntry | Mapping[str, Any]]
    content_sha256: str | None = None

    def __post_init__(self) -> None:
        registry_id = _required_text(self.registry_id, "registry_id")
        entries = _freeze_entries(self.entries)
        digest = canonical_sha256(
            {
                "registry_id": registry_id,
                "identities": sorted(
                    (node_id, entry.issuer_entity_id) for node_id, entry in entries.items()
                ),
            }
        )
        if self.content_sha256 is not None and _sha256_hex(self.content_sha256, "content_sha256") != digest:
            raise ValueError("content_sha256 does not match the canonical issuer registry")
        object.__setattr__(self, "registry_id", registry_id)
        object.__setattr__(self, "entries", entries)
        object.__setattr__(self, "content_sha256", digest)

    def issuer_for_instrument(self, node_id: Any) -> IssuerEntry | None:
        return self.entries.get(_required_text(node_id, "instrument_node_id"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": ISSUER_REGISTRY_SCHEMA,
            "registry_id": self.registry_id,
            "entries": {key: entry.to_dict() for key, entry in self.entries.items()},
            "content_sha256": self.content_sha256,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | IssuerRegistry) -> IssuerRegistry:
        if isinstance(value, IssuerRegistry):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("issuer registry must be IssuerRegistry or a mapping")
        unknown = sorted(str(key) for key in value if key not in (_REGISTRY_KEYS | {"schema_version"}))
        if unknown:
            raise ValueError(f"issuer registry forbids unknown fields: {', '.join(unknown)}")
        schema_version = value.get("schema_version")
        if schema_version not in (None, "", ISSUER_REGISTRY_SCHEMA):
            raise ValueError(f"schema_version must be {ISSUER_REGISTRY_SCHEMA}")
        return cls(
            registry_id=value.get("registry_id"),
            entries=value.get("entries"),
            content_sha256=value.get("content_sha256"),
        )


__all__ = [
    "ISSUER_ID_PREFIX",
    "ISSUER_REGISTRY_SCHEMA",
    "ISSUER_RESOLUTION_METHODS",
    "IssuerEntry",
    "IssuerRegistry",
    "issuer_entity_id_for_listing",
]
