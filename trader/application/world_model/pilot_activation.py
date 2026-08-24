"""Operator-authorized World Model shadow-pilot activation.

RFC default is no implicit activation: register/arm/start stay explicit CLI
commands. Human override: ``activation_policy=operator_authorized_on_boot``
makes a normal daemon start idempotently register, arm, and start the approved
shadow cohort(s) and workers. The versioned config is the authorization; this
is not an implicit runtime default.

No backfill. No trade decision effect. Idle cycles may still write no episode.
Fail-open if the config is missing or invalid. ``CASYS_WORLD_SHADOW_PILOT_ACTIVATION=0``
skips register/arm/start while leaving V1 shadow.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any

import yaml

from trader.application.world_model.baseline import MODEL_ID as MARKOV_MODEL_ID
from trader.application.world_model.cohort_ports import (
    WorldOntologyHeadsProof,
    WorldOntologyProofQuery,
    WorldRuntimeIdentityPort,
)
from trader.application.world_model.cohort_service import WorldCohortService
from trader.application.world_model.encoding import world_lane_encoder_profile
from trader.application.world_model.gru import MODEL_ID as GRU_MODEL_ID
from trader.application.world_model.runtime_identity import MeasuredWorldRuntimeIdentityService
from trader.domain.world_cohort import (
    COHORT_AUTHORITY,
    COHORT_DECISION_EFFECT,
    COHORT_RECOMMENDATION,
    ArmWorldCohort,
    CohortPhase,
    InvalidateWorldCohort,
    InvalidationReason,
    RegisterWorldCohort,
    SensorMask,
    StartWorldCohort,
    WorldCohort,
    WorldCohortId,
    WorldCohortManifest,
    WorldContrastDefinition,
    WorldContrastTerm,
    WorldLaneDefinition,
    WorldRuntimeIdentity,
    WorldRuntimeIdentityIntent,
    WorldSensorRequirement,
)
from trader.domain.world_episode import canonical_sha256, parse_utc_timestamp
from trader.domain.world_feature_contract import (
    GRAPH_FEATURE_CONTRACT_VERSION,
    WORLD_GRAPH_V3_ONTOLOGY_REVISION,
    WORLD_SCOPE_MAPPING_ID,
    WORLD_SCOPE_MAPPING_SHA256,
)


WORLD_SHADOW_PILOT_SCHEMA = "world_shadow_pilot.v2"
WORLD_SHADOW_PILOT_PRIOR_SCHEMA = "world_shadow_pilot.v1"
WORLD_SHADOW_PILOT_CONFIG_NAME = "world_shadow_pilot.yaml"
WORLD_SHADOW_PILOT_ACTIVATION_FLAG = "CASYS_WORLD_SHADOW_PILOT_ACTIVATION"
WORLD_SHADOW_PILOT_PRIOR_ID = "world_shadow_pilot.v1"
_C1_LOGICAL = ("market", "status_only", "company", "macro", "joint")
_GRAPH_LOGICAL = ("graph",)
_GRU_SEQUENCE_LENGTH = 4


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _mapping(value: object, field_name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{field_name} must be a mapping")
    return {str(key): item for key, item in value.items()}


def _required_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value.strip()


def _required_bool(value: object, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise TypeError(f"{field_name} must be a bool")
    return value


@dataclass(frozen=True)
class WorldShadowPilotConfig:
    schema_version: str
    content_sha256: str
    activation_policy: str
    enabled: bool
    window: Mapping[str, Any]
    workers: Mapping[str, bool]
    runtime_identity_intent: WorldRuntimeIdentityIntent
    payload: Mapping[str, Any]


@dataclass(frozen=True)
class WorldShadowPilotActivation:
    status: str
    reason: str
    authority: str = COHORT_AUTHORITY
    decision_effect: str = COHORT_DECISION_EFFECT
    recommendation: str = COHORT_RECOMMENDATION
    causal_claim: bool = False
    pnl_claim: bool = False
    config_sha256: str | None = None
    window: Mapping[str, Any] | None = None
    cohorts: tuple[dict[str, Any], ...] = ()
    workers: Mapping[str, bool] = MappingProxyType({})
    episodes_appended: int = 0
    backfill: bool = False
    graph_cohort_id: str | None = None
    prior_cohort_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class WorldShadowPilotInvalidation:
    status: str
    reason: str
    authority: str = COHORT_AUTHORITY
    decision_effect: str = COHORT_DECISION_EFFECT
    recommendation: str = COHORT_RECOMMENDATION
    prior_cohort_ids: tuple[str, ...] = ()
    invalidated_cohort_ids: tuple[str, ...] = ()
    apply: bool = False


def world_shadow_pilot_activation_enabled(environ: Mapping[str, str] | None = None) -> bool:
    raw = (environ or {}).get(WORLD_SHADOW_PILOT_ACTIVATION_FLAG)
    if raw is None:
        return True
    try:
        return int(raw) != 0
    except (TypeError, ValueError):
        return False


def _skip(
    reason: str,
    *,
    config: WorldShadowPilotConfig | None = None,
    workers: Mapping[str, bool] | None = None,
) -> WorldShadowPilotActivation:
    resolved_workers = dict(config.workers) if config is not None else dict(workers or {})
    return WorldShadowPilotActivation(
        status="skipped",
        reason=reason,
        config_sha256=None if config is None else config.content_sha256,
        workers=MappingProxyType(resolved_workers),
    )


def _config_path(config_dir: str | Path) -> Path:
    path = Path(config_dir)
    if path.is_file():
        return path
    return path / WORLD_SHADOW_PILOT_CONFIG_NAME


def _parse_world_shadow_pilot_config(path: Path) -> WorldShadowPilotConfig:
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise TypeError("world_shadow_pilot.yaml must be a mapping")
    hashed = {str(key): value for key, value in payload.items() if key != "content_sha256"}
    claimed = _required_text(payload.get("content_sha256"), "content_sha256")
    digest = canonical_sha256(hashed)
    if digest != claimed:
        raise ValueError("content_sha256 does not match the canonical World shadow-pilot config")
    schema = _required_text(payload.get("schema_version"), "schema_version")
    if schema != WORLD_SHADOW_PILOT_SCHEMA:
        raise ValueError(f"schema_version must be {WORLD_SHADOW_PILOT_SCHEMA}")
    authority = _required_text(payload.get("authority"), "authority")
    if authority != COHORT_AUTHORITY:
        raise ValueError("authority must be shadow_only")
    decision_effect = _required_text(payload.get("decision_effect"), "decision_effect")
    if decision_effect != COHORT_DECISION_EFFECT:
        raise ValueError("decision_effect must be none")
    recommendation = _required_text(payload.get("recommendation"), "recommendation")
    if recommendation != COHORT_RECOMMENDATION:
        raise ValueError("recommendation must be NO_GO")
    if _required_bool(payload.get("causal_claim"), "causal_claim"):
        raise ValueError("causal_claim must be false")
    if _required_bool(payload.get("pnl_claim"), "pnl_claim"):
        raise ValueError("pnl_claim must be false")
    policy = _required_text(payload.get("activation_policy"), "activation_policy")
    if policy != "operator_authorized_on_boot":
        raise ValueError("activation_policy must be operator_authorized_on_boot")
    window = _mapping(payload.get("window"), "window")
    if window.get("planned_start") != "boot_event_time":
        raise ValueError("window.planned_start must be boot_event_time")
    duration = window.get("duration_days")
    if not isinstance(duration, int) or isinstance(duration, bool) or duration <= 0:
        raise ValueError("window.duration_days must be a positive int")
    if window.get("collection_stop_kind") != "fixed_end":
        raise ValueError("window.collection_stop_kind must be fixed_end")
    scope = _mapping(payload.get("scope_mapping"), "scope_mapping")
    if scope.get("mapping_id") != WORLD_SCOPE_MAPPING_ID:
        raise ValueError("scope_mapping must reuse the committed world_scope_mapping.v1")
    if scope.get("mapping_sha256") != WORLD_SCOPE_MAPPING_SHA256:
        raise ValueError("scope_mapping must reuse the committed world_scope_mapping hash")
    workers_raw = _mapping(payload.get("workers"), "workers")
    workers = {
        key: _required_bool(workers_raw.get(key), f"workers.{key}")
        for key in ("v1_shadow", "context_v2", "macro_source_only", "graph_v3")
    }
    if payload.get("runtime_identity") is not None:
        raise ValueError("runtime_identity is measured at activation and must not be committed")
    intent = WorldRuntimeIdentityIntent.from_mapping(
        _mapping(payload.get("runtime_identity_intent"), "runtime_identity_intent")
    )
    supersedes = _required_text(payload.get("supersedes_pilot_id"), "supersedes_pilot_id")
    if supersedes != WORLD_SHADOW_PILOT_PRIOR_ID:
        raise ValueError("supersedes_pilot_id must name the prior immutable pilot")
    generation = payload.get("lifecycle_generation")
    if not isinstance(generation, int) or isinstance(generation, bool) or generation < 2:
        raise ValueError("lifecycle_generation must be an int >= 2")
    return WorldShadowPilotConfig(
        schema_version=schema,
        content_sha256=digest,
        activation_policy=policy,
        enabled=_required_bool(payload.get("enabled"), "enabled"),
        window=MappingProxyType(window),
        workers=MappingProxyType(workers),
        runtime_identity_intent=intent,
        payload=MappingProxyType(dict(payload)),
    )


def load_world_shadow_pilot_config(config_dir: str | Path) -> WorldShadowPilotConfig | None:
    path = _config_path(config_dir)
    try:
        if not path.is_file():
            return None
        return _parse_world_shadow_pilot_config(path)
    except Exception:  # noqa: BLE001 - missing/invalid config cannot block trading
        return None


def _try_load(service: WorldCohortService, cohort_id: str) -> WorldCohort | None:
    try:
        return service.repository.load(WorldCohortId(cohort_id))
    except LookupError:
        return None


def _logical_name(lane_id: str) -> str:
    return lane_id.rsplit(".", 1)[-1]


def _role_for(lane_id: str, *, graph: bool) -> str:
    family, logical = lane_id.split(".", 1)
    if family == "gru" and not graph:
        return "secondary_challenger"
    if graph:
        return "primary_control" if family == "markov" else "pilot_treatment"
    if logical == "market":
        return "primary_control"
    if logical == "joint":
        return "pilot_treatment"
    return "process_control"


def _encoder_kind(logical: str, *, family: str) -> str:
    if logical != "graph":
        return logical
    return "topology_status_only" if family == "markov" else "graph_content"


def _lane(family: str, logical: str) -> WorldLaneDefinition:
    profile = world_lane_encoder_profile(_encoder_kind(logical, family=family))
    lane_id = f"{family}.{logical}"
    sequence_length = None if family == "markov" else _GRU_SEQUENCE_LENGTH
    return WorldLaneDefinition(
        lane_id=lane_id,
        model_family=family,
        model_id=MARKOV_MODEL_ID if family == "markov" else GRU_MODEL_ID,
        model_version=f"cohort.{logical}.{family}.v1",
        feature_contract_id=profile.contract.contract_id,
        feature_contract_fingerprint=profile.contract.fingerprint,
        feature_mask_id=profile.mask.mask_id,
        feature_mask_fingerprint=profile.mask.fingerprint,
        seed=0,
        sequence_length=sequence_length,
        hyperparameters_sha256=canonical_sha256(
            {
                "family": family,
                "lane_id": lane_id,
                "sequence_length": sequence_length,
                "seed": 0,
            }
        ),
        role=_role_for(lane_id, graph=logical == "graph"),
    )


def _contrasts(lanes: Sequence[WorldLaneDefinition]) -> tuple[WorldContrastDefinition, ...]:
    ids = {lane.lane_id for lane in lanes}
    specs: list[tuple[str, str, str, str]] = []
    if "markov.market" in ids and "markov.status_only" in ids:
        specs.append(("markov.status_only_minus_market.v1", "markov.status_only", "markov.market", "pipeline_control"))
    if "markov.company" in ids and "markov.status_only" in ids:
        specs.append(
            ("markov.company_minus_status_only.v1", "markov.company", "markov.status_only", "pipeline_control")
        )
    if "markov.macro" in ids and "markov.status_only" in ids:
        specs.append(("markov.macro_minus_status_only.v1", "markov.macro", "markov.status_only", "pipeline_control"))
    if "markov.joint" in ids and "markov.status_only" in ids:
        specs.append(("markov.joint_minus_status_only.v1", "markov.joint", "markov.status_only", "pilot_treatment"))
    if "gru.graph" in ids and "markov.graph" in ids:
        specs.append(("gru.graph_minus_markov.graph.v1", "gru.graph", "markov.graph", "pilot_treatment"))
    return tuple(
        WorldContrastDefinition(
            contrast_id=contrast_id,
            terms=(
                WorldContrastTerm(lane_id=positive, coefficient=1),
                WorldContrastTerm(lane_id=negative, coefficient=-1),
            ),
            primary_metric="paired_multiclass_log_loss",
            role=role,
        )
        for contrast_id, positive, negative, role in specs
    )


def _sensors(lanes: Sequence[WorldLaneDefinition]) -> tuple[WorldSensorRequirement, ...]:
    ids = tuple(lane.lane_id for lane in lanes)
    sensors: list[WorldSensorRequirement] = []
    company_lanes = tuple(lane_id for lane_id in ids if _logical_name(lane_id) in {"company", "joint"})
    if company_lanes:
        sensors.append(
            WorldSensorRequirement(
                sensor_id="company",
                source_contract_id="company_intelligence_brief.v1",
                projection_contract_id="company_context_projection.v1",
                mode="required",
                lane_ids=company_lanes,
            )
        )
    macro_lanes = tuple(lane_id for lane_id in ids if _logical_name(lane_id) in {"macro", "joint"})
    if macro_lanes:
        sensors.append(
            WorldSensorRequirement(
                sensor_id="macro",
                source_contract_id="macro_world_observation.v1",
                projection_contract_id="macro_context_projection.v1",
                mode="required",
                lane_ids=macro_lanes,
            )
        )
    graph_lanes = tuple(lane_id for lane_id in ids if _logical_name(lane_id) == "graph")
    if graph_lanes:
        sensors.append(
            WorldSensorRequirement(
                sensor_id="graph",
                source_contract_id=GRAPH_FEATURE_CONTRACT_VERSION,
                projection_contract_id="graph_v3_projection.v1",
                mode="required",
                lane_ids=graph_lanes,
            )
        )
    return tuple(sensors)


def _legacy_stable_cohort_id(pilot_id: str, schema_version: str, key: str, activation_policy: str) -> str:
    return "world_cohort:v1:" + canonical_sha256(
        {
            "pilot_id": pilot_id,
            "schema_version": schema_version,
            "cohort_key": key,
            "activation_policy": activation_policy,
        }
    )


def _stable_cohort_id(config: WorldShadowPilotConfig, key: str) -> str:
    return "world_cohort:v1:" + canonical_sha256(
        {
            "pilot_id": config.payload.get("pilot_id") or WORLD_SHADOW_PILOT_SCHEMA,
            "schema_version": config.schema_version,
            "cohort_key": key,
            "activation_policy": config.activation_policy,
            "config_sha256": config.content_sha256,
            "lifecycle_generation": config.payload.get("lifecycle_generation"),
        }
    )


def _window_for(config: WorldShadowPilotConfig, now: datetime) -> tuple[datetime, datetime]:
    start = _utc(now)
    days = int(config.window["duration_days"])
    return start, start + timedelta(days=days)


def _materialize_manifest(
    config: WorldShadowPilotConfig,
    spec: Mapping[str, Any],
    *,
    cohort_id: str,
    planned_start: datetime,
    stop_at: datetime,
    runtime_identity: WorldRuntimeIdentity,
) -> WorldCohortManifest:
    key = _required_text(spec.get("key"), "cohorts[].key")
    families = tuple(_required_text(item, "families[]") for item in spec.get("families") or ())
    logicals = tuple(_required_text(item, "logical_lanes[]") for item in spec.get("logical_lanes") or ())
    if not families or not logicals:
        raise ValueError(f"{key} must declare families and logical_lanes")
    if "graph" in logicals:
        if tuple(logicals) != _GRAPH_LOGICAL:
            raise ValueError("graph cohort must keep V3 graph lanes off technical C1")
    elif tuple(logicals) != _C1_LOGICAL:
        raise ValueError("technical C1 must declare market/status_only/company/macro/joint")
    lanes = tuple(_lane(family, logical) for family in families for logical in logicals)
    graph = "graph" in logicals
    context_contract = (
        GRAPH_FEATURE_CONTRACT_VERSION
        if graph
        else _required_text(config.payload.get("context_feature_contract"), "context_feature_contract")
    )
    if graph:
        ontology_revision = _required_text(
            spec.get("ontology_revision") or WORLD_GRAPH_V3_ONTOLOGY_REVISION,
            "ontology_revision",
        )
        if ontology_revision != WORLD_GRAPH_V3_ONTOLOGY_REVISION:
            raise ValueError("graph cohort ontology_revision must be market_ontology.v1")
    else:
        ontology_revision = _required_text(
            spec.get("ontology_revision") or config.payload.get("ontology_revision"),
            "ontology_revision",
        )
    return WorldCohortManifest(
        cohort_id=cohort_id,
        study_kind=_required_text(spec.get("study_kind"), "study_kind"),
        created_at=planned_start,
        question=_required_text(spec.get("question"), "question"),
        planned_start_not_before=planned_start,
        collection_stop_rule={"kind": "fixed_end", "at": stop_at},
        venues=tuple(config.payload.get("venues") or ()),
        bar_interval=_required_text(config.payload.get("bar_interval"), "bar_interval"),
        horizons=tuple(config.payload.get("horizons") or ()),
        primary_horizon=_required_text(config.payload.get("primary_horizon"), "primary_horizon"),
        label_contract=_required_text(config.payload.get("label_contract"), "label_contract"),
        sampling_policy_version=_required_text(
            config.payload.get("sampling_policy_version"),
            "sampling_policy_version",
        ),
        market_feature_contract=_required_text(
            config.payload.get("market_feature_contract"),
            "market_feature_contract",
        ),
        context_feature_contract=context_contract,
        ontology_revision=ontology_revision,
        scope_mapping=dict(config.payload["scope_mapping"]),
        sensor_requirements=_sensors(lanes),
        lanes=lanes,
        contrasts=_contrasts(lanes),
        statistical_protocol=dict(config.payload["statistical_protocol"]),
        support_gates=dict(config.payload["support_gates"]),
        runtime_identity=runtime_identity,
        authority=COHORT_AUTHORITY,
        decision_effect=COHORT_DECISION_EFFECT,
        causal_claim=False,
        pnl_claim=False,
    )


def _graph_ontology_proof(
    manifest: WorldCohortManifest,
    *,
    ontology_proof: WorldOntologyProofQuery | None,
    now: datetime,
) -> WorldOntologyHeadsProof | None:
    if ontology_proof is None or manifest.scope_mapping is None:
        return None
    return ontology_proof.proven_heads(
        revision_id=manifest.ontology_revision,
        scope_mapping_id=manifest.scope_mapping.mapping_id,
        scope_mapping_hash=manifest.scope_mapping.mapping_sha256,
        at=now,
    )


def _block_lanes_for_drift(service: WorldCohortService, cohort: WorldCohort) -> WorldCohort:
    if cohort.phase is not CohortPhase.COLLECTING:
        return cohort
    for lane in cohort.manifest.lanes:
        try:
            service.record_config_drift(cohort.cohort_id, lane_id=lane.lane_id)
        except Exception:  # noqa: BLE001 - a blocked lane must not raise into activation
            continue
    loaded = _try_load(service, cohort.cohort_id)
    return cohort if loaded is None else loaded


def _event_id(cohort: WorldCohort, event_type: str) -> str | None:
    matches = [event for event in cohort.events if event.event_type == event_type]
    return None if not matches else matches[-1].event_id


def _cohort_report(
    *,
    key: str,
    cohort: WorldCohort,
    registered_event_id: str,
    armed_event_id: str | None,
    started_event_id: str | None,
    already_present: bool,
    reason: str,
    blocked_reason: str | None = None,
) -> dict[str, Any]:
    return {
        "key": key,
        "cohort_id": cohort.cohort_id,
        "phase": cohort.phase.value,
        "manifest_sha256": cohort.manifest.manifest_sha256,
        "planned_start_not_before": cohort.manifest.planned_start_not_before,
        "collection_stop_at": cohort.manifest.collection_stop_rule.at,
        "ontology_revision": cohort.manifest.ontology_revision,
        "registered_event_id": registered_event_id,
        "armed_event_id": armed_event_id,
        "started_event_id": started_event_id,
        "already_present": already_present,
        "reason": reason,
        "blocked_reason": blocked_reason,
    }


def _activate_one(
    service: WorldCohortService,
    config: WorldShadowPilotConfig,
    spec: Mapping[str, Any],
    *,
    now: datetime,
    measured: WorldRuntimeIdentity,
    ontology_proof: WorldOntologyProofQuery | None,
) -> dict[str, Any]:
    key = _required_text(spec.get("key"), "cohorts[].key")
    cohort_id = _stable_cohort_id(config, key)
    logicals = tuple(_required_text(item, "logical_lanes[]") for item in spec.get("logical_lanes") or ())
    graph = "graph" in logicals
    existing = _try_load(service, cohort_id)
    if existing is not None:
        if existing.manifest.runtime_identity != measured:
            drifted = _block_lanes_for_drift(service, existing)
            return _cohort_report(
                key=key,
                cohort=drifted,
                registered_event_id=drifted.events[0].event_id,
                armed_event_id=_event_id(drifted, "world_cohort_armed"),
                started_event_id=None if drifted.started_event is None else drifted.started_event.event_id,
                already_present=True,
                reason="runtime_identity_drift",
                blocked_reason="runtime_identity_drift",
            )
        manifest = existing.manifest
    else:
        planned_start, stop_at = _window_for(config, now)
        manifest = _materialize_manifest(
            config,
            spec,
            cohort_id=cohort_id,
            planned_start=planned_start,
            stop_at=stop_at,
            runtime_identity=measured,
        )
    registered = service.register(RegisterWorldCohort(manifest=manifest))
    loaded = service.repository.load(WorldCohortId(manifest.cohort_id))
    blocked_reason = None
    if graph:
        proof = _graph_ontology_proof(loaded.manifest, ontology_proof=ontology_proof, now=now)
        if proof is None:
            blocked_reason = "graph_ontology_unpublished"
            return _cohort_report(
                key=key,
                cohort=loaded,
                registered_event_id=registered.event.event_id,
                armed_event_id=_event_id(loaded, "world_cohort_armed"),
                started_event_id=None if loaded.started_event is None else loaded.started_event.event_id,
                already_present=existing is not None,
                reason="graph_ontology_unpublished",
                blocked_reason=blocked_reason,
            )
    required = tuple(item.sensor_id for item in loaded.manifest.sensor_requirements if item.mode is SensorMask.REQUIRED)
    armed = registered
    if loaded.phase in {CohortPhase.REGISTERED, CohortPhase.ARMED}:
        armed = service.arm(
            ArmWorldCohort(
                cohort_id=loaded.cohort_id,
                manifest_sha256=loaded.manifest.manifest_sha256,
                runtime_identity=measured,
                satisfied_sensor_ids=required,
            )
        )
        loaded = service.repository.load(WorldCohortId(manifest.cohort_id))
    started = armed
    if loaded.phase in {CohortPhase.ARMED, CohortPhase.COLLECTING}:
        started = service.start(
            StartWorldCohort(
                cohort_id=loaded.cohort_id,
                manifest_sha256=loaded.manifest.manifest_sha256,
                runtime_identity=measured,
            )
        )
        loaded = service.repository.load(WorldCohortId(manifest.cohort_id))
    return _cohort_report(
        key=key,
        cohort=loaded,
        registered_event_id=registered.event.event_id,
        armed_event_id=armed.event.event_id,
        started_event_id=started.event.event_id,
        already_present=existing is not None,
        reason="operator_authorized_on_boot",
        blocked_reason=blocked_reason,
    )


def _measure_runtime_identity(
    config: WorldShadowPilotConfig,
    *,
    config_path: Path,
    runtime_identity: WorldRuntimeIdentityPort | None,
) -> WorldRuntimeIdentity:
    if runtime_identity is not None:
        measured = runtime_identity.measure()
    else:
        measured = MeasuredWorldRuntimeIdentityService(
            repo_root=config_path.parent.parent,
            application_build_id=config.runtime_identity_intent.application_build_id,
        ).measure()
    if not isinstance(measured, WorldRuntimeIdentity):
        raise TypeError("runtime identity port must return WorldRuntimeIdentity")
    if not config.runtime_identity_intent.accepts(measured):
        raise ValueError("measured runtime identity does not match operator intent")
    return measured


def superseded_world_shadow_cohort_ids(
    config: WorldShadowPilotConfig | None = None,
    *,
    config_dir: str | Path | None = None,
) -> tuple[str, ...]:
    resolved = config
    if resolved is None and config_dir is not None:
        resolved = load_world_shadow_pilot_config(config_dir)
    keys = ("technical_c1", "graph_v3")
    policy = "operator_authorized_on_boot"
    prior_id = WORLD_SHADOW_PILOT_PRIOR_ID
    if resolved is not None:
        specs = resolved.payload.get("cohorts") or ()
        keys = tuple(
            _required_text(spec.get("key"), "cohorts[].key")
            for spec in specs
            if isinstance(spec, Mapping)
        )
        policy = resolved.activation_policy
        prior_id = _required_text(
            resolved.payload.get("supersedes_pilot_id") or WORLD_SHADOW_PILOT_PRIOR_ID,
            "supersedes_pilot_id",
        )
    return tuple(_legacy_stable_cohort_id(prior_id, WORLD_SHADOW_PILOT_PRIOR_SCHEMA, key, policy) for key in keys)


def invalidate_superseded_world_shadow_cohorts(
    *,
    cohort_service: WorldCohortService,
    now: datetime | str,
    config_dir: str | Path | None = None,
    config: WorldShadowPilotConfig | None = None,
    apply: bool = False,
) -> WorldShadowPilotInvalidation:
    """Append-only operator path for prior immutable cohort IDs. Default is dry-run."""

    clock = _utc(now if isinstance(now, datetime) else parse_utc_timestamp(now, "now"))
    resolved = config
    if resolved is None and config_dir is not None:
        resolved = load_world_shadow_pilot_config(config_dir)
    prior_ids = superseded_world_shadow_cohort_ids(resolved, config_dir=config_dir)
    if not apply:
        return WorldShadowPilotInvalidation(
            status="dry_run",
            reason="operator_confirm_required",
            prior_cohort_ids=prior_ids,
            apply=False,
        )
    invalidated: list[str] = []
    for cohort_id in prior_ids:
        existing = _try_load(cohort_service, cohort_id)
        if existing is None:
            continue
        if existing.phase is CohortPhase.COMPLETE:
            continue
        envelope = cohort_service.invalidate(
            cohort_id,
            InvalidateWorldCohort(
                reason=InvalidationReason.ACCEPTED_DRIFT,
                scope="prior_pilot",
                proofs=(
                    WORLD_SHADOW_PILOT_SCHEMA,
                    resolved.content_sha256 if resolved is not None else "operator_prior_invalidation",
                ),
                occurred_at=clock,
            ),
        )
        if envelope.event.event_type == "world_cohort_invalidated":
            invalidated.append(cohort_id)
    return WorldShadowPilotInvalidation(
        status="invalidated",
        reason="operator_authorized_prior_invalidation",
        prior_cohort_ids=prior_ids,
        invalidated_cohort_ids=tuple(invalidated),
        apply=True,
    )


def activate_world_shadow_pilot(
    *,
    cohort_service: WorldCohortService | None,
    config_dir: str | Path,
    now: datetime | str,
    environ: Mapping[str, str] | None = None,
    runtime_identity: WorldRuntimeIdentityPort | None = None,
    ontology_proof: WorldOntologyProofQuery | None = None,
) -> WorldShadowPilotActivation:
    """Idempotently register/arm/start approved shadow cohorts. Never backfills."""

    try:
        clock = _utc(now if isinstance(now, datetime) else parse_utc_timestamp(now, "now"))
        if not world_shadow_pilot_activation_enabled(environ):
            return _skip("env_disabled")
        path = _config_path(config_dir)
        if not path.is_file():
            return _skip("config_missing")
        try:
            config = _parse_world_shadow_pilot_config(path)
        except Exception:  # noqa: BLE001 - invalid authorization cannot block V1/Trader
            return _skip("config_invalid")
        if not config.enabled:
            return _skip("disabled", config=config)
        if cohort_service is None:
            return _skip("no_cohort_service", config=config)
        try:
            measured = _measure_runtime_identity(
                config,
                config_path=path,
                runtime_identity=runtime_identity,
            )
        except Exception:  # noqa: BLE001 - unmeasurable identity cannot block V1/Trader
            return _skip("runtime_identity_unavailable", config=config)
        specs = config.payload.get("cohorts")
        if not isinstance(specs, Sequence) or isinstance(specs, (str, bytes, bytearray)):
            return _skip("config_invalid", config=config)
        reports: list[dict[str, Any]] = []
        for spec in specs:
            if not isinstance(spec, Mapping):
                return _skip("config_invalid", config=config)
            reports.append(
                _activate_one(
                    cohort_service,
                    config,
                    spec,
                    now=clock,
                    measured=measured,
                    ontology_proof=ontology_proof,
                )
            )
        window = None
        if reports:
            window = MappingProxyType(
                {
                    "planned_start_not_before": reports[0]["planned_start_not_before"],
                    "collection_stop_at": reports[0]["collection_stop_at"],
                }
            )
        graph_id = next((item["cohort_id"] for item in reports if item["key"] == "graph_v3"), None)
        already = bool(reports) and all(item["already_present"] for item in reports)
        return WorldShadowPilotActivation(
            status="already_collecting" if already else "started",
            reason="operator_authorized_on_boot",
            config_sha256=config.content_sha256,
            window=window,
            cohorts=tuple(reports),
            workers=config.workers,
            graph_cohort_id=graph_id,
            prior_cohort_ids=superseded_world_shadow_cohort_ids(config),
        )
    except Exception:  # noqa: BLE001 - activation cannot raise into the trader loop
        return _skip("activation_error")


__all__ = [
    "WORLD_SHADOW_PILOT_ACTIVATION_FLAG",
    "WORLD_SHADOW_PILOT_CONFIG_NAME",
    "WORLD_SHADOW_PILOT_PRIOR_ID",
    "WORLD_SHADOW_PILOT_PRIOR_SCHEMA",
    "WORLD_SHADOW_PILOT_SCHEMA",
    "WorldShadowPilotActivation",
    "WorldShadowPilotConfig",
    "WorldShadowPilotInvalidation",
    "activate_world_shadow_pilot",
    "invalidate_superseded_world_shadow_cohorts",
    "load_world_shadow_pilot_config",
    "superseded_world_shadow_cohort_ids",
    "world_shadow_pilot_activation_enabled",
]
