"""Prospective macro→graph knowledge bridge. Runs outside episode capture."""

from __future__ import annotations

from trader.application.world_model.graph_ports import (
    BridgeRequestId,
    MacroGraphBridgeKey,
    MacroGraphBridgeLedger,
    MacroObservationScanPort,
    WorldGraphLedger,
)
from trader.domain.world_episode import canonical_sha256
from trader.domain.world_graph import (
    MACRO_GRAPH_BRIDGE_RUN_SPEC_SCHEMA,
    KnowledgeWorldRelationAsserted,
    MacroGraphBridgeFence,
    MacroGraphBridgeRegistry,
    MacroGraphBridgeRun,
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
    same_bridge_schema_family,
)
from trader.domain.world_feature_contract import WORLD_SCOPE_MAPPING_ID
from trader.domain.world_ontology_lifecycle import admits_market_ontology_family
from trader.domain.world_macro import MACRO_PRODUCER_VERSION, MacroCollectionPlan
from trader.domain.world_scope import WorldScopeMapping


class RegisterMacroObservationKnowledge:
    """Align and reconcile a single-active macro graph bridge."""

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
            schema_version=MACRO_GRAPH_BRIDGE_RUN_SPEC_SCHEMA,
            collection_plan_id=self._collection_plan.plan_id,
            collection_plan_hash=self._collection_plan.content_sha256,
            producer_version=MACRO_PRODUCER_VERSION,
        )

    def _load(self) -> MacroGraphBridgeRegistry:
        return self._bridge.load(self._key())

    def _append(
        self, registry: MacroGraphBridgeRegistry, fence: MacroGraphBridgeFence | None
    ) -> MacroGraphBridgeRegistry:
        event = registry.events[-1]
        self._bridge.append_event(event, expected_registry_version=registry.version - 1, fence=fence)
        return self._load()

    def align(self, request_id: str) -> MacroGraphBridgeRegistry:
        """Pre-collection lifecycle alignment. No observation scan and no provider I/O."""

        return self.ensure(request_id)

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
            return self._append(updated, fence=registry.fence)
        if decision.status == "drifted_active" and decision.run is not None:
            updated = registry.block(reason="config_drift", expected_version=registry.version)
            registry = self._append(updated, fence=registry.fence)
            if same_bridge_schema_family(decision.run.spec, desired):
                return self.activate(request_id)
        if decision.status == "roll_generation":
            return self.activate(request_id)
        raise UnknownMacroGraphBridgeDrift(
            "unknown_config_drift",
            context={
                "durable_spec": None if registry.active_run is None else registry.active_run.spec.to_dict(),
                "desired_spec": desired.to_dict(),
            },
        )

    def activate(self, request_id: str) -> MacroGraphBridgeRegistry:
        registry = self._load()
        desired = self._spec()
        predecessor = registry.active_run
        # A retry of this generation uses its original predecessor. A return
        # A -> B -> A instead names B, so the old A reservation is never reused.
        if predecessor is not None and predecessor.spec == desired:
            predecessor = next((run for run in registry.runs if run.epoch == predecessor.epoch - 1), None)
        reservation = self._scan.reserve_activation_cursor(
            self._key(), BridgeRequestId(_activation_request_id(request_id, desired, predecessor))
        )
        updated = registry.activate(reservation=reservation, spec=desired, expected_version=registry.version)
        if updated.version == registry.version:
            return updated
        return self._append(updated, fence=None)

    def activate_explicit_migration(
        self,
        *,
        expected_predecessor_run_id: str,
        expected_predecessor_epoch: int,
        expected_desired_spec: MacroGraphBridgeRunSpec,
    ) -> MacroGraphBridgeRegistry:
        """Start one explicitly approved, prospective generation after unknown drift.

        Automatic ``ensure`` remains fail-closed for this case.  The caller must
        name the blocked predecessor and the complete desired spec; the cursor
        reservation is derived from both generations so it cannot reuse an
        alignment reservation from another run.
        """

        if not isinstance(expected_desired_spec, MacroGraphBridgeRunSpec):
            raise TypeError("expected_desired_spec must be MacroGraphBridgeRunSpec")
        predecessor_run_id = str(expected_predecessor_run_id).strip()
        if not predecessor_run_id:
            raise ValueError("expected_predecessor_run_id must be non-empty")
        if isinstance(expected_predecessor_epoch, bool) or not isinstance(expected_predecessor_epoch, int):
            raise TypeError("expected_predecessor_epoch must be an int")
        if expected_predecessor_epoch < 1:
            raise ValueError("expected_predecessor_epoch must be positive")
        desired = self._spec()
        if desired != expected_desired_spec:
            raise ValueError("expected_desired_spec does not match the configured bridge spec")
        registry = self._load()
        run = registry.active_run
        if (
            run is not None
            and run.status == "active"
            and run.spec == desired
            and run.epoch == expected_predecessor_epoch + 1
            and any(
                item.run_id == predecessor_run_id and item.epoch == expected_predecessor_epoch
                for item in registry.runs
            )
        ):
            return registry
        if run is None or run.run_id != predecessor_run_id or run.epoch != expected_predecessor_epoch:
            raise ValueError("active bridge generation does not match the expected predecessor")
        if run.status != "blocked":
            raise ValueError("expected predecessor must be blocked")
        if not _is_collection_plan_migration(run.spec, desired):
            raise ValueError("explicit migration only permits a collection-plan change")
        decision = classify_macro_graph_bridge(registry, desired=desired)
        if decision.status != "unknown_drift":
            raise ValueError("explicit migration is only valid for unknown bridge drift")
        request_id = explicit_migration_request_id(
            bridge_key=self._bridge_key,
            predecessor_run_id=predecessor_run_id,
            predecessor_epoch=expected_predecessor_epoch,
            desired_spec=desired,
        )
        reservation = self._scan.reserve_activation_cursor(self._key(), BridgeRequestId(request_id))
        updated = registry.activate(reservation=reservation, spec=desired, expected_version=registry.version)
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


def explicit_migration_request_id(
    *,
    bridge_key: str,
    predecessor_run_id: str,
    predecessor_epoch: int,
    desired_spec: MacroGraphBridgeRunSpec,
) -> str:
    """Stable retry token scoped to exactly one predecessor → desired migration."""

    if not isinstance(desired_spec, MacroGraphBridgeRunSpec):
        raise TypeError("desired_spec must be MacroGraphBridgeRunSpec")
    key = str(bridge_key).strip()
    run_id = str(predecessor_run_id).strip()
    if not key or not run_id:
        raise ValueError("bridge_key and predecessor_run_id must be non-empty")
    if isinstance(predecessor_epoch, bool) or not isinstance(predecessor_epoch, int) or predecessor_epoch < 1:
        raise ValueError("predecessor_epoch must be a positive int")
    return "macro_graph_bridge_request:v1:" + canonical_sha256(
        {
            "bridge_key": key,
            "predecessor_run_id": run_id,
            "predecessor_epoch": predecessor_epoch,
            "desired_spec": desired_spec.to_dict(),
        }
    )


def _activation_request_id(
    request_id: str, desired_spec: MacroGraphBridgeRunSpec, predecessor: MacroGraphBridgeRun | None
) -> str:
    """Scope an operator's stable activation intent to the desired generation."""

    supplied = str(request_id).strip()
    if not supplied:
        raise ValueError("request_id must be non-empty")
    if not isinstance(desired_spec, MacroGraphBridgeRunSpec):
        raise TypeError("desired_spec must be MacroGraphBridgeRunSpec")
    return "macro_graph_bridge_request:v1:" + canonical_sha256(
        {
            "activation_request_id": supplied,
            "desired_spec": desired_spec.to_dict(),
            "predecessor_run_id": None if predecessor is None else predecessor.run_id,
            "predecessor_epoch": None if predecessor is None else predecessor.epoch,
        }
    )


def _is_collection_plan_migration(
    predecessor: MacroGraphBridgeRunSpec,
    desired: MacroGraphBridgeRunSpec,
) -> bool:
    """Allow a plan change within the same producer, schema, and ontology family."""

    return (
        predecessor.schema_version == desired.schema_version
        and predecessor.scope_mapping_id == desired.scope_mapping_id == WORLD_SCOPE_MAPPING_ID
        and admits_market_ontology_family(predecessor.ontology_revision_id)
        and admits_market_ontology_family(desired.ontology_revision_id)
        and predecessor.producer_version == desired.producer_version
        and (
            predecessor.collection_plan_id != desired.collection_plan_id
            or predecessor.collection_plan_hash != desired.collection_plan_hash
        )
    )
