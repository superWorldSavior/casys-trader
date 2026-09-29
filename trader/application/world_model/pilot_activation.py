"""Operator-authorized World Model shadow-pilot activation.

RFC default is no implicit activation: register/arm/start stay explicit CLI
commands. Human override: ``activation_policy=operator_authorized_on_boot``
makes a normal daemon start idempotently register, arm, and start the approved
shadow cohort(s) and workers. The versioned config is the authorization; this
is not an implicit runtime default.

No backfill. No trade decision effect. Idle cycles may still write no episode.
Fail-open if the config is missing or invalid. ``CASYS_WORLD_SHADOW_PILOT_ACTIVATION=0``
skips register/arm/start while leaving market shadow.

The pilot never derives the graph ontology revision itself: the caller passes
the revision the ontology attestation committed (``ensure_published``), and
the pilot pins exactly that. A missing pin blocks the graph cohort loudly
(``graph_ontology_unpinned``) instead of registering a cohort that can never
prove its heads.
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
from trader.application.world_model.encoding import (
    PredictorIdentityCollisionError,
    world_lane_encoder_profile,
)
from trader.application.world_model.gru import MODEL_ID as GRU_MODEL_ID
from trader.application.world_model.runtime_identity import MeasuredWorldRuntimeIdentityService
from trader.application.world_model.scope_mapping_ports import WorldScopeMappingGenerationRepository
from trader.domain.world_cohort import (
    COHORT_AUTHORITY,
    COHORT_DECISION_EFFECT,
    COHORT_RECOMMENDATION,
    ArmWorldCohort,
    CloseWorldCohort,
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
    is_live_same_shape_mapping_cohort,
    is_same_shape_mapping_cohort,
    world_cohort_lane_signature,
)
from trader.domain.world_episode import SUPPORTED_WORLD_HORIZONS, canonical_sha256, parse_utc_timestamp
from trader.domain.world_feature_contract import (
    CONTEXT_FEATURE_CONTRACT_ID,
    GRAPH_FEATURE_CONTRACT_ID,
    MARKET_ONTOLOGY_REVISION,
    WORLD_SCOPE_MAPPING_ID,
)
from trader.domain.world_ontology_lifecycle import (
    admits_market_ontology_family,
    is_market_ontology_revision_instance,
)
from trader.domain.world_scope import WorldScopeMapping
from trader.domain.world_macro import (
    MACRO_LANE_IDENTITY,
    MACRO_PRODUCER_VERSION,
    WORLD_MACRO_COLLECTION_PLAN_ID,
    WORLD_MACRO_COLLECTION_PLAN_SHA256,
)


WORLD_SHADOW_PILOT_SCHEMA = "world_shadow_pilot.v1"
WORLD_SHADOW_PILOT_PRIOR_SCHEMA = "world_shadow_pilot.v1"
WORLD_SHADOW_PILOT_CONFIG_NAME = "world_shadow_pilot.yaml"
WORLD_SHADOW_PILOT_ACTIVATION_FLAG = "CASYS_WORLD_SHADOW_PILOT_ACTIVATION"
WORLD_SHADOW_PILOT_PRIOR_ID = "world_shadow_pilot.v1"
_C1_LOGICAL = ("market", "status_only", "company", "macro", "joint")
_GRAPH_LOGICAL = ("graph", "joint")
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
    raw_horizons = payload.get("horizons")
    if isinstance(raw_horizons, (str, bytes)) or not isinstance(raw_horizons, Sequence):
        raise TypeError("horizons must be a sequence")
    horizons = tuple(_required_text(item, "horizons[]") for item in raw_horizons)
    if not horizons or len(set(horizons)) != len(horizons):
        raise ValueError("horizons must be non-empty and unique")
    supported_horizons = {item.horizon_id for item in SUPPORTED_WORLD_HORIZONS}
    unsupported = sorted(set(horizons) - supported_horizons)
    if unsupported:
        raise ValueError(f"unsupported World shadow-pilot horizons: {', '.join(unsupported)}")
    primary_horizon = _required_text(payload.get("primary_horizon"), "primary_horizon")
    if primary_horizon not in horizons:
        raise ValueError("primary_horizon must be one of horizons")
    scope = _mapping(payload.get("scope_mapping"), "scope_mapping")
    if scope.get("mapping_id") != WORLD_SCOPE_MAPPING_ID:
        raise ValueError(f"scope_mapping must reuse the committed {WORLD_SCOPE_MAPPING_ID}")
    macro = _mapping(payload.get("macro_producer"), "macro_producer")
    if macro.get("producer_version") != MACRO_PRODUCER_VERSION:
        raise ValueError(f"macro_producer.producer_version must be {MACRO_PRODUCER_VERSION}")
    if macro.get("lane_identity") != MACRO_LANE_IDENTITY:
        raise ValueError(f"macro_producer.lane_identity must be {MACRO_LANE_IDENTITY}")
    if macro.get("collection_plan_id") != WORLD_MACRO_COLLECTION_PLAN_ID:
        raise ValueError("macro_producer.collection_plan_id must match the committed source-backed plan")
    if macro.get("collection_plan_sha256") != WORLD_MACRO_COLLECTION_PLAN_SHA256:
        raise ValueError("macro_producer.collection_plan_sha256 must match the committed source-backed plan")
    workers_raw = _mapping(payload.get("workers"), "workers")
    workers = {
        key: _required_bool(workers_raw.get(key), f"workers.{key}")
        for key in ("market", "context", "macro_source", "graph")
    }
    if payload.get("runtime_identity") is not None:
        raise ValueError("runtime_identity is measured at activation and must not be committed")
    if payload.get("supersedes_pilot_id") is not None:
        raise ValueError("current world_shadow_pilot.v1 must not declare supersedes_pilot_id")
    intent = WorldRuntimeIdentityIntent.from_mapping(
        _mapping(payload.get("runtime_identity_intent"), "runtime_identity_intent")
    )
    generation = payload.get("lifecycle_generation")
    if generation is not None and (not isinstance(generation, int) or isinstance(generation, bool) or generation < 1):
        raise ValueError("lifecycle_generation must be a positive int")
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


def _role_for(lane_id: str, *, graph: bool, graph_cohort: bool = False) -> str:
    family, logical = lane_id.split(".", 1)
    if graph_cohort and not graph:
        return "process_control"
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


def _lane(family: str, logical: str, *, graph_cohort: bool = False) -> WorldLaneDefinition:
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
        role=_role_for(lane_id, graph=logical == "graph", graph_cohort=graph_cohort),
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
                source_contract_id=MACRO_PRODUCER_VERSION,
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
                source_contract_id=GRAPH_FEATURE_CONTRACT_ID,
                projection_contract_id="graph_projection.v1",
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


def _stable_cohort_id(
    config: WorldShadowPilotConfig,
    key: str,
    *,
    mapping_sha256: str | None = None,
    ontology_revision: str | None = None,
    predecessor_cohort_id: str | None = None,
    predecessor_stop_at: str | None = None,
) -> str:
    payload: dict[str, Any] = {
        "pilot_id": config.payload.get("pilot_id") or WORLD_SHADOW_PILOT_SCHEMA,
        "schema_version": config.schema_version,
        "cohort_key": key,
        "activation_policy": config.activation_policy,
        "config_sha256": config.content_sha256,
        "lifecycle_generation": config.payload.get("lifecycle_generation"),
        "mapping_sha256": mapping_sha256,
        "ontology_revision": ontology_revision,
    }
    if predecessor_cohort_id is not None:
        payload["predecessor_cohort_id"] = predecessor_cohort_id
        payload["predecessor_stop_at"] = predecessor_stop_at
    return "world_cohort:v1:" + canonical_sha256(payload)


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
    mapping: WorldScopeMapping,
    committed_ontology_revision: str | None = None,
) -> WorldCohortManifest:
    key = _required_text(spec.get("key"), "cohorts[].key")
    families = tuple(_required_text(item, "families[]") for item in spec.get("families") or ())
    logicals = tuple(_required_text(item, "logical_lanes[]") for item in spec.get("logical_lanes") or ())
    if not families or not logicals:
        raise ValueError(f"{key} must declare families and logical_lanes")
    if "graph" in logicals:
        if tuple(logicals) != _GRAPH_LOGICAL:
            raise ValueError("graph cohort must declare exactly graph/joint lanes")
    elif tuple(logicals) != _C1_LOGICAL:
        raise ValueError("technical C1 must declare market/status_only/company/macro/joint")
    graph = "graph" in logicals
    # The pattern study pairs its graph treatment against exactly one
    # flat-context control: the stable markov baseline. A second context
    # lane would make every control slot ambiguous in the report.
    lanes = tuple(
        _lane(family, logical, graph_cohort=graph)
        for family, logical in _lane_pairs_for_spec(families, logicals)
    )
    if graph:
        context_lanes = tuple(
            lane.lane_id
            for lane in lanes
            if lane.feature_contract_id == CONTEXT_FEATURE_CONTRACT_ID
        )
        if len(context_lanes) != 1:
            raise PredictorIdentityCollisionError(
                code="graph_cohort_context_lane_count",
                context={"cohort_key": key, "context_lanes": list(context_lanes)},
                recovery="declare families including markov so the graph cohort mints exactly markov.joint",
            )
    if graph:
        # Declared contracts gate lane contracts: the pattern cohort pairs
        # graph treatment lanes with a joint-context control lane.
        market_contract = GRAPH_FEATURE_CONTRACT_ID
        context_contract = _required_text(
            config.payload.get("context_feature_contract"), "context_feature_contract"
        )
    else:
        market_contract = _required_text(
            config.payload.get("market_feature_contract"), "market_feature_contract"
        )
        context_contract = _required_text(
            config.payload.get("context_feature_contract"), "context_feature_contract"
        )
    if graph:
        declared = _required_text(
            spec.get("ontology_revision") or MARKET_ONTOLOGY_REVISION,
            "ontology_revision",
        )
        if declared != MARKET_ONTOLOGY_REVISION and not admits_market_ontology_family(declared):
            raise ValueError(f"graph cohort ontology_revision must be {MARKET_ONTOLOGY_REVISION}")
        if committed_ontology_revision is None or not str(committed_ontology_revision).strip():
            raise ValueError("graph cohort requires the committed ontology revision")
        ontology_revision = _required_text(committed_ontology_revision, "committed_ontology_revision")
        if not is_market_ontology_revision_instance(ontology_revision):
            raise ValueError("graph cohort ontology_revision must be a market ontology revision instance")
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
        market_feature_contract=market_contract,
        context_feature_contract=context_contract,
        ontology_revision=ontology_revision,
        scope_mapping={
            "mapping_id": mapping.mapping_id,
            "mapping_sha256": mapping.content_sha256,
        },
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


def _lane_pairs_for_spec(
    families: tuple[str, ...], logicals: tuple[str, ...]
) -> tuple[tuple[str, str], ...]:
    graph = "graph" in logicals
    return tuple(
        (family, logical)
        for family in families
        for logical in logicals
        if not (graph and logical == "joint" and family != "markov")
    )


def _lane_signature_for_spec(spec: Mapping[str, Any]) -> tuple[str, ...]:
    families = tuple(_required_text(item, "families[]") for item in spec.get("families") or ())
    logicals = tuple(_required_text(item, "logical_lanes[]") for item in spec.get("logical_lanes") or ())
    return world_cohort_lane_signature(
        tuple(
            _lane(family, logical, graph_cohort="graph" in logicals)
            for family, logical in _lane_pairs_for_spec(families, logicals)
        )
    )


def _persist_mapping_generation(
    mapping_generations: WorldScopeMappingGenerationRepository | None,
    mapping: WorldScopeMapping,
) -> None:
    if mapping_generations is None:
        return
    mapping_generations.persist_mapping_generation(mapping)


def _collection_window_elapsed(cohort: WorldCohort, now: datetime) -> bool:
    """Inclusive end: the window is still open at collection_stop_rule.at."""

    return _utc(now) > cohort.manifest.collection_stop_rule.at


def _same_shape_operator_cohorts(
    service: WorldCohortService,
    config: WorldShadowPilotConfig,
    spec: Mapping[str, Any],
    *,
    mapping: WorldScopeMapping,
) -> list[WorldCohort]:
    key = _required_text(spec.get("key"), "cohorts[].key")
    study_kind = _required_text(spec.get("study_kind"), "cohorts[].study_kind")
    question = _required_text(spec.get("question"), "cohorts[].question")
    signature = _lane_signature_for_spec(spec)
    logicals = tuple(_required_text(item, "logical_lanes[]") for item in spec.get("logical_lanes") or ())
    graph = "graph" in logicals
    matches: list[WorldCohort] = []
    for cohort in service.query.list_live_cohorts():
        if not is_live_same_shape_mapping_cohort(
            cohort,
            mapping_id=mapping.mapping_id,
            lane_signature=signature,
            study_kind=study_kind,
            question=question,
        ):
            continue
        pin = cohort.manifest.scope_mapping
        if pin is None:
            continue
        if not _operator_lifecycle_contains(
            service,
            config,
            key,
            mapping_sha256=pin.mapping_sha256,
            ontology_pin=cohort.manifest.ontology_revision if graph else None,
            cohort_id=cohort.cohort_id,
        ):
            continue
        matches.append(cohort)
    matches.sort(key=lambda item: (item.manifest.planned_start_not_before, item.cohort_id))
    return matches


def _reusable_same_shape_cohort(
    service: WorldCohortService,
    config: WorldShadowPilotConfig,
    spec: Mapping[str, Any],
    *,
    mapping: WorldScopeMapping,
    now: datetime,
) -> WorldCohort | None:
    """Return the live same-shape cohort of this operator lifecycle, pinned to its mapping."""

    matches = [
        cohort
        for cohort in _same_shape_operator_cohorts(service, config, spec, mapping=mapping)
        if not _collection_window_elapsed(cohort, now)
    ]
    if not matches:
        return None
    return matches[0]


def _close_expired_same_shape_cohorts(
    service: WorldCohortService,
    config: WorldShadowPilotConfig,
    spec: Mapping[str, Any],
    *,
    mapping: WorldScopeMapping,
    ontology_pin: str | None,
    now: datetime,
) -> None:
    study_kind = _required_text(spec.get("study_kind"), "cohorts[].study_kind")
    question = _required_text(spec.get("question"), "cohorts[].question")
    signature = _lane_signature_for_spec(spec)
    for cohort in service.query.list_live_cohorts():
        if cohort.phase is not CohortPhase.COLLECTING:
            continue
        if not _collection_window_elapsed(cohort, now):
            continue
        if cohort.manifest.scope_mapping is None:
            continue
        if not is_same_shape_mapping_cohort(
            cohort,
            mapping_id=mapping.mapping_id,
            lane_signature=signature,
            study_kind=study_kind,
            question=question,
        ):
            continue
        service.close(cohort.cohort_id, CloseWorldCohort(reason="fixed_end reached"))
    key = _required_text(spec.get("key"), "cohorts[].key")
    current_id = _cohort_id_after_closed_predecessors(
        service,
        config,
        key,
        mapping=mapping,
        ontology_pin=ontology_pin,
    )
    current = _try_load(service, current_id)
    if (
        current is not None
        and current.phase is CohortPhase.COLLECTING
        and _collection_window_elapsed(current, now)
    ):
        service.close(current.cohort_id, CloseWorldCohort(reason="fixed_end reached"))


def _successor_id_after(
    config: WorldShadowPilotConfig,
    key: str,
    *,
    mapping_sha256: str,
    ontology_pin: str | None,
    predecessor: WorldCohort,
) -> str:
    return _stable_cohort_id(
        config,
        key,
        mapping_sha256=mapping_sha256,
        ontology_revision=ontology_pin,
        predecessor_cohort_id=predecessor.cohort_id,
        predecessor_stop_at=predecessor.manifest.collection_stop_rule.at.isoformat(),
    )


def _operator_lifecycle_contains(
    service: WorldCohortService,
    config: WorldShadowPilotConfig,
    key: str,
    *,
    mapping_sha256: str,
    ontology_pin: str | None,
    cohort_id: str,
) -> bool:
    """True when cohort_id is the origin id or a closed-predecessor successor of this pin."""

    cursor = _stable_cohort_id(
        config,
        key,
        mapping_sha256=mapping_sha256,
        ontology_revision=ontology_pin,
    )
    seen: set[str] = set()
    while cursor not in seen:
        seen.add(cursor)
        if cursor == cohort_id:
            return True
        existing = _try_load(service, cursor)
        if existing is None or existing.phase is not CohortPhase.COLLECTION_CLOSED:
            return False
        cursor = _successor_id_after(
            config,
            key,
            mapping_sha256=mapping_sha256,
            ontology_pin=ontology_pin,
            predecessor=existing,
        )
    return False


def _cohort_id_after_closed_predecessors(
    service: WorldCohortService,
    config: WorldShadowPilotConfig,
    key: str,
    *,
    mapping: WorldScopeMapping,
    ontology_pin: str | None,
) -> str:
    cohort_id = _stable_cohort_id(
        config,
        key,
        mapping_sha256=mapping.content_sha256,
        ontology_revision=ontology_pin,
    )
    seen: set[str] = set()
    while cohort_id not in seen:
        seen.add(cohort_id)
        existing = _try_load(service, cohort_id)
        if existing is None or existing.phase is not CohortPhase.COLLECTION_CLOSED:
            return cohort_id
        cohort_id = _successor_id_after(
            config,
            key,
            mapping_sha256=mapping.content_sha256,
            ontology_pin=ontology_pin,
            predecessor=existing,
        )
    return cohort_id


def _unpinned_graph_report(*, key: str) -> dict[str, Any]:
    """Blocked report when the attestation committed nothing to pin. No cohort exists."""

    return {
        "key": key,
        "cohort_id": None,
        "phase": None,
        "manifest_sha256": None,
        "planned_start_not_before": None,
        "collection_stop_at": None,
        "ontology_revision": None,
        "registered_event_id": None,
        "armed_event_id": None,
        "started_event_id": None,
        "already_present": False,
        "reason": "graph_ontology_unpinned",
        "blocked_reason": "graph_ontology_unpinned",
    }


def _activate_one(
    service: WorldCohortService,
    config: WorldShadowPilotConfig,
    spec: Mapping[str, Any],
    *,
    now: datetime,
    measured: WorldRuntimeIdentity,
    ontology_proof: WorldOntologyProofQuery | None,
    ontology_revision: str | None,
    mapping: WorldScopeMapping,
) -> dict[str, Any]:
    key = _required_text(spec.get("key"), "cohorts[].key")
    logicals = tuple(_required_text(item, "logical_lanes[]") for item in spec.get("logical_lanes") or ())
    graph = "graph" in logicals
    if graph:
        if ontology_revision is None or not str(ontology_revision).strip():
            return _unpinned_graph_report(key=key)
        ontology_pin: str | None = _required_text(ontology_revision, "ontology_revision")
        if not is_market_ontology_revision_instance(ontology_pin):
            raise ValueError("graph cohort ontology_revision must be a market ontology revision instance")
    else:
        ontology_pin = None
    _close_expired_same_shape_cohorts(
        service, config, spec, mapping=mapping, ontology_pin=ontology_pin, now=now
    )
    reusable = _reusable_same_shape_cohort(service, config, spec, mapping=mapping, now=now)
    if (
        reusable is not None
        and graph
        and reusable.phase is not CohortPhase.COLLECTING
        and (
            reusable.manifest.scope_mapping is None
            or reusable.manifest.scope_mapping.mapping_sha256 != mapping.content_sha256
            or reusable.manifest.ontology_revision != ontology_pin
        )
    ):
        # A never-started graph cohort pinned to a stale generation must not
        # shadow a fresh one: reusing it would re-hit an unprovable pin
        # forever. Only COLLECTING cohorts stay pinned to their generation.
        reusable = None
    if reusable is not None:
        cohort_id = reusable.cohort_id
        existing = reusable
    else:
        cohort_id = _cohort_id_after_closed_predecessors(
            service,
            config,
            key,
            mapping=mapping,
            ontology_pin=ontology_pin,
        )
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
            mapping=mapping,
            committed_ontology_revision=ontology_pin,
        )
    registered = service.register(RegisterWorldCohort(manifest=manifest))
    loaded = service.repository.load(WorldCohortId(manifest.cohort_id))
    blocked_reason = None
    if graph and loaded.phase is not CohortPhase.COLLECTING:
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
    keys = ("technical_c1", "graph")
    policy = "operator_authorized_on_boot"
    prior_id = WORLD_SHADOW_PILOT_PRIOR_ID
    if resolved is not None:
        specs = resolved.payload.get("cohorts") or ()
        keys = tuple(_required_text(spec.get("key"), "cohorts[].key") for spec in specs if isinstance(spec, Mapping))
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
    ontology_revision: str | None = None,
    mapping: WorldScopeMapping | None = None,
    mapping_generations: WorldScopeMappingGenerationRepository | None = None,
) -> WorldShadowPilotActivation:
    """Idempotently register/arm/start approved shadow cohorts. Never backfills.

    ``ontology_revision`` is the revision the ontology attestation committed;
    graph cohorts pin exactly that. ``None`` blocks the graph cohort loudly
    while other cohorts still activate.
    """

    try:
        clock = _utc(now if isinstance(now, datetime) else parse_utc_timestamp(now, "now"))
        if not world_shadow_pilot_activation_enabled(environ):
            return _skip("env_disabled")
        path = _config_path(config_dir)
        if not path.is_file():
            return _skip("config_missing")
        try:
            config = _parse_world_shadow_pilot_config(path)
        except Exception:  # noqa: BLE001 - invalid authorization cannot block market/Trader
            return _skip("config_invalid")
        if not config.enabled:
            return _skip("disabled", config=config)
        if cohort_service is None:
            return _skip("no_cohort_service", config=config)
        live_mapping = mapping
        if live_mapping is None:
            try:
                from trader.application.world_model.world_scope_resolver import WorldScopeResolver

                live_mapping = WorldScopeResolver.load(path.parent).mapping
            except Exception:  # noqa: BLE001 - mapping load cannot block market/Trader
                return _skip("mapping_unavailable", config=config)
        if live_mapping.mapping_id != WORLD_SCOPE_MAPPING_ID:
            return _skip("mapping_unavailable", config=config)
        try:
            _persist_mapping_generation(mapping_generations, live_mapping)
        except Exception:  # noqa: BLE001 - mapping generation persist cannot block market/Trader
            return _skip("mapping_generation_unavailable", config=config)
        try:
            measured = _measure_runtime_identity(
                config,
                config_path=path,
                runtime_identity=runtime_identity,
            )
        except Exception:  # noqa: BLE001 - unmeasurable identity cannot block market/Trader
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
                    ontology_revision=ontology_revision,
                    mapping=live_mapping,
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
        graph_id = next((item["cohort_id"] for item in reports if item["key"] == "graph"), None)
        already = bool(reports) and all(item["already_present"] for item in reports)
        blocked = any(item.get("blocked_reason") for item in reports)
        return WorldShadowPilotActivation(
            status="blocked" if blocked else ("already_collecting" if already else "started"),
            reason="operator_authorized_on_boot",
            config_sha256=config.content_sha256,
            window=window,
            cohorts=tuple(reports),
            workers=config.workers,
            graph_cohort_id=graph_id,
            prior_cohort_ids=(),
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
