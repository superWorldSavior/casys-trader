from __future__ import annotations

import ast
from pathlib import Path

from tests.package_layout._helpers import REPO_ROOT
from trader.domain.world_scope import (
    WorldCanonicalScopeRef,
    WorldMarketAnchorRef,
    WorldScopeMapping,
    WorldScopeMappingEntry,
)
from trader.domain.world_scope_listing import UnresolvedInstrumentListing, WorldInstrumentListing


MODULE_PATH = REPO_ROOT / "trader" / "application" / "world_model" / "scope_mapping_reconcile.py"
PORTS_PATH = REPO_ROOT / "trader" / "application" / "world_model" / "scope_mapping_ports.py"
_FORBIDDEN = ("trader.runtime", "trader.infrastructure", "trader.reporting")


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
        venue=WorldCanonicalScopeRef(kind="venue", entity_id=venue),
        country=WorldCanonicalScopeRef(kind="country", entity_id=country),
        region=WorldCanonicalScopeRef(kind="region", entity_id=region),
        world=WorldCanonicalScopeRef(kind="world", entity_id="market"),
        provider_proofs=proofs,
        taxonomy_version="sessions_mic.v1",
    )


def _mapping(*entries: WorldScopeMappingEntry) -> WorldScopeMapping:
    return WorldScopeMapping(mapping_id="world_scope_mapping.v1", entries=entries)


def _import_violations(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    violations: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            if any(node.module == prefix or node.module.startswith(f"{prefix}.") for prefix in _FORBIDDEN):
                violations.append(node.module)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if any(alias.name == prefix or alias.name.startswith(f"{prefix}.") for prefix in _FORBIDDEN):
                    violations.append(alias.name)
    return violations


class _Universe:
    def __init__(self, anchors: tuple[tuple[str, str], ...]) -> None:
        self._anchors = anchors

    def current_anchors(self) -> tuple[tuple[str, str], ...]:
        return self._anchors


class _Listings:
    def __init__(self, by_symbol: dict[str, WorldInstrumentListing | Exception]) -> None:
        self._by_symbol = by_symbol

    def lookup(self, *, market_venue: str, instrument: str) -> WorldInstrumentListing:
        item = self._by_symbol[instrument]
        if isinstance(item, Exception):
            raise item
        return item


class _Store:
    def __init__(self, mapping: WorldScopeMapping) -> None:
        self.loaded = mapping
        self.saved: WorldScopeMapping | None = None

    def load(self) -> WorldScopeMapping:
        return self.loaded

    def save(self, mapping: WorldScopeMapping) -> None:
        self.saved = mapping
        self.loaded = mapping


def test_reconcile_module_is_application_owned() -> None:
    assert MODULE_PATH.exists()
    assert PORTS_PATH.exists()
    assert _import_violations(MODULE_PATH) == []
    assert _import_violations(PORTS_PATH) == []
    source = MODULE_PATH.read_text(encoding="utf-8")
    assert "trader.market.rotation" not in source
    assert "write_universe" not in source


def test_reconcile_covers_nasdaq_and_nyse_and_preserves_existing() -> None:
    from trader.application.world_model.scope_mapping_reconcile import WorldScopeMappingReconcileService

    current = _mapping(
        _entry(
            market_venue="US",
            instrument="GM",
            venue="mic:XNYS",
            country="iso-3166:US",
            region="iso-un-m49:021",
            proofs=("provider:gm",),
        )
    )
    store = _Store(current)
    service = WorldScopeMappingReconcileService(
        universe=_Universe((("US", "GM"), ("US", "REGN"), ("US", "NEM"))),
        listings=_Listings(
            {
                "REGN": WorldInstrumentListing(
                    market_venue="US",
                    instrument="REGN",
                    exchange_code="NMS",
                    provider_proofs=("yfinance.exchange:NMS",),
                ),
                "NEM": WorldInstrumentListing(
                    market_venue="US",
                    instrument="NEM",
                    exchange_code="NYQ",
                    provider_proofs=("yfinance.exchange:NYQ",),
                ),
            }
        ),
        store=store,
    )
    result = service.reconcile(persist=True)
    assert result.action == "record_generation"
    assert result.mapping.mapping_id == "world_scope_mapping.v1"
    by_instrument = {entry.anchor.instrument: entry for entry in result.mapping.entries}
    assert by_instrument["GM"].venue.entity_id == "mic:XNYS"
    assert by_instrument["GM"].provider_proofs == ("provider:gm",)
    assert by_instrument["REGN"].venue.entity_id == "mic:XNAS"
    assert by_instrument["NEM"].venue.entity_id == "mic:XNYS"
    assert store.saved is not None
    assert store.saved.content_sha256 == result.mapping.content_sha256
    assert result.mapping.content_sha256 != current.content_sha256


def test_reconcile_is_idempotent_when_universe_already_mapped() -> None:
    from trader.application.world_model.scope_mapping_reconcile import WorldScopeMappingReconcileService

    current = _mapping(
        _entry(
            market_venue="US",
            instrument="GM",
            venue="mic:XNYS",
            country="iso-3166:US",
            region="iso-un-m49:021",
        )
    )
    store = _Store(current)
    service = WorldScopeMappingReconcileService(
        universe=_Universe((("US", "GM"),)),
        listings=_Listings({}),
        store=store,
    )
    first = service.reconcile(persist=True)
    second = service.reconcile(persist=True)
    assert first.action == second.action == "ready"
    assert first.mapping.content_sha256 == current.content_sha256 == second.mapping.content_sha256
    assert store.saved is None


def test_provider_failure_leaves_symbol_unresolved_and_does_not_write() -> None:
    from trader.application.world_model.scope_mapping_reconcile import WorldScopeMappingReconcileService

    current = _mapping(
        _entry(
            market_venue="US",
            instrument="GM",
            venue="mic:XNYS",
            country="iso-3166:US",
            region="iso-un-m49:021",
        )
    )
    store = _Store(current)
    service = WorldScopeMappingReconcileService(
        universe=_Universe((("US", "GM"), ("US", "PSKY"))),
        listings=_Listings({"PSKY": RuntimeError("provider down")}),
        store=store,
    )
    result = service.reconcile(persist=True)
    assert result.action == "ready"
    assert result.unresolved == (
        UnresolvedInstrumentListing(
            market_venue="US",
            instrument="PSKY",
            status="unresolved",
            reason="provider_failure",
        ),
    )
    assert [entry.anchor.instrument for entry in result.mapping.entries] == ["GM"]
    assert store.saved is None


def test_dry_run_does_not_persist_a_new_generation() -> None:
    from trader.application.world_model.scope_mapping_reconcile import WorldScopeMappingReconcileService

    current = _mapping(
        _entry(
            market_venue="TW",
            instrument="2330.TW",
            venue="mic:XTAI",
            country="iso-3166:TW",
            region="iso-un-m49:030",
        )
    )
    store = _Store(current)
    service = WorldScopeMappingReconcileService(
        universe=_Universe((("TW", "2330.TW"), ("EU", "AIR.PA"))),
        listings=_Listings(
            {
                "AIR.PA": WorldInstrumentListing(
                    market_venue="EU",
                    instrument="AIR.PA",
                    exchange_code=None,
                    provider_proofs=("trader.market.rotation.wiring.venue_of:.PA=EU",),
                )
            }
        ),
        store=store,
    )
    result = service.reconcile(persist=False)
    assert result.action == "record_generation"
    assert store.saved is None
    assert "AIR.PA" in {entry.anchor.instrument for entry in result.mapping.entries}


def test_future_mapping_reconcile_does_not_require_dependent_hash_edits() -> None:
    import yaml

    from trader.application.world_model.pilot_activation import activate_world_shadow_pilot
    from trader.application.world_model.scope_mapping_reconcile import WorldScopeMappingReconcileService
    from trader.domain.world_cohort import CohortPhase, WorldCohortId
    from trader.domain.world_ontology_lifecycle import market_ontology_revision_id
    from tests.application.test_world_cohort_lifecycle import (
        IDENTITY_A,
        _FixedIdentity,
        _FixedOntologyProof,
        _matching_graph_proof,
    )
    from tests.application.test_world_cohort_service import _MemoryWorldCohortStore
    from trader.application.world_model.cohort_service import WorldCohortService
    from trader.domain.world_feature_contract import WORLD_GRAPH_CONFIG_SHA256

    graph_path = REPO_ROOT / "config" / "world_graph.yaml"
    pilot_path = REPO_ROOT / "config" / "world_shadow_pilot.yaml"
    graph_before = graph_path.read_text(encoding="utf-8")
    pilot_before = pilot_path.read_text(encoding="utf-8")
    graph_payload = yaml.safe_load(graph_before)
    pilot_payload = yaml.safe_load(pilot_before)
    assert "mapping_sha256" not in graph_payload["scope_mapping"]
    assert "mapping_sha256" not in pilot_payload["scope_mapping"]
    assert graph_payload["content_sha256"] == WORLD_GRAPH_CONFIG_SHA256

    current = _mapping(
        _entry(
            market_venue="US",
            instrument="GM",
            venue="mic:XNYS",
            country="iso-3166:US",
            region="iso-un-m49:021",
        )
    )
    store = _Store(current)
    result = WorldScopeMappingReconcileService(
        universe=_Universe((("US", "GM"), ("US", "REGN"))),
        listings=_Listings(
            {
                "REGN": WorldInstrumentListing(
                    market_venue="US",
                    instrument="REGN",
                    exchange_code="NMS",
                    provider_proofs=("yfinance.exchange:NMS",),
                )
            }
        ),
        store=store,
    ).reconcile(persist=True)
    assert result.action == "record_generation"
    assert result.mapping.content_sha256 != current.content_sha256
    assert graph_path.read_text(encoding="utf-8") == graph_before
    assert pilot_path.read_text(encoding="utf-8") == pilot_before

    cohort_store = _MemoryWorldCohortStore()
    service = WorldCohortService(repository=cohort_store, query=cohort_store)
    from datetime import datetime, timezone

    report = activate_world_shadow_pilot(
        cohort_service=service,
        config_dir=REPO_ROOT / "config",
        now=datetime(2026, 8, 24, 2, 0, tzinfo=timezone.utc),
        environ={},
        runtime_identity=_FixedIdentity(IDENTITY_A),
        ontology_proof=_FixedOntologyProof(_matching_graph_proof(result.mapping)),
        ontology_revision=market_ontology_revision_id(result.mapping),
        mapping=result.mapping,
    )
    assert report.cohorts, report
    graph = cohort_store.load(
        WorldCohortId(next(item["cohort_id"] for item in report.cohorts if item["key"] == "graph"))
    )
    assert graph.manifest.scope_mapping.mapping_sha256 == result.mapping.content_sha256
    assert graph.manifest.ontology_revision == market_ontology_revision_id(result.mapping)
    assert graph.phase is CohortPhase.COLLECTING
    assert graph_path.read_text(encoding="utf-8") == graph_before
    assert pilot_path.read_text(encoding="utf-8") == pilot_before
