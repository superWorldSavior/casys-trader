import json

from trader.agent import llm
from trader.agent.company_micro import LlmCompanyMicroAnalyst, build_company_micro_prompt
from trader.agent.company_micro.prompt import parse_company_micro_completion
from trader.application.analyst.company_micro import CompanyMicroAnalysisRequest
from trader.domain.company import CompanyEvidenceItem, CompanyEvidenceSnapshot, IssuerIdentity


def _request() -> CompanyMicroAnalysisRequest:
    item = CompanyEvidenceItem.from_mapping(
        {
            "item_id": "fixture:AAPL:10-Q",
            "symbol": "AAPL",
            "provider": "fixture",
            "kind": "filing",
            "source_ref": "fixture:AAPL:10-Q",
            "source_name": "Apple 10-Q",
            "as_of": "2026-07-09T00:00:00+00:00",
            "payload": {"revenue": 100, "currency": "USD"},
        }
    )
    assert item is not None
    evidence = CompanyEvidenceSnapshot.build(
        symbol="AAPL",
        as_of="2026-07-10T00:00:00+00:00",
        identity=IssuerIdentity("Apple Inc.", exchange="NASDAQ", identity_status="verified"),
        items=[item],
        coverage={"status": "partial", "financials": "present"},
    )
    return CompanyMicroAnalysisRequest(
        symbol="AAPL",
        as_of="2026-07-10T00:00:00+00:00",
        evidence=evidence,
        candidate_scope_ids=("scope-us",),
    )


def _completion(*, source_ref: str = "fixture:AAPL:10-Q") -> str:
    point = {
        "point": "Reported revenue remains positive.",
        "source_refs": [source_ref],
        "evidence_label": "analyst_interpretation",
        "confidence": "medium",
    }
    return json.dumps(
        {
            "business": {
                "summary": "Consumer hardware and services company.",
                "source_refs": [source_ref],
                "points": [point],
            },
            "financial_snapshot": {"points": [point]},
            "earnings_and_guidance": {"points": [point]},
            "company_thesis": {"status": "intact", "pillars": [point]},
            "catalysts": [point],
            "risks": [point],
            "open_questions": [point],
            "selection_view": {
                "posture": "supports_selection",
                "confidence": "medium",
                "reasons": ["Latest filing supports continued surveillance."],
            },
            "security_readiness": "conditional",
            "source_refs": [source_ref],
        }
    )


def test_company_micro_prompt_contains_bounded_authority_and_evidence() -> None:
    prompt = build_company_micro_prompt(_request())

    assert "fixture:AAPL:10-Q" in prompt
    assert "Tu ne sélectionnes jamais la hotlist" in prompt
    assert "security_readiness" in prompt
    assert "N'invente aucun chiffre" in prompt


def test_parser_forces_symbol_signature_and_rejects_selection_authority() -> None:
    request = _request()
    brief, error = parse_company_micro_completion(_completion(), request=request)

    assert error is None
    assert brief is not None
    assert brief.symbol == "AAPL"
    assert brief.input_signature == request.evidence.input_signature

    forbidden = json.dumps({"selected_hotlist": ["AAPL"], "business": {}})
    brief, error = parse_company_micro_completion(forbidden, request=request)
    assert brief is None
    assert error == "forbidden_authority_fields:selected_hotlist"


def test_llm_analyst_filters_untrusted_refs_and_degrades_unsourced_view() -> None:
    class Router:
        def complete(self, prompt: str, *, timeout_s: int):
            return llm.LlmCompletion(
                provider="fake",
                model="fake-model",
                text=_completion(source_ref="invented:ref"),
            )

    brief = LlmCompanyMicroAnalyst(router=Router()).analyze(_request())

    assert brief.source_refs == ()
    assert brief.business.summary == ""
    assert brief.selection_view.posture == "insufficient_evidence"
    assert brief.security_readiness == "not_decision_grade"
