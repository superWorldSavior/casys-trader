from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from trader.agent.tools.core import AgentToolCall
from trader.agent.universe.tools import make_get_company_briefs_spec
from trader.domain.company import CompanyIntelligenceBrief


def _brief(symbol: str = "SAP.DE") -> CompanyIntelligenceBrief:
    point = {
        "point": "Cloud backlog supports the medium-term revenue path. " * 8,
        "source_refs": ["filing:2026Q2"],
        "evidence_label": "analyst_interpretation",
        "confidence": "medium",
    }
    brief = CompanyIntelligenceBrief.from_mapping(
        {
            "symbol": symbol,
            "as_of": "2026-07-10T01:00:00+00:00",
            "input_signature": "sig-1",
            "depth": "screen",
            "issuer_identity": {"issuer_name": "SAP SE", "identity_status": "verified"},
            "coverage": {"status": "full"},
            "business": {"summary": "Enterprise software vendor.", "points": [point]},
            "financial_snapshot": {"summary": "Cash flow remains positive.", "points": [point]},
            "earnings_and_guidance": {"summary": "Guidance was raised.", "points": [point]},
            "company_thesis": {"status": "intact", "summary": "Cloud transition remains on track.", "pillars": [point]},
            "catalysts": [point],
            "risks": [point],
            "selection_view": {
                "posture": "supports_selection",
                "confidence": "medium",
                "reasons": ["Evidence is supportive."],
            },
            "security_readiness": "conditional",
            "source_refs": ["filing:2026Q2"],
        }
    )
    assert brief is not None
    return brief


class FakeStore:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], str]] = []

    def read_current_many(self, symbols, *, depth="screen"):
        requested = list(symbols)
        self.calls.append((requested, depth))
        return {symbol: (_brief(symbol) if symbol == "SAP.DE" else None) for symbol in requested}


def test_get_company_briefs_projects_only_requested_sections_and_bounds_them() -> None:
    store = FakeStore()
    spec = make_get_company_briefs_spec(store)
    call = AgentToolCall(
        id="briefs-1",
        tool="get_company_briefs",
        args={"symbols": ["SAP.DE", "MISS"], "sections": ["thesis", "risks"], "max_chars": 200},
    )

    payload = spec.handler(call, SimpleNamespace(intelligence_store=store))

    assert store.calls == [(["SAP.DE", "MISS"], "screen")]
    assert payload["rows"][1] == {"symbol": "MISS", "error": "not_found"}
    row = payload["rows"][0]
    assert row["symbol"] == "SAP.DE"
    assert row["as_of"] == "2026-07-10T01:00:00+00:00"
    assert row["brief_ref"]["brief_id"]
    assert row["selection_view"] == {"posture": "supports_selection", "confidence": "medium"}
    assert set(row["sections"]) == {"thesis", "risks"}
    assert "business" not in row["sections"]
    assert len(json.dumps(row["sections"], ensure_ascii=False, separators=(",", ":"))) <= 200


@pytest.mark.parametrize(
    ("args", "message"),
    [
        ({}, "symbols"),
        ({"symbols": []}, "symbols"),
        ({"symbols": ["A"] * 11}, "symbols"),
        ({"symbols": ["A", 3]}, "symbols"),
        ({"symbols": ["A"], "sections": ["unknown"]}, "sections"),
        ({"symbols": ["A"], "sections": "risks"}, "sections"),
        ({"symbols": ["A"], "max_chars": True}, "max_chars"),
        ({"symbols": ["A"], "max_chars": 199}, "max_chars"),
        ({"symbols": ["A"], "max_chars": 4001}, "max_chars"),
    ],
)
def test_get_company_briefs_rejects_invalid_args(args: dict, message: str) -> None:
    error = make_get_company_briefs_spec(FakeStore()).validate_args(args)

    assert error is not None
    assert message in error


def test_get_company_briefs_defaults_to_all_sections_and_default_budget() -> None:
    store = FakeStore()
    spec = make_get_company_briefs_spec(store)

    payload = spec.handler(
        AgentToolCall(id="briefs-1", tool="get_company_briefs", args={"symbols": ["SAP.DE"]}),
        SimpleNamespace(intelligence_store=store),
    )

    assert set(payload["rows"][0]["sections"]) == {
        "thesis",
        "catalysts",
        "risks",
        "financial",
        "earnings",
        "business",
    }
    encoded = json.dumps(payload["rows"][0]["sections"], ensure_ascii=False, separators=(",", ":"))
    assert len(encoded) <= 1200
