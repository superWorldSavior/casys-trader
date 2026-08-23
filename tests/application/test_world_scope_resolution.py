from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from tests.package_layout._helpers import REPO_ROOT
from trader.application.world_model.world_scope_resolver import WorldScopeResolver
from trader.domain.world_scope import WorldMarketAnchorRef, WorldScopeMapping, WorldScopeResolution


CONFIG_DIR = REPO_ROOT / "config"
MAPPING_PATH = CONFIG_DIR / "world_scope_mapping.yaml"
RESOLVER_PATH = REPO_ROOT / "trader" / "application" / "world_model" / "world_scope_resolver.py"


def _resolver() -> WorldScopeResolver:
    return WorldScopeResolver.load(MAPPING_PATH)


def test_load_committed_mapping_persists_mapping_id_and_hash() -> None:
    resolver = _resolver()
    resolved = resolver.resolve(WorldMarketAnchorRef(market_venue="TW", instrument="2301.TW"))
    assert isinstance(resolved, WorldScopeResolution)
    assert resolved.status == "resolved"
    assert resolved.mapping_id == resolver.mapping.mapping_id == "world_scope_mapping.v1"
    assert resolved.mapping_sha256 == resolver.mapping.content_sha256
    assert len(resolved.mapping_sha256) == 64
    replayed = WorldScopeResolution.from_mapping(resolved.to_dict())
    assert replayed.mapping_id == resolved.mapping_id
    assert replayed.mapping_sha256 == resolved.mapping_sha256


def test_two_suffix_resolves_to_xtai_without_a_runtime_heuristic() -> None:
    resolved = _resolver().resolve(WorldMarketAnchorRef(market_venue="TW", instrument="6488.TWO"))
    assert resolved.status == "resolved"
    assert [scope.kind for scope in resolved.scopes] == ["venue", "country", "region", "world"]
    assert [scope.entity_id for scope in resolved.scopes] == [
        "mic:XTAI",
        "iso-3166:TW",
        "iso-un-m49:030",
        "market",
    ]


def test_europe_region_is_iso_un_m49_150() -> None:
    resolved = _resolver().resolve(WorldMarketAnchorRef(market_venue="EU", instrument="HO.PA"))
    assert resolved.status == "resolved"
    assert resolved.scopes[0].entity_id == "mic:XPAR"
    assert resolved.scopes[2].kind == "region"
    assert resolved.scopes[2].entity_id == "iso-un-m49:150"


def test_gm_stays_explicitly_unmapped() -> None:
    resolver = _resolver()
    for instrument in ("BMW.DE", "AIR.PA", "GM"):
        unresolved = resolver.resolve(WorldMarketAnchorRef(market_venue="GM", instrument=instrument))
        assert unresolved.status == "unmapped"
        assert unresolved.scopes == ()
        assert unresolved.mapping_id == resolver.mapping.mapping_id
        assert unresolved.mapping_sha256 == resolver.mapping.content_sha256


def test_logical_venues_are_not_defaulted_to_a_mic() -> None:
    resolver = _resolver()
    for venue, instrument in (("TW", "UNMAPPED.TW"), ("US", "AAPL"), ("EU", "AIR.PA"), ("XTAI", "2301.TW")):
        unresolved = resolver.resolve(WorldMarketAnchorRef(market_venue=venue, instrument=instrument))
        assert unresolved.status == "unmapped"
        assert unresolved.scopes == ()


def test_resolve_signature_is_pure_anchor_lookup() -> None:
    params = inspect.signature(WorldScopeResolver.resolve).parameters
    assert list(params) == ["self", "anchor"]
    source = RESOLVER_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(RESOLVER_PATH))
    forbidden = ("trader.runtime", "trader.infrastructure", "trader.reporting")
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            assert not any(node.module == prefix or node.module.startswith(f"{prefix}.") for prefix in forbidden)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                assert not any(alias.name == prefix or alias.name.startswith(f"{prefix}.") for prefix in forbidden)
    lowered = source.lower()
    assert "xtai" not in lowered
    assert "xnys" not in lowered
    assert "xnas" not in lowered
    assert "xpar" not in lowered
    assert "news_macro" not in lowered
    assert "gdelt" not in lowered


def test_load_rejects_tampered_operator_hash(tmp_path: Path) -> None:
    import yaml

    payload = yaml.safe_load(MAPPING_PATH.read_text(encoding="utf-8"))
    payload["operator_config_sha256"] = "0" * 64
    tampered = tmp_path / "world_scope_mapping.yaml"
    tampered.write_text(yaml.safe_dump(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="operator_config_sha256"):
        WorldScopeResolver.load(tampered)


def test_loaded_mapping_is_the_domain_world_scope_mapping() -> None:
    resolver = _resolver()
    assert isinstance(resolver.mapping, WorldScopeMapping)
    assert resolver.mapping.resolve(WorldMarketAnchorRef(market_venue="TW", instrument="2404.TW")).status == "resolved"
    assert "ready_at" not in inspect.signature(WorldScopeResolver.load).parameters
    assert "ready_at" not in inspect.signature(WorldScopeResolver.resolve).parameters
