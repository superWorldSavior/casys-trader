from __future__ import annotations

import pytest
import yaml

from tests.package_layout._helpers import REPO_ROOT
from trader.application.world_model.world_scope_resolver import WorldScopeResolver
from trader.domain.world_feature_contract import (
    WORLD_SCOPE_MAPPING_ID,
    WORLD_SCOPE_MAPPING_PREDECESSOR_ID,
    WORLD_SCOPE_MAPPING_SHA256,
)
from trader.domain.world_scope import WorldMarketAnchorRef


CONFIG_DIR = REPO_ROOT / "config"
MAPPING_PATH = CONFIG_DIR / "world_scope_mapping.yaml"
UNIVERSE_PATH = CONFIG_DIR / "universe.yaml"

# Frozen live hot-set at mapping v2 (2026-08-24). Exact anchors only; no suffix fallback.
COMMITTED_UNIVERSE_ANCHORS: tuple[tuple[str, str], ...] = (
    ("EU", "AZN.L"),
    ("EU", "BAER.SW"),
    ("EU", "BLND.L"),
    ("EU", "BRBY.L"),
    ("EU", "CARL-B.CO"),
    ("EU", "DSY.PA"),
    ("EU", "EBS.VI"),
    ("EU", "ENGI.PA"),
    ("EU", "EOAN.DE"),
    ("EU", "EVO.ST"),
    ("EU", "FRE.DE"),
    ("EU", "GALP.LS"),
    ("EU", "GLE.PA"),
    ("EU", "HO.PA"),
    ("EU", "KBC.BR"),
    ("EU", "KER.PA"),
    ("EU", "LEG.DE"),
    ("EU", "MBG.DE"),
    ("EU", "METSB.HE"),
    ("EU", "NESTE.HE"),
    ("EU", "NIBE-B.ST"),
    ("EU", "NOKIA.HE"),
    ("EU", "OMV.VI"),
    ("EU", "REP.MC"),
    ("EU", "SAN.PA"),
    ("EU", "SAP.DE"),
    ("EU", "SHELL.AS"),
    ("EU", "SOLB.BR"),
    ("EU", "STMN.SW"),
    ("EU", "VOLV-B.ST"),
    ("TW", "1440.TW"),
    ("TW", "3081.TWO"),
    ("US", "GM"),
)


def _resolver() -> WorldScopeResolver:
    return WorldScopeResolver.load(MAPPING_PATH)


def test_committed_universe_anchors_are_exactly_mapped_without_suffix_fallback() -> None:
    payload = yaml.safe_load(MAPPING_PATH.read_text(encoding="utf-8"))
    assert payload["mapping_id"] == WORLD_SCOPE_MAPPING_ID == "world_scope_mapping.v2"
    assert payload["supersedes_mapping_id"] == WORLD_SCOPE_MAPPING_PREDECESSOR_ID
    assert payload["no_logical_venue_mic_fallback"] is True
    resolver = _resolver()
    assert resolver.mapping.mapping_id == WORLD_SCOPE_MAPPING_ID
    assert resolver.mapping.content_sha256 == WORLD_SCOPE_MAPPING_SHA256
    assert len(COMMITTED_UNIVERSE_ANCHORS) == 33
    assert len({item[1] for item in COMMITTED_UNIVERSE_ANCHORS}) == 33
    assert list(resolver.resolve.__code__.co_varnames[:2]) == ["self", "anchor"]
    for market_venue, instrument in COMMITTED_UNIVERSE_ANCHORS:
        resolved = resolver.resolve(WorldMarketAnchorRef(market_venue=market_venue, instrument=instrument))
        assert resolved.status == "resolved", (market_venue, instrument, resolved.status)
        assert [scope.kind for scope in resolved.scopes] == ["venue", "country", "region", "world"]
        assert resolved.mapping_id == WORLD_SCOPE_MAPPING_ID
        assert resolved.mapping_sha256 == WORLD_SCOPE_MAPPING_SHA256
        assert resolved.scopes[0].entity_id.startswith("mic:")
        wrong_venue = resolver.resolve(WorldMarketAnchorRef(market_venue="XTAI", instrument=instrument))
        assert wrong_venue.status == "unmapped"


def test_committed_universe_anchor_scopes_match_operator_proof_conventions() -> None:
    resolver = _resolver()
    expected = {
        ("TW", "1440.TW"): ("mic:XTAI", "iso-3166:TW", "iso-un-m49:030"),
        ("TW", "3081.TWO"): ("mic:XTAI", "iso-3166:TW", "iso-un-m49:030"),
        ("US", "GM"): ("mic:XNYS", "iso-3166:US", "iso-un-m49:021"),
        ("EU", "HO.PA"): ("mic:XPAR", "iso-3166:FR", "iso-un-m49:150"),
        ("EU", "GALP.LS"): ("mic:XLIS", "iso-3166:PT", "iso-un-m49:150"),
        ("EU", "AZN.L"): ("mic:XLON", "iso-3166:GB", "iso-un-m49:150"),
        ("EU", "KBC.BR"): ("mic:XBRU", "iso-3166:BE", "iso-un-m49:150"),
        ("EU", "CARL-B.CO"): ("mic:XCSE", "iso-3166:DK", "iso-un-m49:150"),
        ("EU", "BAER.SW"): ("mic:XSWX", "iso-3166:CH", "iso-un-m49:150"),
        ("EU", "REP.MC"): ("mic:XMAD", "iso-3166:ES", "iso-un-m49:150"),
        ("EU", "VOLV-B.ST"): ("mic:XSTO", "iso-3166:SE", "iso-un-m49:150"),
    }
    for anchor, scopes in expected.items():
        resolved = resolver.resolve(WorldMarketAnchorRef(market_venue=anchor[0], instrument=anchor[1]))
        assert tuple(scope.entity_id for scope in resolved.scopes[:3]) == scopes
        proofs = next(
            entry.provider_proofs
            for entry in resolver.mapping.entries
            if (entry.anchor.market_venue, entry.anchor.instrument) == anchor
        )
        assert proofs
        assert all(item.startswith("trader.") for item in proofs)


def test_live_universe_yaml_is_fully_mapped_when_present() -> None:
    if not UNIVERSE_PATH.is_file():
        pytest.skip("config/universe.yaml is local-only and not committed")
    payload = yaml.safe_load(UNIVERSE_PATH.read_text(encoding="utf-8"))
    symbols = list(payload.get("symbols") or [])
    assert symbols, "live universe.yaml must declare symbols"
    resolver = _resolver()
    by_instrument = {(entry.anchor.market_venue, entry.anchor.instrument): entry for entry in resolver.mapping.entries}
    missing: list[str] = []
    for symbol in symbols:
        matches = [anchor for anchor in by_instrument if anchor[1] == symbol]
        if not matches:
            missing.append(symbol)
            continue
        resolved = resolver.resolve(WorldMarketAnchorRef(market_venue=matches[0][0], instrument=symbol))
        assert resolved.status == "resolved", symbol
    assert missing == []
