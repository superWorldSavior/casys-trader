"""Consumer-owned ports for World scope-mapping reconciliation.

Adapters implement these contracts. This module does not import runtime,
infrastructure, or reporting. Universe rotation remains a separate authority.
"""

from __future__ import annotations

from typing import Protocol

from trader.domain.world_scope import WorldScopeMapping
from trader.domain.world_scope_listing import WorldInstrumentListing


class UniverseAnchorSource(Protocol):
    def current_anchors(self) -> tuple[tuple[str, str], ...]: ...


class InstrumentListingMetadataPort(Protocol):
    def lookup(self, *, market_venue: str, instrument: str) -> WorldInstrumentListing: ...


class WorldScopeMappingConfigStore(Protocol):
    def load(self) -> WorldScopeMapping: ...

    def save(self, mapping: WorldScopeMapping) -> None: ...


__all__ = [
    "InstrumentListingMetadataPort",
    "UniverseAnchorSource",
    "WorldScopeMappingConfigStore",
]
