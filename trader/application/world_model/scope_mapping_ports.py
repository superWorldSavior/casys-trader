"""Consumer-owned ports for World scope-mapping reconciliation.

Adapters implement these contracts. This module does not import runtime,
infrastructure, or reporting. Universe rotation remains a separate authority.
"""

from __future__ import annotations

from typing import Protocol

from trader.domain.world_scope import WorldScopeMapping
from trader.domain.world_scope_lifecycle import WorldScopeMappingGeneration
from trader.domain.world_scope_listing import WorldInstrumentListing


class UniverseAnchorSource(Protocol):
    def current_anchors(self) -> tuple[tuple[str, str], ...]: ...


class InstrumentListingMetadataPort(Protocol):
    def lookup(self, *, market_venue: str, instrument: str) -> WorldInstrumentListing: ...


class WorldScopeMappingConfigStore(Protocol):
    def load(self) -> WorldScopeMapping: ...

    def save(self, mapping: WorldScopeMapping) -> None: ...


class WorldScopeMappingGenerationQuery(Protocol):
    def load_mapping_generation(
        self,
        mapping_id: str,
        mapping_sha256: str,
    ) -> WorldScopeMappingGeneration | None:
        """Return the durable generation pinned by identity, or None if unpublished."""
        ...


class WorldScopeMappingGenerationRepository(WorldScopeMappingGenerationQuery, Protocol):
    def persist_mapping_generation(
        self,
        mapping: WorldScopeMapping | WorldScopeMappingGeneration,
    ) -> WorldScopeMappingGeneration:
        """Append-only. Exact replay is idempotent. Divergent payload conflicts."""
        ...


__all__ = [
    "InstrumentListingMetadataPort",
    "UniverseAnchorSource",
    "WorldScopeMappingConfigStore",
    "WorldScopeMappingGenerationQuery",
    "WorldScopeMappingGenerationRepository",
]
