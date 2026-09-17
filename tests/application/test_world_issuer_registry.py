from __future__ import annotations

import pytest

from trader.application.world_model.issuer_registry import (
    ISSUER_EXCLUSION_REASONS,
    IssuerExclusion,
    build_issuer_registry,
    index_briefs_by_symbol,
)
from trader.domain.company.intelligence import CompanyIntelligenceBrief
from trader.domain.world_scope import (
    WorldCanonicalScopeRef,
    WorldMarketAnchorRef,
    WorldScopeMapping,
    WorldScopeMappingEntry,
)

AS_OF = "2026-09-05T17:26:49+00:00"


def _brief(
    symbol: str,
    *,
    exchange: str | None = "TAI",
    status: str = "verified",
    as_of: str = AS_OF,
) -> CompanyIntelligenceBrief:
    parsed = CompanyIntelligenceBrief.from_mapping(
        {
            "brief_id": f"company_micro:v1:{symbol}:abc",
            "symbol": symbol,
            "as_of": as_of,
            "input_signature": "0" * 64,
            "depth": "screen",
            "issuer_identity": {"issuer_name": f"{symbol} Inc", "exchange": exchange, "identity_status": status},
            "coverage": {"status": "full"},
            "company_thesis": {"status": "watch"},
            "source_refs": ("s1",),
        }
    )
    assert parsed is not None
    return parsed


def _mapping(*symbols: str) -> WorldScopeMapping:
    return WorldScopeMapping(
        mapping_id="world_scope_mapping.v1",
        entries=tuple(
            WorldScopeMappingEntry(
                anchor=WorldMarketAnchorRef(market_venue="TW", instrument=symbol),
                venue=WorldCanonicalScopeRef(kind="venue", entity_id="mic:XTAI"),
                country=WorldCanonicalScopeRef(kind="country", entity_id="iso-3166:TW"),
                region=WorldCanonicalScopeRef(kind="region", entity_id="iso-un-m49:030"),
                world=WorldCanonicalScopeRef(kind="world", entity_id="market"),
                provider_proofs=("provider:listing",),
                taxonomy_version="geo.v1",
            )
            for symbol in symbols
        ),
    )


def test_build_binds_verified_briefs_with_exchange_cross_check() -> None:
    build = build_issuer_registry({"2330": _brief("2330")}, _mapping("2330"))
    assert build.excluded == ()
    entry = build.registry.issuer_for_instrument("instrument:mic:XTAI:symbol:2330")
    assert entry is not None
    assert entry.issuer_entity_id == "issuer:yahoo:v1:XTAI:2330"
    assert entry.resolution_method == "mapping_plus_exchange"
    assert entry.source_refs == ("company_micro:v1:2330:abc",)


def test_build_degrades_to_mapping_only_without_exchange() -> None:
    build = build_issuer_registry({"2330": _brief("2330", exchange=None)}, _mapping("2330"))
    assert build.excluded == ()
    entry = build.registry.issuer_for_instrument("instrument:mic:XTAI:symbol:2330")
    assert entry is not None
    assert entry.resolution_method == "mapping_only"


def test_build_counts_every_exclusion_reason() -> None:
    briefs = {
        "unverified": _brief("unverified", status="unverified"),
        "conflict": _brief("conflict", exchange="PAR"),
        "unreadable": {"symbol": "unreadable"},
    }
    build = build_issuer_registry(briefs, _mapping("absent", "unverified", "conflict", "unreadable"))
    assert build.exclusion_counts == {
        "missing_brief": 1,
        "identity_unverified": 1,
        "exchange_conflict": 1,
        "brief_unreadable": 1,
    }
    assert build.registry.entries == {}
    reasons = {(item.symbol, item.reason) for item in build.excluded}
    assert reasons == {
        ("absent", "missing_brief"),
        ("unverified", "identity_unverified"),
        ("conflict", "exchange_conflict"),
        ("unreadable", "brief_unreadable"),
    }
    conflict = next(item for item in build.excluded if item.symbol == "conflict")
    assert conflict.detail == "brief=XPAR mapping=XTAI"
    assert ISSUER_EXCLUSION_REASONS == frozenset(
        {"missing_brief", "brief_unreadable", "identity_unverified", "exchange_conflict"}
    )


def test_build_accepts_mapping_briefs_and_rejects_garbage() -> None:
    brief = _brief("2330")
    from_mapping = build_issuer_registry({"2330": brief.to_dict()}, _mapping("2330"))
    assert from_mapping.excluded == ()
    assert from_mapping.registry.content_sha256 == build_issuer_registry({"2330": brief}, _mapping("2330")).registry.content_sha256
    with pytest.raises(TypeError, match="briefs must be a mapping"):
        build_issuer_registry(42, _mapping("2330"))  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="CompanyIntelligenceBrief or mappings"):
        build_issuer_registry({"2330": 42}, _mapping("2330"))  # type: ignore[dict-item]
    with pytest.raises(TypeError, match="mapping must be WorldScopeMapping"):
        build_issuer_registry({"2330": brief}, object())  # type: ignore[arg-type]


def test_exclusion_is_closed_and_frozen() -> None:
    excluded = IssuerExclusion(market_venue="TW", symbol="2330", reason="missing_brief")
    assert excluded.to_dict()["reason"] == "missing_brief"
    with pytest.raises(ValueError, match="exclusion reason"):
        IssuerExclusion(market_venue="TW", symbol="2330", reason="vibes")


def test_build_folds_brief_keys_on_join() -> None:
    brief = _brief("2330")
    build = build_issuer_registry({"2330": brief}, _mapping("2330"))
    assert build.excluded == ()
    assert set(build.registry.entries) == {"instrument:mic:XTAI:symbol:2330"}
    folded = build_issuer_registry({"aapl": _brief("AAPL")}, _mapping("AAPL"))
    assert folded.excluded == ()
    assert set(folded.registry.entries) == {"instrument:mic:XTAI:symbol:AAPL"}
    assert index_briefs_by_symbol({"b": brief, "A": brief}) == {"B": brief, "A": brief}
    with pytest.raises(TypeError, match="briefs must be a mapping"):
        index_briefs_by_symbol(42)  # type: ignore[arg-type]


def test_build_treats_raising_parse_as_unreadable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom(_raw: object) -> CompanyIntelligenceBrief:
        raise ValueError("boom")

    monkeypatch.setattr(CompanyIntelligenceBrief, "from_mapping", _boom)
    build = build_issuer_registry({"2330": {"symbol": "2330"}}, _mapping("2330"))
    assert build.exclusion_counts == {"brief_unreadable": 1}


def test_index_briefs_first_sorted_key_wins_fold_collision() -> None:
    indexed = index_briefs_by_symbol({"aapl": _brief("aapl"), "AAPL": _brief("AAPL")})
    assert list(indexed) == ["AAPL"]
    assert indexed["AAPL"].symbol == "AAPL"
