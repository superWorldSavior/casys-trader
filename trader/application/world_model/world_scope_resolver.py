"""Load the committed WorldScopeMapping and resolve market anchors without heuristics.

Logical market venues are never converted to a default MIC at runtime. An exact
``(market_venue, instrument)`` row is required; otherwise the resolution is
``unmapped`` or ``ambiguous`` and the mapping id/hash stay attached.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import yaml

from trader.domain.world_episode import canonical_sha256
from trader.domain.world_scope import WorldMarketAnchorRef, WorldScopeMapping, WorldScopeResolution


@dataclass(frozen=True)
class WorldScopeResolver:
    """Application adapter over the operator-committed, hashed mapping table."""

    mapping: WorldScopeMapping
    operator_config_sha256: str

    def resolve(self, anchor: WorldMarketAnchorRef) -> WorldScopeResolution:
        return self.mapping.resolve(WorldMarketAnchorRef.from_mapping(anchor))

    @classmethod
    def load(cls, path: str | Path) -> WorldScopeResolver:
        mapping_path = Path(path)
        if mapping_path.is_dir():
            mapping_path = mapping_path / "world_scope_mapping.yaml"
        payload = yaml.safe_load(mapping_path.read_text(encoding="utf-8"))
        if not isinstance(payload, Mapping):
            raise TypeError("world scope mapping must be a mapping")
        hashed = dict(payload)
        claimed = hashed.pop("operator_config_sha256", None)
        if not claimed:
            raise ValueError("operator_config_sha256 is required")
        digest = canonical_sha256(hashed)
        if digest != str(claimed):
            raise ValueError("operator_config_sha256 does not match the canonical operator config")
        mapping = WorldScopeMapping.from_mapping(
            {
                "schema_version": payload.get("schema_version"),
                "mapping_id": payload.get("mapping_id"),
                "entries": payload.get("entries") or (),
                "content_sha256": payload.get("content_sha256"),
            }
        )
        return cls(mapping=mapping, operator_config_sha256=str(claimed))


__all__ = ["WorldScopeResolver"]
