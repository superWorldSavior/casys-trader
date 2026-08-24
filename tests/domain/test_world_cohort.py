from __future__ import annotations

import ast
import inspect
import sys
from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
from typing import Any

import pytest

from tests.package_layout._helpers import REPO_ROOT, _domain_import_violations
from trader.domain.world_availability import (
    AvailabilityEvidence,
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
from trader.domain.world_feature_contract import (
    WorldFeatureMask,
    market_feature_contract,
    context_feature_contract,
)
from trader.domain.world_scope import (
    WorldCanonicalScopeRef,
    WorldMarketAnchorRef,
    WorldScopeMapping,
    WorldScopeMappingEntry,
    WorldScopeResolution,
)


UTC = timezone.utc
CREATED = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)
PLANNED_START = datetime(2026, 8, 24, 0, 0, tzinfo=UTC)
STOP_AT = datetime(2026, 10, 24, 0, 0, tzinfo=UTC)
START_READY = datetime(2026, 8, 24, 0, 5, tzinfo=UTC)
START_SEEN = datetime(2026, 8, 24, 0, 6, tzinfo=UTC)
ANCHOR_TS = datetime(2026, 8, 24, 1, 0, tzinfo=UTC)
GIT = "a" * 40
COHORT_ID = "world_cohort:v1:" + "c" * 64
MODULE_PATH = REPO_ROOT / "trader" / "domain" / "world_cohort.py"

V1 = market_feature_contract()
V2 = context_feature_contract()
MARKET_MASK = WorldFeatureMask.bind(V1, mask_id="market.v1", selected_groups=("market",))
STATUS_MASK = WorldFeatureMask.bind(V2, mask_id="status_only.v1", selected_groups=("market", "status"))
COMPANY_MASK = WorldFeatureMask.bind(V2, mask_id="company.v1", selected_groups=("market", "status", "company"))
MACRO_MASK = WorldFeatureMask.bind(V2, mask_id="macro.v1", selected_groups=("market", "status", "macro"))
JOINT_MASK = WorldFeatureMask.bind(V2, mask_id="joint.v1", selected_groups=("market", "status", "company", "macro"))


def _runtime(**overrides: object) -> Any:
    from trader.domain.world_cohort import WorldRuntimeIdentity

    values: dict[str, object] = {
        "git_commit": GIT,
        "python_version": "3.11.9",
        "numpy_version": "1.26.4",
        "application_build_id": "casys-trader.world.20260823",
    }
    values.update(overrides)
    return WorldRuntimeIdentity(**values)  # type: ignore[arg-type]


def test_runtime_identity_intent_never_carries_git_commit_and_gates_measured_identity() -> None:
    from trader.domain.world_cohort import WorldRuntimeIdentityIntent

    intent = WorldRuntimeIdentityIntent(application_build_id="casys-trader.world.shadow_pilot.v1")
    assert "git_commit" not in intent.to_dict()
    assert intent.accepts(_runtime()) is False
    matching = _runtime(application_build_id="casys-trader.world.shadow_pilot.v1")
    assert intent.accepts(matching) is True
    with pytest.raises(ValueError, match="latest"):
        WorldRuntimeIdentityIntent(application_build_id="latest")


def _lane(
    lane_id: str,
    *,
    family: str = "markov",
    contract=V1,
    mask=MARKET_MASK,
    role: str = "primary_control",
    sequence_length: int | None = None,
    seed: int = 0,
    version: str | None = None,
) -> Any:
    from trader.domain.world_cohort import WorldLaneDefinition

    return WorldLaneDefinition(
        lane_id=lane_id,
        model_family=family,
        model_id="hierarchical_dirichlet_world_baseline" if family == "markov" else "world_gru.v1",
        model_version=version or f"cohort.{lane_id}.v1",
        feature_contract_id=contract.contract_id,
        feature_contract_fingerprint=contract.fingerprint,
        feature_mask_id=mask.mask_id,
        feature_mask_fingerprint=mask.fingerprint,
        seed=seed,
        sequence_length=sequence_length,
        hyperparameters_sha256=canonical_sha256({"family": family, "lane": lane_id}),
        role=role,
    )


def _pilot_lanes() -> tuple[Any, ...]:
    return (
        _lane("markov.market", contract=V1, mask=MARKET_MASK, role="primary_control"),
        _lane("markov.status_only", contract=V2, mask=STATUS_MASK, role="process_control"),
        _lane("markov.company", contract=V2, mask=COMPANY_MASK, role="pilot_treatment"),
    )


def _pilot_contrasts() -> tuple[Any, ...]:
    from trader.domain.world_cohort import WorldContrastDefinition, WorldContrastTerm

    return (
        WorldContrastDefinition(
            contrast_id="markov.status_only_minus_market.v1",
            terms=(
                WorldContrastTerm(lane_id="markov.status_only", coefficient=1),
                WorldContrastTerm(lane_id="markov.market", coefficient=-1),
            ),
            primary_metric="paired_multiclass_log_loss",
            role="pipeline_control",
        ),
        WorldContrastDefinition(
            contrast_id="markov.company_minus_status_only.v1",
            terms=(
                WorldContrastTerm(lane_id="markov.company", coefficient=1),
                WorldContrastTerm(lane_id="markov.status_only", coefficient=-1),
            ),
            primary_metric="paired_multiclass_log_loss",
            role="pilot_treatment",
        ),
    )


def _sensor(*, mode: str = "required") -> Any:
    from trader.domain.world_cohort import WorldSensorRequirement

    return WorldSensorRequirement(
        sensor_id="company",
        source_contract_id="company_intelligence_brief.v1",
        projection_contract_id="company_context_projection.v1",
        mode=mode,
        lane_ids=("markov.company",),
    )


def _support(*, mode: str = "descriptive_only", formal: int | None = None, descriptive: int = 20) -> Any:
    from trader.domain.world_cohort import WorldSupportGates

    return WorldSupportGates(
        mode=mode,
        descriptive_minimum_unique_anchors=descriptive,
        formal_minimum_unique_anchors=formal,
    )


def _stop() -> Any:
    from trader.domain.world_cohort import WorldCollectionStopRule

    return WorldCollectionStopRule(kind="fixed_end", at=STOP_AT)


def _protocol() -> Any:
    from trader.domain.world_cohort import WorldStatisticalProtocol

    return WorldStatisticalProtocol(
        pair_unit="unique_market_anchor",
        block_key="venue_session",
        ci_method="deterministic_block_bootstrap.v1",
        ci_level=0.95,
        bootstrap_resamples=2000,
        seed=20260823,
    )


def _manifest(**overrides: object) -> Any:
    from trader.domain.world_cohort import WorldCohortManifest

    values: dict[str, object] = {
        "cohort_id": COHORT_ID,
        "study_kind": "pipeline_pilot",
        "created_at": CREATED,
        "question": "Can paired lanes be captured without causal violations?",
        "planned_start_not_before": PLANNED_START,
        "collection_stop_rule": _stop(),
        "venues": ("EU", "TW", "US"),
        "bar_interval": "1h",
        "horizons": ("elapsed_4h.v1", "elapsed_1d.v1"),
        "primary_horizon": "elapsed_1d.v1",
        "label_contract": "simple_return_band_50bp.v1",
        "sampling_policy_version": "active_tradable_completed_bar.v1",
        "market_feature_contract": V1.contract_id,
        "context_feature_contract": V2.contract_id,
        "ontology_revision": "semantic_catalog.v1",
        "scope_mapping": None,
        "sensor_requirements": (_sensor(),),
        "lanes": _pilot_lanes(),
        "contrasts": _pilot_contrasts(),
        "statistical_protocol": _protocol(),
        "support_gates": _support(),
        "runtime_identity": _runtime(),
        "authority": "shadow_only",
        "decision_effect": "none",
        "causal_claim": False,
        "pnl_claim": False,
    }
    values.update(overrides)
    return WorldCohortManifest(**values)  # type: ignore[arg-type]


def _attested_receipt(*, subject: WorldAvailabilitySubjectRef, ready: datetime, scope: str) -> WorldAvailabilityReceipt:
    locator = WorldStorageLocator(kind="jsonl", store_id="world-cohort-jsonl.v1", path="cohort/events.jsonl")
    identity = _receipt_identity_payload(
        schema_version="world_availability_receipt.v1",
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


def _evidence_for(
    event: Any, *, ready: datetime = START_READY, first_seen: datetime = START_SEEN
) -> AvailabilityEvidence:
    from trader.domain.world_cohort import world_cohort_event_payload_hash

    subject = WorldAvailabilitySubjectRef(
        kind="world_cohort_event",
        subject_id=event.event_id,
        content_sha256=world_cohort_event_payload_hash(event),
    )
    receipt = _attested_receipt(subject=subject, ready=ready, scope=f"world_cohort:{event.cohort_id}")
    return AvailabilityEvidence(receipt=receipt, first_seen_at=first_seen)


def _register(manifest: Any | None = None) -> Any:
    from trader.domain.world_cohort import RegisterWorldCohort, WorldCohort

    return WorldCohort.register(RegisterWorldCohort(manifest=manifest or _manifest()))


def _armed(cohort: Any | None = None, *, sensors: tuple[str, ...] = ("company",), runtime: Any | None = None) -> Any:
    from trader.domain.world_cohort import ArmWorldCohort

    current = cohort or _register()
    return current.arm(
        ArmWorldCohort(
            cohort_id=current.cohort_id,
            manifest_sha256=current.manifest.manifest_sha256,
            runtime_identity=runtime or current.manifest.runtime_identity,
            satisfied_sensor_ids=sensors,
        )
    )


def _started(cohort: Any | None = None) -> Any:
    from trader.domain.world_cohort import StartWorldCohort

    current = cohort or _armed()
    return current.start(
        StartWorldCohort(
            cohort_id=current.cohort_id,
            manifest_sha256=current.manifest.manifest_sha256,
            runtime_identity=current.manifest.runtime_identity,
        )
    )


def _slot_for(cohort: Any, **overrides: object) -> Any:
    from trader.domain.world_cohort import WorldCohortSlot

    started = cohort.started_event
    values: dict[str, object] = {
        "cohort_id": cohort.cohort_id,
        "manifest_sha256": cohort.manifest.manifest_sha256,
        "venue": "US",
        "symbol": "AAPL",
        "bar_interval": "1h",
        "as_of_bar_ts": ANCHOR_TS,
        "anchor_end_at": ANCHOR_TS,
        "comparison_batch_id": "batch:v1:anchor-aapl",
        "episode_refs_by_contract": {
            V1.contract_id: "world-episode:v1:" + "1" * 64,
            V2.contract_id: "world-episode:v1:" + "2" * 64,
        },
        "expected_lane_ids": tuple(lane.lane_id for lane in cohort.manifest.lanes),
        "feature_contract_fingerprints": {
            lane.lane_id: lane.feature_contract_fingerprint for lane in cohort.manifest.lanes
        },
        "feature_mask_fingerprints": {lane.lane_id: lane.feature_mask_fingerprint for lane in cohort.manifest.lanes},
        "scope_resolution": None,
        "started_event_id": None if started is None else started.event_id,
    }
    values.update(overrides)
    return WorldCohortSlot(**values)  # type: ignore[arg-type]


def _admit(cohort: Any, slot: Any | None = None, evidence: AvailabilityEvidence | None = None) -> Any:
    from trader.domain.world_cohort import AdmitWorldCohortSlot

    started = cohort.started_event
    assert started is not None
    return cohort.admit_slot(
        AdmitWorldCohortSlot(
            slot=slot or _slot_for(cohort),
            started_evidence=evidence or _evidence_for(started),
        )
    )


def _completion_evidence(cohort: Any) -> Any:
    from trader.domain.world_cohort import (
        WorldCohortCompletionEvidence,
        WorldCohortCompletionHorizonLeaf,
        WorldCohortCompletionSlotEvidence,
    )

    return WorldCohortCompletionEvidence(
        slots=tuple(
            WorldCohortCompletionSlotEvidence(
                slot_id=slot.slot_id,
                leaves=(
                    WorldCohortCompletionHorizonLeaf(
                        horizon_id="elapsed_4h.v1",
                        status="observed",
                        digest="4" * 64,
                    ),
                    WorldCohortCompletionHorizonLeaf(
                        horizon_id="elapsed_1d.v1",
                        status="observed",
                        digest="d" * 64,
                    ),
                ),
            )
            for slot in cohort.admitted_slots
        )
    )


def _scope_mapping() -> WorldScopeMapping:
    return WorldScopeMapping(
        mapping_id="world_scope_mapping.v1",
        entries=(
            WorldScopeMappingEntry(
                anchor=WorldMarketAnchorRef(market_venue="US", instrument="AAPL"),
                venue=WorldCanonicalScopeRef(kind="venue", entity_id="mic:XNAS"),
                country=WorldCanonicalScopeRef(kind="country", entity_id="iso-3166:US"),
                region=WorldCanonicalScopeRef(kind="region", entity_id="iso-un-m49:021"),
                world=WorldCanonicalScopeRef(kind="world", entity_id="market"),
                provider_proofs=("provider:listing",),
                taxonomy_version="geo.v1",
            ),
        ),
    )


def _formal_lanes() -> tuple[Any, ...]:
    logical = (
        ("market", V1, MARKET_MASK, "primary_control"),
        ("status_only", V2, STATUS_MASK, "process_control"),
        ("company", V2, COMPANY_MASK, "pilot_treatment"),
        ("macro", V2, MACRO_MASK, "primary_treatment"),
        ("joint", V2, JOINT_MASK, "primary_treatment"),
    )
    lanes: list[Any] = []
    for name, contract, mask, role in logical:
        lanes.append(_lane(f"markov.{name}", contract=contract, mask=mask, role=role))
        lanes.append(
            _lane(
                f"gru.{name}",
                family="gru",
                contract=contract,
                mask=mask,
                role="secondary_challenger",
                sequence_length=32,
            )
        )
    return tuple(lanes)


def _formal_contrasts() -> tuple[Any, ...]:
    from trader.domain.world_cohort import WorldContrastDefinition, WorldContrastTerm

    return (
        WorldContrastDefinition(
            contrast_id="markov.joint_minus_market.v1",
            terms=(
                WorldContrastTerm(lane_id="markov.joint", coefficient=1),
                WorldContrastTerm(lane_id="markov.market", coefficient=-1),
            ),
            primary_metric="paired_multiclass_log_loss",
            role="primary",
        ),
        WorldContrastDefinition(
            contrast_id="markov.status_only_minus_market.v1",
            terms=(
                WorldContrastTerm(lane_id="markov.status_only", coefficient=1),
                WorldContrastTerm(lane_id="markov.market", coefficient=-1),
            ),
            primary_metric="paired_multiclass_log_loss",
            role="control",
        ),
        WorldContrastDefinition(
            contrast_id="markov.company_minus_status_only.v1",
            terms=(
                WorldContrastTerm(lane_id="markov.company", coefficient=1),
                WorldContrastTerm(lane_id="markov.status_only", coefficient=-1),
            ),
            primary_metric="paired_multiclass_log_loss",
            role="control",
        ),
        WorldContrastDefinition(
            contrast_id="markov.macro_minus_status_only.v1",
            terms=(
                WorldContrastTerm(lane_id="markov.macro", coefficient=1),
                WorldContrastTerm(lane_id="markov.status_only", coefficient=-1),
            ),
            primary_metric="paired_multiclass_log_loss",
            role="control",
        ),
    )


def test_world_cohort_module_is_stdlib_domain_and_reuses_shared_kernels() -> None:
    from trader.domain.world_cohort import (
        WorldCohort,
        WorldCohortManifest,
        WorldCohortSlot,
        WorldFeatureContract,
        WorldFeatureMask as CohortMask,
        WorldScopeResolution as CohortResolution,
    )

    assert MODULE_PATH.exists()
    assert _domain_import_violations([MODULE_PATH], REPO_ROOT) == []
    source = MODULE_PATH.read_text(encoding="utf-8")
    assert "class WorldFeatureContract" not in source
    assert "class WorldFeatureMask" not in source
    assert "class WorldScopeResolution" not in source
    assert "class WorldScopeMapping:" not in source
    assert WorldFeatureContract.__module__ == "trader.domain.world_feature_contract"
    assert CohortMask.__module__ == "trader.domain.world_feature_contract"
    assert CohortResolution.__module__ == "trader.domain.world_scope"
    assert WorldCohort.__module__ == "trader.domain.world_cohort"
    assert WorldCohortManifest.__module__ == "trader.domain.world_cohort"
    assert WorldCohortSlot.__module__ == "trader.domain.world_cohort"
    assert "trader.application" not in source
    assert "trader.infrastructure" not in source
    assert "trader.runtime" not in source
    assert "trader.reporting" not in source
    assert "import numpy" not in source
    assert "import networkx" not in source
    assert "numpy." not in source
    assert "networkx." not in source.lower()
    assert "set_status" not in source
    assert "set_phase" not in source
    tree = ast.parse(source, filename=str(MODULE_PATH))
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


def test_manifest_hash_is_canonical_and_deeply_immutable() -> None:
    left = _manifest()
    right = _manifest(
        venues=("US", "EU", "TW"),
        lanes=tuple(reversed(_pilot_lanes())),
        contrasts=tuple(reversed(_pilot_contrasts())),
    )
    assert left.schema_version == "world_cohort_manifest.v1"
    assert left.cohort_id == COHORT_ID
    assert left.manifest_sha256 == right.manifest_sha256
    assert len(left.manifest_sha256) == 64
    assert left.cohort_id != f"world_cohort:v1:{left.manifest_sha256}"
    replayed = type(left).from_mapping(left.to_dict())
    assert replayed == left
    with pytest.raises(FrozenInstanceError):
        left.question = "mutated"  # type: ignore[misc]
    with pytest.raises(AttributeError):
        left.venues.append("JP")  # type: ignore[attr-defined]
    exported = left.to_dict()
    exported["lanes"].append({})
    assert left.manifest_sha256 == replayed.manifest_sha256
    assert left.to_dict()["lanes"] == replayed.to_dict()["lanes"]
    mutated_runtime = _manifest(runtime_identity=_runtime(python_version="3.12.1"))
    mutated_seed = _manifest(lanes=(_lane("markov.market", seed=7),) + _pilot_lanes()[1:])
    mutated_stop = _manifest(
        collection_stop_rule=type(left.collection_stop_rule)(kind="fixed_end", at=datetime(2026, 11, 1, tzinfo=UTC))
    )
    assert mutated_runtime.manifest_sha256 != left.manifest_sha256
    assert mutated_seed.manifest_sha256 != left.manifest_sha256
    assert mutated_stop.manifest_sha256 != left.manifest_sha256
    assert mutated_runtime.cohort_id == left.cohort_id
    with pytest.raises(ValueError, match="manifest_sha256"):
        type(left).from_mapping({**left.to_dict(), "manifest_sha256": "0" * 64})


def test_manifest_is_shadow_only_with_decision_effect_none() -> None:
    manifest = _manifest()
    assert manifest.authority == "shadow_only"
    assert manifest.decision_effect == "none"
    assert manifest.causal_claim is False
    assert manifest.pnl_claim is False
    with pytest.raises(ValueError, match="shadow_only"):
        _manifest(authority="live")
    with pytest.raises(ValueError, match="decision_effect"):
        _manifest(decision_effect="orders")
    with pytest.raises(ValueError, match="causal_claim"):
        _manifest(causal_claim=True)
    with pytest.raises(ValueError, match="pnl_claim"):
        _manifest(pnl_claim=True)


def test_lane_gru_requires_sequence_length_and_markov_forbids_it() -> None:
    from trader.domain.world_cohort import WorldLaneDefinition

    markov = _lane("markov.market")
    gru = _lane("gru.market", family="gru", sequence_length=16, role="secondary_challenger")
    assert markov.sequence_length is None
    assert gru.sequence_length == 16
    with pytest.raises(ValueError, match="sequence_length"):
        _lane("markov.market", sequence_length=8)
    with pytest.raises(ValueError, match="sequence_length"):
        _lane("gru.market", family="gru", sequence_length=None, role="secondary_challenger")
    with pytest.raises(ValueError, match="model_family"):
        WorldLaneDefinition(
            lane_id="other.market",
            model_family="xgboost",
            model_id="xgb",
            model_version="v1",
            feature_contract_id=V1.contract_id,
            feature_contract_fingerprint=V1.fingerprint,
            feature_mask_id=MARKET_MASK.mask_id,
            feature_mask_fingerprint=MARKET_MASK.fingerprint,
            seed=0,
            sequence_length=None,
            hyperparameters_sha256="b" * 64,
            role="primary_control",
        )


def test_sensor_requirement_and_contrast_invariants() -> None:
    from trader.domain.world_cohort import WorldContrastDefinition, WorldContrastTerm, WorldSensorRequirement

    required = _sensor(mode="required")
    optional = _sensor(mode="optional")
    assert required.mode.value == "required"
    assert optional.mode.value == "optional"
    with pytest.raises(ValueError, match="lane"):
        WorldSensorRequirement(
            sensor_id="company",
            source_contract_id="company_intelligence_brief.v1",
            projection_contract_id="company_context_projection.v1",
            mode="required",
            lane_ids=(),
        )
    with pytest.raises(ValueError, match="coefficient"):
        WorldContrastDefinition(
            contrast_id="broken.v1",
            terms=(
                WorldContrastTerm(lane_id="markov.market", coefficient=1),
                WorldContrastTerm(lane_id="markov.status_only", coefficient=1),
            ),
            primary_metric="paired_multiclass_log_loss",
            role="pipeline_control",
        )
    with pytest.raises(ValueError, match="twice|duplicate"):
        WorldContrastDefinition(
            contrast_id="dup.v1",
            terms=(
                WorldContrastTerm(lane_id="markov.market", coefficient=1),
                WorldContrastTerm(lane_id="markov.market", coefficient=-1),
            ),
            primary_metric="paired_multiclass_log_loss",
            role="pipeline_control",
        )
    with pytest.raises(ValueError, match="two terms|at least two"):
        WorldContrastDefinition(
            contrast_id="one.v1",
            terms=(WorldContrastTerm(lane_id="markov.market", coefficient=1),),
            primary_metric="paired_multiclass_log_loss",
            role="pipeline_control",
        )
    with pytest.raises(ValueError, match="unknown lane"):
        _manifest(
            contrasts=(
                WorldContrastDefinition(
                    contrast_id="ghost.v1",
                    terms=(
                        WorldContrastTerm(lane_id="markov.ghost", coefficient=1),
                        WorldContrastTerm(lane_id="markov.market", coefficient=-1),
                    ),
                    primary_metric="paired_multiclass_log_loss",
                    role="pipeline_control",
                ),
            )
        )
    with pytest.raises(ValueError, match="primary"):
        _manifest(
            contrasts=(
                WorldContrastDefinition(
                    contrast_id="only_status.v1",
                    terms=(
                        WorldContrastTerm(lane_id="markov.status_only", coefficient=1),
                        WorldContrastTerm(lane_id="markov.company", coefficient=-1),
                    ),
                    primary_metric="paired_multiclass_log_loss",
                    role="pipeline_control",
                ),
            )
        )


def test_support_gates_reject_null_formal_minimum_and_placeholder() -> None:
    from trader.domain.world_cohort import WorldSupportGates

    descriptive = _support()
    assert descriptive.mode.value == "descriptive_only"
    assert descriptive.formal_minimum_unique_anchors is None
    assert descriptive.descriptive_minimum_unique_anchors == 20
    fixed = _support(mode="fixed_minimum", formal=80)
    assert fixed.formal_minimum_unique_anchors == 80
    with pytest.raises(ValueError, match="formal"):
        WorldSupportGates(
            mode="fixed_minimum", descriptive_minimum_unique_anchors=20, formal_minimum_unique_anchors=None
        )
    with pytest.raises(ValueError, match="formal"):
        WorldSupportGates(mode="fixed_minimum", descriptive_minimum_unique_anchors=20, formal_minimum_unique_anchors=0)
    with pytest.raises(ValueError, match="formal"):
        WorldSupportGates(
            mode="descriptive_only",
            descriptive_minimum_unique_anchors=20,
            formal_minimum_unique_anchors=80,
        )
    with pytest.raises(ValueError, match="latest"):
        _runtime(python_version="latest")
    with pytest.raises(ValueError, match="latest"):
        _runtime(numpy_version="latest")
    with pytest.raises(ValueError, match="git_commit"):
        _runtime(git_commit="abc")


def test_prospective_evaluation_requires_fixed_minimum_and_core_lanes() -> None:
    mapping = _scope_mapping()
    sensors = (
        _sensor(),
        type(_sensor())(
            sensor_id="macro",
            source_contract_id="macro_world_observation.v1",
            projection_contract_id="macro_context_projection.v1",
            mode="required",
            lane_ids=("markov.macro", "markov.joint", "gru.macro", "gru.joint"),
        ),
    )
    formal = _manifest(
        study_kind="prospective_evaluation",
        scope_mapping={"mapping_id": mapping.mapping_id, "mapping_sha256": mapping.content_sha256},
        sensor_requirements=sensors,
        lanes=_formal_lanes(),
        contrasts=_formal_contrasts(),
        support_gates=_support(mode="fixed_minimum", formal=80),
    )
    assert formal.study_kind.value == "prospective_evaluation"
    assert formal.support_gates.mode.value == "fixed_minimum"
    with pytest.raises(ValueError, match="fixed_minimum"):
        _manifest(
            study_kind="prospective_evaluation",
            scope_mapping={"mapping_id": mapping.mapping_id, "mapping_sha256": mapping.content_sha256},
            sensor_requirements=sensors,
            lanes=_formal_lanes(),
            contrasts=_formal_contrasts(),
            support_gates=_support(),
        )
    with pytest.raises(ValueError, match="macro|joint|status_only"):
        _manifest(
            study_kind="prospective_evaluation",
            support_gates=_support(mode="fixed_minimum", formal=80),
        )


def test_scope_mapping_is_required_when_macro_or_graph_lanes_exist() -> None:
    with pytest.raises(ValueError, match="scope_mapping"):
        _manifest(
            lanes=_pilot_lanes() + (_lane("markov.macro", contract=V2, mask=MACRO_MASK, role="primary_treatment"),),
            contrasts=_pilot_contrasts()
            + (
                type(_pilot_contrasts()[0])(
                    contrast_id="markov.macro_minus_status_only.v1",
                    terms=(
                        type(_pilot_contrasts()[0].terms[0])(lane_id="markov.macro", coefficient=1),
                        type(_pilot_contrasts()[0].terms[0])(lane_id="markov.status_only", coefficient=-1),
                    ),
                    primary_metric="paired_multiclass_log_loss",
                    role="control",
                ),
            ),
        )
    mapping = _scope_mapping()
    ok = _manifest(
        lanes=_pilot_lanes() + (_lane("markov.macro", contract=V2, mask=MACRO_MASK, role="primary_treatment"),),
        contrasts=_pilot_contrasts()
        + (
            type(_pilot_contrasts()[0])(
                contrast_id="markov.macro_minus_status_only.v1",
                terms=(
                    type(_pilot_contrasts()[0].terms[0])(lane_id="markov.macro", coefficient=1),
                    type(_pilot_contrasts()[0].terms[0])(lane_id="markov.status_only", coefficient=-1),
                ),
                primary_metric="paired_multiclass_log_loss",
                role="control",
            ),
        ),
        scope_mapping={"mapping_id": mapping.mapping_id, "mapping_sha256": mapping.content_sha256},
    )
    assert ok.scope_mapping.mapping_sha256 == mapping.content_sha256
    assert _manifest().scope_mapping is None


def test_register_is_idempotent_and_same_id_different_hash_is_a_conflict() -> None:
    from trader.domain.world_cohort import RegisterWorldCohort, WorldCohort

    first = WorldCohort.register(RegisterWorldCohort(manifest=_manifest()))
    same = WorldCohort.register(RegisterWorldCohort(manifest=_manifest()), existing=first)
    assert same is first or same.events == first.events
    other = _manifest(question="A different pre-registered question")
    assert other.cohort_id == first.cohort_id
    assert other.manifest_sha256 != first.manifest.manifest_sha256
    with pytest.raises(ValueError, match="hash"):
        WorldCohort.register(RegisterWorldCohort(manifest=other), existing=first)
    replayed = WorldCohort.reconstruct(first.manifest, first.events)
    assert replayed.phase.value == "registered"
    assert replayed.events == first.events


def test_lifecycle_register_arm_start_close_complete_and_illegal_transitions() -> None:
    from trader.domain.world_cohort import (
        CloseWorldCohort,
        CompleteWorldCohort,
        CohortPhase,
        InvalidateWorldCohort,
        InvalidationReason,
        StartWorldCohort,
        WorldCohort,
    )

    registered = _register()
    assert registered.phase is CohortPhase.REGISTERED
    with pytest.raises(ValueError, match="armed|arm"):
        registered.start(
            StartWorldCohort(
                cohort_id=registered.cohort_id,
                manifest_sha256=registered.manifest.manifest_sha256,
                runtime_identity=registered.manifest.runtime_identity,
            )
        )
    armed = _armed(registered)
    assert armed.phase is CohortPhase.ARMED
    started = _started(armed)
    assert started.phase is CohortPhase.COLLECTING
    started_again = _started(started)
    assert started_again.events == started.events
    admitted = _admit(started)
    assert len(admitted.admitted_slots) == 1
    closed = admitted.close(CloseWorldCohort(reason="fixed_end reached"))
    assert closed.phase is CohortPhase.COLLECTION_CLOSED
    with pytest.raises(ValueError, match="admit|collection_closed|closed"):
        _admit(closed)
    completed = closed.complete(CompleteWorldCohort(evidence=_completion_evidence(closed)))
    assert completed.phase is CohortPhase.COMPLETE
    with pytest.raises(ValueError, match="complete|terminal"):
        completed.close(CloseWorldCohort(reason="again"))
    invalidated = _started().invalidate(
        InvalidateWorldCohort(
            reason=InvalidationReason.FUTURE_LEAK,
            scope="cohort",
            proofs=("proof:future-leak",),
            occurred_at=datetime(2026, 8, 24, 2, 0, tzinfo=UTC),
        )
    )
    assert invalidated.phase is CohortPhase.INVALIDATED
    with pytest.raises(ValueError, match="invalidat"):
        _admit(invalidated)
    replayed = WorldCohort.reconstruct(completed.manifest, completed.events)
    assert replayed.phase is CohortPhase.COMPLETE
    assert [type(event).__name__ for event in replayed.events] == [
        "WorldCohortRegistered",
        "WorldCohortArmed",
        "WorldCohortStarted",
        "WorldCohortSlotAdmitted",
        "WorldCohortCollectionClosed",
        "WorldCohortCompleted",
    ]


def test_arm_requires_runtime_match_required_sensors_and_allows_optional_gaps() -> None:
    from trader.domain.world_cohort import ArmWorldCohort

    from trader.domain.world_cohort import WorldSensorRequirement

    registered = _register(
        _manifest(
            sensor_requirements=(
                _sensor(mode="required"),
                WorldSensorRequirement(
                    sensor_id="news",
                    source_contract_id="news_macro_brief.v1",
                    projection_contract_id="news_status_projection.v1",
                    mode="optional",
                    lane_ids=("markov.status_only",),
                ),
            )
        )
    )
    with pytest.raises(ValueError, match="runtime"):
        _armed(registered, runtime=_runtime(python_version="3.12.0"))
    with pytest.raises(ValueError, match="required"):
        _armed(registered, sensors=())
    armed = _armed(registered, sensors=("company",))
    assert armed.phase.value == "armed"
    again = armed.arm(
        ArmWorldCohort(
            cohort_id=armed.cohort_id,
            manifest_sha256=armed.manifest.manifest_sha256,
            runtime_identity=armed.manifest.runtime_identity,
            satisfied_sensor_ids=("company",),
        )
    )
    assert again.events == armed.events


def test_reconstruct_rejects_armed_history_missing_required_sensors() -> None:
    from trader.domain.world_cohort import WorldCohort, WorldCohortArmed

    registered = _register()
    with pytest.raises(ValueError, match="required"):
        WorldCohort.reconstruct(
            registered.manifest,
            (
                *registered.events,
                WorldCohortArmed(
                    cohort_id=registered.cohort_id,
                    manifest_sha256=registered.manifest.manifest_sha256,
                    runtime_identity=registered.manifest.runtime_identity,
                    satisfied_sensor_ids=(),
                ),
            ),
        )


def test_admit_slot_is_the_only_admission_gate_and_is_strictly_after_start() -> None:
    from trader.domain.world_cohort import (
        AdmitWorldCohortSlot,
        WorldCohort,
        WorldCohortEventEnvelope,
        WorldCohortSlotAdmitted,
    )

    started = _started()
    evidence = _evidence_for(started.started_event)
    assert evidence.effective_ready_at == START_SEEN
    too_early = _slot_for(started, anchor_end_at=START_SEEN, as_of_bar_ts=START_SEEN)
    with pytest.raises(ValueError, match="strictly|after"):
        started.admit_slot(AdmitWorldCohortSlot(slot=too_early, started_evidence=evidence))
    admitted = _admit(started, evidence=evidence)
    assert isinstance(admitted.events[-1], WorldCohortSlotAdmitted)
    same = _admit(admitted, slot=admitted.admitted_slots[0], evidence=evidence)
    assert same.events == admitted.events
    conflicting = _slot_for(started, comparison_batch_id="batch:other")
    object.__setattr__(conflicting, "slot_id", admitted.admitted_slots[0].slot_id)
    with pytest.raises(ValueError, match="conflict"):
        admitted.admit_slot(AdmitWorldCohortSlot(slot=conflicting, started_evidence=evidence))
    unproven = WorldCohortEventEnvelope.bind(started.started_event, sequence=3, evidence=None)
    assert unproven.availability_status == "availability_unproven"
    with pytest.raises(ValueError, match="availability_unproven|evidence"):
        started.admit_slot(AdmitWorldCohortSlot(slot=_slot_for(started), started_evidence=None))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="availability_unproven|evidence"):
        WorldCohortEventEnvelope.bind(started.started_event, sequence=3, evidence=None).require_proven()
    source = MODULE_PATH.read_text(encoding="utf-8")
    assert "def admit_slot" in source
    assert inspect.getattr_static(WorldCohort, "admit_slot") is not None
    assert not hasattr(WorldCohort, "append_slot")
    assert not hasattr(WorldCohort, "set_status")


def test_unproven_start_envelope_cannot_be_used_as_cutoff_proof() -> None:
    from trader.domain.world_cohort import WorldCohortEventEnvelope

    started = _started()
    envelope = WorldCohortEventEnvelope.bind(started.started_event, sequence=3, evidence=None)
    assert envelope.evidence is None
    assert "ready_at" not in started.started_event.to_dict()
    with pytest.raises((TypeError, FrozenInstanceError, AttributeError)):
        started.started_event.ready_at = START_READY  # type: ignore[attr-defined]
    proven = WorldCohortEventEnvelope.bind(
        started.started_event,
        sequence=3,
        evidence=_evidence_for(started.started_event),
    )
    assert proven.payload_hash == envelope.payload_hash
    assert proven.event.event_id == envelope.event.event_id
    assert proven.availability_status != "availability_unproven"
    with pytest.raises(ValueError, match="sequence"):
        WorldCohortEventEnvelope.bind(started.started_event, sequence=0, evidence=None)


def test_lane_block_config_drift_is_typed_and_restorable() -> None:
    from trader.domain.world_cohort import (
        BlockWorldCohortLane,
        LaneBlockReason,
        LaneOperationalStatus,
        RestoreWorldCohortLane,
    )

    started = _started()
    blocked = started.block_lane(
        BlockWorldCohortLane(
            lane_id="markov.company",
            reason=LaneBlockReason.CONFIG_DRIFT,
            affected_from=datetime(2026, 8, 24, 3, 0, tzinfo=UTC),
            affected_until=None,
        )
    )
    state = blocked.lane_states["markov.company"]
    assert state.status is LaneOperationalStatus.BLOCKED
    assert state.reason is LaneBlockReason.CONFIG_DRIFT
    with pytest.raises(TypeError):
        blocked.block_lane(
            BlockWorldCohortLane(
                lane_id="markov.company",
                reason="config_drift",  # type: ignore[arg-type]
                affected_from=datetime(2026, 8, 24, 3, 0, tzinfo=UTC),
                affected_until=None,
            )
        )
    restored = blocked.restore_lane(RestoreWorldCohortLane(lane_id="markov.company"))
    assert restored.lane_states["markov.company"].status is LaneOperationalStatus.ACTIVE
    assert restored.lane_states["markov.company"].reason is None
    still_collecting = _admit(blocked)
    assert still_collecting.phase.value == "collecting"


def test_complete_requires_terminal_leaves_for_every_admitted_slot() -> None:
    from trader.domain.world_cohort import (
        CloseWorldCohort,
        CompleteWorldCohort,
        WorldCohortCompletionEvidence,
        WorldCohortCompletionHorizonLeaf,
        WorldCohortCompletionSlotEvidence,
    )

    admitted = _admit(_started())
    with pytest.raises(ValueError, match="collection_closed|closed"):
        admitted.complete(CompleteWorldCohort(evidence=_completion_evidence(admitted)))
    closed = admitted.close(CloseWorldCohort(reason="stop"))
    with pytest.raises(ValueError, match="slot"):
        closed.complete(CompleteWorldCohort(evidence=WorldCohortCompletionEvidence(slots=())))
    partial = WorldCohortCompletionEvidence(
        slots=(
            WorldCohortCompletionSlotEvidence(
                slot_id=closed.admitted_slots[0].slot_id,
                leaves=(
                    WorldCohortCompletionHorizonLeaf(
                        horizon_id="elapsed_4h.v1",
                        status="observed",
                        digest="4" * 64,
                    ),
                ),
            ),
        )
    )
    with pytest.raises(ValueError, match="elapsed_1d|horizon"):
        closed.complete(CompleteWorldCohort(evidence=partial))
    with pytest.raises(ValueError, match="terminal|pending"):
        WorldCohortCompletionHorizonLeaf(horizon_id="elapsed_4h.v1", status="pending", digest="4" * 64)
    completed = closed.complete(CompleteWorldCohort(evidence=_completion_evidence(closed)))
    assert completed.completion_evidence is not None
    assert completed.completion_evidence.slots[0].leaves[0].digest == "4" * 64


def test_unmapped_or_ambiguous_scope_resolution_is_admitted_not_dropped() -> None:
    mapping = _scope_mapping()
    from trader.domain.world_cohort import WorldContrastDefinition, WorldContrastTerm

    manifest = _manifest(
        lanes=_pilot_lanes() + (_lane("markov.macro", contract=V2, mask=MACRO_MASK, role="primary_treatment"),),
        contrasts=_pilot_contrasts()
        + (
            WorldContrastDefinition(
                contrast_id="markov.macro_minus_status_only.v1",
                terms=(
                    WorldContrastTerm(lane_id="markov.macro", coefficient=1),
                    WorldContrastTerm(lane_id="markov.status_only", coefficient=-1),
                ),
                primary_metric="paired_multiclass_log_loss",
                role="control",
            ),
        ),
        scope_mapping={"mapping_id": mapping.mapping_id, "mapping_sha256": mapping.content_sha256},
    )
    started = _started(_armed(_register(manifest), sensors=("company",)))
    unmapped = WorldScopeResolution(
        mapping_id=mapping.mapping_id,
        mapping_sha256=mapping.content_sha256,
        anchor=WorldMarketAnchorRef(market_venue="US", instrument="AAPL"),
        status="unmapped",
    )
    admitted = _admit(started, slot=_slot_for(started, scope_resolution=unmapped))
    assert admitted.admitted_slots[0].scope_resolution.status == "unmapped"
    with pytest.raises(ValueError, match="scope"):
        _admit(started, slot=_slot_for(started, scope_resolution=None))


def test_typed_commands_are_the_only_mutation_surface() -> None:
    from trader.domain import world_cohort as cohort_mod
    from trader.domain.world_cohort import (
        AdmitWorldCohortSlot,
        ArmWorldCohort,
        BlockWorldCohortLane,
        CloseWorldCohort,
        CompleteWorldCohort,
        InvalidateWorldCohort,
        RegisterWorldCohort,
        RestoreWorldCohortLane,
        StartWorldCohort,
        WorldCohort,
        WorldCohortCommand,
    )

    expected = {
        RegisterWorldCohort,
        ArmWorldCohort,
        StartWorldCohort,
        AdmitWorldCohortSlot,
        BlockWorldCohortLane,
        RestoreWorldCohortLane,
        CloseWorldCohort,
        CompleteWorldCohort,
        InvalidateWorldCohort,
    }
    assert expected <= set(getattr(cohort_mod, "WORLD_COHORT_COMMANDS"))
    registered = WorldCohort.register(RegisterWorldCohort(manifest=_manifest()))
    armed = registered.handle(
        ArmWorldCohort(
            cohort_id=registered.cohort_id,
            manifest_sha256=registered.manifest.manifest_sha256,
            runtime_identity=registered.manifest.runtime_identity,
            satisfied_sensor_ids=("company",),
        )
    )
    assert armed.phase.value == "armed"
    assert inspect.isclass(WorldCohortCommand) or WorldCohortCommand is not None
    for command_type in expected:
        assert command_type.__dataclass_params__.frozen  # type: ignore[attr-defined]


def test_matched_set_rejects_mismatches_and_does_not_invent_members() -> None:
    from trader.domain.world_cohort import WorldCohortMatchedMember, WorldCohortMatchedSet

    def member(lane_id: str, **overrides: object) -> WorldCohortMatchedMember:
        values: dict[str, object] = {
            "lane_id": lane_id,
            "study_cohort_id": COHORT_ID,
            "manifest_sha256": "f" * 64,
            "venue": "US",
            "symbol": "AAPL",
            "bar_interval": "1h",
            "as_of_bar_ts": ANCHOR_TS,
            "horizon_id": "elapsed_1d.v1",
            "comparison_batch_id": "batch:1",
            "predicted_at": ANCHOR_TS,
            "training_cutoff": ANCHOR_TS,
            "training_lineage_fingerprint": "1" * 64,
            "label_move_class": "UP",
            "label_target_at": datetime(2026, 8, 25, 1, 0, tzinfo=UTC),
            "label_evidence_digest": "e" * 64,
        }
        values.update(overrides)
        return WorldCohortMatchedMember(**values)  # type: ignore[arg-type]

    paired = WorldCohortMatchedSet(
        contrast_id="markov.status_only_minus_market.v1",
        expected_lane_ids=("markov.status_only", "markov.market"),
        members=(member("markov.status_only"), member("markov.market")),
    )
    assert tuple(item.lane_id for item in paired.members) == ("markov.market", "markov.status_only") or set(
        item.lane_id for item in paired.members
    ) == {"markov.market", "markov.status_only"}
    with pytest.raises(ValueError, match="batch"):
        WorldCohortMatchedSet(
            contrast_id="markov.status_only_minus_market.v1",
            expected_lane_ids=("markov.status_only", "markov.market"),
            members=(member("markov.status_only"), member("markov.market", comparison_batch_id="batch:other")),
        )
    with pytest.raises(ValueError, match="absent|missing"):
        WorldCohortMatchedSet(
            contrast_id="markov.status_only_minus_market.v1",
            expected_lane_ids=("markov.status_only", "markov.market"),
            members=(member("markov.status_only"),),
        )


def test_report_is_shadow_only_with_decision_effect_none() -> None:
    from trader.domain.world_cohort import CloseWorldCohort, CompleteWorldCohort, WorldCohortReport

    closed = _admit(_started()).close(CloseWorldCohort(reason="stop"))
    completed = closed.complete(CompleteWorldCohort(evidence=_completion_evidence(closed)))
    report = WorldCohortReport.from_cohort(completed)
    assert report.schema_version == "world_cohort_report.v1"
    assert report.authority == "shadow_only"
    assert report.decision_effect == "none"
    assert report.recommendation == "NO_GO"
    assert report.causal_claim is False
    assert report.pnl_claim is False
    assert report.actual_trader_contribution == "not_attributable"
    with pytest.raises(ValueError, match="shadow_only|decision_effect|NO_GO"):
        WorldCohortReport.from_mapping({**report.to_dict(), "recommendation": "GO"})


def test_event_union_is_closed_and_reconstruction_is_deterministic() -> None:
    from trader.domain.world_cohort import (
        WORLD_COHORT_EVENTS,
        WorldCohort,
        WorldCohortArmed,
        WorldCohortCollectionClosed,
        WorldCohortCompleted,
        WorldCohortInvalidated,
        WorldCohortLaneBlocked,
        WorldCohortLaneRestored,
        WorldCohortRegistered,
        WorldCohortSlotAdmitted,
        WorldCohortStarted,
        parse_world_cohort_event,
    )

    names = {cls.__name__ for cls in WORLD_COHORT_EVENTS}
    assert names == {
        "WorldCohortRegistered",
        "WorldCohortArmed",
        "WorldCohortStarted",
        "WorldCohortSlotAdmitted",
        "WorldCohortLaneBlocked",
        "WorldCohortLaneRestored",
        "WorldCohortCollectionClosed",
        "WorldCohortCompleted",
        "WorldCohortInvalidated",
    }
    started = _started()
    replayed = parse_world_cohort_event(started.events[0].to_dict())
    assert isinstance(replayed, WorldCohortRegistered)
    reconstructed = WorldCohort.reconstruct(started.manifest, [event.to_dict() for event in started.events])
    assert reconstructed.events == started.events
    with pytest.raises(ValueError, match="event_type"):
        parse_world_cohort_event({"event_type": "world_cohort_mutated", "schema_version": "world_cohort_event.v1"})
    assert WorldCohortArmed is not None
    assert WorldCohortSlotAdmitted is not None
    assert WorldCohortLaneBlocked is not None
    assert WorldCohortLaneRestored is not None
    assert WorldCohortCollectionClosed is not None
    assert WorldCohortCompleted is not None
    assert WorldCohortInvalidated is not None
    assert WorldCohortStarted is not None
