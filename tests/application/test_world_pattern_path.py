from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone

from tests.application.test_world_pattern_discovery import (
    _about,
    _ancestry,
    _binding,
    _instrument,
    _market_episode,
    _observes,
    _record,
    _regime_driver,
    _snapshot,
    _structural,
    _venue,
)
from trader.application.world_model.pattern_discovery_ports import PatternDriverStateBinding
from trader.application.world_model.pattern_path import (
    paths_matching_steps,
    pattern_path_signature,
    project_pattern_path_details,
    project_pattern_paths,
)
from trader.domain.world_driver import DriverState
from trader.domain.world_feature_contract import GRAPH_PATH_RULE_VERSION
from trader.domain.world_graph import KnowledgeWorldRelation, StructuralWorldRelation, WorldGraphSnapshot


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


@dataclass(frozen=True)
class _PathSource:
    snapshot: WorldGraphSnapshot
    structural_relations: tuple[StructuralWorldRelation, ...]
    knowledge_relations: tuple[KnowledgeWorldRelation, ...] = ()
    driver_state_bindings: tuple[PatternDriverStateBinding, ...] = ()

    @property
    def driver_state_by_relation_id(self) -> dict[str, PatternDriverStateBinding]:
        return {item.relation_id: item for item in self.driver_state_bindings}


def _geographic_chain(steps: tuple[object, ...]) -> tuple[tuple[str, str, str], ...]:
    return tuple((step.source_kind, step.relation_kind, step.target_kind) for step in steps)


_FULL_GEOGRAPHIC_CHAIN = (
    ("instrument", "TRADED_ON", "venue"),
    ("venue", "LOCATED_IN", "country"),
    ("country", "LOCATED_IN", "region"),
    ("region", "PART_OF_WORLD", "world"),
)


def test_pattern_path_imports_the_domain_geographic_ancestry_walk() -> None:
    import inspect

    from trader.application.world_model import pattern_path
    from trader.domain.world_graph import GEOGRAPHIC_ANCESTRY_WALK, ROOT_BRANCH_WALK

    assert pattern_path.GEOGRAPHIC_ANCESTRY_WALK is GEOGRAPHIC_ANCESTRY_WALK
    assert pattern_path.ROOT_BRANCH_WALK is ROOT_BRANCH_WALK
    source = inspect.getsource(pattern_path)
    assert "GEOGRAPHIC_ANCESTRY_WALK =" not in source
    assert "_ANCESTRY_WALK =" not in source
    assert "_BACKBONE_FORWARD" not in source


def test_budget_truncated_ancestry_is_not_emitted_as_a_pattern_path() -> None:
    as_of = datetime(2026, 8, 27, 4, 30, tzinfo=UTC)
    root = _instrument()
    truncated = _ancestry(root)[:2]
    peers = tuple(_structural("TRADED_ON", _instrument(f"I{index:02d}"), _venue()) for index in range(31))
    members = truncated + peers
    market = _market_episode(symbol="2330", as_of=as_of)
    snapshot = replace(
        _snapshot(market, structural=members),
        status="partial",
        missingness={"budget": "graph_budget_exceeded"},
        snapshot_id=None,
        content_sha256=None,
    )
    details = project_pattern_path_details(_PathSource(snapshot=snapshot, structural_relations=members))
    chains = {_geographic_chain(path.steps) for path in details}
    assert _FULL_GEOGRAPHIC_CHAIN not in chains
    assert not any(chain == _FULL_GEOGRAPHIC_CHAIN[:2] for chain in chains)
    assert not any(chain == _FULL_GEOGRAPHIC_CHAIN[:3] for chain in chains)
    assert not any(
        len(path.steps) in {2, 3} and path.steps[-1].relation_kind in {"TRADED_ON", "LOCATED_IN", "PART_OF_WORLD"}
        for path in details
    )


def test_complete_short_graph_still_projects_its_full_available_ancestry() -> None:
    as_of = datetime(2026, 8, 1, tzinfo=UTC)
    record = _record(as_of=as_of, structural=_ancestry(_instrument())[:1])
    details = project_pattern_path_details(record)
    chains = {_geographic_chain(path.steps) for path in details}
    assert (_FULL_GEOGRAPHIC_CHAIN[0],) in chains


def test_complete_geographic_ancestry_is_emitted_with_or_without_overlay() -> None:
    as_of = datetime(2026, 8, 27, 11, 0, tzinfo=UTC)
    root = _instrument()
    without_overlay = _record(as_of=as_of, structural=_ancestry(root))
    overlay = _observes(_venue(), digest="aa" * 32, effective_from=as_of - timedelta(hours=1))
    with_overlay = _record(as_of=as_of, knowledge=(overlay,))
    bare_chains = {_geographic_chain(path.steps) for path in project_pattern_path_details(without_overlay)}
    overlay_details = project_pattern_path_details(with_overlay)
    overlay_chains = {_geographic_chain(path.steps) for path in overlay_details}
    assert _FULL_GEOGRAPHIC_CHAIN in bare_chains
    assert _FULL_GEOGRAPHIC_CHAIN in overlay_chains
    assert any(path.steps[-1].relation_kind == "OBSERVES" for path in overlay_details)
    assert all(path.steps[-1].relation_kind != "OBSERVES" for path in project_pattern_path_details(without_overlay))
    assert any(path.steps[-1].relation_kind == "ISSUED_BY" for path in project_pattern_path_details(without_overlay))
    assert any(
        path.steps[-1].relation_kind == "MEMBER_OF_FAMILY" for path in project_pattern_path_details(without_overlay)
    )


def test_partial_snapshot_with_complete_ancestry_remains_pattern_eligible() -> None:
    as_of = datetime(2026, 8, 27, 1, 15, tzinfo=UTC)
    root = _instrument()
    members = _ancestry(root) + tuple(
        _structural("TRADED_ON", _instrument(f"I{index:02d}"), _venue()) for index in range(20)
    )
    market = _market_episode(symbol="2330", as_of=as_of)
    snapshot = replace(
        _snapshot(market, structural=members),
        status="partial",
        missingness={"budget": "graph_budget_exceeded"},
        snapshot_id=None,
        content_sha256=None,
    )
    details = project_pattern_path_details(_PathSource(snapshot=snapshot, structural_relations=members))
    assert _FULL_GEOGRAPHIC_CHAIN in {_geographic_chain(path.steps) for path in details}


def test_budget_truncated_capture_still_projects_valid_branches_and_overlays() -> None:
    as_of = datetime(2026, 8, 27, 4, 30, tzinfo=UTC)
    root = _instrument()
    ancestry = _ancestry(root)
    truncated = ancestry[:2]
    branches = tuple(item for item in ancestry if item.kind in {"ISSUED_BY", "MEMBER_OF_FAMILY"})
    overlay = _observes(_venue(), digest="bb" * 32, effective_from=as_of - timedelta(hours=1))
    members = truncated + branches
    market = _market_episode(symbol="2330", as_of=as_of)
    snapshot = replace(
        _snapshot(market, structural=members, knowledge=(overlay,)),
        status="partial",
        missingness={"budget": "graph_budget_exceeded", "ancestry": "incomplete"},
        snapshot_id=None,
        content_sha256=None,
    )
    details = project_pattern_path_details(
        _PathSource(
            snapshot=snapshot,
            structural_relations=members,
            knowledge_relations=(overlay,),
            driver_state_bindings=(_binding(overlay, _regime_driver()),),
        )
    )
    chains = {_geographic_chain(path.steps) for path in details}
    assert not any(chain == _FULL_GEOGRAPHIC_CHAIN[:length] for chain in chains for length in range(1, 5))
    assert (("instrument", "ISSUED_BY", "company"),) in chains
    assert (("instrument", "MEMBER_OF_FAMILY", "family"),) in chains
    assert any(path.steps[-1].relation_kind == "OBSERVES" for path in details)
