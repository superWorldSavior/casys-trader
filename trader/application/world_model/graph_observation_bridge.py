"""Prospective macro→graph knowledge bridge. Runs outside episode capture."""

from __future__ import annotations

from trader.application.world_model.graph_ports import (
    BridgeRequestId,
    MacroGraphBridgeKey,
    MacroGraphBridgeLedger,
    MacroObservationScanPort,
    WorldGraphLedger,
)
from trader.domain.world_graph import (
    KnowledgeWorldRelationAsserted,
    MacroGraphBridgeFence,
    MacroGraphBridgeRegistry,
    MacroGraphBridgeRunSpec,
    MacroGraphObservationLinked,
    MacroGraphObservationSkipped,
    MacroObservationCursor,
    MacroObservationKnowledgeLink,
    StaleBridgeEpoch,
    WorldOntologyRevision,
)
from trader.domain.world_scope import WorldScopeMapping


class RegisterMacroObservationKnowledge:
    """Activate, reconcile, block, and hand off a single-active macro graph bridge."""

    def __init__(
        self,
        *,
        scan: MacroObservationScanPort,
        graph: WorldGraphLedger,
        bridge: MacroGraphBridgeLedger,
        scope_mapping: WorldScopeMapping,
        structural_revision: WorldOntologyRevision,
        bridge_key: str = "macro_graph_bridge.v1",
    ) -> None:
        if not isinstance(scope_mapping, WorldScopeMapping):
            raise TypeError("scope_mapping must be WorldScopeMapping")
        if not isinstance(structural_revision, WorldOntologyRevision):
            raise TypeError("structural_revision must be WorldOntologyRevision")
        self._scan = scan
        self._graph = graph
        self._bridge = bridge
        self._scope_mapping = scope_mapping
        self._structural_revision = structural_revision
        self._bridge_key = str(bridge_key).strip()

    def _key(self) -> MacroGraphBridgeKey:
        return MacroGraphBridgeKey(self._bridge_key)

    def _spec(self) -> MacroGraphBridgeRunSpec:
        return MacroGraphBridgeRunSpec(
            scope_mapping_id=self._scope_mapping.mapping_id,
            scope_mapping_hash=self._scope_mapping.content_sha256,
            ontology_revision_id=self._structural_revision.revision_id,
            ontology_revision_hash=self._structural_revision.content_sha256,
        )

    def _load(self) -> MacroGraphBridgeRegistry:
        return self._bridge.load(self._key())

    def _append(self, registry: MacroGraphBridgeRegistry, fence: MacroGraphBridgeFence | None) -> MacroGraphBridgeRegistry:
        event = registry.events[-1]
        self._bridge.append_event(event, expected_registry_version=registry.version - 1, fence=fence)
        return self._load()

    def activate(self, request_id: str) -> MacroGraphBridgeRegistry:
        reservation = self._scan.reserve_activation_cursor(self._key(), BridgeRequestId(str(request_id)))
        registry = self._load()
        updated = registry.activate(reservation=reservation, spec=self._spec(), expected_version=registry.version)
        if updated.version == registry.version:
            return updated
        return self._append(updated, fence=None)

    def reconcile(self, *, limit: int = 32) -> MacroGraphBridgeRegistry:
        registry = self._load()
        run = registry.active_run
        if run is None:
            return registry
        if self._spec() != run.spec:
            if run.status == "blocked":
                return registry
            updated = registry.block(reason="config_drift", expected_version=registry.version)
            return self._append(updated, fence=registry.fence)
        if run.status == "blocked":
            return registry
        fence = registry.fence
        envelopes = self._scan.list_available_after(run.cursor, limit)
        completed = run.cursor
        for envelope in envelopes:
            observation_id = envelope.observation.observation_id
            cursor = self._scan.cursor_for(observation_id)
            if _already_terminal(registry, observation_id):
                completed = cursor
                continue
            try:
                registry = self._reconcile_one(registry, envelope, cursor, fence)
                fence = registry.fence
                completed = cursor
            except StaleBridgeEpoch:
                raise
            except OSError:
                break
        return self._advance_if_needed(registry, completed, fence)

    def handoff(
        self,
        *,
        scope_mapping: WorldScopeMapping,
        structural_revision: WorldOntologyRevision,
    ) -> MacroGraphBridgeRegistry:
        if not isinstance(scope_mapping, WorldScopeMapping):
            raise TypeError("scope_mapping must be WorldScopeMapping")
        if not isinstance(structural_revision, WorldOntologyRevision):
            raise TypeError("structural_revision must be WorldOntologyRevision")
        next_spec = MacroGraphBridgeRunSpec(
            scope_mapping_id=scope_mapping.mapping_id,
            scope_mapping_hash=scope_mapping.content_sha256,
            ontology_revision_id=structural_revision.revision_id,
            ontology_revision_hash=structural_revision.content_sha256,
        )
        registry = self._load()
        run = registry.active_run
        if run is None:
            raise ValueError("handoff requires a blocked active run")
        updated = registry.handoff(
            active_run_id=run.run_id,
            next_run_spec=next_spec,
            expected_version=registry.version,
        )
        return self._append(updated, fence=None)

    def _reconcile_one(
        self,
        registry: MacroGraphBridgeRegistry,
        envelope: object,
        cursor: MacroObservationCursor,
        fence: MacroGraphBridgeFence | None,
    ) -> MacroGraphBridgeRegistry:
        link = MacroObservationKnowledgeLink.from_envelope(
            envelope,
            self._scope_mapping,
            self._structural_revision,
        )
        observation_id = envelope.observation.observation_id  # type: ignore[attr-defined]
        if link.status == "skipped":
            updated = registry.skip_observation(
                observation_id=observation_id,
                reason=str(link.skip_reason),
                cursor=cursor,
                expected_version=registry.version,
            )
            if updated.version == registry.version:
                return updated
            return self._append(updated, fence=fence)
        asserted = KnowledgeWorldRelationAsserted(relation=link.relation)
        persisted = self._graph.append_knowledge_relation_event(
            asserted,
            fence=fence,
            expected_registry_version=registry.version,
        )
        updated = registry.link_observation(
            observation_id=observation_id,
            relation=link.relation,
            relation_event_id=str(persisted.identity),
            cursor=cursor,
            expected_version=registry.version,
        )
        if updated.version == registry.version:
            return updated
        return self._append(updated, fence=fence)

    def _advance_if_needed(
        self,
        registry: MacroGraphBridgeRegistry,
        completed: MacroObservationCursor,
        fence: MacroGraphBridgeFence | None,
    ) -> MacroGraphBridgeRegistry:
        run = registry.active_run
        if run is None or run.status != "active" or completed == run.cursor:
            return registry
        updated = registry.advance_cursor(completed, expected_version=registry.version)
        if updated.version == registry.version:
            return updated
        return self._append(updated, fence=fence)


def _already_terminal(registry: MacroGraphBridgeRegistry, observation_id: str) -> bool:
    return any(
        isinstance(event, (MacroGraphObservationLinked, MacroGraphObservationSkipped))
        and event.observation_id == observation_id
        for event in registry.events
    )
