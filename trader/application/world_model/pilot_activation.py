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
from trader.application.world_model.cohort_service import WorldCohortService
from trader.application.world_model.encoding import world_lane_encoder_profile
from trader.application.world_model.gru import MODEL_ID as GRU_MODEL_ID
from trader.domain.world_cohort import (
    COHORT_AUTHORITY,
    COHORT_DECISION_EFFECT,
    COHORT_RECOMMENDATION,
    ArmWorldCohort,
    CohortPhase,
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
    WorldSensorRequirement,
)
from trader.domain.world_episode import canonical_sha256, parse_utc_timestamp
from trader.domain.world_feature_contract import (
    GRAPH_FEATURE_CONTRACT_VERSION,
    WORLD_SCOPE_MAPPING_ID,
    WORLD_SCOPE_MAPPING_SHA256,
)


WORLD_SHADOW_PILOT_SCHEMA = "world_shadow_pilot.v1"
WORLD_SHADOW_PILOT_CONFIG_NAME = "world_shadow_pilot.yaml"
WORLD_SHADOW_PILOT_ACTIVATION_FLAG = "CASYS_WORLD_SHADOW_PILOT_ACTIVATION"
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
    return WorldShadowPilotConfig(
        schema_version=schema,
        content_sha256=digest,
        activation_policy=policy,
        enabled=_required_bool(payload.get("enabled"), "enabled"),
        window=MappingProxyType(window),
        workers=MappingProxyType(workers),
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


def _stable_cohort_id(config: WorldShadowPilotConfig, key: str) -> str:
    return "world_cohort:v1:" + canonical_sha256(
        {
            "pilot_id": config.payload.get("pilot_id") or WORLD_SHADOW_PILOT_SCHEMA,
            "schema_version": config.schema_version,
            "cohort_key": key,
            "activation_policy": config.activation_policy,
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
        ontology_revision=_required_text(config.payload.get("ontology_revision"), "ontology_revision"),
        scope_mapping=dict(config.payload["scope_mapping"]),
        sensor_requirements=_sensors(lanes),
        lanes=lanes,
        contrasts=_contrasts(lanes),
        statistical_protocol=dict(config.payload["statistical_protocol"]),
        support_gates=dict(config.payload["support_gates"]),
        runtime_identity=WorldRuntimeIdentity.from_mapping(dict(config.payload["runtime_identity"])),
        authority=COHORT_AUTHORITY,
        decision_effect=COHORT_DECISION_EFFECT,
        causal_claim=False,
        pnl_claim=False,
    )


def _activate_one(
    service: WorldCohortService,
    config: WorldShadowPilotConfig,
    spec: Mapping[str, Any],
    *,
    now: datetime,
) -> dict[str, Any]:
    key = _required_text(spec.get("key"), "cohorts[].key")
    cohort_id = _stable_cohort_id(config, key)
    existing = _try_load(service, cohort_id)
    if existing is not None:
        manifest = existing.manifest
    else:
        planned_start, stop_at = _window_for(config, now)
        manifest = _materialize_manifest(
            config,
            spec,
            cohort_id=cohort_id,
            planned_start=planned_start,
            stop_at=stop_at,
        )
    registered = service.register(RegisterWorldCohort(manifest=manifest))
    loaded = service.repository.load(WorldCohortId(manifest.cohort_id))
    required = tuple(item.sensor_id for item in loaded.manifest.sensor_requirements if item.mode is SensorMask.REQUIRED)
    armed = registered
    if loaded.phase in {CohortPhase.REGISTERED, CohortPhase.ARMED}:
        armed = service.arm(
            ArmWorldCohort(
                cohort_id=loaded.cohort_id,
                manifest_sha256=loaded.manifest.manifest_sha256,
                runtime_identity=loaded.manifest.runtime_identity,
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
                runtime_identity=loaded.manifest.runtime_identity,
            )
        )
        loaded = service.repository.load(WorldCohortId(manifest.cohort_id))
    return {
        "key": key,
        "cohort_id": loaded.cohort_id,
        "phase": loaded.phase.value,
        "manifest_sha256": loaded.manifest.manifest_sha256,
        "planned_start_not_before": loaded.manifest.planned_start_not_before,
        "collection_stop_at": loaded.manifest.collection_stop_rule.at,
        "registered_event_id": registered.event.event_id,
        "armed_event_id": armed.event.event_id,
        "started_event_id": started.event.event_id,
        "already_present": existing is not None,
    }


def activate_world_shadow_pilot(
    *,
    cohort_service: WorldCohortService | None,
    config_dir: str | Path,
    now: datetime | str,
    environ: Mapping[str, str] | None = None,
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
        specs = config.payload.get("cohorts")
        if not isinstance(specs, Sequence) or isinstance(specs, (str, bytes, bytearray)):
            return _skip("config_invalid", config=config)
        reports: list[dict[str, Any]] = []
        for spec in specs:
            if not isinstance(spec, Mapping):
                return _skip("config_invalid", config=config)
            reports.append(_activate_one(cohort_service, config, spec, now=clock))
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
        )
    except Exception:  # noqa: BLE001 - activation cannot raise into the trader loop
        return _skip("activation_error")


__all__ = [
    "WORLD_SHADOW_PILOT_ACTIVATION_FLAG",
    "WORLD_SHADOW_PILOT_CONFIG_NAME",
    "WORLD_SHADOW_PILOT_SCHEMA",
    "WorldShadowPilotActivation",
    "WorldShadowPilotConfig",
    "activate_world_shadow_pilot",
    "load_world_shadow_pilot_config",
    "world_shadow_pilot_activation_enabled",
]
