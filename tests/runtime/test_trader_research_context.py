from datetime import datetime, timezone

from trader.application.decide.planner_batch import build_symbol_facts
from trader.domain.company import CompanyIntelligenceBrief
from trader.domain.universe import SymbolMandate, UniverseMandate
from trader.infrastructure.state_db.company_intelligence_store import CompanyIntelligenceStore
from trader.infrastructure.state_db.universe_mandate_store import UniverseMandateStore
from trader.runtime.trader_research_context import _requires_company_delta, load_trader_research_context


def _brief(symbol: str) -> CompanyIntelligenceBrief:
    brief = CompanyIntelligenceBrief.from_mapping(
        {
            "symbol": symbol,
            "as_of": "2026-07-10T08:00:00+00:00",
            "input_signature": f"sig-{symbol}",
            "depth": "screen",
            "issuer_identity": {"issuer_name": symbol, "identity_status": "verified"},
            "coverage": {"status": "partial"},
            "business": {
                "summary": f"Business for {symbol}",
                "source_refs": [f"source:{symbol}"],
                "freshness": {"freshness": "fresh"},
            },
            "company_thesis": {"status": "intact", "summary": f"Thesis for {symbol}"},
            "selection_view": {"posture": "neutral", "confidence": "medium"},
            "security_readiness": "conditional",
            "source_refs": [f"source:{symbol}"],
        }
    )
    assert brief is not None
    return brief


def test_context_is_strictly_sliced_per_symbol_and_sticky_is_manageable(tmp_path) -> None:
    config_dir = tmp_path / "config"
    state_dir = tmp_path / "state"
    config_dir.mkdir()
    (config_dir / "company_intelligence.yaml").write_text("trader_company_context_mode: observe\n")
    companies = CompanyIntelligenceStore(state_dir / "company_intelligence")
    companies.append(_brief("AAA"))
    companies.append(_brief("BBB"))
    mandates = UniverseMandateStore(state_dir / "universe_mandates")
    mandates.write_prepared(
        UniverseMandate(
            mandate_id="mandate-1",
            candidate_scope_id="scope-1",
            venue="US",
            agent_run_id="run-1",
            as_of="2026-07-10T08:00:00+00:00",
            valid_until=None,
            status="prepared",
            symbols={
                "AAA": SymbolMandate(
                    "AAA",
                    "Selected AAA",
                    "leader",
                    "constructive",
                    allowed_sides=("long",),
                    family_context={"family": "software", "posture": "favored"},
                    company_context={
                        "as_of": "2026-07-10T08:00:00+00:00",
                        "input_signature": "sig-AAA",
                        "summary": "Frozen mandate micro for AAA",
                        "event_risk": "none_known",
                        "source_refs": ["mandate-source:AAA"],
                    },
                    company_brief_ref={"brief_id": "brief-AAA"},
                    confidence="fresh",
                    directional_view="long_bias",
                    portfolio_context={"exposure_note": "Keep gross exposure bounded."},
                ),
            },
        )
    )
    mandates.activate(
        venue="US",
        candidate_scope_id="scope-1",
        as_of="2026-07-10T09:00:00+00:00",
        selected_symbols=("AAA",),
    )

    company, mandate = load_trader_research_context(
        config_dir=config_dir,
        state_dir=state_dir,
        symbols=("AAA", "BBB"),
        active_at="2026-07-10T10:00:00+00:00",
        held_symbols=("BBB",),
    )

    assert "AAA" not in company
    assert company["BBB"]["summary"] == "Thesis for BBB"
    assert "AAA" not in str(company["BBB"])

    active_mandate = mandate["AAA"]["symbol_mandate"]
    assert active_mandate == {
        "symbol": "AAA",
        "why_selected": "Selected AAA",
        "role": "leader",
        "posture": "constructive",
        "allowed_sides": ["long"],
        "family_context": {"family": "software", "posture": "favored"},
        "company_context": {
            "as_of": "2026-07-10T08:00:00+00:00",
            "input_signature": "sig-AAA",
            "summary": "Frozen mandate micro for AAA",
            "event_risk": "none_known",
            "source_refs": ["mandate-source:AAA"],
        },
        "company_brief_ref": {"brief_id": "brief-AAA"},
        "confidence": "fresh",
        "directional_view": "long_bias",
        "portfolio_context": {"exposure_note": "Keep gross exposure bounded."},
    }

    facts = build_symbol_facts(
        "AAA",
        data_age_by_symbol={},
        now=datetime(2026, 7, 10, tzinfo=timezone.utc),
        active_watches_by_symbol={},
        company_context_by_symbol=company,
        mandate_context_by_symbol=mandate,
    )
    assert "company_intelligence_delta" not in facts
    assert facts["universe_mandate"]["symbol_mandate"]["company_context"]["summary"] == (
        "Frozen mandate micro for AAA"
    )

    assert mandate["BBB"]["symbol_mandate"] == {
        "symbol": "BBB",
        "why_selected": "",
        "role": "managed_existing",
        "posture": "sticky_unmandated",
        "allowed_sides": [],
        "reduce_close_always_allowed": True,
    }


def test_context_pushes_bounded_micro_delta_only_when_newer_than_mandate(tmp_path) -> None:
    config_dir = tmp_path / "config"
    state_dir = tmp_path / "state"
    config_dir.mkdir()
    (config_dir / "company_intelligence.yaml").write_text("trader_company_context_mode: observe\n")
    companies = CompanyIntelligenceStore(state_dir / "company_intelligence")
    companies.append(_brief("AAA"))
    mandates = UniverseMandateStore(state_dir / "universe_mandates")
    mandates.write_prepared(
        UniverseMandate(
            mandate_id="mandate-1",
            candidate_scope_id="scope-1",
            venue="US",
            agent_run_id="run-1",
            as_of="2026-07-09T08:00:00+00:00",
            valid_until=None,
            status="prepared",
            symbols={
                "AAA": SymbolMandate(
                    "AAA",
                    "Selected AAA",
                    "leader",
                    "constructive",
                    company_context={
                        "as_of": "2026-07-09T08:00:00+00:00",
                        "input_signature": "old-signature",
                        "summary": "Old",
                    },
                ),
            },
        )
    )
    mandates.activate(
        venue="US",
        candidate_scope_id="scope-1",
        as_of="2026-07-10T09:00:00+00:00",
        selected_symbols=("AAA",),
    )

    company, mandate = load_trader_research_context(
        config_dir=config_dir,
        state_dir=state_dir,
        symbols=("AAA",),
        active_at="2026-07-10T10:00:00+00:00",
    )

    assert mandate["AAA"]["symbol_mandate"]["company_context"]["summary"] == "Old"
    assert company["AAA"]["authority"] == "newer_than_universe_mandate"
    assert company["AAA"]["delta_from_mandate_as_of"] == "2026-07-09T08:00:00+00:00"
    assert company["AAA"]["summary"] == "Thesis for AAA"


def test_company_delta_requires_changed_or_newer_evidence_snapshot() -> None:
    current = {"as_of": "2026-07-10T08:00:00+00:00", "input_signature": "same"}

    assert not _requires_company_delta(current, {"as_of": current["as_of"], "input_signature": "same"})
    assert not _requires_company_delta(current, {"as_of": current["as_of"], "input_signature": "old"})
    assert not _requires_company_delta(
        current,
        {"as_of": "2026-07-11T08:00:00+00:00", "input_signature": "old"},
    )
    assert _requires_company_delta(current, {"summary": "legacy mandate snapshot"})
    assert not _requires_company_delta({"input_signature": "new"}, {"as_of": current["as_of"]})
