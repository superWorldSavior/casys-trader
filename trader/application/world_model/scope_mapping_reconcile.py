"""Reconcile the live universe into typed WorldScopeMapping generations.

Existing mapped rows are preserved. Provider failures and unknown listings
stay unresolved. Persistence is opt-in; rotation must inject this use case
fail-open and never import it into the trading write path.
"""

from __future__ import annotations

from dataclasses import dataclass

from trader.application.world_model.scope_mapping_ports import (
    InstrumentListingMetadataPort,
    UniverseAnchorSource,
    WorldScopeMappingConfigStore,
)
from trader.domain.world_scope import WorldScopeMapping, WorldScopeMappingEntry
from trader.domain.world_scope_lifecycle import plan_world_scope_mapping_generation
from trader.domain.world_scope_listing import (
    UnresolvedInstrumentListing,
    propose_world_scope_mapping_entry,
)


@dataclass(frozen=True)
class WorldScopeMappingReconcileResult:
    action: str
    mapping: WorldScopeMapping
    unresolved: tuple[UnresolvedInstrumentListing, ...]
    added_anchors: tuple[tuple[str, str], ...]
    preserved_conflicts: tuple[tuple[str, str], ...]


class WorldScopeMappingReconcileService:
    def __init__(
        self,
        *,
        universe: UniverseAnchorSource,
        listings: InstrumentListingMetadataPort,
        store: WorldScopeMappingConfigStore,
    ) -> None:
        self._universe = universe
        self._listings = listings
        self._store = store

    def reconcile(self, *, persist: bool = False) -> WorldScopeMappingReconcileResult:
        current = self._store.load()
        if not isinstance(current, WorldScopeMapping):
            raise TypeError("mapping store must return WorldScopeMapping")
        proposed: list[WorldScopeMappingEntry] = []
        unresolved: list[UnresolvedInstrumentListing] = []
        mapped = {(entry.anchor.market_venue, entry.anchor.instrument) for entry in current.entries}
        for market_venue, instrument in self._universe.current_anchors():
            key = (str(market_venue), str(instrument))
            if key in mapped:
                continue
            try:
                listing = self._listings.lookup(market_venue=key[0], instrument=key[1])
                outcome = propose_world_scope_mapping_entry(listing)
            except Exception:  # noqa: BLE001 - listing failure must not invent a MIC
                unresolved.append(
                    UnresolvedInstrumentListing(
                        market_venue=key[0],
                        instrument=key[1],
                        status="unresolved",
                        reason="provider_failure",
                    )
                )
                continue
            if isinstance(outcome, UnresolvedInstrumentListing):
                unresolved.append(outcome)
                continue
            proposed.append(outcome)
        plan = plan_world_scope_mapping_generation(current=current, proposed=proposed)
        if persist and plan.action == "record_generation":
            self._store.save(plan.mapping)
        return WorldScopeMappingReconcileResult(
            action=plan.action,
            mapping=plan.mapping,
            unresolved=tuple(unresolved),
            added_anchors=plan.added_anchors,
            preserved_conflicts=plan.preserved_conflicts,
        )


__all__ = [
    "WorldScopeMappingReconcileResult",
    "WorldScopeMappingReconcileService",
]
