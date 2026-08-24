from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from tests.package_layout._helpers import REPO_ROOT, _domain_import_violations
from trader.domain.world_scope import (
    WORLD_SCOPE_MAPPING_SCHEMA,
    WorldCanonicalScopeRef,
    WorldMarketAnchorRef,
    WorldScopeMapping,
    WorldScopeMappingEntry,
)
from trader.domain.world_scope_lifecycle import (
    MAPPING_GENERATION_ACTIONS,
    WorldScopeMappingGenerationPlan,
    plan_world_scope_mapping_generation,
)


MODULE_PATH = REPO_ROOT / "trader" / "domain" / "world_scope_lifecycle.py"


def _scope(kind: str, entity_id: str) -> WorldCanonicalScopeRef:
    return WorldCanonicalScopeRef(kind=kind, entity_id=entity_id)


def _entry(
    *,
    market_venue: str,
    instrument: str,
    venue: str,
    country: str,
    region: str,
    proofs: tuple[str, ...] = ("provider:listing",),
) -> WorldScopeMappingEntry:
    return WorldScopeMappingEntry(
        anchor=WorldMarketAnchorRef(market_venue=market_venue, instrument=instrument),
        venue=_scope("venue", venue),
        country=_scope("country", country),
        region=_scope("region", region),
        world=_scope("world", "market"),
        provider_proofs=proofs,
        taxonomy_version="sessions_mic.v1",
    )


def _mapping(*entries: WorldScopeMappingEntry) -> WorldScopeMapping:
    return WorldScopeMapping(mapping_id="world_scope_mapping.v1", entries=entries)


def test_lifecycle_module_is_stdlib_domain_without_schema_bump() -> None:
    assert MODULE_PATH.exists()
    assert _domain_import_violations([MODULE_PATH], REPO_ROOT) == []
    source = MODULE_PATH.read_text(encoding="utf-8")
    assert "trader.runtime" not in source
    assert "trader.infrastructure" not in source
    assert "world_scope_mapping.v2" not in source
    assert "world_scope_mapping.v3" not in source
    assert "backfill" not in source
    assert "supersede" not in source


def test_identical_proposed_entries_are_ready_and_idempotent() -> None:
    current = _mapping(
        _entry(
            market_venue="US",
            instrument="GM",
            venue="mic:XNYS",
            country="iso-3166:US",
            region="iso-un-m49:021",
        )
    )
    replayed = _entry(
        market_venue="US",
        instrument="GM",
        venue="mic:XNYS",
        country="iso-3166:US",
        region="iso-un-m49:021",
        proofs=("provider:other",),
    )
    plan = plan_world_scope_mapping_generation(current=current, proposed=(replayed,))
    assert plan.action == "ready"
    assert plan.mapping is current or plan.mapping.content_sha256 == current.content_sha256
    assert plan.mapping.mapping_id == WORLD_SCOPE_MAPPING_SCHEMA
    assert plan.added_anchors == ()
    assert MAPPING_GENERATION_ACTIONS == frozenset({"ready", "record_generation"})
    with pytest.raises(FrozenInstanceError):
        plan.action = "other"  # type: ignore[misc]


def test_new_resolved_rows_record_a_new_mapping_hash_without_rewriting_existing() -> None:
    existing = _entry(
        market_venue="US",
        instrument="GM",
        venue="mic:XNYS",
        country="iso-3166:US",
        region="iso-un-m49:021",
        proofs=("provider:gm",),
    )
    incoming = _entry(
        market_venue="US",
        instrument="REGN",
        venue="mic:XNAS",
        country="iso-3166:US",
        region="iso-un-m49:021",
        proofs=("yfinance.exchange:NMS=mic:XNAS",),
    )
    current = _mapping(existing)
    plan = plan_world_scope_mapping_generation(current=current, proposed=(incoming,))
    assert plan.action == "record_generation"
    assert plan.mapping.mapping_id == "world_scope_mapping.v1"
    assert plan.mapping.schema_version == WORLD_SCOPE_MAPPING_SCHEMA
    assert plan.mapping.content_sha256 != current.content_sha256
    instruments = {entry.anchor.instrument for entry in plan.mapping.entries}
    assert instruments == {"GM", "REGN"}
    gm = next(entry for entry in plan.mapping.entries if entry.anchor.instrument == "GM")
    assert gm.provider_proofs == existing.provider_proofs
    assert gm.venue.entity_id == "mic:XNYS"
    assert plan.added_anchors == (("US", "REGN"),)


def test_conflicting_proposal_for_existing_anchor_is_preserved_not_rewritten() -> None:
    existing = _entry(
        market_venue="US",
        instrument="GM",
        venue="mic:XNYS",
        country="iso-3166:US",
        region="iso-un-m49:021",
    )
    conflict = _entry(
        market_venue="US",
        instrument="GM",
        venue="mic:XNAS",
        country="iso-3166:US",
        region="iso-un-m49:021",
        proofs=("yfinance.exchange:NMS=mic:XNAS",),
    )
    extra = _entry(
        market_venue="US",
        instrument="AMGN",
        venue="mic:XNAS",
        country="iso-3166:US",
        region="iso-un-m49:021",
        proofs=("yfinance.exchange:NMS=mic:XNAS",),
    )
    current = _mapping(existing)
    plan = plan_world_scope_mapping_generation(current=current, proposed=(conflict, extra))
    assert plan.action == "record_generation"
    gm = next(entry for entry in plan.mapping.entries if entry.anchor.instrument == "GM")
    assert gm.venue.entity_id == "mic:XNYS"
    assert plan.preserved_conflicts == (("US", "GM"),)
    assert plan.added_anchors == (("US", "AMGN"),)


def test_generation_plan_rejects_schema_id_bump() -> None:
    current = _mapping(
        _entry(
            market_venue="TW",
            instrument="2330.TW",
            venue="mic:XTAI",
            country="iso-3166:TW",
            region="iso-un-m49:030",
        )
    )
    with pytest.raises(ValueError, match="world_scope_mapping.v1"):
        WorldScopeMappingGenerationPlan(
            action="record_generation",
            mapping=WorldScopeMapping(mapping_id="world_scope_mapping.v2", entries=current.entries),
            added_anchors=(),
            preserved_conflicts=(),
        )
