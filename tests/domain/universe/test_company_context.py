from trader.domain.company import CompanyIntelligenceBrief
from trader.domain.universe import project_company_briefs_to_universe_context


def _brief(symbol: str = "EXM") -> CompanyIntelligenceBrief:
    brief = CompanyIntelligenceBrief.from_mapping(
        {
            "symbol": symbol,
            "as_of": "2026-07-10T08:00:00+00:00",
            "input_signature": "sig-1",
            "depth": "screen",
            "issuer_identity": {"issuer_name": "Example Corp", "identity_status": "verified"},
            "coverage": {"status": "full"},
            "business": {
                "summary": "A compact business summary.",
                "source_refs": ["source:1"],
                "freshness": {"freshness": "fresh", "refresh_after": "2026-08-01T00:00:00+00:00"},
            },
            "financial_snapshot": {
                "source_refs": ["source:1"],
                "freshness": {"freshness": "fresh", "refresh_after": "2026-08-01T00:00:00+00:00"},
            },
            "earnings_and_guidance": {
                "source_refs": ["source:1"],
                "freshness": {"freshness": "fresh", "refresh_after": "2026-08-01T00:00:00+00:00"},
            },
            "company_thesis": {
                "status": "intact",
                "summary": "The thesis is intact.",
                "pillars": [
                    {"point": "Recurring growth", "source_refs": ["source:1"]},
                    {"point": "Margin durability", "source_refs": ["source:1"]},
                    {"point": "This third point is bounded out", "source_refs": ["source:1"]},
                ],
            },
            "catalysts": [{"point": "Earnings", "source_refs": ["source:1"]}],
            "risks": [{"point": "Valuation", "source_refs": ["source:1"]}],
            "selection_view": {
                "posture": "supports_selection",
                "confidence": "medium",
                "reasons": ["Fundamentals support monitoring"],
            },
            "security_readiness": "conditional",
            "source_refs": ["source:1"],
        }
    )
    assert brief is not None
    return brief


def test_projection_keeps_every_candidate_and_bounds_details() -> None:
    context = project_company_briefs_to_universe_context(
        {"EXM": _brief()},
        candidate_symbols=("EXM", "MISS"),
        active_at="2026-07-10T09:00:00+00:00",
        mode="active",
    )

    assert context.coverage == {
        "fresh": 1,
        "partial": 0,
        "stale": 0,
        "missing": 1,
        "unsupported": 0,
        "identity_mismatch": 0,
    }
    assert context.symbols["MISS"] == {"status": "missing", "brief_ref": None}
    assert context.symbols["EXM"]["drivers"] == ["Recurring growth", "Margin durability"]


def test_projection_marks_expired_sections_stale() -> None:
    context = project_company_briefs_to_universe_context(
        {"EXM": _brief()},
        candidate_symbols=("EXM",),
        active_at="2026-09-01T00:00:00+00:00",
    )

    assert context.symbols["EXM"]["status"] == "stale"
