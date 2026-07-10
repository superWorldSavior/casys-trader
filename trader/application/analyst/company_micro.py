"""Application use case for longitudinal per-symbol company analysis."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from trader.domain.company import CompanyEvidenceSnapshot, CompanyIntelligenceBrief


@dataclass(frozen=True)
class CompanyMicroAnalysisRequest:
    symbol: str
    as_of: str
    evidence: CompanyEvidenceSnapshot
    depth: str = "screen"
    trigger: str = "scheduled"
    candidate_scope_ids: tuple[str, ...] = ()
    input_refs: dict | None = None


@dataclass(frozen=True)
class CompanyMicroAnalysisResult:
    triggered: bool
    written: bool
    brief_ref: dict[str, str] | None = None
    error_code: str | None = None
    error_message: str | None = None


@runtime_checkable
class CompanyMicroAnalyst(Protocol):
    def analyze(self, request: CompanyMicroAnalysisRequest) -> CompanyIntelligenceBrief:
        ...


@runtime_checkable
class CompanyIntelligenceRepository(Protocol):
    def append(self, brief: CompanyIntelligenceBrief) -> tuple[dict[str, str], bool]:
        ...


@runtime_checkable
class CompanyEvidenceProvider(Protocol):
    def collect(self, *, symbol: str, as_of: str) -> CompanyEvidenceSnapshot | None:
        ...


def run_company_micro_analysis(
    request: CompanyMicroAnalysisRequest,
    *,
    analyst: CompanyMicroAnalyst,
    repository: CompanyIntelligenceRepository,
) -> CompanyMicroAnalysisResult:
    """Analyze one symbol and preserve the previous current brief on failure."""

    try:
        brief = analyst.analyze(request)
        brief_ref, written = repository.append(brief)
    except Exception as exc:  # noqa: BLE001 - best-effort research must not escape the worker
        return CompanyMicroAnalysisResult(
            triggered=True,
            written=False,
            error_code=exc.__class__.__name__,
            error_message=str(exc)[:500],
        )
    return CompanyMicroAnalysisResult(
        triggered=True,
        written=written,
        brief_ref=brief_ref,
    )


__all__ = [
    "CompanyIntelligenceRepository",
    "CompanyEvidenceProvider",
    "CompanyMicroAnalysisRequest",
    "CompanyMicroAnalysisResult",
    "CompanyMicroAnalyst",
    "run_company_micro_analysis",
]
