"""Predictor lineage: same lane shared across cohorts must not collide.

Regression tests for the P0-1 review finding on D20: predictor identity was
(model_id, model_version) without cohort, so two cohorts declaring the same
lane (C1 joint vs graph joint, or two C1 generations) silently lost all but
one predictor, and a closed cohort could starve a collecting one.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from tests.domain.test_world_cohort import JOINT_MASK, V2, _lane, _manifest
from trader.application.world_model.encoding import (
    PredictorIdentityCollisionError,
    predictor_runtime_key,
)
from trader.runtime.world_model_runtime import WorldModelRuntime

VERSION = "cohort.joint.markov.v1"


def _joint_lane() -> Any:
    return _lane(
        "markov.joint",
        contract=V2,
        mask=JOINT_MASK,
        role="process_control",
        version=VERSION,
    )


def _cohort(cohort_id: str) -> Any:
    from trader.domain.world_cohort import (
        WorldContrastDefinition,
        WorldContrastTerm,
        WorldSensorRequirement,
    )
    from tests.domain.test_world_cohort import MARKET_MASK, V1, _scope_mapping
    from trader.domain.world_feature_contract import WORLD_SCOPE_MAPPING_ID

    return SimpleNamespace(
        manifest=_manifest(
            cohort_id=cohort_id,
            scope_mapping={
                "mapping_id": WORLD_SCOPE_MAPPING_ID,
                "mapping_sha256": _scope_mapping().content_sha256,
            },
            lanes=(
                _joint_lane(),
                _lane("markov.market", contract=V1, mask=MARKET_MASK, role="primary_control"),
            ),
            contrasts=(
                WorldContrastDefinition(
                    contrast_id="markov.joint_minus_market.v1",
                    terms=(
                        WorldContrastTerm(lane_id="markov.joint", coefficient=1),
                        WorldContrastTerm(lane_id="markov.market", coefficient=-1),
                    ),
                    primary_metric="paired_multiclass_log_loss",
                    role="pipeline_control",
                ),
            ),
            sensor_requirements=(
                WorldSensorRequirement(
                    sensor_id="company",
                    source_contract_id="company_intelligence_brief.v1",
                    projection_contract_id="company_context_projection.v1",
                    mode="required",
                    lane_ids=("markov.joint",),
                ),
            ),
        ),
    )


class _Store:
    def __init__(self, cohorts: dict[str, Any]) -> None:
        self._cohorts = cohorts

    def list_collecting_cohort_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._cohorts))

    # Alias for trees where the runtime mints labeling cohorts.
    def list_labeling_cohort_ids(self) -> tuple[str, ...]:
        return self.list_collecting_cohort_ids()

    def load(self, cohort_id: str) -> Any:
        return self._cohorts[cohort_id]


class _Cohorts:
    def cold_lanes(self, cohort_id: str) -> tuple[Any, ...]:
        return (
            SimpleNamespace(
                lane_id="markov.joint",
                study_cohort_id=cohort_id,
                manifest_sha256="m" * 64,
                started_event_id=f"world_cohort_event:v1:{cohort_id[-8:]}",
            ),
        )


def _runtime(cohort_ids: tuple[str, ...]) -> WorldModelRuntime:
    store = _Store({cohort_id: _cohort(cohort_id) for cohort_id in cohort_ids})
    return WorldModelRuntime(store=store, cohort_service=_Cohorts())


def test_runtime_key_discards_cohort_sharing() -> None:
    first = SimpleNamespace(
        model_id="m", model_version="v", lane_identity=SimpleNamespace(study_cohort_id="c1", lane_id="l")
    )
    second = SimpleNamespace(
        model_id="m", model_version="v", lane_identity=SimpleNamespace(study_cohort_id="c2", lane_id="l")
    )
    assert predictor_runtime_key(first) != predictor_runtime_key(second)


def test_runtime_key_falls_back_to_model_pair_without_lane_identity() -> None:
    assert predictor_runtime_key(SimpleNamespace(model_id="m", model_version="v")) == (
        "",
        "",
        "m",
        "v",
    )


def test_two_collecting_cohorts_sharing_a_lane_each_get_predictor() -> None:
    runtime = _runtime(("world_cohort:v1:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "world_cohort:v1:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"))

    assert len(runtime.predictors) == 2
    assert {item.lane_identity.study_cohort_id for item in runtime.predictors} == {
        "world_cohort:v1:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "world_cohort:v1:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    }


def test_true_duplicate_lineage_raises_coded_collision() -> None:
    runtime = _runtime(("world_cohort:v1:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",))
    assert len(runtime.predictors) == 1

    from trader.application.world_model.service import WorldModelService

    with pytest.raises(PredictorIdentityCollisionError) as exc_info:
        WorldModelService(
            store=object(),
            predictor=None,
            labeler=None,
            bar_provider=None,
            predictors=[runtime.predictors[0], runtime.predictors[0]],
        )

    assert exc_info.value.code == "predictor_identity_collision"
    assert "world_cohort:v1:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" in str(exc_info.value.context)
    assert "markov.joint" in str(exc_info.value.context)
