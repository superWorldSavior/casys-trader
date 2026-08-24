from __future__ import annotations

import ast
import inspect
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import get_args, get_origin, get_type_hints

import pytest

from tests.package_layout._helpers import REPO_ROOT
from trader.domain.world_availability import (
    AvailabilityEvidence,
    PersistedWorldRef,
    PointInTimeEligibilityPolicy,
    WorldAvailabilityReceipt,
    WorldAvailabilitySubjectRef,
    WorldStorageLocator,
    _attest_verified_store_receipt,
    _iso,
    _receipt_hash_payload,
    _receipt_id_for,
    _receipt_identity_payload,
)
from trader.domain.world_episode import canonical_sha256
from trader.domain.world_macro import (
    MACRO_FEATURE_KEYS,
    MACRO_PRODUCER_VERSION,
    MACRO_SOURCE_REGISTRY_VERSION,
    MACRO_TRANSFORM_VERSION,
    MACRO_WORLD_OBSERVATION_SUBJECT_KIND,
    MacroCategoryValue,
    MacroCollectionCompleted,
    MacroCollectionEvent,
    MacroCollectionEventId,
    MacroCollectionPlan,
    MacroCollectionRegistered,
    MacroCollectionRunId,
    MacroCollectionStarted,
    MacroCollectionTarget,
    MacroCoverage,
    MacroDerivationPolicy,
    MacroDimensionState,
    MacroFactSource,
    MacroNumericValue,
    MacroObservationEnvelope,
    MacroObservationId,
    MacroObservationPublished,
    MacroScope,
    MacroSourceCompleted,
    MacroSourceFailed,
    MacroSourceFact,
    MacroSourceFactVersionId,
    MacroSourceRegistry,
    MacroSourceRegistryEntry,
    MacroWorldObservation,
)


UTC = timezone.utc
CUTOFF = datetime(2026, 8, 23, 13, 0, tzinfo=UTC)
VALID_UNTIL = datetime(2026, 8, 23, 17, 0, tzinfo=UTC)
READY = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)
STORE_ID = "world-macro-jsonl.v1"
_PORTS_PATH = REPO_ROOT / "trader" / "application" / "world_model" / "macro_ports.py"
_PIPELINE_PATH = REPO_ROOT / "trader" / "application" / "world_model" / "macro_pipeline.py"


def _scope(*, kind: str = "country", entity_id: str = "iso-3166:US") -> MacroScope:
    return MacroScope(kind=kind, entity_id=entity_id)


def _source(**overrides: object) -> MacroFactSource:
    values: dict[str, object] = {
        "provider_id": "official_provider",
        "adapter_version": "official_provider.v1",
        "source_record_id": "stable-provider-id",
        "source_ref": "https://source.example/record",
    }
    values.update(overrides)
    return MacroFactSource(**values)  # type: ignore[arg-type]


def _fact(**overrides: object) -> MacroSourceFact:
    values: dict[str, object] = {
        "fact_kind": "series_point",
        "metric_key": "policy_rate",
        "scope": _scope(kind="country", entity_id="iso-3166:US"),
        "value": MacroNumericValue(number=4.25, unit="percent"),
        "period": "2026-08",
        "occurred_at": "2026-08-23T00:00:00Z",
        "published_at": "2026-08-23T12:30:00Z",
        "ingested_at": "2026-08-23T12:31:10Z",
        "source": _source(),
        "valid_until": VALID_UNTIL,
    }
    values.update(overrides)
    return MacroSourceFact(**values)  # type: ignore[arg-type]


def _registry() -> MacroSourceRegistry:
    return MacroSourceRegistry(
        registry_version=MACRO_SOURCE_REGISTRY_VERSION,
        entries=(
            MacroSourceRegistryEntry(
                source_id="fed_policy_rate",
                provider_id="official_provider",
                provider_entity_id="FED/H15",
                canonical_scope=_scope(kind="country", entity_id="iso-3166:US"),
                adapter_version="official_provider.v1",
                fact_kind="series_point",
                metric_key="policy_rate",
            ),
            MacroSourceRegistryEntry(
                source_id="brent",
                provider_id="market_benchmark",
                provider_entity_id="BRN",
                canonical_scope=_scope(kind="world", entity_id="market"),
                adapter_version="commodities.v1",
                fact_kind="market_benchmark",
                metric_key="brent",
            ),
            MacroSourceRegistryEntry(
                source_id="broad_usd_index",
                provider_id="official_provider",
                provider_entity_id="USD/BROAD",
                canonical_scope=_scope(kind="world", entity_id="market"),
                adapter_version="official_provider.v1",
                fact_kind="series_point",
                metric_key="usd_index",
            ),
        ),
    )


def _policy(*, transform_version: str = MACRO_TRANSFORM_VERSION) -> MacroDerivationPolicy:
    return MacroDerivationPolicy(transform_version=transform_version, producer_version=MACRO_PRODUCER_VERSION)


def _attested_receipt(
    *,
    subject_kind: str,
    subject_id: str,
    content_sha256: str,
    scope: str,
    ready: datetime = READY,
) -> WorldAvailabilityReceipt:
    subject = WorldAvailabilitySubjectRef(kind=subject_kind, subject_id=subject_id, content_sha256=content_sha256)
    locator = WorldStorageLocator(kind="jsonl", store_id=STORE_ID, path="macro/test.jsonl")
    identity = _receipt_identity_payload(
        schema_version="availability_receipt.v2",
        subject=subject,
        scope=scope,
        storage_locator=locator,
    )
    receipt_id = _receipt_id_for(identity)
    digest = canonical_sha256(_receipt_hash_payload(identity, receipt_id=receipt_id, ready_at=ready))
    receipt = WorldAvailabilityReceipt.from_mapping(
        {
            **identity,
            "receipt_id": receipt_id,
            "ready_at": _iso(ready),
            "receipt_sha256": digest,
        }
    )
    return _attest_verified_store_receipt(
        receipt,
        expected_subject=receipt.subject,
        expected_scope=receipt.scope,
        expected_locator=receipt.storage_locator,
    )


def _persist(identity: object, *, subject_kind: str, subject_id: str, content_sha256: str, scope: str, ready: datetime = READY):
    receipt = _attested_receipt(
        subject_kind=subject_kind,
        subject_id=subject_id,
        content_sha256=content_sha256,
        scope=scope,
        ready=ready,
    )
    return PersistedWorldRef(identity=identity, receipt=receipt)


class _Source:
    def __init__(self, facts: Sequence[MacroSourceFact] = (), error: BaseException | None = None) -> None:
        self.facts = tuple(facts)
        self.error = error
        self.calls: list[tuple[MacroScope, datetime]] = []

    def read_facts(self, scope: MacroScope, observed_at: datetime) -> tuple[MacroSourceFact, ...]:
        self.calls.append((scope, observed_at))
        if self.error is not None:
            raise self.error
        return self.facts


class _History:
    def __init__(self, *, fact_ready: datetime = READY, observation_ready: datetime = READY) -> None:
        self.fact_ready = fact_ready
        self.observation_ready = observation_ready
        self.facts: list[MacroSourceFact] = []
        self.observations: list[MacroWorldObservation] = []
        self._fact_refs: dict[str, PersistedWorldRef[MacroSourceFactVersionId]] = {}
        self._envelopes: dict[str, MacroObservationEnvelope] = {}

    def append_fact(self, fact: MacroSourceFact) -> PersistedWorldRef[MacroSourceFactVersionId]:
        key = fact.fact_version_id.value
        existing = self._fact_refs.get(key)
        if existing is not None:
            return existing
        self.facts.append(fact)
        persisted = _persist(
            fact.fact_version_id,
            subject_kind="macro_source_fact",
            subject_id=key,
            content_sha256=fact.content_sha256,
            scope=f"{fact.scope.kind}:{fact.scope.entity_id}",
            ready=self.fact_ready,
        )
        self._fact_refs[key] = persisted
        return persisted

    def append_observation(self, observation: MacroWorldObservation) -> MacroObservationEnvelope:
        existing = self._envelopes.get(observation.observation_id)
        if existing is not None:
            if existing.observation.content_sha256 != observation.content_sha256:
                raise ValueError("conflict: same observation id with different content")
            return existing
        self.observations.append(observation)
        persisted = _persist(
            MacroObservationId(observation.observation_id),
            subject_kind=MACRO_WORLD_OBSERVATION_SUBJECT_KIND,
            subject_id=observation.observation_id,
            content_sha256=observation.content_sha256,
            scope=f"{observation.scope.kind}:{observation.scope.entity_id}",
            ready=self.observation_ready,
        )
        envelope = MacroObservationEnvelope(
            observation=observation,
            persisted=persisted,
            evidence=AvailabilityEvidence(receipt=persisted.receipt, first_seen_at=self.observation_ready),
        )
        self._envelopes[observation.observation_id] = envelope
        return envelope


class _Ledger:
    def __init__(self) -> None:
        self._events: dict[str, list[MacroCollectionEvent]] = {}

    def append_event(self, event: MacroCollectionEvent) -> PersistedWorldRef[MacroCollectionEventId]:
        self._events.setdefault(event.run_id, []).append(event)
        return _persist(
            MacroCollectionEventId(event.event_id),
            subject_kind="macro_collection_event",
            subject_id=event.event_id,
            content_sha256=canonical_sha256(event.to_dict()),
            scope=event.run_id,
        )

    def load(self, run_id: MacroCollectionRunId) -> tuple[MacroCollectionEvent, ...]:
        return tuple(self._events.get(run_id.value, ()))


class _Reader:
    def __init__(self, items: Sequence[MacroObservationEnvelope] = ()) -> None:
        self.items = tuple(items)
        self.calls: list[tuple[MacroScope, datetime]] = []

    def list_candidates_available_through(
        self, scope: MacroScope, cutoff_at: datetime
    ) -> tuple[MacroObservationEnvelope, ...]:
        self.calls.append((scope, cutoff_at))
        return tuple(item for item in self.items if item.observation.scope == scope)


def _pipeline(
    *,
    sources: Mapping[str, _Source] | None = None,
    history: _History | None = None,
    ledger: _Ledger | None = None,
    reader: _Reader | None = None,
    policy: MacroDerivationPolicy | None = None,
    eligibility: PointInTimeEligibilityPolicy | None = None,
    project=None,
    source_timeouts=None,
):
    from trader.application.world_model.macro_pipeline import MacroWorldPipeline

    values = {
        "history": history if history is not None else _History(),
        "ledger": ledger if ledger is not None else _Ledger(),
        "reader": reader if reader is not None else _Reader(),
        "policy": policy if policy is not None else _policy(),
        "eligibility": eligibility if eligibility is not None else PointInTimeEligibilityPolicy(),
    }
    if project is not None:
        values["project"] = project
    if source_timeouts is not None:
        values["source_timeouts"] = source_timeouts
    return MacroWorldPipeline(**values)


def _plan(registry: MacroSourceRegistry | None = None) -> MacroCollectionPlan:
    return MacroCollectionPlan.from_registry(registry if registry is not None else _registry())


def _collect(
    pipeline,
    *,
    sources: Mapping[str, _Source],
    registry: MacroSourceRegistry | None = None,
    target: MacroCollectionTarget | None = None,
    cutoff_at: datetime = CUTOFF,
):
    resolved_registry = registry if registry is not None else _registry()
    resolved_target = target if target is not None else _plan(resolved_registry).target_for(_scope())
    return pipeline.collect(
        target=resolved_target,
        cutoff_at=cutoff_at,
        registry=resolved_registry,
        sources=sources,
        observed_at=cutoff_at,
    )


def _fed_fact(**overrides: object) -> MacroSourceFact:
    return _fact(**overrides)


def _brent_fact() -> MacroSourceFact:
    return _fact(
        fact_kind="market_benchmark",
        metric_key="brent",
        scope=_scope(kind="world", entity_id="market"),
        value=MacroNumericValue(number=80.0, unit="usd_per_barrel"),
        period="2026-08-22",
        source=_source(provider_id="market_benchmark", adapter_version="commodities.v1", source_record_id="BRN"),
    )


def _usd_fact() -> MacroSourceFact:
    return _fact(
        metric_key="usd_index",
        scope=_scope(kind="world", entity_id="market"),
        value=MacroNumericValue(number=120.0, unit="index"),
        period="2026-08-22",
        source=_source(source_record_id="USD-BROAD"),
    )


def _rates_category_fact() -> MacroSourceFact:
    return _fact(
        metric_key="rates_regime",
        value=MacroCategoryValue(category="stable"),
        source=_source(source_record_id="rates-regime-stable"),
    )


def test_rfc_7_1_ports_are_consumer_owned_typed_contracts() -> None:
    from trader.application.world_model.macro_ports import (
        MacroCollectionLedger,
        MacroHistory,
        MacroSourcePort,
        WorldMacroObservationReader,
    )

    source_hints = get_type_hints(MacroSourcePort.read_facts)
    assert list(inspect.signature(MacroSourcePort.read_facts).parameters) == ["self", "scope", "observed_at"]
    assert source_hints["scope"] is MacroScope
    assert source_hints["observed_at"] is datetime
    assert source_hints["return"] == tuple[MacroSourceFact, ...]

    fact_hints = get_type_hints(MacroHistory.append_fact)
    assert list(inspect.signature(MacroHistory.append_fact).parameters) == ["self", "fact"]
    assert fact_hints["fact"] is MacroSourceFact
    assert get_origin(fact_hints["return"]) is PersistedWorldRef
    assert get_args(fact_hints["return"]) == (MacroSourceFactVersionId,)

    observation_hints = get_type_hints(MacroHistory.append_observation)
    assert list(inspect.signature(MacroHistory.append_observation).parameters) == ["self", "observation"]
    assert observation_hints["observation"] is MacroWorldObservation
    assert observation_hints["return"] is MacroObservationEnvelope

    reader_hints = get_type_hints(WorldMacroObservationReader.list_candidates_available_through)
    assert list(inspect.signature(WorldMacroObservationReader.list_candidates_available_through).parameters) == [
        "self",
        "scope",
        "cutoff_at",
    ]
    assert reader_hints["scope"] is MacroScope
    assert reader_hints["cutoff_at"] is datetime
    assert reader_hints["return"] == tuple[MacroObservationEnvelope, ...]

    append_hints = get_type_hints(MacroCollectionLedger.append_event)
    assert list(inspect.signature(MacroCollectionLedger.append_event).parameters) == ["self", "event"]
    assert append_hints["event"] == MacroCollectionEvent
    assert get_origin(append_hints["return"]) is PersistedWorldRef
    assert get_args(append_hints["return"]) == (MacroCollectionEventId,)

    load_hints = get_type_hints(MacroCollectionLedger.load)
    assert list(inspect.signature(MacroCollectionLedger.load).parameters) == ["self", "run_id"]
    assert load_hints["run_id"] is MacroCollectionRunId
    assert load_hints["return"] == tuple[MacroCollectionEvent, ...]


def test_ports_reject_caller_controlled_ready_at_and_policy_contaminated_inputs() -> None:
    from trader.application.world_model import macro_ports

    source = _PORTS_PATH.read_text(encoding="utf-8")
    assert "ready_at" not in source
    assert "NewsMacroBrief" not in source
    assert "trader.infrastructure" not in source
    assert "trader.runtime" not in source
    assert "trader.reporting" not in source
    for name in ("MacroSourcePort", "MacroHistory", "WorldMacroObservationReader", "MacroCollectionLedger"):
        cls = getattr(macro_ports, name)
        for method in cls.__dict__.values():
            if not callable(method) or method.__name__.startswith("_"):
                continue
            assert "ready_at" not in inspect.signature(method).parameters
            for parameter in inspect.signature(method).parameters.values():
                assert parameter.annotation is not dict
                assert get_origin(parameter.annotation) is not dict


def test_application_macro_modules_do_not_import_infrastructure_runtime_or_reporting() -> None:
    forbidden = ("trader.runtime", "trader.infrastructure", "trader.reporting")
    violations: list[str] = []
    for path in (_PORTS_PATH, _PIPELINE_PATH):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                if any(node.module == prefix or node.module.startswith(f"{prefix}.") for prefix in forbidden):
                    violations.append(f"{path.name}: from {node.module}")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if any(alias.name == prefix or alias.name.startswith(f"{prefix}.") for prefix in forbidden):
                        violations.append(f"{path.name}: import {alias.name}")
        text = path.read_text(encoding="utf-8")
        assert "NewsMacroBrief" not in text
        assert "ready_at" not in inspect.signature(
            getattr(__import__("trader.application.world_model.macro_pipeline", fromlist=["MacroWorldPipeline"]), "MacroWorldPipeline")
        ).parameters
    assert violations == []


def test_collect_orchestrates_run_facts_and_observation_without_inventing_regimes() -> None:
    from trader.application.world_model.macro_pipeline import MacroWorldPipeline

    world = _scope(kind="world", entity_id="market")
    fed = _Source((_fed_fact(),))
    brent = _Source((_brent_fact(),))
    usd = _Source(error=TimeoutError("gdelt timeout"))
    history = _History()
    ledger = _Ledger()
    pipeline = _pipeline(history=history, ledger=ledger)
    run = _collect(
        pipeline,
        sources={"fed_policy_rate": fed, "brent": brent, "broad_usd_index": usd},
        target=_plan().target_for(world),
    )

    assert isinstance(pipeline, MacroWorldPipeline)
    assert run.status == "completed_partial"
    assert run.expected_source_ids == ("brent", "broad_usd_index")
    assert run.terminal_result is not None
    assert run.terminal_result.failed_source_ids == ("broad_usd_index",)
    envelope = run.published_envelope
    assert envelope is not None
    observation = envelope.observation
    assert observation.scope == world
    assert observation.cutoff_at == CUTOFF
    assert observation.producer_version == MACRO_PRODUCER_VERSION
    assert observation.transform_version == MACRO_TRANSFORM_VERSION
    assert observation.source_registry_version == MACRO_SOURCE_REGISTRY_VERSION
    assert observation.fact_refs == (_brent_fact().fact_version_id.value,)
    assert observation.features["macro_regime"] == "unknown"
    assert observation.features["rates_regime"] == "unknown"
    assert observation.features["usd_regime"] == "unknown"
    assert observation.coverage.status == "partial"
    assert observation.coverage.required_sources == 2
    assert observation.coverage.fresh_sources == 1
    assert observation.coverage.missing_source_ids == ("broad_usd_index",)
    assert history.facts == [_brent_fact()]
    event_types = [type(event) for event in ledger.load(MacroCollectionRunId(run.run_id))]
    assert event_types[0] is MacroCollectionRegistered
    assert event_types[1] is MacroCollectionStarted
    assert MacroSourceCompleted in event_types
    assert MacroSourceFailed in event_types
    assert MacroObservationPublished in event_types
    assert event_types[-1] is MacroCollectionCompleted
    failed = next(event for event in run.events if isinstance(event, MacroSourceFailed))
    assert failed.source_id == "broad_usd_index"
    assert failed.reason == "timeout"
    assert fed.calls == []
    assert brent.calls == [(world, CUTOFF)]
    assert usd.calls == [(world, CUTOFF)]


def test_same_input_and_versions_yield_the_same_observation_regardless_of_order() -> None:
    fact_a = _brent_fact()
    fact_b = _usd_fact()
    world = _scope(kind="world", entity_id="market")
    target = _plan().target_for(world)
    sources_left = {
        "brent": _Source((fact_a,)),
        "broad_usd_index": _Source((fact_b,)),
    }
    sources_right = {
        "broad_usd_index": _Source((fact_b,)),
        "brent": _Source((fact_a,)),
    }
    left = _collect(_pipeline(), sources=sources_left, target=target)
    right = _collect(_pipeline(), sources=sources_right, target=target)
    left_obs = left.published_envelope.observation
    right_obs = right.published_envelope.observation
    assert left.run_id == right.run_id
    assert left_obs.observation_id == right_obs.observation_id
    assert left_obs.content_sha256 == right_obs.content_sha256
    assert left_obs.scope == world
    assert left_obs.fact_refs == tuple(sorted((fact_a.fact_version_id.value, fact_b.fact_version_id.value)))
    assert left_obs.to_dict() == right_obs.to_dict()


def test_injected_derivation_policy_versions_are_hashed_into_the_observation() -> None:
    alt = _policy(transform_version="macro_regimes.v2-test")
    sources = {
        "fed_policy_rate": _Source((_fed_fact(),)),
        "brent": _Source(),
        "broad_usd_index": _Source(),
    }
    default_run = _collect(_pipeline(), sources=sources)
    alt_run = _collect(_pipeline(policy=alt), sources=sources)
    default_obs = default_run.published_envelope.observation
    alt_obs = alt_run.published_envelope.observation
    assert default_obs.transform_version == MACRO_TRANSFORM_VERSION
    assert alt_obs.transform_version == "macro_regimes.v2-test"
    assert alt_obs.observation_id != default_obs.observation_id
    assert alt_obs.producer_version == alt.producer_version
    assert default_run.run_id != alt_run.run_id


def test_injected_projector_is_used_instead_of_use_case_thresholds() -> None:
    from trader.application.world_model.macro_pipeline import project_macro_world_observation

    fact = _fed_fact()

    def _project(**kwargs):
        observation = project_macro_world_observation(**kwargs)
        assert observation is not None
        features = {**observation.features, "macro_regime": "mixed", "rates_regime": "stable"}
        dimensions = (
            MacroDimensionState(
                dimension="macro_regime",
                value="mixed",
                coverage_status="complete",
                method=kwargs["policy"].transform_version,
                fact_refs=observation.fact_refs,
            ),
            MacroDimensionState(
                dimension="rates_regime",
                value="stable",
                coverage_status="complete",
                method=kwargs["policy"].transform_version,
                fact_refs=observation.fact_refs,
            ),
            observation.dimensions[2],
        )
        return MacroWorldObservation(
            scope=observation.scope,
            cutoff_at=observation.cutoff_at,
            fact_refs=observation.fact_refs,
            features=features,
            dimensions=dimensions,
            coverage=observation.coverage,
            producer_version=observation.producer_version,
            transform_version=observation.transform_version,
            source_registry_version=observation.source_registry_version,
            valid_until=observation.valid_until,
        )

    run = _collect(
        _pipeline(project=_project),
        sources={
            "fed_policy_rate": _Source((fact,)),
            "brent": _Source(),
            "broad_usd_index": _Source(),
        },
    )
    observation = run.published_envelope.observation
    assert observation.features["macro_regime"] == "mixed"
    assert observation.features["rates_regime"] == "stable"
    assert observation.features["usd_regime"] == "unknown"


def test_category_facts_pass_through_closed_vocabulary_without_inventing_numeric_regimes() -> None:
    from trader.application.world_model.macro_pipeline import project_macro_world_observation

    rates = _rates_category_fact()
    numeric = _fed_fact()
    observation = project_macro_world_observation(
        policy=_policy(),
        scope=_scope(),
        cutoff_at=CUTOFF,
        source_registry_version=MACRO_SOURCE_REGISTRY_VERSION,
        facts=(numeric, rates),
        expected_source_ids=("fed_policy_rate",),
        failed_source_ids=(),
        fresh_source_ids=("fed_policy_rate",),
    )
    assert observation is not None
    assert observation.features["rates_regime"] == "stable"
    assert observation.features["macro_regime"] == "unknown"
    assert observation.features["usd_regime"] == "unknown"
    rates_dim = next(item for item in observation.dimensions if item.dimension == "rates_regime")
    assert rates_dim.fact_refs == (rates.fact_version_id.value,)
    assert numeric.fact_version_id.value in observation.fact_refs
    assert "unknown" in _policy().allowed_values("usd_regime")


def test_no_admissible_facts_fails_the_run_without_a_fake_observation() -> None:
    history = _History()
    run = _collect(
        _pipeline(history=history),
        sources={
            "fed_policy_rate": _Source(error=RuntimeError("http 429 too many requests")),
            "brent": _Source(error=TimeoutError("timeout")),
            "broad_usd_index": _Source(),
        },
    )
    assert run.status == "failed"
    assert run.published_envelope is None
    assert run.terminal_result is not None
    assert run.terminal_result.observation_id is None
    assert run.terminal_result.reason == "no_admissible_observation"
    assert history.observations == []
    reasons = {
        event.source_id: event.reason
        for event in run.events
        if isinstance(event, MacroSourceFailed)
    }
    assert reasons == {"fed_policy_rate": "http_429"}


def test_unproven_or_stale_facts_are_excluded_instead_of_being_treated_as_fresh() -> None:
    stale = _fact(valid_until=datetime(2026, 8, 23, 12, 0, tzinfo=UTC))
    late_history = _History(fact_ready=datetime(2026, 8, 23, 13, 5, tzinfo=UTC))
    late_run = _collect(
        _pipeline(history=late_history),
        sources={
            "fed_policy_rate": _Source((_fed_fact(),)),
            "brent": _Source(),
            "broad_usd_index": _Source(),
        },
    )
    assert late_run.status == "failed"
    assert late_run.published_envelope is None
    assert late_history.facts == [_fed_fact()]

    stale_run = _collect(
        _pipeline(),
        sources={
            "fed_policy_rate": _Source((stale,)),
            "brent": _Source(),
            "broad_usd_index": _Source(),
        },
    )
    assert stale_run.status == "failed"
    assert stale_run.published_envelope is None


def test_missing_source_port_is_a_typed_failure_not_an_optimistic_gap_fill() -> None:
    world = _scope(kind="world", entity_id="market")
    run = _collect(
        _pipeline(),
        sources={"brent": _Source((_brent_fact(),))},
        target=_plan().target_for(world),
    )
    assert run.status == "completed_partial"
    observation = run.published_envelope.observation
    assert observation.scope == world
    assert "broad_usd_index" in observation.coverage.missing_source_ids
    assert observation.features["usd_regime"] == "unknown"
    assert observation.coverage.status != "complete"
    failed = next(event for event in run.events if isinstance(event, MacroSourceFailed))
    assert failed.source_id == "broad_usd_index"
    assert failed.reason == "missing"


def test_replay_of_the_same_run_is_idempotent_and_does_not_re_read_sources() -> None:
    fed = _Source((_fed_fact(),))
    sources = {
        "fed_policy_rate": fed,
        "brent": _Source(),
        "broad_usd_index": _Source(),
    }
    history = _History()
    ledger = _Ledger()
    pipeline = _pipeline(history=history, ledger=ledger)
    first = _collect(pipeline, sources=sources)
    second = _collect(pipeline, sources=sources)
    assert second == first
    assert second.published_envelope.observation.observation_id == first.published_envelope.observation.observation_id
    assert len(fed.calls) == 1
    assert len(history.observations) == 1
    assert len(ledger.load(MacroCollectionRunId(first.run_id))) == len(first.events)


def test_select_applies_point_in_time_policy_and_picks_the_last_admissible_observation() -> None:
    fact = _fed_fact()
    earlier_cutoff = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)
    later_cutoff = datetime(2026, 8, 23, 12, 30, tzinfo=UTC)

    def _observation_at(cutoff: datetime, *, transform_version: str = MACRO_TRANSFORM_VERSION) -> MacroWorldObservation:
        refs = (fact.fact_version_id.value,)
        return MacroWorldObservation(
            scope=_scope(),
            cutoff_at=cutoff,
            fact_refs=refs,
            features={"macro_regime": "unknown", "rates_regime": "unknown", "usd_regime": "unknown"},
            dimensions=tuple(
                MacroDimensionState(
                    dimension=name,
                    value="unknown",
                    coverage_status="unknown",
                    method=transform_version,
                    fact_refs=(),
                )
                for name in MACRO_FEATURE_KEYS
            ),
            coverage=MacroCoverage(status="partial", required_sources=3, fresh_sources=1, missing_source_ids=("brent", "broad_usd_index")),
            producer_version=MACRO_PRODUCER_VERSION,
            transform_version=transform_version,
            source_registry_version=MACRO_SOURCE_REGISTRY_VERSION,
            valid_until=VALID_UNTIL,
        )

    def _envelope(observation: MacroWorldObservation, *, ready: datetime, first_seen: datetime) -> MacroObservationEnvelope:
        persisted = _persist(
            MacroObservationId(observation.observation_id),
            subject_kind=MACRO_WORLD_OBSERVATION_SUBJECT_KIND,
            subject_id=observation.observation_id,
            content_sha256=observation.content_sha256,
            scope=f"{observation.scope.kind}:{observation.scope.entity_id}",
            ready=ready,
        )
        return MacroObservationEnvelope(
            observation=observation,
            persisted=persisted,
            evidence=AvailabilityEvidence(receipt=persisted.receipt, first_seen_at=first_seen),
        )

    earlier = _envelope(_observation_at(earlier_cutoff), ready=READY, first_seen=READY)
    later = _envelope(_observation_at(later_cutoff), ready=READY, first_seen=READY)
    unproven = _envelope(
        _observation_at(later_cutoff, transform_version="macro_regimes.unproven"),
        ready=READY,
        first_seen=datetime(2026, 8, 23, 13, 5, tzinfo=UTC),
    )
    stale = _envelope(
        MacroWorldObservation(
            scope=_scope(),
            cutoff_at=earlier_cutoff,
            fact_refs=(fact.fact_version_id.value,),
            features={"macro_regime": "unknown", "rates_regime": "unknown", "usd_regime": "unknown"},
            dimensions=tuple(
                MacroDimensionState(
                    dimension=name,
                    value="unknown",
                    coverage_status="unknown",
                    method="macro_regimes.stale",
                    fact_refs=(),
                )
                for name in MACRO_FEATURE_KEYS
            ),
            coverage=MacroCoverage(
                status="partial",
                required_sources=3,
                fresh_sources=1,
                missing_source_ids=("brent", "broad_usd_index"),
            ),
            producer_version=MACRO_PRODUCER_VERSION,
            transform_version="macro_regimes.stale",
            source_registry_version=MACRO_SOURCE_REGISTRY_VERSION,
            valid_until=datetime(2026, 8, 23, 13, 0, tzinfo=UTC),
        ),
        ready=READY,
        first_seen=READY,
    )
    reader = _Reader((stale, unproven, later, earlier))
    pipeline = _pipeline(reader=reader)
    selected = pipeline.select(scope=_scope(), cutoff_at=CUTOFF)
    assert selected is not None
    assert selected.observation.observation_id == later.observation.observation_id
    assert selected.observation.cutoff_at == later_cutoff
    assert reader.calls == [(_scope(), CUTOFF)]

    missing = _pipeline(reader=_Reader()).select(scope=_scope(), cutoff_at=CUTOFF)
    assert missing is None


def test_select_does_not_treat_reader_order_or_unmapped_scope_as_latest() -> None:
    fact = _fed_fact()
    refs = (fact.fact_version_id.value,)
    observation = MacroWorldObservation(
        scope=_scope(),
        cutoff_at=datetime(2026, 8, 23, 12, 0, tzinfo=UTC),
        fact_refs=refs,
        features={"macro_regime": "unknown", "rates_regime": "unknown", "usd_regime": "unknown"},
        dimensions=tuple(
            MacroDimensionState(
                dimension=name,
                value="unknown",
                coverage_status="unknown",
                method=MACRO_TRANSFORM_VERSION,
                fact_refs=(),
            )
            for name in MACRO_FEATURE_KEYS
        ),
        coverage=MacroCoverage(status="partial", required_sources=3, fresh_sources=1, missing_source_ids=("brent", "broad_usd_index")),
        valid_until=VALID_UNTIL,
    )
    persisted = _persist(
        MacroObservationId(observation.observation_id),
        subject_kind=MACRO_WORLD_OBSERVATION_SUBJECT_KIND,
        subject_id=observation.observation_id,
        content_sha256=observation.content_sha256,
        scope=f"{observation.scope.kind}:{observation.scope.entity_id}",
    )
    envelope = MacroObservationEnvelope(
        observation=observation,
        persisted=persisted,
        evidence=AvailabilityEvidence(receipt=persisted.receipt, first_seen_at=READY),
    )
    other_scope = _pipeline(reader=_Reader((envelope,))).select(
        scope=_scope(kind="venue", entity_id="mic:XTAI"),
        cutoff_at=CUTOFF,
    )
    assert other_scope is None
    admitted = PointInTimeEligibilityPolicy(admitted_versions=frozenset({"macro_regimes.other"}))
    rejected = _pipeline(reader=_Reader((envelope,)), eligibility=admitted).select(scope=_scope(), cutoff_at=CUTOFF)
    assert rejected is None


def test_pipeline_public_constructors_do_not_accept_ready_at() -> None:
    from trader.application.world_model.macro_pipeline import MacroWorldPipeline, project_macro_world_observation

    assert "ready_at" not in inspect.signature(MacroWorldPipeline).parameters
    assert "ready_at" not in inspect.signature(MacroWorldPipeline.collect).parameters
    assert "ready_at" not in inspect.signature(MacroWorldPipeline.select).parameters
    assert "ready_at" not in inspect.signature(project_macro_world_observation).parameters
    assert list(inspect.signature(MacroWorldPipeline.collect).parameters)[0] == "self"
    assert "target" in inspect.signature(MacroWorldPipeline.collect).parameters
    assert "scope" not in inspect.signature(MacroWorldPipeline.collect).parameters


def test_projector_refuses_foreign_scope_facts_and_does_not_stamp_a_mixed_observation() -> None:
    from trader.application.world_model.macro_pipeline import project_macro_world_observation

    xtai = _scope(kind="venue", entity_id="mic:XTAI")
    us_fact = _fed_fact()
    world_fact = _brent_fact()
    mixed = project_macro_world_observation(
        policy=_policy(),
        scope=xtai,
        cutoff_at=CUTOFF,
        source_registry_version=MACRO_SOURCE_REGISTRY_VERSION,
        facts=(us_fact, world_fact),
        expected_source_ids=("fed_policy_rate", "brent", "broad_usd_index"),
        failed_source_ids=(),
        fresh_source_ids=("fed_policy_rate", "brent"),
    )
    foreign_only = project_macro_world_observation(
        policy=_policy(),
        scope=xtai,
        cutoff_at=CUTOFF,
        source_registry_version=MACRO_SOURCE_REGISTRY_VERSION,
        facts=(us_fact,),
        expected_source_ids=("fed_policy_rate",),
        failed_source_ids=(),
        fresh_source_ids=("fed_policy_rate",),
    )
    assert mixed is None
    assert foreign_only is None
    us_scope = _scope(kind="country", entity_id="iso-3166:US")
    admitted = project_macro_world_observation(
        policy=_policy(),
        scope=us_scope,
        cutoff_at=CUTOFF,
        source_registry_version=MACRO_SOURCE_REGISTRY_VERSION,
        facts=(us_fact,),
        expected_source_ids=("fed_policy_rate",),
        failed_source_ids=(),
        fresh_source_ids=("fed_policy_rate",),
    )
    assert admitted is not None
    assert admitted.scope == us_scope
    assert admitted.fact_refs == (us_fact.fact_version_id.value,)
    refused_mixed_us = project_macro_world_observation(
        policy=_policy(),
        scope=us_scope,
        cutoff_at=CUTOFF,
        source_registry_version=MACRO_SOURCE_REGISTRY_VERSION,
        facts=(us_fact, world_fact),
        expected_source_ids=("fed_policy_rate", "brent"),
        failed_source_ids=(),
        fresh_source_ids=("fed_policy_rate", "brent"),
    )
    assert refused_mixed_us is None


def test_collect_binds_exact_target_source_ids_not_the_whole_registry() -> None:
    registry = _registry()
    plan = _plan(registry)
    us = _scope(kind="country", entity_id="iso-3166:US")
    world = _scope(kind="world", entity_id="market")
    fed = _Source((_fed_fact(),))
    brent = _Source((_brent_fact(),))
    usd = _Source()
    sources = {"fed_policy_rate": fed, "brent": brent, "broad_usd_index": usd}
    us_run = _collect(_pipeline(), sources=sources, registry=registry, target=plan.target_for(us))
    world_run = _collect(_pipeline(), sources=sources, registry=registry, target=plan.target_for(world))
    assert us_run.expected_source_ids == ("fed_policy_rate",)
    assert world_run.expected_source_ids == ("brent", "broad_usd_index")
    assert us_run.scope == us
    assert world_run.scope == world
    assert us_run.published_envelope is not None
    assert us_run.published_envelope.observation.scope == us
    assert us_run.published_envelope.observation.fact_refs == (_fed_fact().fact_version_id.value,)
    assert fed.calls == [(us, CUTOFF)]
    assert brent.calls == [(world, CUTOFF)]
    assert usd.calls == [(world, CUTOFF)]


def test_collect_rejects_unsourced_or_mismatched_target_before_registering() -> None:
    history = _History()
    ledger = _Ledger()
    pipeline = _pipeline(history=history, ledger=ledger)
    registry = _registry()
    xtai = MacroCollectionTarget(
        scope=_scope(kind="venue", entity_id="mic:XTAI"),
        source_ids=("fed_policy_rate",),
    )
    mixed = MacroCollectionTarget(
        scope=_scope(kind="country", entity_id="iso-3166:US"),
        source_ids=("fed_policy_rate", "brent", "broad_usd_index"),
    )
    sources = {"fed_policy_rate": _Source((_fed_fact(),)), "brent": _Source((_brent_fact(),)), "broad_usd_index": _Source()}
    with pytest.raises(ValueError, match="collection target"):
        pipeline.collect(target=xtai, cutoff_at=CUTOFF, registry=registry, sources=sources)
    with pytest.raises(ValueError, match="bound registry plan"):
        pipeline.collect(target=mixed, cutoff_at=CUTOFF, registry=registry, sources=sources)
    assert history.facts == []
    assert history.observations == []
    assert ledger._events == {}


def test_foreign_scope_or_provenance_mismatch_is_typed_failure_not_a_mixed_observation() -> None:
    registry = _registry()
    us_target = _plan(registry).target_for(_scope(kind="country", entity_id="iso-3166:US"))
    history = _History()
    foreign_run = _collect(
        _pipeline(history=history),
        sources={"fed_policy_rate": _Source((_brent_fact(),))},
        registry=registry,
        target=us_target,
    )
    assert foreign_run.status == "failed"
    assert foreign_run.published_envelope is None
    assert history.observations == []
    assert history.facts == []
    failed = next(event for event in foreign_run.events if isinstance(event, MacroSourceFailed))
    assert failed.source_id == "fed_policy_rate"
    assert failed.reason == "scope_mismatch"

    poisoned = _fed_fact(source=_source(provider_id="other_provider"))
    provenance_history = _History()
    provenance_run = _collect(
        _pipeline(history=provenance_history),
        sources={"fed_policy_rate": _Source((poisoned,))},
        registry=registry,
        target=us_target,
    )
    assert provenance_run.status == "failed"
    assert provenance_run.published_envelope is None
    assert provenance_history.facts == []
    assert provenance_history.observations == []
    provenance_failed = next(event for event in provenance_run.events if isinstance(event, MacroSourceFailed))
    assert provenance_failed.reason == "provenance_mismatch"

    mixed_history = _History()
    mixed_run = _collect(
        _pipeline(history=mixed_history),
        sources={"fed_policy_rate": _Source((_fed_fact(), _brent_fact()))},
        registry=registry,
        target=us_target,
    )
    assert mixed_run.status == "failed"
    assert mixed_run.published_envelope is None
    assert mixed_history.facts == []
    assert mixed_history.observations == []
    mixed_failed = next(event for event in mixed_run.events if isinstance(event, MacroSourceFailed))
    assert mixed_failed.reason == "scope_mismatch"


def test_hanging_source_is_target_local_timeout_and_does_not_block_sibling_sources() -> None:
    import threading

    hang = threading.Event()

    class _Hang:
        def __init__(self) -> None:
            self.calls = 0

        def read_facts(self, scope, observed_at):
            del scope, observed_at
            self.calls += 1
            hang.wait()
            raise AssertionError("hanging source resumed")

    registry = _registry()
    us_target = _plan(registry).target_for(_scope(kind="country", entity_id="iso-3166:US"))
    world_target = _plan(registry).target_for(_scope(kind="world", entity_id="market"))
    hanging = _Hang()
    brent = _Source((_brent_fact(),))
    usd = _Source()
    pipeline = _pipeline(source_timeouts={"fed_policy_rate": 0.2, "brent": 0.2, "broad_usd_index": 0.2})
    done = threading.Event()
    runs: list[object] = []

    def _run() -> None:
        try:
            us_run = pipeline.collect(
                target=us_target,
                cutoff_at=CUTOFF,
                registry=registry,
                sources={"fed_policy_rate": hanging},
                observed_at=CUTOFF,
            )
            world_run = pipeline.collect(
                target=world_target,
                cutoff_at=CUTOFF,
                registry=registry,
                sources={"brent": brent, "broad_usd_index": usd},
                observed_at=CUTOFF,
            )
            runs.extend((us_run, world_run))
        finally:
            done.set()

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    try:
        assert done.wait(2.0)
        assert len(runs) == 2
        us_run, world_run = runs
        assert us_run.status == "failed"
        failed = next(event for event in us_run.events if isinstance(event, MacroSourceFailed))
        assert failed.source_id == "fed_policy_rate"
        assert failed.reason == "timeout"
        assert world_run.expected_source_ids == ("brent", "broad_usd_index")
        assert brent.calls == [(world_target.scope, CUTOFF)]
        assert usd.calls == [(world_target.scope, CUTOFF)]
        from trader.application.world_model import macro_pipeline as pipeline_mod
        from trader.application.world_model.source_deadline import BoundedSourceDeadline, source_only_deadline

        deadline = source_only_deadline()
        assert isinstance(deadline, BoundedSourceDeadline)
        assert deadline.max_workers >= 7
        bounded_src = inspect.getsource(pipeline_mod._read_facts_bounded)
        assert "source_only_deadline" in bounded_src
        assert "threading.Thread(" not in bounded_src
        owner_src = inspect.getsource(BoundedSourceDeadline)
        assert "ThreadPoolExecutor" not in owner_src
        assert "BoundedSemaphore" in owner_src
    finally:
        hang.set()
