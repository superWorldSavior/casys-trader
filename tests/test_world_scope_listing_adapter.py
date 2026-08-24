from __future__ import annotations

from pathlib import Path

import yaml

from trader.infrastructure.files.world_scope_mapping_config import YamlWorldScopeMappingStore
from trader.infrastructure.market_sources.world_scope_listing import (
    InstrumentListingMetadataAdapter,
    YFinanceListingExchangeLookup,
)
from trader.market.rotation.venues import tick as venue_tick


class _Ticker:
    def __init__(self, exchange: str) -> None:
        self._exchange = exchange

    def get_info(self):
        return {"exchange": self._exchange}


def test_listing_adapter_uses_nasdaq_and_nyse_exchange_codes() -> None:
    lookup = YFinanceListingExchangeLookup(
        ticker_factory=lambda symbol: _Ticker("NMS" if symbol == "REGN" else "NYQ")
    )
    adapter = InstrumentListingMetadataAdapter(exchange_lookup=lookup)
    nasdaq = adapter.lookup(market_venue="US", instrument="REGN")
    nyse = adapter.lookup(market_venue="US", instrument="NEM")
    suffixed = adapter.lookup(market_venue="EU", instrument="SAP.DE")
    assert nasdaq.exchange_code == "NMS"
    assert nyse.exchange_code == "NYQ"
    assert suffixed.exchange_code is None
    assert "yfinance.exchange:NMS" in nasdaq.provider_proofs
    assert "yfinance.exchange:NYQ" in nyse.provider_proofs


def test_mapping_store_roundtrip_preserves_schema_and_recomputes_operator_hash(tmp_path: Path) -> None:
    from tests.package_layout._helpers import REPO_ROOT
    from trader.application.world_model.world_scope_resolver import WorldScopeResolver
    from trader.domain.world_scope import (
        WorldCanonicalScopeRef,
        WorldMarketAnchorRef,
        WorldScopeMapping,
        WorldScopeMappingEntry,
    )

    source = REPO_ROOT / "config" / "world_scope_mapping.yaml"
    target = tmp_path / "world_scope_mapping.yaml"
    target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    store = YamlWorldScopeMappingStore(target)
    current = store.load()
    extra = WorldScopeMappingEntry(
        anchor=WorldMarketAnchorRef(market_venue="US", instrument="ZZZZ"),
        venue=WorldCanonicalScopeRef(kind="venue", entity_id="mic:XNAS"),
        country=WorldCanonicalScopeRef(kind="country", entity_id="iso-3166:US"),
        region=WorldCanonicalScopeRef(kind="region", entity_id="iso-un-m49:021"),
        world=WorldCanonicalScopeRef(kind="world", entity_id="market"),
        provider_proofs=("yfinance.exchange:NMS=mic:XNAS",),
        taxonomy_version="listing_exchange.v1",
    )
    updated = WorldScopeMapping(
        mapping_id=current.mapping_id,
        entries=tuple(current.entries) + (extra,),
    )
    store.save(updated)
    reloaded = WorldScopeResolver.load(target)
    assert reloaded.mapping.mapping_id == "world_scope_mapping.v1"
    assert reloaded.mapping.content_sha256 == updated.content_sha256
    assert reloaded.mapping.content_sha256 != current.content_sha256
    assert reloaded.resolve(WorldMarketAnchorRef(market_venue="US", instrument="GM")).status == "resolved"
    assert reloaded.resolve(WorldMarketAnchorRef(market_venue="US", instrument="ZZZZ")).scopes[0].entity_id == "mic:XNAS"


def test_venue_rotation_observer_failure_does_not_block_universe_write(tmp_path: Path) -> None:
    import json

    from tests.test_rotation_venues import _write_tick_config_with_preopen

    config_dir = tmp_path / "cfg"
    state_dir = tmp_path / "state"
    config_dir.mkdir()
    state_dir.mkdir()
    _write_tick_config_with_preopen(config_dir)
    (state_dir / "venue_state.json").write_text(
        json.dumps(
            {
                "venues": {
                    "TW": {
                        "hotlist": ["2330.TW"],
                        "scores": {"2330.TW": 1.9},
                        "dwell": {"2330.TW": 1},
                        "last_close_at": "2026-06-15T05:30:00+00:00",
                        "stale": False,
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    seen: list[list[str]] = []

    def _boom(symbols):
        seen.append(list(symbols))
        raise RuntimeError("mapping down")

    result = venue_tick(
        str(config_dir),
        str(state_dir),
        "2026-06-16T00:30:00+00:00",
        rank_fn=lambda: {
            "ranked": [],
            "gap_adverse": frozenset(),
            "ineligible": {},
            "components_by_symbol": {},
        },
        sticky_fn=lambda: set(),
        universe_written_observer=_boom,
    )
    written = yaml.safe_load((config_dir / "universe.yaml").read_text(encoding="utf-8"))["symbols"]
    assert "2330.TW" in written
    assert result["written"] is True
    assert seen
    assert "2330.TW" in seen[0]
