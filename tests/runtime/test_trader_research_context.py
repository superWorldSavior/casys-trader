from trader.domain.company import CompanyIntelligenceBrief
from trader.domain.universe import SymbolMandate, UniverseMandate
from trader.infrastructure.state_db.company_intelligence_store import CompanyIntelligenceStore
from trader.infrastructure.state_db.universe_mandate_store import UniverseMandateStore
from trader.runtime.trader_research_context import load_trader_research_context


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
                "AAA": SymbolMandate("AAA", "Selected AAA", "leader", "constructive"),
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

    assert company["AAA"]["summary"] == "Thesis for AAA"
    assert "BBB" not in str(company["AAA"])
    assert mandate["AAA"]["symbol_mandate"]["why_selected"] == "Selected AAA"
    assert mandate["BBB"]["symbol_mandate"]["posture"] == "sticky_unmandated"
    assert mandate["BBB"]["symbol_mandate"]["reduce_close_always_allowed"] is True
