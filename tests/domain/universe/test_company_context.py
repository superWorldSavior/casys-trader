from dataclasses import replace

from trader.domain.company import CompanyIntelligenceBrief, SourcedCompanyPoint
from trader.domain.universe import CompanyContextProjectionLimits, project_company_briefs_to_universe_context


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
    assert context.symbols["EXM"]["drivers"] == [
        "Recurring growth",
        "Margin durability",
        "This third point is bounded out",
    ]


def test_projection_applies_configured_micro_caps() -> None:
    points = tuple(SourcedCompanyPoint(point=f"Point {index}", source_refs=()) for index in range(8))
    brief = _brief()
    capped_brief = replace(
        brief,
        company_thesis=replace(brief.company_thesis, summary="x" * 500, pillars=points),
        catalysts=points,
        risks=points,
        source_refs=tuple(f"source:{index}" for index in range(8)),
    )

    context = project_company_briefs_to_universe_context(
        {"EXM": capped_brief},
        candidate_symbols=("EXM",),
        active_at="2026-07-10T09:00:00+00:00",
    )
    projected = context.symbols["EXM"]

    assert projected["summary"] == "x" * 240
    assert projected["source_refs"] == ["source:0", "source:1", "source:2", "source:3", "source:4"]
    assert len(projected["drivers"]) == 5
    assert len(projected["catalysts"]) == 5
    assert len(projected["risks"]) == 5


def test_projection_applies_explicit_non_default_limits() -> None:
    points = tuple(SourcedCompanyPoint(point=f"Point {index}", source_refs=()) for index in range(4))
    brief = _brief()
    capped_brief = replace(
        brief,
        company_thesis=replace(brief.company_thesis, summary="x" * 100, pillars=points),
        catalysts=points,
        risks=points,
        source_refs=("a", "b", "c", "d"),
    )

    context = project_company_briefs_to_universe_context(
        {"EXM": capped_brief},
        candidate_symbols=("EXM",),
        active_at="2026-07-10T09:00:00+00:00",
        limits=CompanyContextProjectionLimits(summary_chars_per_symbol=17, max_points_per_symbol=2),
    )

    assert context.symbols["EXM"]["summary"] == "x" * 17
    assert context.symbols["EXM"]["drivers"] == ["Point 0", "Point 1"]
    assert context.symbols["EXM"]["source_refs"] == ["a", "b"]


def test_projection_never_backfills_drivers_from_catalysts() -> None:
    brief = _brief()
    no_pillars = replace(brief, company_thesis=replace(brief.company_thesis, pillars=()))

    context = project_company_briefs_to_universe_context(
        {"EXM": no_pillars},
        candidate_symbols=("EXM",),
        active_at="2026-07-10T09:00:00+00:00",
    )
    projected = context.symbols["EXM"]

    assert projected["drivers"] == []
    assert projected["drivers"] != projected["catalysts"]


def test_projection_marks_expired_sections_stale() -> None:
    context = project_company_briefs_to_universe_context(
        {"EXM": _brief()},
        candidate_symbols=("EXM",),
        active_at="2026-09-01T00:00:00+00:00",
    )

    assert context.symbols["EXM"]["status"] == "stale"
