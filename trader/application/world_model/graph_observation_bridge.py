"""Prospective macro→graph knowledge bridge. Runs outside episode capture."""

from __future__ import annotations

from trader.application.world_model.graph_ports import (
    BridgeRequestId,
    MacroGraphBridgeKey,
    MacroGraphBridgeLedger,
    MacroObservationScanPort,
    WorldGraphLedger,
    WorldRelationEventId,
)
from trader.domain.world_graph import (
    MACRO_GRAPH_BRIDGE_RUN_SPEC_SCHEMA_V2,
    KnowledgeWorldRelationAsserted,
    KnowledgeWorldRelationEvent,
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
from trader.domain.world_graph_bridge_lifecycle import (
    UnknownMacroGraphBridgeDrift,
    classify_macro_graph_bridge,
    observes_retirement,
    predecessor_owned_observes,
    remaining_owned_observes,
)
from trader.domain.world_macro import MACRO_PRODUCER_VERSION, MacroCollectionPlan
from trader.domain.world_scope import WorldScopeMapping


class RegisterMacroObservationKnowledge:
    """Align, reconcile, and remediate a single-active macro graph bridge."""

    def __init__(
        self,
        *,
        scan: MacroObservationScanPort,
        graph: WorldGraphLedger,
        bridge: MacroGraphBridgeLedger,
        scope_mapping: WorldScopeMapping,
        structural_revision: WorldOntologyRevision,
        collection_plan: MacroCollectionPlan,
        bridge_key: str = "macro_graph_bridge.v1",
    ) -> None:
        if not isinstance(scope_mapping, WorldScopeMapping):
            raise TypeError("scope_mapping must be WorldScopeMapping")
        if not isinstance(structural_revision, WorldOntologyRevision):
            raise TypeError("structural_revision must be WorldOntologyRevision")
        if not isinstance(collection_plan, MacroCollectionPlan):
            raise TypeError("collection_plan must be MacroCollectionPlan")
        self._scan = scan
        self._graph = graph
        self._bridge = bridge
        self._scope_mapping = scope_mapping
        self._structural_revision = structural_revision
        self._collection_plan = collection_plan
        self._bridge_key = str(bridge_key).strip()

    def _key(self) -> MacroGraphBridgeKey:
        return MacroGraphBridgeKey(self._bridge_key)

    def _spec(self) -> MacroGraphBridgeRunSpec:
        return MacroGraphBridgeRunSpec(
            scope_mapping_id=self._scope_mapping.mapping_id,
            scope_mapping_hash=self._scope_mapping.content_sha256,
            ontology_revision_id=self._structural_revision.revision_id,
            ontology_revision_hash=self._structural_revision.content_sha256,
            schema_version=MACRO_GRAPH_BRIDGE_RUN_SPEC_SCHEMA_V2,
            collection_plan_id=self._collection_plan.plan_id,
            collection_plan_hash=self._collection_plan.content_sha256,
            producer_version=MACRO_PRODUCER_VERSION,
        )

    def _load(self) -> MacroGraphBridgeRegistry:
        return self._bridge.load(self._key())

    def _append(self, registry: MacroGraphBridgeRegistry, fence: MacroGraphBridgeFence | None) -> MacroGraphBridgeRegistry:
        event = registry.events[-1]
        self._bridge.append_event(event, expected_registry_version=registry.version - 1, fence=fence)
        return self._load()

    def ensure(self, request_id: str) -> MacroGraphBridgeRegistry:
        """Classify durable state, then reserve/activate only when the generation is missing."""

        desired = self._spec()
        registry = self._load()
        decision = classify_macro_graph_bridge(registry, desired=desired)
        if decision.status == "missing":
            return self.activate(request_id)
        if decision.status == "matched_active":
            return registry
        if decision.status == "matched_blocked":
            updated = registry.resume(spec=desired, expected_version=registry.version)
            if updated.version == registry.version:
                return updated
            return self._append(updated, fence=None)
        if decision.status == "drifted_active":
            updated = registry.block(reason="config_drift", expected_version=registry.version)
            registry = self._append(updated, fence=registry.fence)
        return self._remediate_then_handoff(registry)

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
        scope_mapping: WorldScopeMapping | None = None,
        structural_revision: WorldOntologyRevision | None = None,
    ) -> MacroGraphBridgeRegistry:
        if scope_mapping is not None:
            if not isinstance(scope_mapping, WorldScopeMapping):
                raise TypeError("scope_mapping must be WorldScopeMapping")
            if scope_mapping != self._scope_mapping:
                raise ValueError("handoff scope_mapping must match the bound mapping")
        if structural_revision is not None:
            if not isinstance(structural_revision, WorldOntologyRevision):
                raise TypeError("structural_revision must be WorldOntologyRevision")
            if structural_revision != self._structural_revision:
                raise ValueError("handoff structural_revision must match the bound revision")
        registry = self._load()
        return self._handoff(registry)

    def _handoff(self, registry: MacroGraphBridgeRegistry) -> MacroGraphBridgeRegistry:
        run = registry.active_run
        if run is None:
            raise ValueError("handoff requires a blocked active run")
        updated = registry.handoff(
            active_run_id=run.run_id,
            next_run_spec=self._spec(),
            expected_version=registry.version,
        )
        return self._append(updated, fence=None)

    def _remediate_then_handoff(self, registry: MacroGraphBridgeRegistry) -> MacroGraphBridgeRegistry:
        desired = self._spec()
        decision = classify_macro_graph_bridge(registry, desired=desired)
        if decision.status == "unknown_drift":
            raise UnknownMacroGraphBridgeDrift(
                decision.reason,
                context={
                    "durable_spec": None if registry.active_run is None else registry.active_run.spec.to_dict(),
                    "desired_spec": desired.to_dict(),
                },
            )
        if decision.status != "drifted_blocked_admitted":
            return registry
        registry = self._retire_owned_observes(registry)
        run = registry.active_run
        if run is None:
            raise UnknownMacroGraphBridgeDrift("blocked_run_missing_after_remediation")
        owned = predecessor_owned_observes(registry, run.run_id)
        found: list[KnowledgeWorldRelationEvent] = []
        for link in owned:
            asserted = self._graph.get_knowledge_relation_event(WorldRelationEventId(link.relation_event_id))
            if not isinstance(asserted, KnowledgeWorldRelationAsserted):
                continue
            expected = observes_retirement(relation=asserted.relation, linked=link, run_id=run.run_id)
            existing = self._graph.get_knowledge_relation_event(WorldRelationEventId(expected.event_id))
            if existing is not None:
                found.append(existing)
        if remaining_owned_observes(owned, found):
            return registry
        return self._handoff(registry)

    def _retire_owned_observes(self, registry: MacroGraphBridgeRegistry) -> MacroGraphBridgeRegistry:
        run = registry.active_run
        if run is None or run.status != "blocked":
            raise UnknownMacroGraphBridgeDrift("remediation_requires_blocked_predecessor")
        fence = registry.fence
        for link in predecessor_owned_observes(registry, run.run_id):
            asserted = self._graph.get_knowledge_relation_event(WorldRelationEventId(link.relation_event_id))
            if not isinstance(asserted, KnowledgeWorldRelationAsserted):
                raise UnknownMacroGraphBridgeDrift(
                    "missing_owned_observes_assertion",
                    context={"relation_event_id": link.relation_event_id, "relation_id": link.relation_id},
                )
            retired = observes_retirement(relation=asserted.relation, linked=link, run_id=run.run_id)
            existing = self._graph.get_knowledge_relation_event(WorldRelationEventId(retired.event_id))
            if existing is not None:
                if existing != retired:
                    raise UnknownMacroGraphBridgeDrift(
                        "retirement_identity_conflict",
                        context={"relation_id": link.relation_id, "event_id": retired.event_id},
                    )
                continue
            self._graph.append_knowledge_relation_event(
                retired,
                fence=fence,
                expected_registry_version=registry.version,
            )
        return self._load()

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
