"""Orchestrate source-only macro collection, projection, and PIT selection.

The use case owns run/facts/observation sequencing for one typed collection
target. Thresholds stay inside the injected ``MacroDerivationPolicy`` /
projector: this module does not choose regimes, does not invent missing
sources, and does not declare causality from store order. Foreign-scope or
off-registry facts fail closed as typed source failures. Infrastructure,
runtime, and reporting stay behind the ports.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from trader.application.world_model.macro_ports import (
    MacroCollectionLedger,
    MacroHistory,
    MacroSourcePort,
    WorldMacroObservationReader,
)
from trader.application.world_model.source_deadline import source_only_deadline
from trader.domain.world_availability import (
    AvailabilityEvidence,
    PersistedWorldRef,
    PointInTimeEligibilityPolicy,
)
from trader.domain.world_macro import (
    MACRO_FEATURE_KEYS,
    MACRO_TERMINAL_STATUSES,
    MacroCategoryValue,
    MacroCollectionPlan,
    MacroCollectionRun,
    MacroCollectionRunId,
    MacroCollectionTarget,
    MacroCoverage,
    MacroDerivationPolicy,
    MacroDimensionState,
    MacroObservationEnvelope,
    MacroScope,
    MacroSourceCompleted,
    MacroSourceFailed,
    MacroSourceFact,
    MacroSourceFactVersionId,
    MacroSourceRegistry,
    MacroSourceRegistryEntry,
    MacroWorldObservation,
)


class MacroObservationProjector(Protocol):
    """Pure projector. Numeric thresholds live in transform_version, not here."""

    def __call__(
        self,
        *,
        policy: MacroDerivationPolicy,
        scope: MacroScope,
        cutoff_at: datetime,
        source_registry_version: str,
        facts: Sequence[MacroSourceFact],
        expected_source_ids: Sequence[str],
        failed_source_ids: Sequence[str],
        fresh_source_ids: Sequence[str],
    ) -> MacroWorldObservation | None: ...


def _unique_sorted(values: Sequence[str]) -> tuple[str, ...]:
    return tuple(sorted(frozenset(values)))


def _fact_boundary_failure(
    fact: MacroSourceFact,
    *,
    entry: MacroSourceRegistryEntry,
    scope: MacroScope,
) -> str | None:
    if fact.scope != scope or fact.scope != entry.canonical_scope:
        return "scope_mismatch"
    if (
        fact.source.provider_id != entry.provider_id
        or fact.source.adapter_version != entry.adapter_version
        or fact.fact_kind != entry.fact_kind
        or fact.metric_key != entry.metric_key
    ):
        return "provenance_mismatch"
    return None


_TYPED_SOURCE_FAILURE_REASONS = frozenset(
    {
        "http_429",
        "invalid_payload",
        "missing",
        "persistence_conflict",
        "provenance_mismatch",
        "scope_mismatch",
        "timeout",
        "unavailable",
    }
)


def _source_failure_reason(exc: BaseException) -> str:
    typed = getattr(exc, "reason", None)
    if isinstance(typed, str) and typed in _TYPED_SOURCE_FAILURE_REASONS:
        return typed
    text = f"{type(exc).__name__} {exc}".lower()
    if "conflict" in text:
        return "persistence_conflict"
    if "429" in text or "too many" in text:
        return "http_429"
    if "timeout" in text:
        return "timeout"
    if isinstance(exc, (TypeError, ValueError)):
        return "invalid_payload"
    return "unavailable"


def _read_facts_bounded(
    port: MacroSourcePort | None,
    scope: MacroScope,
    observed_at: datetime,
    timeout_s: float | None,
) -> tuple[MacroSourceFact, ...]:
    if port is None:
        raise ValueError("missing")
    if timeout_s is None or timeout_s <= 0:
        return tuple(port.read_facts(scope, observed_at))
    return source_only_deadline().run(
        lambda: tuple(port.read_facts(scope, observed_at)),
        timeout_s=float(timeout_s),
    )


def project_macro_world_observation(
    *,
    policy: MacroDerivationPolicy,
    scope: MacroScope,
    cutoff_at: datetime,
    source_registry_version: str,
    facts: Sequence[MacroSourceFact],
    expected_source_ids: Sequence[str],
    failed_source_ids: Sequence[str],
    fresh_source_ids: Sequence[str],
) -> MacroWorldObservation | None:
    """Deterministic closed-vocabulary projection with honest missingness.

    Numeric facts are evidence only. A dimension becomes a known category solely
    when admissible facts carry that closed feature key and agree. Missing or
    failed sources stay ``unknown`` / ``partial``; they are never filled in.
    """

    ordered = tuple(sorted(facts, key=lambda item: item.fact_version_id.value))
    if not ordered:
        return None
    if any(fact.scope != scope for fact in ordered):
        return None
    expected = _unique_sorted(expected_source_ids)
    failed = frozenset(failed_source_ids)
    fresh = frozenset(fresh_source_ids)
    missing = _unique_sorted(tuple(set(expected) - set(fresh)) + tuple(failed))
    required = len(expected)
    fresh_count = len(fresh)
    coverage_status = "complete" if fresh_count == required and not missing else "partial"
    coverage = MacroCoverage(
        status=coverage_status,
        required_sources=required,
        fresh_sources=fresh_count,
        missing_source_ids=missing,
    )
    votes: dict[str, set[str]] = {key: set() for key in MACRO_FEATURE_KEYS}
    refs_by_dim: dict[str, list[str]] = {key: [] for key in MACRO_FEATURE_KEYS}
    for fact in ordered:
        if fact.metric_key not in MACRO_FEATURE_KEYS:
            continue
        if not isinstance(fact.value, MacroCategoryValue):
            continue
        if fact.value.category not in policy.allowed_values(fact.metric_key):
            continue
        votes[fact.metric_key].add(fact.value.category)
        refs_by_dim[fact.metric_key].append(fact.fact_version_id.value)
    features: dict[str, str] = {}
    dimensions: list[MacroDimensionState] = []
    for key in MACRO_FEATURE_KEYS:
        categories = votes[key]
        if len(categories) == 1:
            value = next(iter(categories))
            fact_refs = tuple(refs_by_dim[key])
            coverage_for_dim = "complete"
        else:
            value = "unknown"
            fact_refs = ()
            coverage_for_dim = "unknown"
        features[key] = value
        dimensions.append(
            MacroDimensionState(
                dimension=key,
                value=value,
                coverage_status=coverage_for_dim,
                method=policy.transform_version,
                fact_refs=fact_refs,
            )
        )
    untils = [fact.valid_until for fact in ordered if fact.valid_until is not None]
    return MacroWorldObservation(
        scope=scope,
        cutoff_at=cutoff_at,
        fact_refs=tuple(item.fact_version_id.value for item in ordered),
        features=features,
        dimensions=tuple(dimensions),
        coverage=coverage,
        producer_version=policy.producer_version,
        transform_version=policy.transform_version,
        source_registry_version=source_registry_version,
        valid_until=min(untils) if untils else None,
    )


@dataclass(frozen=True)
class MacroWorldPipeline:
    history: MacroHistory
    ledger: MacroCollectionLedger
    reader: WorldMacroObservationReader
    policy: MacroDerivationPolicy
    eligibility: PointInTimeEligibilityPolicy = PointInTimeEligibilityPolicy()
    project: MacroObservationProjector = project_macro_world_observation
    source_timeouts: Mapping[str, float] | None = None

    def collect(
        self,
        *,
        target: MacroCollectionTarget,
        cutoff_at: datetime,
        registry: MacroSourceRegistry,
        sources: Mapping[str, MacroSourcePort],
        observed_at: datetime | None = None,
    ) -> MacroCollectionRun:
        if not isinstance(target, MacroCollectionTarget):
            raise TypeError("collect requires MacroCollectionTarget")
        if not isinstance(registry, MacroSourceRegistry):
            raise TypeError("collect requires MacroSourceRegistry")
        plan = MacroCollectionPlan.from_registry(registry)
        plan.bind_registry(registry)
        scheduled = plan.require_target(target)
        observed = cutoff_at if observed_at is None else observed_at
        run = MacroCollectionRun.register(
            scope=scheduled.scope,
            cutoff_at=cutoff_at,
            expected_source_ids=scheduled.source_ids,
            producer_version=self.policy.producer_version,
            transform_version=self.policy.transform_version,
            source_registry_version=registry.registry_version,
        )
        existing = self.ledger.load(MacroCollectionRunId(run.run_id))
        if existing:
            run = MacroCollectionRun.from_events(existing)
        else:
            self.ledger.append_event(run.registered)
        if run.status in MACRO_TERMINAL_STATUSES:
            return run
        run = self._advance(run, run.start())
        admissible: list[MacroSourceFact] = []
        fresh: list[str] = []
        for source_id in run.expected_source_ids:
            if source_id in run.source_results:
                continue
            run, eligible = self._collect_source(
                run,
                source_id=source_id,
                port=sources.get(source_id),
                scope=scheduled.scope,
                cutoff_at=cutoff_at,
                observed_at=observed,
                registry=registry,
            )
            if eligible:
                fresh.append(source_id)
                admissible.extend(eligible)
        if run.status in MACRO_TERMINAL_STATUSES:
            return self._reload(run)
        if run.published_envelope is not None:
            run = self._advance(run, run.complete(run.published_envelope))
            return self._reload(run)
        failed_ids = tuple(
            sorted(
                source_id
                for source_id, result in run.source_results.items()
                if isinstance(result, MacroSourceFailed)
            )
        )
        unique_facts = {fact.fact_version_id.value: fact for fact in admissible}
        ordered_facts = tuple(unique_facts[key] for key in sorted(unique_facts))
        observation = self.project(
            policy=self.policy,
            scope=scheduled.scope,
            cutoff_at=cutoff_at,
            source_registry_version=registry.registry_version,
            facts=ordered_facts,
            expected_source_ids=run.expected_source_ids,
            failed_source_ids=failed_ids,
            fresh_source_ids=tuple(sorted(fresh)),
        )
        if observation is None:
            run = self._advance(run, run.complete())
            return self._reload(run)
        envelope = self.history.append_observation(observation)
        run = self._advance(run, run.publish(envelope))
        run = self._advance(run, run.complete(envelope))
        return self._reload(run)

    def select(self, scope: MacroScope, cutoff_at: datetime) -> MacroObservationEnvelope | None:
        candidates = self.reader.list_candidates_available_through(scope, cutoff_at)
        eligible: list[MacroObservationEnvelope] = []
        for envelope in candidates:
            observation = envelope.observation
            if observation.scope != scope:
                continue
            if observation.cutoff_at > cutoff_at:
                continue
            decision = self.eligibility.evaluate(
                evidence=envelope.evidence,
                cutoff_at=cutoff_at,
                valid_until=observation.valid_until,
                version=observation.transform_version,
            )
            if decision.status == "eligible":
                eligible.append(envelope)
        if not eligible:
            return None
        eligible.sort(key=lambda item: (item.observation.cutoff_at, item.observation.observation_id))
        return eligible[-1]

    def _reload(self, run: MacroCollectionRun) -> MacroCollectionRun:
        return MacroCollectionRun.from_events(self.ledger.load(MacroCollectionRunId(run.run_id)))

    def _advance(self, previous: MacroCollectionRun, updated: MacroCollectionRun) -> MacroCollectionRun:
        if len(updated.events) > len(previous.events):
            self.ledger.append_event(updated.events[-1])
        return updated

    def _collect_source(
        self,
        run: MacroCollectionRun,
        *,
        source_id: str,
        port: MacroSourcePort | None,
        scope: MacroScope,
        cutoff_at: datetime,
        observed_at: datetime,
        registry: MacroSourceRegistry,
    ) -> tuple[MacroCollectionRun, tuple[MacroSourceFact, ...]]:
        if port is None:
            failed = MacroSourceFailed(run_id=run.run_id, source_id=source_id, reason="missing")
            return self._advance(run, run.record_source_result(failed)), ()
        timeout_s = None if self.source_timeouts is None else self.source_timeouts.get(source_id)
        try:
            raw = _read_facts_bounded(port, scope, observed_at, timeout_s)
            entry = registry.entry_for(source_id)
            seen: set[str] = set()
            validated: list[MacroSourceFact] = []
            for fact in sorted(raw, key=lambda item: item.fact_version_id.value):
                if not isinstance(fact, MacroSourceFact):
                    raise TypeError("source fact must be MacroSourceFact")
                version = fact.fact_version_id.value
                if version in seen:
                    continue
                seen.add(version)
                reason = _fact_boundary_failure(fact, entry=entry, scope=scope)
                if reason is not None:
                    failed = MacroSourceFailed(run_id=run.run_id, source_id=source_id, reason=reason)
                    return self._advance(run, run.record_source_result(failed)), ()
                validated.append(fact)
            version_ids: list[str] = []
            eligible: list[MacroSourceFact] = []
            for fact in validated:
                persisted = self.history.append_fact(fact)
                version_ids.append(fact.fact_version_id.value)
                if self._fact_is_admissible(fact, persisted, cutoff_at):
                    eligible.append(fact)
        except Exception as exc:
            failed = MacroSourceFailed(
                run_id=run.run_id,
                source_id=source_id,
                reason=_source_failure_reason(exc),
            )
            return self._advance(run, run.record_source_result(failed)), ()
        completed = MacroSourceCompleted(
            run_id=run.run_id,
            source_id=source_id,
            fact_version_ids=tuple(version_ids),
        )
        return self._advance(run, run.record_source_result(completed)), tuple(eligible)

    def _fact_is_admissible(
        self,
        fact: MacroSourceFact,
        persisted: PersistedWorldRef[MacroSourceFactVersionId],
        cutoff_at: datetime,
    ) -> bool:
        stamped = persisted.receipt.ready_at
        if stamped is None:
            return False
        evidence = AvailabilityEvidence(receipt=persisted.receipt, first_seen_at=stamped)
        return self.eligibility.is_eligible(
            evidence=evidence,
            cutoff_at=cutoff_at,
            valid_until=fact.valid_until,
            version=fact.source.adapter_version,
        )


__all__ = [
    "MacroObservationProjector",
    "MacroWorldPipeline",
    "project_macro_world_observation",
]
