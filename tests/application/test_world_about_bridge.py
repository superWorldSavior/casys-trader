from __future__ import annotations

from datetime import datetime, timezone

import pytest

from trader.application.world_model.about_bridge import (
    build_company_about,
    build_news_about,
)
from trader.domain.company.intelligence import CompanyIntelligenceBrief
from trader.domain.world_family_catalog import FamilyCatalog
from trader.domain.world_issuer_registry import IssuerEntry, IssuerRegistry
from trader.domain.world_knowledge import (
    knowledge_artifact_content_sha256,
    knowledge_artifact_row,
)
from trader.domain.world_scope import (
    WorldCanonicalScopeRef,
    WorldMarketAnchorRef,
    WorldScopeMapping,
    WorldScopeMappingEntry,
)

UTC = timezone.utc
REVISION = "market_ontology:v1:" + "ab" * 32


def _mapping() -> WorldScopeMapping:
    def entry(venue: str, mic: str, symbol: str) -> WorldScopeMappingEntry:
        return WorldScopeMappingEntry(
            anchor=WorldMarketAnchorRef(market_venue=venue, instrument=symbol),
            venue=WorldCanonicalScopeRef(kind="venue", entity_id=f"mic:{mic}"),
            country=WorldCanonicalScopeRef(kind="country", entity_id="iso-3166:TW"),
            region=WorldCanonicalScopeRef(kind="region", entity_id="iso-un-m49:030"),
            world=WorldCanonicalScopeRef(kind="world", entity_id="market"),
            provider_proofs=("provider:listing",),
            taxonomy_version="geo.v1",
        )

    return WorldScopeMapping(
        mapping_id="world_scope_mapping.v1",
        entries=(entry("TW", "XTAI", "1301.TW"), entry("US", "XNAS", "AAPL")),
    )


def _note(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "note_key": "brief-1|symbol|1301.TW|0",
        "brief_id": "brief-1",
        "point": "Board declares a quarterly dividend.",
        "symbols": ["1301.TW"],
        "source_uuids": ["uuid-1"],
        "source_names": ["yahoo"],
        "severity": "info",
        "signal": "weak",
        "horizon": "next month",
        "direction": "bullish",
        "event_class": "capital",
        "as_of": "2026-09-10T12:00:00+00:00",
        "valid_from": "2026-09-10T12:00:00+00:00",
        "valid_until": "2026-09-11T08:00:00+00:00",
    }
    values.update(overrides)
    return values


def _brief(symbol: str = "1301.TW", **overrides: object) -> CompanyIntelligenceBrief:
    values: dict[str, object] = {
        "brief_id": f"company_micro:v1:{symbol}:abc",
        "symbol": symbol,
        "as_of": "2026-09-05T17:26:49+00:00",
        "input_signature": "0" * 64,
        "depth": "screen",
        "issuer_identity": {"issuer_name": "FP", "exchange": "TAI", "identity_status": "verified"},
        "coverage": {"status": "full"},
        "company_thesis": {"status": "watch"},
        "source_refs": ("s1",),
    }
    values.update(overrides)
    parsed = CompanyIntelligenceBrief.from_mapping(values)
    assert parsed is not None
    return parsed


def _registry() -> IssuerRegistry:
    return IssuerRegistry(
        registry_id="issuer_registry.v1",
        entries={
            "instrument:mic:XTAI:symbol:1301.TW": IssuerEntry(
                instrument_node_id="instrument:mic:XTAI:symbol:1301.TW",
                market_venue="TW",
                symbol="1301.TW",
                mic="XTAI",
                issuer_entity_id="issuer:yahoo:v1:XTAI:1301.TW",
                identity_status="verified",
                brief_as_of="2026-09-05T17:26:49+00:00",
                source_refs=("company_micro:v1:1301.TW:abc",),
                resolution_method="mapping_plus_exchange",
            )
        },
    )


def _catalog() -> FamilyCatalog:
    return FamilyCatalog.from_grouped("family_catalog.v1", {"v1:chemicals": ["1301.TW"]})


def test_news_bridge_builds_hashed_drafts_with_windows() -> None:
    build = build_news_about([_note()], _mapping(), ontology_revision=REVISION)
    assert build.excluded == ()
    assert len(build.drafts) == 1
    draft = build.drafts[0]
    assert draft.envelope.kind == "news_macro"
    assert draft.envelope.artifact_id.startswith("knowledge_artifact:v1:")
    assert draft.signal.event_class == "capital"
    assert draft.envelope.content_sha256 == knowledge_artifact_content_sha256(
        knowledge_artifact_row(draft.envelope, draft.signal.to_dict())
    )
    assert len(draft.relations) == 1
    relation = draft.relations[0]
    assert relation.kind == "ABOUT"
    assert relation.target.node_id == "instrument:mic:XTAI:symbol:1301.TW"
    assert relation.effective_from == datetime(2026, 9, 10, 12, tzinfo=UTC)
    assert relation.effective_until == datetime(2026, 9, 11, 8, tzinfo=UTC)
    assert relation.ontology_revision == "market_ontology.v1"
    assert relation.source.content_sha256 == draft.envelope.content_sha256


def test_news_bridge_keeps_expired_but_ordered_window() -> None:
    # No build-time clock: an ordered window drafts even when fully past.
    # Expiry is enforced at query time per cutoff, never here.
    build = build_news_about(
        [
            _note(
                valid_from="2020-01-01T00:00:00+00:00",
                as_of="2020-01-01T00:00:00+00:00",
                valid_until="2020-01-02T00:00:00+00:00",
            )
        ],
        _mapping(),
        ontology_revision=REVISION,
    )
    assert build.excluded == ()
    assert len(build.drafts) == 1


def test_news_bridge_fans_out_over_symbols_and_is_deterministic() -> None:
    note = _note(symbols=["1301.TW", "AAPL", "NOPE"])
    first = build_news_about([note], _mapping(), ontology_revision=REVISION)
    second = build_news_about([note], _mapping(), ontology_revision=REVISION)
    assert len(first.drafts) == 1
    assert len(first.drafts[0].relations) == 2
    assert first.drafts[0].envelope.artifact_id == second.drafts[0].envelope.artifact_id
    assert first.drafts[0].envelope.content_sha256 == second.drafts[0].envelope.content_sha256


def test_news_bridge_counts_every_exclusion() -> None:
    notes = [
        _note(note_key="empty", point="   "),
        _note(note_key="nosym", symbols=[]),
        _note(note_key="unmapped", symbols=["NOPE"]),
        _note(note_key="noclock", valid_from=None, as_of=None),
        _note(note_key="badwindow", valid_until="2026-09-10T12:00:00+00:00"),
        _note(note_key="op", is_operational=True),
        "garbage",
    ]
    with pytest.raises(TypeError, match="sequence of mappings"):
        build_news_about(notes, _mapping(), ontology_revision=REVISION)  # type: ignore[list-item]
    build = build_news_about(notes[:-1], _mapping(), ontology_revision=REVISION)
    assert build.drafts == ()
    assert build.exclusion_counts == {
        "empty_point": 1,
        "no_symbols": 1,
        "unmapped_symbols": 1,
        "missing_clocks": 1,
        "invalid_window": 1,
        "operational": 1,
    }
    with pytest.raises(TypeError, match="mapping must be WorldScopeMapping"):
        build_news_about([_note()], object(), ontology_revision=REVISION)  # type: ignore[arg-type]


def test_news_bridge_counts_missing_and_invalid_event_class() -> None:
    build = build_news_about(
        [_note(note_key="noclass", event_class=None), _note(note_key="badclass", event_class="vibes")],
        _mapping(),
        ontology_revision=REVISION,
    )
    assert build.drafts == ()
    assert build.exclusion_counts == {"missing_event_class": 1, "invalid_event_class": 1}


def test_news_bridge_uses_stored_class_without_rederivation() -> None:
    build = build_news_about([_note(event_class="regulatory")], _mapping(), ontology_revision=REVISION)
    assert build.excluded == ()
    assert len(build.drafts) == 1
    assert build.drafts[0].signal.event_class == "regulatory"
    assert build.drafts[0].signal.event_class_source == "analyst"


def test_company_bridge_builds_company_and_instrument_relations() -> None:
    build = build_company_about(
        _registry(), {"1301.TW": _brief()}, _catalog(), ontology_revision=REVISION
    )
    assert build.excluded == ()
    assert len(build.drafts) == 1
    draft = build.drafts[0]
    assert draft.envelope.kind == "company_intelligence"
    assert draft.signal.sector == "v1:chemicals"
    assert draft.envelope.content_sha256 == knowledge_artifact_content_sha256(
        knowledge_artifact_row(draft.envelope, draft.signal.to_dict())
    )
    targets = sorted(item.target.node_id for item in draft.relations)
    assert targets == [
        "company:issuer:yahoo:v1:XTAI:1301.TW",
        "instrument:mic:XTAI:symbol:1301.TW",
    ]
    assert draft.envelope.valid_until == datetime(2026, 10, 5, 17, 26, 49, tzinfo=UTC)
    brief = _brief("1301.TW")
    brief_dict = dict(brief.to_dict())
    brief_dict["issuer_identity"] = {**brief_dict["issuer_identity"], "exchange": None}
    rebuilt = CompanyIntelligenceBrief.from_mapping(brief_dict)
    assert rebuilt is not None
    unhinted = build_company_about(
        _registry(), {"1301.TW": rebuilt}, _catalog(), ontology_revision=REVISION
    )
    assert sorted(item.target.node_id for item in unhinted.drafts[0].relations) == targets
    folded = build_company_about(
        _registry(), {"1301.tw": _brief()}, _catalog(), ontology_revision=REVISION
    )
    assert folded.excluded == ()
    assert sorted(item.target.node_id for item in folded.drafts[0].relations) == targets


def test_company_bridge_counts_every_exclusion() -> None:
    registry = _registry()
    build = build_company_about(registry, {}, _catalog(), ontology_revision=REVISION)
    assert build.excluded[0].reason == "missing_brief"
    assert build.exclusion_counts == {"missing_brief": 1}
    build = build_company_about(
        registry, {"1301.TW": {"symbol": "1301.TW"}}, _catalog(), ontology_revision=REVISION
    )
    assert build.exclusion_counts == {"brief_unreadable": 1}
    empty_catalog = FamilyCatalog(catalog_id="family_catalog.v1", entries={})
    build = build_company_about(
        registry, {"1301.TW": _brief()}, empty_catalog, ontology_revision=REVISION
    )
    assert build.exclusion_counts == {"no_sector": 1}
    bad_clock = _brief()
    bad_dict = dict(bad_clock.to_dict())
    bad_dict["as_of"] = "not-a-date"
    build = build_company_about(registry, {"1301.TW": bad_dict}, _catalog(), ontology_revision=REVISION)
    assert build.exclusion_counts == {"missing_clocks": 1}
    bad_coverage = dict(_brief().to_dict())
    bad_coverage["coverage"] = {"status": "sparse"}
    build = build_company_about(
        registry, {"1301.TW": bad_coverage}, _catalog(), ontology_revision=REVISION
    )
    assert build.excluded == ()
    assert build.drafts[0].signal.coverage_status == "partial"
    corrupted = _brief()
    object.__setattr__(corrupted, "coverage", {"status": "sparse"})
    build = build_company_about(
        registry, {"1301.TW": corrupted}, _catalog(), ontology_revision=REVISION
    )
    assert build.exclusion_counts == {"signal_failed": 1}
    with pytest.raises(TypeError, match="registry must be IssuerRegistry"):
        build_company_about({}, {}, _catalog(), ontology_revision=REVISION)  # type: ignore[arg-type]


def test_bridges_reject_unadmitted_ontology_revisions() -> None:
    with pytest.raises(ValueError, match="not an admitted market ontology revision"):
        build_news_about([_note()], _mapping(), ontology_revision="ontology:draft")
    with pytest.raises(ValueError, match="not an admitted market ontology revision"):
        build_company_about(
            _registry(), {"1301.TW": _brief()}, _catalog(), ontology_revision="ontology:draft"
        )


def _scope_mapping() -> WorldScopeMapping:
    def entry(
        venue: str, mic: str, symbol: str, country: str, region: str
    ) -> WorldScopeMappingEntry:
        return WorldScopeMappingEntry(
            anchor=WorldMarketAnchorRef(market_venue=venue, instrument=symbol),
            venue=WorldCanonicalScopeRef(kind="venue", entity_id=f"mic:{mic}"),
            country=WorldCanonicalScopeRef(kind="country", entity_id=f"iso-3166:{country}"),
            region=WorldCanonicalScopeRef(kind="region", entity_id=f"iso-un-m49:{region}"),
            world=WorldCanonicalScopeRef(kind="world", entity_id="market"),
            provider_proofs=("provider:listing",),
            taxonomy_version="geo.v1",
        )

    return WorldScopeMapping(
        mapping_id="world_scope_mapping.v1",
        entries=(
            entry("TW", "XTAI", "1301.TW", "TW", "030"),
            entry("US", "XNAS", "AAPL", "US", "021"),
            entry("US", "XNYS", "BRK", "US", "021"),
            entry("EU", "XPAR", "AIR.PA", "FR", "150"),
            entry("EU", "XETR", "SAP.DE", "DE", "150"),
        ),
    )


def test_news_bridge_links_symbol_less_macro_to_finest_unambiguous_scope() -> None:
    cases = {
        "TW": "venue:mic:XTAI",
        "US": "country:iso-3166:US",
        "EU": "region:iso-un-m49:150",
        "GLOBAL": "world:market",
    }
    for venue, expected_node in cases.items():
        note = _note(
            note_key=f"macro-{venue}",
            symbols=[],
            event_class="macro",
            venue=venue,
            point="Central bank holds rates amid sticky inflation.",
        )
        first = build_news_about([note], _scope_mapping(), ontology_revision=REVISION)
        assert first.excluded == (), venue
        assert len(first.drafts) == 1
        draft = first.drafts[0]
        assert draft.envelope.kind == "news_macro"
        assert [relation.target.node_id for relation in draft.relations] == [expected_node]
        assert draft.envelope.subjects[0].node_id == expected_node
        second = build_news_about([note], _scope_mapping(), ontology_revision=REVISION)
        assert second.drafts[0].envelope.artifact_id == draft.envelope.artifact_id
        assert second.drafts[0].envelope.content_sha256 == draft.envelope.content_sha256


def test_news_bridge_counts_macro_venue_exclusions() -> None:
    build = build_news_about(
        [
            _note(note_key="blank", symbols=[], event_class="macro", venue="  "),
            _note(note_key="unknown", symbols=[], event_class="macro", venue="JP"),
            _note(note_key="micro", symbols=[], event_class="earnings", venue="US"),
        ],
        _scope_mapping(),
        ontology_revision=REVISION,
    )
    assert build.drafts == ()
    assert build.exclusion_counts == {
        "missing_venue": 1,
        "unmapped_venue": 1,
        "no_symbols": 1,
    }


def test_news_bridge_folds_symbol_case_on_join() -> None:
    build = build_news_about(
        [_note(note_key="k1", symbols=["aapl"])], _mapping(), ontology_revision=REVISION
    )
    assert build.excluded == ()
    assert build.drafts[0].relations[0].target.entity_id == "mic:XNAS:symbol:AAPL"


def test_about_stamp_stable() -> None:
    a = build_news_about([_note()], _mapping(), ontology_revision="market_ontology:v1:" + "ab" * 32)
    b = build_news_about([_note()], _mapping(), ontology_revision="market_ontology:v1:" + "cd" * 32)
    assert a.drafts[0].relations[0].relation_id == b.drafts[0].relations[0].relation_id
