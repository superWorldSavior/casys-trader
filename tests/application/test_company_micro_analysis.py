from trader.application.analyst.company_micro import CompanyMicroAnalysisRequest, run_company_micro_analysis
from trader.domain.company import CompanyEvidenceItem, CompanyEvidenceSnapshot, CompanyIntelligenceBrief, IssuerIdentity


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
            "payload": {"revenue": 100},
        }
    )
    assert item is not None
    evidence = CompanyEvidenceSnapshot.build(
        symbol="AAPL",
        as_of="2026-07-10T00:00:00+00:00",
        identity=IssuerIdentity("Apple Inc.", identity_status="verified"),
        items=[item],
        coverage={"status": "partial"},
    )
    return CompanyMicroAnalysisRequest(
        symbol="AAPL",
        as_of="2026-07-10T00:00:00+00:00",
        evidence=evidence,
    )


class FakeAnalyst:
    def analyze(self, request: CompanyMicroAnalysisRequest) -> CompanyIntelligenceBrief:
        brief = CompanyIntelligenceBrief.from_mapping(
            {
                "symbol": request.symbol,
                "as_of": request.as_of,
                "input_signature": request.evidence.input_signature,
                "depth": request.depth,
                "issuer_identity": request.evidence.identity.to_dict(),
                "coverage": request.evidence.coverage,
                "company_thesis": {"status": "untested"},
                "selection_view": {"posture": "neutral", "confidence": "low"},
            }
        )
        assert brief is not None
        return brief


class FakeRepository:
    def __init__(self) -> None:
        self.briefs: list[CompanyIntelligenceBrief] = []

    def append(self, brief: CompanyIntelligenceBrief) -> tuple[dict[str, str], bool]:
        self.briefs.append(brief)
        return brief.ref(), True


def test_run_company_micro_analysis_persists_success() -> None:
    repository = FakeRepository()

    result = run_company_micro_analysis(_request(), analyst=FakeAnalyst(), repository=repository)

    assert result.written is True
    assert result.brief_ref["symbol"] == "AAPL"
    assert [brief.symbol for brief in repository.briefs] == ["AAPL"]


def test_run_company_micro_analysis_contains_failure() -> None:
    class BrokenAnalyst:
        def analyze(self, request):
            raise RuntimeError("provider unavailable")

    repository = FakeRepository()

    result = run_company_micro_analysis(_request(), analyst=BrokenAnalyst(), repository=repository)

    assert result.written is False
    assert result.error_code == "RuntimeError"
    assert repository.briefs == []
