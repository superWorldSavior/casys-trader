"""Prospective World-context capture. Market ``capture_world_episodes`` stays frozen.

The market control observation is derived from an already-captured market
episode at the same bar slot. Context cutoff is the deterministic completed-bar
clock, never the later poll time. A missing, late, or unproven sensor is
represented explicitly; it does not drop a trainable market episode. An
unparseable bar clock fails closed: no context companion is attached.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Protocol

from trader.application.world_model.capture import (
    SAMPLING_POLICY_VERSION,
    capture_world_episodes,
)
from trader.domain.company import CompanyIntelligenceBrief
from trader.domain.world_episode import MARKET_FEATURE_CONTRACT_ID
from trader.domain.world_context import (
    CONTEXT_FEATURE_CONTRACT_ID,
    EntityRef,
    ONTOLOGY_REVISION,
    SensorEvidence,
    TopologyEdge,
    WorldContextSnapshot,
    bootstrap_instrument_topology,
    company_source_count_bucket,
    model_facing_sensor_evidence,
    overall_context_status,
    topology_path_for_instrument,
    verified_issuer_entity_id,
)
from trader.domain.world_episode import (
    WorldEpisode,
    WorldObservation,
    completed_bar_cutoff,
    parse_utc_timestamp,
)
from trader.domain.world_macro import MacroObservationProvenance


class WorldContextSource(Protocol):
    """Application-owned port. Infrastructure supplies a frozen file reader."""

    def lookup_macro(self, *, venue: str, symbol: str, cutoff_at: datetime | str) -> SensorEvidence: ...

    def lookup_company(self, *, symbol: str, cutoff_at: datetime | str) -> SensorEvidence: ...


def attach_world_context(
    episodes: Sequence[WorldEpisode],
    context_source: WorldContextSource,
) -> tuple[WorldEpisode, ...]:
    """Project one context episode per market episode without widening the market observation."""

    attached: list[WorldEpisode] = []
    for episode in episodes:
        observation = episode.observation
        cutoff = completed_bar_cutoff(
            as_of_bar_ts=observation.as_of_bar_ts,
            timestamp_semantics=observation.anchor.timestamp_semantics,
            bar_interval=observation.bar_interval,
        )
        venue = observation.venue
        symbol = observation.symbol
        if cutoff is None:
            continue
        if observation.available_at is None or cutoff > observation.available_at:
            continue
        macro = model_facing_sensor_evidence(
            context_source.lookup_macro(venue=venue, symbol=symbol, cutoff_at=cutoff),
            cutoff,
        )
        company = model_facing_sensor_evidence(context_source.lookup_company(symbol=symbol, cutoff_at=cutoff), cutoff)
        snapshot = build_world_context_snapshot(
            symbol=symbol,
            venue=venue,
            cutoff_at=cutoff,
            family=_frozen_asset_family(observation),
            macro=macro,
            company=company,
        )
        v2_observation = WorldObservation(
            venue=observation.venue,
            symbol=observation.symbol,
            bar_interval=observation.bar_interval,
            as_of_bar_ts=observation.as_of_bar_ts,
            feature_contract_version=CONTEXT_FEATURE_CONTRACT_ID,
            sampling_policy_version=observation.sampling_policy_version,
            anchor=observation.anchor,
            available_at=observation.available_at,
            captured_at=observation.captured_at,
            freshness=observation.freshness,
            categorical_features=dict(observation.categorical_features),
            numeric_features=dict(observation.numeric_features),
            context=snapshot,
        )
        attached.append(
            WorldEpisode(
                observation=v2_observation,
                training_eligible=episode.training_eligible,
                training_reason=episode.training_reason,
            )
        )
    return tuple(attached)


def capture_world_episodes_with_context(
    active_symbols: Sequence[object],
    tradable_symbols: Sequence[object] | None,
    bars_by_symbol: Mapping[object, Sequence[object]],
    market_metadata_by_symbol: Mapping[object, Mapping[str, object]] | None,
    context_source: WorldContextSource,
    *,
    source: str | None = None,
    interval: str | None = None,
    timestamp_semantics: str | None = None,
    captured_at: datetime | str | None = None,
) -> tuple[tuple[WorldEpisode, ...], tuple[WorldEpisode, ...]]:
    """Return ``(v1_episodes, v2_episodes)`` from one frozen market cohort."""

    v1_episodes = capture_world_episodes(
        active_symbols,
        tradable_symbols,
        bars_by_symbol,
        market_metadata_by_symbol,
        source=source,
        interval=interval,
        timestamp_semantics=timestamp_semantics,
        captured_at=captured_at,
        feature_contract_version=MARKET_FEATURE_CONTRACT_ID,
        sampling_policy_version=SAMPLING_POLICY_VERSION,
    )
    v2_episodes = attach_world_context(v1_episodes, context_source)
    return v1_episodes, v2_episodes


def build_world_context_snapshot(
    *,
    symbol: str,
    venue: str,
    cutoff_at: datetime | str,
    family: str | None,
    macro: SensorEvidence,
    company: SensorEvidence,
) -> WorldContextSnapshot:
    cutoff = parse_utc_timestamp(cutoff_at, "cutoff_at")
    macro = model_facing_sensor_evidence(macro, cutoff)
    company = model_facing_sensor_evidence(company, cutoff)
    edges = list(
        bootstrap_instrument_topology(
            symbol=symbol,
            venue=venue,
            family=family,
            cutoff_at=cutoff,
            ontology_revision=ONTOLOGY_REVISION,
        )
    )
    instrument = EntityRef("instrument", symbol)
    proofs: list[dict[str, object]] = []
    artifact_refs: list[str] = []
    proofs.append(_proof("macro_world_observation", macro))
    if macro.artifact is not None and macro.proven and macro.status in {"complete", "partial"}:
        artifact_refs.append(macro.artifact.artifact_id)
        if macro.artifact.ready_at is not None and macro.artifact.ready_at <= cutoff:
            edges.append(
                TopologyEdge(
                    kind="DESCRIBED_BY",
                    source=macro.artifact.subjects[0],
                    target=EntityRef("sensor", "macro_world_observation"),
                    effective_from=cutoff,
                    ready_at=macro.artifact.ready_at,
                    ontology_revision=ONTOLOGY_REVISION,
                    source_refs=macro.artifact.source_refs or ("macro_world_observation",),
                )
            )
    proofs.append(_proof("company_intelligence", company))
    if company.artifact is not None and company.proven and company.status in {"complete", "partial"}:
        artifact_refs.append(company.artifact.artifact_id)
        if company.artifact.ready_at is not None and company.artifact.ready_at <= cutoff:
            edges.append(
                TopologyEdge(
                    kind="DESCRIBED_BY",
                    source=instrument,
                    target=EntityRef("sensor", "company_intelligence"),
                    effective_from=cutoff,
                    ready_at=company.artifact.ready_at,
                    ontology_revision=ONTOLOGY_REVISION,
                    source_refs=company.artifact.source_refs or ("company_intelligence",),
                )
            )
            issuer_id = None
            if company.payload is not None:
                identity = company.payload.get("issuer_identity") or {}
                if isinstance(identity, Mapping):
                    issuer_id = verified_issuer_entity_id(identity)
            if issuer_id:
                issuer_refs = company.artifact.source_refs or (company.artifact.artifact_id,)
                edges.append(
                    TopologyEdge(
                        kind="ISSUED_BY",
                        source=instrument,
                        target=EntityRef("company", issuer_id),
                        effective_from=company.artifact.ready_at,
                        ready_at=company.artifact.ready_at,
                        ontology_revision=ONTOLOGY_REVISION,
                        source_refs=issuer_refs,
                    )
                )

    sensor_statuses = {
        "macro_world_observation": macro.status,
        "company_intelligence": company.status,
    }
    status = overall_context_status(sensor_statuses)
    categorical = {
        "context_status": status,
        "macro_status": macro.status,
        "company_status": company.status,
        **_macro_features(macro),
        **_company_features(company, cutoff),
    }
    return WorldContextSnapshot(
        instrument=instrument,
        cutoff_at=cutoff,
        ontology_revision=ONTOLOGY_REVISION,
        topology_edges=tuple(edges),
        topology_path=topology_path_for_instrument(tuple(edges), instrument),
        artifact_refs=tuple(artifact_refs),
        artifact_proofs=tuple(proofs),
        categorical_features=categorical,
        numeric_features={},
        status=status,
        sensor_statuses=sensor_statuses,
        feature_contract_version=CONTEXT_FEATURE_CONTRACT_ID,
    )


def _frozen_asset_family(observation: WorldObservation) -> str | None:
    """Return the market-frozen family label, or None when the observation has none."""

    raw = observation.categorical_features.get("asset_family")
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    return text or None


def _proof(kind: str, evidence: SensorEvidence) -> dict[str, object]:
    proof: dict[str, object] = {
        "kind": kind,
        "status": evidence.status,
        "proven": evidence.proven,
    }
    if evidence.reason is not None:
        proof["reason"] = evidence.reason
    if evidence.artifact is not None:
        proof["artifact_id"] = evidence.artifact.artifact_id
        proof["ready_at"] = evidence.artifact.ready_at.isoformat() if evidence.artifact.ready_at else None
        proof["content_sha256"] = evidence.artifact.content_sha256
        if evidence.artifact.valid_until is not None:
            proof["valid_until"] = evidence.artifact.valid_until.isoformat()
        try:
            provenance = MacroObservationProvenance.from_source_refs(evidence.artifact.source_refs)
        except (TypeError, ValueError):
            provenance = None
        else:
            proof["origin_scope"] = provenance.origin_scope.to_dict()
            proof["producer_version"] = provenance.producer_version
            if provenance.ancestry_distance is not None:
                proof["ancestry_distance"] = provenance.ancestry_distance
            if provenance.mapping_id is not None:
                proof["mapping_id"] = provenance.mapping_id
                proof["mapping_sha256"] = provenance.mapping_sha256
    resolution = None
    payload = evidence.payload
    if payload is not None:
        raw = payload.get("scope_resolution")
        if isinstance(raw, Mapping):
            resolution = raw
        if "origin_scope" not in proof:
            origin = payload.get("origin_scope")
            if isinstance(origin, Mapping):
                proof["origin_scope"] = dict(origin)
            distance = payload.get("ancestry_distance")
            if isinstance(distance, int) and not isinstance(distance, bool):
                proof["ancestry_distance"] = distance
            producer_version = str(payload.get("producer_version") or "").strip()
            if producer_version:
                proof["producer_version"] = producer_version
    if resolution is not None:
        mapping_id = str(resolution.get("mapping_id") or "").strip()
        mapping_sha256 = str(resolution.get("mapping_sha256") or "").strip()
        status = str(resolution.get("resolution_status") or resolution.get("status") or "").strip()
        if mapping_id and "mapping_id" not in proof:
            proof["mapping_id"] = mapping_id
        if mapping_sha256 and "mapping_sha256" not in proof:
            proof["mapping_sha256"] = mapping_sha256
        if status:
            proof["resolution_status"] = status
        anchor = resolution.get("anchor")
        if isinstance(anchor, Mapping):
            proof["anchor"] = dict(anchor)
    return proof


def _macro_features(evidence: SensorEvidence) -> dict[str, str]:
    missing = {
        "context_macro_regime": "missing",
        "context_rates_regime": "missing",
        "context_usd_regime": "missing",
    }
    if evidence.status not in {"complete", "partial"} or evidence.payload is None:
        return missing
    payload = evidence.payload
    features = payload.get("features") if isinstance(payload.get("features"), Mapping) else payload
    if not isinstance(features, Mapping):
        return missing
    return {
        "context_macro_regime": _structured_text(features, "macro_regime", "regime") or "missing",
        "context_rates_regime": _structured_text(features, "rates_regime", "rates") or "missing",
        "context_usd_regime": _structured_text(features, "usd_regime", "usd") or "missing",
    }


def _company_features(evidence: SensorEvidence, cutoff: datetime) -> dict[str, str]:
    if evidence.status != "complete" or evidence.payload is None:
        return {
            "company_thesis_status": "missing",
            "company_coverage_status": "missing",
            "company_freshness_status": "missing",
            "company_source_count_bucket": "b0",
        }
    payload = evidence.payload
    thesis = payload.get("company_thesis") if isinstance(payload.get("company_thesis"), Mapping) else {}
    coverage = payload.get("coverage") if isinstance(payload.get("coverage"), Mapping) else {}
    thesis_status = _structured_text(thesis, "status") or "missing"
    coverage_status = _structured_text(coverage, "status") or "missing"
    refs = payload.get("source_refs")
    count = len(refs) if isinstance(refs, (list, tuple)) else 0
    freshness = "missing"
    brief = CompanyIntelligenceBrief.from_mapping(payload)
    if brief is not None:
        freshness = brief.freshness_status(cutoff)
    return {
        "company_thesis_status": thesis_status,
        "company_coverage_status": coverage_status,
        "company_freshness_status": freshness,
        "company_source_count_bucket": company_source_count_bucket(count),
    }


def _structured_text(container: Mapping[str, object] | object, *names: str) -> str | None:
    if not isinstance(container, Mapping):
        return None
    for name in names:
        raw = container.get(name)
        if isinstance(raw, str) and raw.strip() and len(raw.strip()) <= 80:
            return raw.strip().lower()
    return None


__all__ = [
    "CONTEXT_FEATURE_CONTRACT_ID",
    "WorldContextSource",
    "attach_world_context",
    "build_world_context_snapshot",
    "capture_world_episodes_with_context",
]
