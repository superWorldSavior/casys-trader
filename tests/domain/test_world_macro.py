from __future__ import annotations

import ast
import inspect
import sys
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone

import pytest

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
from tests.package_layout._helpers import REPO_ROOT, _domain_import_violations
from trader.domain.world_macro import (
    MACRO_COLLECTION_PLAN_SCHEMA,
    MACRO_FACT_KINDS,
    MACRO_LANE_IDENTITY,
    MACRO_LANE_IDENTITY_V1,
    MACRO_POLICY_DENYLIST,
    MACRO_PRODUCER_VERSION,
    MACRO_PRODUCER_VERSION_V1,
    MACRO_REGIME_VALUES,
    MACRO_SOURCE_REGISTRY_VERSION,
    MACRO_TRANSFORM_VERSION,
    WORLD_MACRO_COLLECTION_PLAN_ID,
    WORLD_MACRO_COLLECTION_PLAN_SHA256,
    MacroObservationProvenance,
    committed_macro_collection_plan,
    derive_macro_source_fact_valid_until,
    require_committed_macro_collection_plan,
    MacroCategoryValue,
    MacroCollectionCompleted,
    MacroCollectionEvent,
    MacroCollectionPlan,
    MacroCollectionRegistered,
    MacroCollectionTarget,
    MacroCollectionRun,
    MacroCollectionStarted,
    MacroCollectionTerminalResult,
    MacroContextSearchPlan,
    MacroContextSelection,
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
    MacroSourceRegistry,
    MacroSourceRegistryEntry,
    MacroWorldObservation,
    WorldScopeMapping,
    WorldScopeResolution,
    assert_source_only_payload,
    is_admitted_macro_producer,
    macro_observes_producer_ref,
    parse_macro_collection_event,
    reconcile_macro_source_fact,
    reconcile_macro_world_observation,
)
from trader.domain.world_scope import WorldCanonicalScopeRef, WorldMarketAnchorRef


UTC = timezone.utc
CUTOFF = datetime(2026, 8, 23, 13, 0, tzinfo=UTC)
VALID_UNTIL = datetime(2026, 8, 23, 17, 0, tzinfo=UTC)
READY = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)
FIRST_SEEN = datetime(2026, 8, 23, 12, 5, tzinfo=UTC)
STORE_ID = "world-macro-jsonl.v1"


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
        "scope": _scope(),
        "value": MacroNumericValue(number=4.25, unit="percent"),
        "period": "2026-08",
        "occurred_at": "2026-08-23T00:00:00Z",
        "published_at": "2026-08-23T12:30:00Z",
        "ingested_at": "2026-08-23T12:31:10Z",
        "source": _source(),
    }
    values.update(overrides)
    return MacroSourceFact(**values)  # type: ignore[arg-type]


def _dimension(
    *,
    dimension: str = "rates_regime",
    value: str = "stable",
    coverage_status: str = "complete",
    method: str = MACRO_TRANSFORM_VERSION,
    fact_refs: tuple[str, ...] | None = None,
) -> MacroDimensionState:
    return MacroDimensionState(
        dimension=dimension,
        value=value,
        coverage_status=coverage_status,
        method=method,
        fact_refs=() if fact_refs is None else fact_refs,
    )


def _observation(*, fact: MacroSourceFact | None = None, **overrides: object) -> MacroWorldObservation:
    resolved = fact if fact is not None else _fact()
    fact_ref = resolved.fact_version_id.value
    values: dict[str, object] = {
        "scope": resolved.scope,
        "cutoff_at": CUTOFF,
        "producer_version": MACRO_PRODUCER_VERSION,
        "transform_version": MACRO_TRANSFORM_VERSION,
        "source_registry_version": MACRO_SOURCE_REGISTRY_VERSION,
        "fact_refs": (fact_ref,),
        "features": {"macro_regime": "mixed", "rates_regime": "stable", "usd_regime": "unknown"},
        "dimensions": (
            _dimension(dimension="macro_regime", value="mixed", fact_refs=(fact_ref,)),
            _dimension(dimension="rates_regime", value="stable", fact_refs=(fact_ref,)),
            _dimension(
                dimension="usd_regime",
                value="unknown",
                coverage_status="unknown",
                fact_refs=(),
            ),
        ),
        "coverage": MacroCoverage(
            status="partial",
            required_sources=3,
            fresh_sources=2,
            missing_source_ids=("broad_usd_index",),
        ),
        "valid_until": VALID_UNTIL,
    }
    values.update(overrides)
    return MacroWorldObservation(**values)  # type: ignore[arg-type]


def _locator() -> WorldStorageLocator:
    return WorldStorageLocator(kind="jsonl", store_id=STORE_ID, path="observations/2026-08-23.jsonl")


def _attested_receipt(
    observation: MacroWorldObservation,
    *,
    ready: datetime = READY,
    subject_kind: str = "macro_world_observation",
    scope: str | None = None,
) -> WorldAvailabilityReceipt:
    subject = WorldAvailabilitySubjectRef(
        kind=subject_kind,
        subject_id=observation.observation_id,
        content_sha256=observation.content_sha256,
    )
    locator = _locator()
    identity = _receipt_identity_payload(
        schema_version="availability_receipt.v2",
        subject=subject,
        scope=scope if scope is not None else f"{observation.scope.kind}:{observation.scope.entity_id}",
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


def _envelope(observation: MacroWorldObservation | None = None) -> MacroObservationEnvelope:
    resolved = observation if observation is not None else _observation()
    receipt = _attested_receipt(resolved)
    return MacroObservationEnvelope(
        observation=resolved,
        persisted=PersistedWorldRef(identity=MacroObservationId(resolved.observation_id), receipt=receipt),
        evidence=AvailabilityEvidence(receipt=receipt, first_seen_at=FIRST_SEEN),
    )


def _registry() -> MacroSourceRegistry:
    return MacroSourceRegistry(
        registry_version=MACRO_SOURCE_REGISTRY_VERSION,
        entries=(
            MacroSourceRegistryEntry(
                source_id="fed_policy_rate",
                provider_id="official_provider",
                provider_entity_id="FED/H15",
                canonical_scope=_scope(),
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


def _run(*, expected: tuple[str, ...] = ("fed_policy_rate", "brent", "broad_usd_index")) -> MacroCollectionRun:
    return MacroCollectionRun.register(
        scope=_scope(),
        cutoff_at=CUTOFF,
        expected_source_ids=expected,
    )


def test_macro_scope_reuses_canonical_v3_ids_and_rejects_company_family_instrument() -> None:
    world = _scope(kind="world", entity_id="market")
    assert world.canonical_ref() == WorldCanonicalScopeRef(kind="world", entity_id="market")
    assert _scope(kind="region", entity_id="iso-un-m49:030").kind == "region"
    with pytest.raises(ValueError, match="company"):
        MacroScope(kind="company", entity_id="lei:123")
    with pytest.raises(ValueError, match="family"):
        MacroScope(kind="family", entity_id="taxonomy:v1:semiconductors")
    with pytest.raises(ValueError, match="instrument"):
        MacroScope(kind="instrument", entity_id="mic:XTAI:symbol:2330")
    with pytest.raises(ValueError, match="iso-3166:"):
        MacroScope(kind="country", entity_id="TW")
    with pytest.raises(FrozenInstanceError):
        world.kind = "country"  # type: ignore[misc]


def test_source_registry_is_hashed_and_has_no_implicit_provider_crosswalk() -> None:
    registry = _registry()
    replayed = MacroSourceRegistry.from_mapping(registry.to_dict())
    assert replayed == registry
    assert replayed.content_sha256 == registry.content_sha256
    resolved = registry.resolve_canonical_scope(provider_id="official_provider", provider_entity_id="FED/H15")
    assert resolved.status == "resolved"
    assert resolved.scope == _scope()
    missing = registry.resolve_canonical_scope(provider_id="twse", provider_entity_id="TW")
    assert missing.status == "unmapped"
    assert missing.scope is None


def test_collection_plan_owns_typed_targets_bound_to_registry_identity() -> None:
    registry = _registry()
    world = _scope(kind="world", entity_id="market")
    us = _scope(kind="country", entity_id="iso-3166:US")
    europe = _scope(kind="region", entity_id="iso-un-m49:150")
    xtai = _scope(kind="venue", entity_id="mic:XTAI")

    plan = MacroCollectionPlan.from_registry(registry)
    again = MacroCollectionPlan.from_registry(registry)
    assert plan == again
    assert plan.schema_version == MACRO_COLLECTION_PLAN_SCHEMA
    assert plan.registry_version == registry.registry_version
    assert plan.registry_content_sha256 == registry.content_sha256
    assert plan.content_sha256 == again.content_sha256
    assert plan.plan_id.startswith("macro_collection_plan:v1:")
    us_target = MacroCollectionTarget(scope=us, source_ids=("fed_policy_rate",))
    world_target = MacroCollectionTarget(scope=world, source_ids=("brent", "broad_usd_index"))
    assert plan.targets == (us_target, world_target)
    assert tuple(plan) == plan.targets
    assert len(plan) == 2
    assert plan.target_for(us) == us_target
    assert plan.target_for(world) == world_target
    assert plan.scopes == (us, world)
    assert xtai not in plan.scopes
    assert europe not in plan.scopes
    source_ids = [source_id for target in plan.targets for source_id in target.source_ids]
    assert all(target.source_ids == tuple(sorted(target.source_ids)) for target in plan.targets)
    assert len(source_ids) == len(set(source_ids)) == len(registry.entries)
    assert all(target.source_ids for target in plan.targets)
    params = list(inspect.signature(MacroCollectionPlan.from_registry).parameters)
    assert params == ["registry"]
    assert "control_scopes" not in params
    assert "mapping" not in params
    assert "scope_mapping" not in params
    assert plan.require_target(us_target) == us_target
    plan.bind_registry(registry)
    with pytest.raises(ValueError, match="bound registry plan"):
        plan.require_target(MacroCollectionTarget(scope=us, source_ids=("brent",)))
    drifted = MacroSourceRegistry(registry_version="macro_sources.v2-test", entries=registry.entries)
    with pytest.raises(ValueError, match="exact source registry"):
        plan.bind_registry(drifted)
    with pytest.raises(ValueError, match="committed identity"):
        require_committed_macro_collection_plan(plan)
    with pytest.raises(ValueError, match="committed identity"):
        committed_macro_collection_plan(registry)
    with pytest.raises(FrozenInstanceError):
        plan.targets = ()  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        us_target.source_ids = ()  # type: ignore[misc]
    with pytest.raises(ValueError, match="collection target"):
        plan.target_for(xtai)
    with pytest.raises(ValueError, match="source_ids"):
        MacroCollectionTarget(scope=us, source_ids=())
    with pytest.raises(ValueError, match="unique"):
        MacroCollectionTarget(scope=us, source_ids=("fed_policy_rate", "fed_policy_rate"))
    with pytest.raises(ValueError, match="exactly once"):
        MacroCollectionPlan(
            registry_version=registry.registry_version,
            registry_content_sha256=registry.content_sha256,
            targets=(us_target, MacroCollectionTarget(scope=xtai, source_ids=("fed_policy_rate",))),
        )
    replayed = MacroCollectionPlan.from_mapping(plan.to_dict())
    assert replayed == plan
    assert replayed.content_sha256 == plan.content_sha256
    assert_source_only_payload(plan.to_dict(), "macro_collection_plan")
    fed = next(entry for entry in registry.entries if entry.source_id == "fed_policy_rate")
    with pytest.raises(ValueError, match="conflict"):
        MacroSourceRegistry(
            registry_version=MACRO_SOURCE_REGISTRY_VERSION,
            entries=(
                fed,
                MacroSourceRegistryEntry(
                    source_id="other",
                    provider_id="official_provider",
                    provider_entity_id="FED/H15",
                    canonical_scope=_scope(kind="country", entity_id="iso-3166:TW"),
                    adapter_version="official_provider.v1",
                    fact_kind="series_point",
                    metric_key="policy_rate",
                ),
            ),
        )


def test_fact_hashes_are_deterministic_and_ignore_ingest_order_and_ingested_at() -> None:
    first = _fact()
    second = _fact(ingested_at="2026-08-23T19:00:00Z")
    assert first.fact_key == second.fact_key
    assert first.fact_version_id == second.fact_version_id
    assert first.content_sha256 == second.content_sha256
    assert first.to_dict()["schema_version"] == "macro_source_fact.v1"
    replayed = MacroSourceFact.from_mapping(first.to_dict())
    assert replayed == first
    assert reconcile_macro_source_fact(first, second) == first


def test_fact_valid_until_is_derived_from_published_at_not_the_observation_clock() -> None:
    published = datetime(2026, 8, 23, 12, 30, tzinfo=UTC)
    daily = timedelta(hours=72)
    monthly = timedelta(days=40)
    benchmark = timedelta(hours=24)
    assert (
        derive_macro_source_fact_valid_until(
            fact_kind="series_point",
            period="2026-08-22",
            published_at=published,
            series_point_daily=daily,
            series_point_monthly=monthly,
            market_benchmark_daily=benchmark,
        )
        == published + daily
    )
    assert (
        derive_macro_source_fact_valid_until(
            fact_kind="series_point",
            period="2026-07",
            published_at=published,
            series_point_daily=daily,
            series_point_monthly=monthly,
            market_benchmark_daily=benchmark,
        )
        == published + monthly
    )
    assert (
        derive_macro_source_fact_valid_until(
            fact_kind="market_benchmark",
            period="2026-08-21",
            published_at=published,
            series_point_daily=daily,
            series_point_monthly=monthly,
            market_benchmark_daily=benchmark,
        )
        == published + benchmark
    )


def test_same_version_different_valid_until_remains_an_explicit_conflict() -> None:
    first = _fact(valid_until=VALID_UNTIL)
    drifted = _fact(valid_until=datetime(2026, 8, 24, 17, 0, tzinfo=UTC))
    assert drifted.fact_version_id == first.fact_version_id
    assert drifted.content_sha256 != first.content_sha256
    with pytest.raises(ValueError, match="conflict"):
        reconcile_macro_source_fact(first, drifted)


def test_fact_kind_and_value_vocabularies_are_closed() -> None:
    assert MACRO_FACT_KINDS == frozenset({"series_point", "market_benchmark"})
    with pytest.raises(ValueError, match="fact_kind"):
        _fact(fact_kind="calendar_event")
    with pytest.raises(ValueError, match="fact_kind"):
        _fact(fact_kind="global_event")
    categorical = _fact(value=MacroCategoryValue(category="announced"))
    assert categorical.value.to_dict() == {"category": "announced"}
    with pytest.raises((TypeError, ValueError)):
        _fact(value={"number": True, "unit": "percent"})
    with pytest.raises(ValueError, match="finite"):
        MacroNumericValue(number=float("nan"), unit="percent")
    with pytest.raises(ValueError, match="unit"):
        MacroSourceFact.from_mapping({**_fact().to_dict(), "value": {"number": 4.25}})


def test_correction_keeps_fact_key_and_supersedes_previous_leaf() -> None:
    original = _fact()
    correction = original.corrected(
        value=MacroNumericValue(number=4.5, unit="percent"),
        published_at="2026-08-23T18:00:00Z",
        ingested_at="2026-08-23T18:01:00Z",
    )
    assert correction.fact_key == original.fact_key
    assert correction.fact_version_id != original.fact_version_id
    assert correction.supersedes_fact_version_id == original.fact_version_id.value
    assert correction.value.number == 4.5  # type: ignore[union-attr]
    revised_pointer = original.corrected(
        value=MacroNumericValue(number=4.5, unit="percent"),
        published_at="2026-08-23T18:00:00Z",
        ingested_at="2026-08-23T18:01:00Z",
        source=_source(adapter_version="official_provider.v2", source_ref="https://source.example/revised"),
    )
    assert revised_pointer.fact_key == original.fact_key
    with pytest.raises(ValueError, match="MacroFactKey"):
        original.corrected(
            value=MacroNumericValue(number=4.5, unit="percent"),
            published_at="2026-08-23T18:00:00Z",
            ingested_at="2026-08-23T18:01:00Z",
            source=_source(provider_id="other_provider"),
        )
    with pytest.raises(ValueError, match="conflict"):
        reconcile_macro_source_fact(
            original,
            _fact(source=_source(source_ref="https://source.example/other")),
        )


def test_observation_id_is_order_insensitive_and_round_trips() -> None:
    fact_a = _fact()
    fact_b = _fact(
        metric_key="brent",
        period="2026-08-22",
        source=_source(provider_id="market_benchmark", source_record_id="BRN", adapter_version="commodities.v1"),
        value=MacroNumericValue(number=80.0, unit="usd_per_barrel"),
        scope=_scope(kind="world", entity_id="market"),
        fact_kind="market_benchmark",
    )
    refs_left = (fact_a.fact_version_id.value, fact_b.fact_version_id.value)
    refs_right = (fact_b.fact_version_id.value, fact_a.fact_version_id.value)
    left = _observation(fact_refs=refs_left, dimensions=_observation().dimensions)
    # dimensions still point at the original single fact; rebuild with union refs
    dimensions = (
        _dimension(dimension="macro_regime", value="mixed", fact_refs=refs_left),
        _dimension(dimension="rates_regime", value="stable", fact_refs=(fact_a.fact_version_id.value,)),
        _dimension(dimension="usd_regime", value="unknown", coverage_status="unknown", fact_refs=()),
    )
    left = _observation(fact_refs=refs_left, dimensions=dimensions)
    right = _observation(fact_refs=refs_right, dimensions=dimensions)
    assert left.observation_id == right.observation_id
    assert left.content_sha256 == right.content_sha256
    assert left.fact_refs == tuple(sorted(refs_left))
    replayed = MacroWorldObservation.from_mapping(left.to_dict())
    assert replayed == left
    assert left.observation_id.startswith("macro_world_observation:v1:")


def test_unknown_is_always_in_closed_feature_vocabularies() -> None:
    policy = MacroDerivationPolicy(transform_version=MACRO_TRANSFORM_VERSION)
    for dimension, values in (
        ("macro_regime", MACRO_REGIME_VALUES),
        ("rates_regime", policy.allowed_values("rates_regime")),
        ("usd_regime", policy.allowed_values("usd_regime")),
    ):
        assert "unknown" in values
        assert policy.allowed_values(dimension) == values
    observation = _observation()
    assert observation.features["usd_regime"] == "unknown"
    usd = [item for item in observation.dimensions if item.dimension == "usd_regime"][0]
    assert usd.coverage_status == "unknown"
    assert usd.fact_refs == ()
    with pytest.raises(ValueError, match="unknown"):
        _dimension(dimension="usd_regime", value="strong", coverage_status="complete", fact_refs=())
    with pytest.raises(ValueError, match="complete"):
        MacroCoverage(status="complete", required_sources=3, fresh_sources=3, missing_source_ids=("broad_usd_index",))


def test_deny_list_is_recursive_and_rejects_camel_case_instead_of_stripping() -> None:
    payload = _observation().to_dict()
    payload["features"]["candidateScope"] = "hot"
    with pytest.raises(ValueError, match="forbidden"):
        MacroWorldObservation.from_mapping(payload)

    nested = _observation().to_dict()
    nested["coverage"] = {**nested["coverage"], "meta": {"brainDecision": "BUY"}}
    with pytest.raises(ValueError, match="forbidden"):
        MacroWorldObservation.from_mapping(nested)

    coverage_payload = {**_observation().coverage.to_dict(), "rank": 1}
    with pytest.raises(ValueError, match="forbidden"):
        MacroCoverage.from_mapping(coverage_payload)
    with pytest.raises(ValueError, match="forbidden"):
        _observation(coverage=coverage_payload)
    dimension_payload = {**_observation().dimensions[2].to_dict(), "candidate": "x"}
    with pytest.raises(ValueError, match="forbidden"):
        MacroDimensionState.from_mapping(dimension_payload)
    poisoned_dimensions = [item.to_dict() for item in _observation().dimensions]
    poisoned_dimensions[2]["action"] = "BUY"
    with pytest.raises(ValueError, match="forbidden"):
        _observation(dimensions=poisoned_dimensions)

    fact_payload = _fact().to_dict()
    fact_payload["source"]["companyBrief"] = "ignored"
    with pytest.raises(ValueError, match="forbidden"):
        MacroSourceFact.from_mapping(fact_payload)

    rfc_denylist = frozenset(
        {
            "candidate",
            "candidate_scope",
            "hotlist",
            "mandate",
            "rank",
            "attractiveness",
            "brain_decision",
            "action",
            "intent",
            "order",
            "trade",
            "position",
            "portfolio",
            "risk_gate",
            "scheduler",
            "fill",
            "pnl",
            "reward",
            "feedback",
            "outcome",
            "company_brief",
            "company_report",
            "selected_symbol",
            "model_rationale",
            "prompt",
            "tool_trace",
            "memory",
            "memrl",
        }
    )
    assert MACRO_POLICY_DENYLIST == rfc_denylist
    for key in rfc_denylist:
        poisoned = _observation().to_dict()
        poisoned["metadata"] = {key: "x"}
        with pytest.raises(ValueError, match="forbidden"):
            MacroWorldObservation.from_mapping(poisoned)


def test_point_in_time_eligibility_uses_strict_valid_until_boundary() -> None:
    policy = PointInTimeEligibilityPolicy()
    fact = _fact(valid_until=VALID_UNTIL)
    envelope = _envelope()
    eligible = policy.evaluate(
        evidence=envelope.evidence,
        cutoff_at=CUTOFF,
        valid_until=fact.valid_until,
        version=fact.source.adapter_version,
    )
    assert eligible.status == "eligible"
    at_expiry = policy.evaluate(
        evidence=envelope.evidence,
        cutoff_at=VALID_UNTIL,
        valid_until=fact.valid_until,
    )
    assert at_expiry.status == "stale"
    assert policy.is_eligible(evidence=envelope.evidence, cutoff_at=VALID_UNTIL, valid_until=VALID_UNTIL) is False
    superseded = policy.evaluate(evidence=envelope.evidence, cutoff_at=CUTOFF, superseded=True)
    assert superseded.status == "superseded"


def test_observation_envelope_keeps_persisted_ref_and_evidence() -> None:
    observation = _observation()
    envelope = _envelope(observation)
    assert envelope.observation.observation_id == observation.observation_id
    assert envelope.persisted.identity.value == observation.observation_id
    assert envelope.evidence.effective_ready_at == FIRST_SEEN
    assert envelope.persisted.receipt.ready_at == READY
    unsigned = WorldAvailabilityReceipt(
        receipt_id="unsigned",
        subject=WorldAvailabilitySubjectRef(
            kind="macro_world_observation",
            subject_id=observation.observation_id,
            content_sha256=observation.content_sha256,
        ),
        scope=f"{observation.scope.kind}:{observation.scope.entity_id}",
        storage_locator=_locator(),
        content_sha256=observation.content_sha256,
        schema_version="availability_receipt.v2",
    )
    with pytest.raises(ValueError, match="store-attested"):
        MacroObservationEnvelope(
            observation=observation,
            persisted=PersistedWorldRef(identity=MacroObservationId(observation.observation_id), receipt=unsigned),
            evidence=AvailabilityEvidence(receipt=unsigned, first_seen_at=FIRST_SEEN),
        )
    later = _attested_receipt(observation, ready=datetime(2026, 8, 23, 12, 30, tzinfo=UTC))
    earlier = _attested_receipt(observation, ready=READY)
    assert later.receipt_id == earlier.receipt_id
    assert later.ready_at != earlier.ready_at
    with pytest.raises(ValueError, match="receipt"):
        MacroObservationEnvelope(
            observation=observation,
            persisted=PersistedWorldRef(identity=MacroObservationId(observation.observation_id), receipt=earlier),
            evidence=AvailabilityEvidence(receipt=later, first_seen_at=FIRST_SEEN),
        )
    for target in (MacroSourceFact, MacroWorldObservation, MacroCollectionRun.register, MacroObservationEnvelope):
        assert "ready_at" not in inspect.signature(target).parameters
    with pytest.raises(TypeError, match="MacroObservationId"):
        MacroObservationEnvelope(
            observation=observation,
            persisted=PersistedWorldRef(identity=observation.observation_id, receipt=earlier),
            evidence=AvailabilityEvidence(receipt=earlier, first_seen_at=FIRST_SEEN),
        )
    wrong_kind = _attested_receipt(observation, subject_kind="world_graph_snapshot")
    with pytest.raises(ValueError, match="macro_world_observation"):
        MacroObservationEnvelope(
            observation=observation,
            persisted=PersistedWorldRef(identity=MacroObservationId(observation.observation_id), receipt=wrong_kind),
            evidence=AvailabilityEvidence(receipt=wrong_kind, first_seen_at=FIRST_SEEN),
        )
    wrong_scope = _attested_receipt(observation, scope="world:market")
    with pytest.raises(ValueError, match="scope"):
        MacroObservationEnvelope(
            observation=observation,
            persisted=PersistedWorldRef(identity=MacroObservationId(observation.observation_id), receipt=wrong_scope),
            evidence=AvailabilityEvidence(receipt=wrong_scope, first_seen_at=FIRST_SEEN),
        )


def test_same_observation_id_with_different_features_is_a_conflict() -> None:
    first = _observation()
    different_features = dict(first.features)
    different_features["macro_regime"] = "quiet"
    dimensions = (
        _dimension(dimension="macro_regime", value="quiet", fact_refs=first.fact_refs),
        _dimension(dimension="rates_regime", value="stable", fact_refs=first.fact_refs),
        _dimension(dimension="usd_regime", value="unknown", coverage_status="unknown", fact_refs=()),
    )
    second = _observation(features=different_features, dimensions=dimensions)
    assert first.observation_id == second.observation_id
    assert first.content_sha256 != second.content_sha256
    with pytest.raises(ValueError, match="conflict"):
        reconcile_macro_world_observation(first, second)
    assert reconcile_macro_world_observation(first, first) == first


def test_collection_run_rejects_publish_until_every_source_has_a_typed_result() -> None:
    run = _run().start()
    fact = _fact()
    envelope = _envelope(_observation(fact=fact))
    with pytest.raises(ValueError, match="source"):
        run.publish(envelope)
    run = run.record_source_result(
        MacroSourceCompleted(
            run_id=run.run_id, source_id="fed_policy_rate", fact_version_ids=(fact.fact_version_id.value,)
        )
    )
    run = run.record_source_result(MacroSourceFailed(run_id=run.run_id, source_id="brent", reason="missing"))
    with pytest.raises(ValueError, match="source"):
        run.publish(envelope)
    with pytest.raises(ValueError, match="source"):
        run.complete()
    with pytest.raises(ValueError, match="source"):
        run.complete(envelope)
    assert run.status == "collecting"


def test_publish_refuses_a_fake_empty_observation_and_complete_fails_without_one() -> None:
    run = _run().start()
    for source_id in ("fed_policy_rate", "brent", "broad_usd_index"):
        run = run.record_source_result(MacroSourceFailed(run_id=run.run_id, source_id=source_id, reason="missing"))
    empty_features = {"macro_regime": "unknown", "rates_regime": "unknown", "usd_regime": "unknown"}
    empty_dimensions = (
        _dimension(dimension="macro_regime", value="unknown", coverage_status="unknown", fact_refs=()),
        _dimension(dimension="rates_regime", value="unknown", coverage_status="unknown", fact_refs=()),
        _dimension(dimension="usd_regime", value="unknown", coverage_status="unknown", fact_refs=()),
    )
    fake = _observation(
        fact_refs=(),
        features=empty_features,
        dimensions=empty_dimensions,
        coverage=MacroCoverage(
            status="unknown",
            required_sources=3,
            fresh_sources=0,
            missing_source_ids=("fed_policy_rate", "brent", "broad_usd_index"),
        ),
    )
    with pytest.raises(ValueError, match="admissible"):
        run.publish(_envelope(fake))
    failed = run.complete()
    assert failed.status == "failed"
    assert failed.published_envelope is None
    result = failed.terminal_result
    assert isinstance(result, MacroCollectionTerminalResult)
    assert result.status == "failed"
    assert result.observation_id is None
    completed = [event for event in failed.events if isinstance(event, MacroCollectionCompleted)][-1]
    assert completed.status == "failed"
    assert completed.observation_id is None


def test_all_sources_ok_then_publish_complete_is_completed() -> None:
    run = _run().start()
    fact = _fact()
    observation = _observation(fact=fact)
    envelope = _envelope(observation)
    for source_id in ("fed_policy_rate", "brent", "broad_usd_index"):
        refs = (fact.fact_version_id.value,) if source_id == "fed_policy_rate" else ()
        run = run.record_source_result(
            MacroSourceCompleted(run_id=run.run_id, source_id=source_id, fact_version_ids=refs)
        )
    published = run.publish(envelope)
    assert published.status == "collecting"
    assert published.published_envelope is not None
    with pytest.raises(ValueError, match="envelope"):
        published.complete()
    completed = published.complete(envelope)
    assert completed.status == "completed"
    assert completed.terminal_result.observation_id == observation.observation_id
    assert completed.published_envelope == envelope
    replayed = MacroCollectionRun.from_events(completed.events)
    assert replayed == completed
    assert replayed.published_envelope == envelope
    assert replayed.complete() == completed


def test_failed_source_plus_published_observation_is_completed_partial() -> None:
    run = _run().start()
    fact = _fact()
    envelope = _envelope(_observation(fact=fact))
    run = run.record_source_result(
        MacroSourceCompleted(
            run_id=run.run_id, source_id="fed_policy_rate", fact_version_ids=(fact.fact_version_id.value,)
        )
    )
    run = run.record_source_result(MacroSourceCompleted(run_id=run.run_id, source_id="brent", fact_version_ids=()))
    run = run.record_source_result(MacroSourceFailed(run_id=run.run_id, source_id="broad_usd_index", reason="timeout"))
    completed = run.publish(envelope).complete(envelope)
    assert completed.status == "completed_partial"
    assert completed.terminal_result.failed_source_ids == ("broad_usd_index",)
    assert isinstance(completed.events[0], MacroCollectionRegistered)
    assert isinstance(completed.events[1], MacroCollectionStarted)
    types = {type(event) for event in completed.events}
    assert MacroSourceCompleted in types
    assert MacroSourceFailed in types
    assert MacroObservationPublished in types
    assert MacroCollectionCompleted in types
    published_event = next(event for event in completed.events if isinstance(event, MacroObservationPublished))
    assert published_event.envelope == envelope
    assert published_event.receipt_id == envelope.persisted.receipt.receipt_id


def test_event_union_round_trips_and_reconstruction_is_typed() -> None:
    run = _run().start()
    fact = _fact()
    envelope = _envelope(_observation(fact=fact))
    run = run.record_source_result(
        MacroSourceCompleted(
            run_id=run.run_id, source_id="fed_policy_rate", fact_version_ids=(fact.fact_version_id.value,)
        )
    )
    run = run.record_source_result(MacroSourceCompleted(run_id=run.run_id, source_id="brent", fact_version_ids=()))
    run = run.record_source_result(MacroSourceFailed(run_id=run.run_id, source_id="broad_usd_index", reason="timeout"))
    completed = run.publish(envelope).complete(envelope)
    assert completed.status == "completed_partial"
    parsed: list[MacroCollectionEvent] = []
    for event in completed.events:
        payload = event.to_dict()
        if isinstance(event, MacroObservationPublished):
            payload["envelope"] = event.envelope
        parsed.append(parse_macro_collection_event(payload))
    assert {type(event) for event in parsed} == {
        MacroCollectionRegistered,
        MacroCollectionStarted,
        MacroSourceCompleted,
        MacroSourceFailed,
        MacroObservationPublished,
        MacroCollectionCompleted,
    }
    assert all(isinstance(event, MacroCollectionEvent) for event in parsed)
    published = next(event for event in parsed if isinstance(event, MacroObservationPublished))
    assert published.envelope == envelope
    failed = next(event for event in parsed if isinstance(event, MacroSourceFailed))
    assert failed.source_id == "broad_usd_index"
    rebuilt = MacroCollectionRun.from_events(parsed)
    assert rebuilt == completed
    assert rebuilt.status == "completed_partial"
    assert rebuilt.terminal_result is not None
    assert rebuilt.terminal_result.status == "completed_partial"
    assert rebuilt.published_envelope == envelope
    with pytest.raises(ValueError, match="registered"):
        MacroCollectionRun.from_events(parsed[1:])
    with pytest.raises(TypeError, match="envelope"):
        parse_macro_collection_event(published.to_dict())


def test_from_mapping_does_not_invent_identity_versions() -> None:
    observation_payload = _observation().to_dict()
    for key in ("schema_version", "producer_version", "transform_version", "source_registry_version"):
        missing = dict(observation_payload)
        missing.pop(key)
        with pytest.raises(ValueError, match=key):
            MacroWorldObservation.from_mapping(missing)
    fact_payload = _fact().to_dict()
    missing_schema = dict(fact_payload)
    missing_schema.pop("schema_version")
    with pytest.raises(ValueError, match="schema_version"):
        MacroSourceFact.from_mapping(missing_schema)
    registered_payload = _run().registered.to_dict()
    for key in ("schema_version", "producer_version", "transform_version", "source_registry_version"):
        missing = dict(registered_payload)
        missing.pop(key)
        with pytest.raises(ValueError, match=key):
            MacroCollectionRegistered.from_mapping(missing)
    registry_payload = _registry().to_dict()
    registry_payload.pop("schema_version")
    with pytest.raises(ValueError, match="schema_version"):
        MacroSourceRegistry.from_mapping(registry_payload)


def test_observation_rejects_cutoff_at_or_after_valid_until() -> None:
    with pytest.raises(ValueError, match="valid_until"):
        _observation(cutoff_at=VALID_UNTIL, valid_until=VALID_UNTIL)
    with pytest.raises(ValueError, match="valid_until"):
        _observation(cutoff_at=datetime(2026, 8, 23, 18, 0, tzinfo=UTC), valid_until=VALID_UNTIL)


def test_world_scope_types_are_exported_and_module_is_stdlib_only() -> None:
    assert WorldScopeMapping.__module__ == "trader.domain.world_scope"
    assert WorldScopeResolution.__module__ == "trader.domain.world_scope"
    path = REPO_ROOT / "trader" / "domain" / "world_macro.py"
    source = path.read_text(encoding="utf-8")
    assert "NewsMacroBrief" not in source
    assert "trader.infrastructure" not in source
    assert "trader.runtime" not in source
    assert _domain_import_violations([path], REPO_ROOT) == []
    tree = ast.parse(source, filename=str(path))
    violations: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            root = node.module.split(".", 1)[0]
            if node.module.startswith("trader.") and not node.module.startswith("trader.domain"):
                violations.append(node.module)
            elif root != "trader" and root not in sys.stdlib_module_names:
                violations.append(node.module)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".", 1)[0]
                if root != "trader" and root not in sys.stdlib_module_names:
                    violations.append(alias.name)
    assert violations == []
    assert "ready_at" not in inspect.signature(MacroObservationEnvelope).parameters
    assert "as_of" not in inspect.signature(MacroCollectionRun.register).parameters
    assert list(inspect.signature(MacroCollectionRun.complete).parameters) == ["self", "envelope"]


def test_nested_observation_and_aggregate_events_are_deeply_immutable() -> None:
    observation = _observation()
    with pytest.raises(TypeError):
        observation.features["macro_regime"] = "quiet"  # type: ignore[index]
    with pytest.raises(FrozenInstanceError):
        observation.coverage.status = "complete"  # type: ignore[misc]
    with pytest.raises(AttributeError):
        observation.coverage.missing_source_ids.append("usd")  # type: ignore[attr-defined]
    with pytest.raises(FrozenInstanceError):
        observation.dimensions[0].value = "tightening"  # type: ignore[misc]
    with pytest.raises(AttributeError):
        observation.dimensions[0].fact_refs.append("x")  # type: ignore[attr-defined]
    run = _run().start()
    fact = _fact()
    run = run.record_source_result(
        MacroSourceCompleted(
            run_id=run.run_id, source_id="fed_policy_rate", fact_version_ids=(fact.fact_version_id.value,)
        )
    )
    with pytest.raises(AttributeError):
        run.events.append(run.events[0])  # type: ignore[attr-defined]
    with pytest.raises(FrozenInstanceError):
        run.events[0].expected_source_ids = ("only",)  # type: ignore[misc]
    with pytest.raises(TypeError):
        run.source_results["brent"] = run.events[-1]  # type: ignore[index]


def test_expected_source_order_does_not_change_run_or_registered_event_identity() -> None:
    left = MacroCollectionRun.register(
        scope=_scope(kind="venue", entity_id="mic:XTAI"),
        cutoff_at=CUTOFF,
        expected_source_ids=("brent", "fed_policy_rate", "broad_usd_index"),
    )
    right = MacroCollectionRun.register(
        scope=_scope(kind="venue", entity_id="mic:XTAI"),
        cutoff_at=CUTOFF,
        expected_source_ids=("broad_usd_index", "brent", "fed_policy_rate"),
    )
    canonical = tuple(sorted(("brent", "fed_policy_rate", "broad_usd_index")))
    assert left.run_id == right.run_id
    assert left.registered.event_id == right.registered.event_id
    assert left.registered.expected_source_ids == canonical
    assert right.registered.expected_source_ids == canonical
    replayed = MacroCollectionRegistered.from_mapping(left.registered.to_dict())
    assert replayed.event_id == left.registered.event_id


def test_publish_rejects_optimistic_or_inconsistent_coverage() -> None:
    run = _run().start()
    fact = _fact()
    run = run.record_source_result(
        MacroSourceCompleted(
            run_id=run.run_id, source_id="fed_policy_rate", fact_version_ids=(fact.fact_version_id.value,)
        )
    )
    run = run.record_source_result(MacroSourceCompleted(run_id=run.run_id, source_id="brent", fact_version_ids=()))
    run = run.record_source_result(MacroSourceFailed(run_id=run.run_id, source_id="broad_usd_index", reason="timeout"))
    optimistic = _observation(
        fact=fact,
        coverage=MacroCoverage(status="complete", required_sources=3, fresh_sources=3),
    )
    with pytest.raises(ValueError, match="coverage"):
        run.publish(_envelope(optimistic))
    omitted_failed = _observation(
        fact=fact,
        coverage=MacroCoverage(
            status="partial",
            required_sources=3,
            fresh_sources=2,
            missing_source_ids=("brent",),
        ),
    )
    with pytest.raises(ValueError, match="coverage"):
        run.publish(_envelope(omitted_failed))
    wrong_required = _observation(
        fact=fact,
        coverage=MacroCoverage(
            status="partial",
            required_sources=2,
            fresh_sources=1,
            missing_source_ids=("broad_usd_index",),
        ),
    )
    with pytest.raises(ValueError, match="coverage"):
        run.publish(_envelope(wrong_required))


def test_publish_does_not_require_observation_receipt_to_predate_run_cutoff() -> None:
    run = _run().start()
    fact = _fact()
    observation = _observation(fact=fact)
    late = datetime(2026, 8, 23, 13, 5, tzinfo=UTC)
    receipt = _attested_receipt(observation, ready=late)
    envelope = MacroObservationEnvelope(
        observation=observation,
        persisted=PersistedWorldRef(identity=MacroObservationId(observation.observation_id), receipt=receipt),
        evidence=AvailabilityEvidence(receipt=receipt, first_seen_at=late),
    )
    for source_id in ("fed_policy_rate", "brent", "broad_usd_index"):
        refs = (fact.fact_version_id.value,) if source_id == "fed_policy_rate" else ()
        run = run.record_source_result(
            MacroSourceCompleted(run_id=run.run_id, source_id=source_id, fact_version_ids=refs)
        )
    published = run.publish(envelope)
    assert published.published_envelope == envelope
    assert envelope.evidence.effective_ready_at > run.cutoff_at


def _resolution(
    *,
    market_venue: str = "TW",
    instrument: str = "2301.TW",
    status: str = "resolved",
    venue: str = "mic:XTAI",
    country: str = "iso-3166:TW",
    region: str = "iso-un-m49:030",
) -> WorldScopeResolution:
    anchor = WorldMarketAnchorRef(market_venue=market_venue, instrument=instrument)
    if status != "resolved":
        return WorldScopeResolution(
            mapping_id="world_scope_mapping.v2",
            mapping_sha256="e" * 64,
            anchor=anchor,
            status=status,
        )
    return WorldScopeResolution(
        mapping_id="world_scope_mapping.v2",
        mapping_sha256="e" * 64,
        anchor=anchor,
        status="resolved",
        scopes=(
            WorldCanonicalScopeRef(kind="venue", entity_id=venue),
            WorldCanonicalScopeRef(kind="country", entity_id=country),
            WorldCanonicalScopeRef(kind="region", entity_id=region),
            WorldCanonicalScopeRef(kind="world", entity_id="market"),
        ),
    )


def test_live_producer_contract_is_v2_and_keeps_v1_parseable() -> None:
    assert MACRO_PRODUCER_VERSION == "macro_source_only.v2"
    assert MACRO_PRODUCER_VERSION_V1 == "macro_source_only.v1"
    assert MACRO_LANE_IDENTITY == "context.v2.macro_source.v2"
    assert MACRO_LANE_IDENTITY_V1 == "context.v2.macro_source.v1"
    assert MACRO_LANE_IDENTITY != MACRO_LANE_IDENTITY_V1
    assert is_admitted_macro_producer(MACRO_PRODUCER_VERSION) is True
    assert is_admitted_macro_producer(MACRO_PRODUCER_VERSION_V1) is False
    assert macro_observes_producer_ref(MACRO_PRODUCER_VERSION) == "producer:macro_source_only.v2"
    assert WORLD_MACRO_COLLECTION_PLAN_ID == f"macro_collection_plan:v1:{WORLD_MACRO_COLLECTION_PLAN_SHA256}"
    v1 = _observation(producer_version=MACRO_PRODUCER_VERSION_V1)
    replayed = MacroWorldObservation.from_mapping(v1.to_dict())
    assert replayed.producer_version == MACRO_PRODUCER_VERSION_V1
    assert replayed.observation_id == v1.observation_id
    v2 = _observation()
    assert v2.producer_version == MACRO_PRODUCER_VERSION
    assert v2.observation_id != v1.observation_id


def test_search_plan_walks_resolved_ancestry_and_ignores_v1_venue_contamination() -> None:
    tw = MacroContextSearchPlan.from_resolution(_resolution())
    assert [scope.to_dict() for scope in tw.ancestry] == [
        {"kind": "venue", "entity_id": "mic:XTAI"},
        {"kind": "country", "entity_id": "iso-3166:TW"},
        {"kind": "region", "entity_id": "iso-un-m49:030"},
        {"kind": "world", "entity_id": "market"},
    ]
    venue_v1 = _envelope(
        _observation(scope=_scope(kind="venue", entity_id="mic:XTAI"), producer_version=MACRO_PRODUCER_VERSION_V1)
    )
    world = _envelope(_observation(scope=_scope(kind="world", entity_id="market")))
    selected = tw.select((venue_v1, world), cutoff_at=CUTOFF)
    assert selected is not None
    assert selected.origin_scope == world.observation.scope
    assert selected.distance == 3
    assert selected.envelope.observation.observation_id == world.observation.observation_id
    assert selected.eligibility_status == "eligible"
    assert selected.to_dict()["origin_scope"] == {"kind": "world", "entity_id": "market"}
    with pytest.raises(ValueError, match="producer"):
        MacroContextSelection(
            envelope=venue_v1,
            origin_scope=venue_v1.observation.scope,
            distance=0,
            search_plan=tw,
        )


def test_search_plan_selects_eu_region_then_us_country_before_world() -> None:
    europe = _envelope(_observation(scope=_scope(kind="region", entity_id="iso-un-m49:150")))
    world = _envelope(_observation(scope=_scope(kind="world", entity_id="market")))
    eu = MacroContextSearchPlan.from_resolution(
        _resolution(
            market_venue="EU", instrument="SAP.DE", venue="mic:XETR", country="iso-3166:DE", region="iso-un-m49:150"
        )
    )
    eu_selected = eu.select((world, europe), cutoff_at=CUTOFF)
    assert eu_selected is not None
    assert eu_selected.origin_scope.kind == "region"
    assert eu_selected.origin_scope.entity_id == "iso-un-m49:150"
    assert eu_selected.distance == 2
    assert eu_selected.envelope.observation.scope != _scope(kind="venue", entity_id="mic:XETR")

    us_country = _envelope(_observation(scope=_scope(kind="country", entity_id="iso-3166:US")))
    us = MacroContextSearchPlan.from_resolution(
        _resolution(
            market_venue="US", instrument="GM", venue="mic:XNYS", country="iso-3166:US", region="iso-un-m49:021"
        )
    )
    us_selected = us.select((world, us_country), cutoff_at=CUTOFF)
    assert us_selected is not None
    assert us_selected.origin_scope == us_country.observation.scope
    assert us_selected.distance == 1


def test_unmapped_search_fails_closed_and_does_not_borrow_world() -> None:
    world = _envelope(_observation(scope=_scope(kind="world", entity_id="market")))
    unmapped = MacroContextSearchPlan.from_resolution(
        _resolution(status="unmapped", market_venue="GM", instrument="BMW.DE")
    )
    assert unmapped.ancestry == ()
    assert unmapped.select((world,), cutoff_at=CUTOFF) is None


def test_selection_provenance_round_trips_without_restamping_origin() -> None:
    world = _envelope(_observation(scope=_scope(kind="world", entity_id="market")))
    plan = MacroContextSearchPlan.from_resolution(_resolution())
    selected = plan.select((world,), cutoff_at=CUTOFF)
    assert selected is not None
    provenance = selected.provenance()
    assert provenance.origin_scope == world.observation.scope
    assert provenance.ancestry_distance == 3
    assert provenance.producer_version == MACRO_PRODUCER_VERSION
    assert provenance.observation_id == world.observation.observation_id
    assert provenance.fact_refs == world.observation.fact_refs
    replayed = MacroObservationProvenance.from_source_refs(provenance.to_source_refs())
    assert replayed == provenance
    assert "ancestry_distance:3" in provenance.to_source_refs()
    assert "origin_scope:world:market" in provenance.to_source_refs()
