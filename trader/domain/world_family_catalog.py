"""Versioned, hash-pinned family catalog for MEMBER_OF_FAMILY derivation.

The hand-maintained sector catalog cannot be consumed raw in point-in-time
logic: silent edits would rewrite history. ``FamilyCatalog`` freezes one
generation (id + symbol→family entries + content hash). Any catalog change is
a new generation and, through D0 derivation, a new ontology revision. Symbols
absent from the catalog get no family edge; ambiguous dual membership fails
closed instead of silently picking a winner.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from trader.domain.world_episode import canonical_sha256

FAMILY_CATALOG_SCHEMA = "family_catalog.v1"

_FAMILY_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_.:-]{0,63}$")
_CATALOG_KEYS = frozenset({"catalog_id", "entries", "content_sha256"})


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


def normalize_symbol(value: Any) -> str:
    """Upper/strip normalization shared by catalog build and lookup."""

    text = _required_text(value, "symbol").upper()
    if any(char.isspace() for char in text):
        raise ValueError("symbol must not contain whitespace")
    return text


def normalize_family_id(value: Any) -> str:
    """Bare taxonomy id (the ``taxonomy:`` prefix is added at entity creation)."""

    text = _required_text(value, "family").lower()
    if not _FAMILY_ID_RE.fullmatch(text):
        raise ValueError("family must be a bare taxonomy id ([a-z0-9_.:-], 1-64 chars)")
    return text


def _freeze_entries(value: Mapping[str, Any] | None) -> Mapping[str, str]:
    if value is None:
        return MappingProxyType({})
    if not isinstance(value, Mapping):
        raise TypeError("entries must be a mapping of symbol to family")
    frozen: dict[str, str] = {}
    for raw_symbol, raw_family in value.items():
        symbol = normalize_symbol(raw_symbol)
        if symbol in frozen:
            raise ValueError(f"duplicate catalog symbol after normalization: {symbol}")
        frozen[symbol] = normalize_family_id(raw_family)
    return MappingProxyType(dict(sorted(frozen.items())))


@dataclass(frozen=True)
class FamilyCatalog:
    """One immutable family-catalog generation."""

    catalog_id: str
    entries: Mapping[str, str]
    content_sha256: str | None = None

    def __post_init__(self) -> None:
        catalog_id = _required_text(self.catalog_id, "catalog_id")
        entries = _freeze_entries(self.entries)
        digest = canonical_sha256({"catalog_id": catalog_id, "entries": dict(entries)})
        if self.content_sha256 is not None and _sha256_hex(self.content_sha256, "content_sha256") != digest:
            raise ValueError("content_sha256 does not match the canonical family catalog")
        object.__setattr__(self, "catalog_id", catalog_id)
        object.__setattr__(self, "entries", entries)
        object.__setattr__(self, "content_sha256", digest)

    @property
    def families(self) -> frozenset[str]:
        return frozenset(self.entries.values())

    def family_for_symbol(self, symbol: Any) -> str | None:
        return self.entries.get(normalize_symbol(symbol))

    def symbols_for_family(self, family: Any) -> tuple[str, ...]:
        wanted = normalize_family_id(family)
        return tuple(sorted(symbol for symbol, member in self.entries.items() if member == wanted))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": FAMILY_CATALOG_SCHEMA,
            "catalog_id": self.catalog_id,
            "entries": dict(self.entries),
            "content_sha256": self.content_sha256,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | FamilyCatalog) -> FamilyCatalog:
        if isinstance(value, FamilyCatalog):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("family catalog must be FamilyCatalog or a mapping")
        unknown = sorted(str(key) for key in value if key not in (_CATALOG_KEYS | {"schema_version"}))
        if unknown:
            raise ValueError(f"family catalog forbids unknown fields: {', '.join(unknown)}")
        schema_version = value.get("schema_version")
        if schema_version not in (None, "", FAMILY_CATALOG_SCHEMA):
            raise ValueError(f"schema_version must be {FAMILY_CATALOG_SCHEMA}")
        return cls(
            catalog_id=value.get("catalog_id"),
            entries=value.get("entries"),
            content_sha256=value.get("content_sha256"),
        )

    @classmethod
    def from_grouped(cls, catalog_id: str, grouped: Mapping[str, Any]) -> FamilyCatalog:
        """Build from a family→symbols grouping (e.g. the sector catalog)."""

        if not isinstance(grouped, Mapping):
            raise TypeError("grouped catalog must be a mapping of family to symbols")
        entries: dict[str, str] = {}
        for raw_family, raw_symbols in grouped.items():
            family = normalize_family_id(raw_family)
            if isinstance(raw_symbols, (str, bytes, bytearray)) or not isinstance(raw_symbols, (tuple, list)):
                raise TypeError(f"family {family} members must be a list or tuple of symbols")
            for raw_symbol in raw_symbols:
                symbol = normalize_symbol(raw_symbol)
                if symbol in entries and entries[symbol] != family:
                    raise ValueError(
                        f"symbol {symbol} is claimed by families {entries[symbol]} and {family}"
                    )
                entries[symbol] = family
        return cls(catalog_id=catalog_id, entries=entries)


__all__ = [
    "FAMILY_CATALOG_SCHEMA",
    "FamilyCatalog",
    "normalize_family_id",
    "normalize_symbol",
]
