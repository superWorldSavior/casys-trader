from __future__ import annotations

from datetime import datetime, timedelta, timezone

from tests.application.test_world_pattern_discovery import (
    _about,
    _ancestry,
    _binding,
    _instrument,
    _observes,
    _record,
    _regime_driver,
    _structural,
    _venue,
)
from trader.application.world_model.pattern_path import (
    paths_matching_steps,
    pattern_path_signature,
    project_pattern_path_details,
    project_pattern_paths,
)
from trader.domain.world_driver import DriverState
from trader.domain.world_feature_contract import GRAPH_PATH_RULE_VERSION


UTC = timezone.utc


def test_shared_projection_matches_discovery_and_keeps_evidence_off_identity() -> None:
    as_of = datetime(2026, 8, 1, tzinfo=UTC)
    overlay = _observes(_venue(), digest="e" * 64, effective_from=as_of - timedelta(hours=1))
    about = _about(_instrument(), digest="f" * 64, effective_from=as_of - timedelta(hours=2))
    sibling = _structural("TRADED_ON", _instrument("2317"), _venue())
    record = _record(as_of=as_of, knowledge=(overlay, about), extra_structural=(sibling,))
    steps = project_pattern_paths(record)
    details = project_pattern_path_details(record)
    assert tuple(path.steps for path in details) == steps
    signatures = {pattern_path_signature(item) for item in steps}
    joined = " ".join(signatures)
    assert "venue:TRADED_ON:reverse:instrument" not in joined
    assert "venue:OBSERVES:reverse:world_observation" in joined
    assert "instrument:ABOUT:reverse:knowledge_artifact" in joined
    for path in details:
        assert all(step.evidence_rule_version == GRAPH_PATH_RULE_VERSION for step in path.steps)
        for step, hop in zip(path.steps, path.hops, strict=True):
            assert step.identity_tuple() == hop.identity_tuple()
            assert "evidence_refs" not in step.to_dict()


def test_exact_driver_state_match_rejects_rising_versus_falling() -> None:
    as_of = datetime(2026, 8, 1, tzinfo=UTC)
    overlay = _observes(_venue(), digest="11" * 32, effective_from=as_of - timedelta(hours=1))
    rising = _regime_driver(rates_regime="rising")
    falling = _regime_driver(rates_regime="falling")
    record = _record(
        as_of=as_of,
        structural=_ancestry(_instrument())[:1],
        knowledge=(overlay,),
        driver_state_bindings=(_binding(overlay, rising),),
    )
    rising_steps = next(
        path.steps
        for path in project_pattern_path_details(record)
        if path.steps[-1].relation_kind == "OBSERVES"
    )
    assert paths_matching_steps(record, rising_steps)
    falling_record = _record(
        as_of=as_of,
        structural=_ancestry(_instrument())[:1],
        knowledge=(overlay,),
        driver_state_bindings=(_binding(overlay, falling),),
    )
    assert paths_matching_steps(falling_record, rising_steps) == ()
    assert rising.identity_tuple() != falling.identity_tuple()
    assert DriverState.missing(missingness="observation_unjoined").identity_tuple() != rising.identity_tuple()
