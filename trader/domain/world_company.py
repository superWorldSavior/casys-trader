"""Closed company-signal vocabulary for ABOUT overlay hops. Stdlib/domain only.

``DriverCompanyBundle`` is frozen onto ``DriverState`` for
``company_intelligence`` ABOUT relations. It copies closed brief semantics
(sector, thesis/coverage/freshness statuses, catalyst/risk count buckets,
analysis depth), never point text, pillar content, symbols, or issuer names.
Decision-flavored brief fields (selection posture, security readiness) are
deliberately excluded: a World context record must not carry selection
judgment. Closed sets are derived from the brief contract Literals, so the
brief module stays the single source of truth.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, get_args

from trader.domain.company.intelligence import (
    AnalysisDepth,
    CompanyIntelligenceBrief,
    CoverageStatus,
    Freshness,
    CompanyThesisStatus,
)
from trader.domain.world_family_catalog import normalize_family_id

COMPANY_PRODUCER_VERSION = "company_intelligence_brief.v1"
COMPANY_TRANSFORM_VERSION = "company_signal.v1"
DRIVER_COMPANY_BUNDLE_SCHEMA = "driver_company_bundle.v1"

COMPANY_THESIS_STATUSES = frozenset(get_args(CompanyThesisStatus))
COMPANY_COVERAGE_STATUSES = frozenset(get_args(CoverageStatus))
COMPANY_FRESHNESS_STATUSES = frozenset(get_args(Freshness))
COMPANY_DEPTHS = frozenset(get_args(AnalysisDepth))
COMPANY_COUNT_BUCKETS = frozenset({"none", "few", "many"})

_FRESHNESS_RANK = {"missing": 0, "stale": 1, "unknown": 2, "fresh": 3}
_FRESHNESS_SECTIONS = ("business", "financial_snapshot", "earnings_and_guidance")

_DRIVER_COMPANY_BUNDLE_KEYS = frozenset(
    {
        "schema_version",
        "sector",
        "thesis_status",
        "coverage_status",
        "freshness_status",
        "catalyst_bucket",
        "risk_bucket",
        "depth",
    }
)
_FORBIDDEN_COMPANY_KEYS = frozenset(
    {
        "brief_id",
        "symbol",
        "issuer_name",
        "point",
        "summary",
        "pillars",
        "reasons",
        "posture",
        "selection_view",
        "security_readiness",
        "source_refs",
        "text",
        "report_text",
    }
)


def _required_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a non-empty string")
    text = value.strip()
    if not text:
        raise ValueError(f"{field_name} must be a non-empty string")
    return text


def _closed_member(value: Any, field_name: str, allowed: frozenset[str]) -> str:
    text = _required_text(value, field_name)
    if text not in allowed:
        allowed_text = ", ".join(sorted(allowed))
        raise ValueError(f"{field_name} must be one of: {allowed_text}")
    return text


def _sector_id(value: Any) -> str:
    return normalize_family_id(value)


def count_bucket(count: Any) -> str:
    """Closed 0 / 1-2 / 3+ bucket for catalyst and risk list lengths."""

    if isinstance(count, bool) or not isinstance(count, int):
        raise TypeError("count must be an integer")
    if count < 0:
        raise ValueError("count must be non-negative")
    if count == 0:
        return "none"
    if count <= 2:
        return "few"
    return "many"


def worst_freshness(values: Any) -> str:
    """Most degraded freshness wins: missing > stale > unknown > fresh."""

    if isinstance(values, (str, bytes, bytearray)) or not isinstance(values, (tuple, list)):
        raise TypeError("freshness values must be a list or tuple of status strings")
    if not values:
        return "unknown"
    ranked = [_closed_member(item, "freshness", COMPANY_FRESHNESS_STATUSES) for item in values]
    unranked = sorted(set(ranked) - _FRESHNESS_RANK.keys())
    if unranked:
        raise ValueError(f"freshness statuses have no degradation rank: {', '.join(unranked)}")
    return min(ranked, key=lambda item: _FRESHNESS_RANK[item])


def company_signal_from_brief(
    brief: CompanyIntelligenceBrief | Mapping[str, Any],
    *,
    sector: str,
) -> DriverCompanyBundle:
    """Pure brief → closed company bundle. Selection judgment never enters."""

    if isinstance(brief, Mapping):
        parsed = CompanyIntelligenceBrief.from_mapping(brief)
        if parsed is None:
            raise ValueError("company brief is missing symbol, as_of, signature, or depth")
        brief = parsed
    if not isinstance(brief, CompanyIntelligenceBrief):
        raise TypeError("brief must be CompanyIntelligenceBrief or a mapping")
    freshness = worst_freshness(
        [getattr(brief, section).freshness.at(brief.as_of) for section in _FRESHNESS_SECTIONS]
    )
    coverage_status = brief.coverage.get("status") if isinstance(brief.coverage, Mapping) else None
    return DriverCompanyBundle(
        sector=_sector_id(sector),
        thesis_status=brief.company_thesis.status,
        coverage_status=coverage_status,
        freshness_status=freshness,
        catalyst_bucket=count_bucket(len(brief.catalysts)),
        risk_bucket=count_bucket(len(brief.risks)),
        depth=brief.depth,
    )


@dataclass(frozen=True)
class DriverCompanyBundle:
    """Closed company semantics for one ABOUT overlay hop. No raw text."""

    sector: str
    thesis_status: str
    coverage_status: str
    freshness_status: str
    catalyst_bucket: str
    risk_bucket: str
    depth: str
    schema_version: str = DRIVER_COMPANY_BUNDLE_SCHEMA

    def __post_init__(self) -> None:
        schema_version = _required_text(self.schema_version, "schema_version")
        if schema_version != DRIVER_COMPANY_BUNDLE_SCHEMA:
            raise ValueError(f"schema_version must be {DRIVER_COMPANY_BUNDLE_SCHEMA}")
        object.__setattr__(self, "schema_version", schema_version)
        object.__setattr__(self, "sector", _sector_id(self.sector))
        object.__setattr__(
            self, "thesis_status", _closed_member(self.thesis_status, "thesis_status", COMPANY_THESIS_STATUSES)
        )
        object.__setattr__(
            self,
            "coverage_status",
            _closed_member(self.coverage_status, "coverage_status", COMPANY_COVERAGE_STATUSES),
        )
        object.__setattr__(
            self,
            "freshness_status",
            _closed_member(self.freshness_status, "freshness_status", COMPANY_FRESHNESS_STATUSES),
        )
        object.__setattr__(
            self, "catalyst_bucket", _closed_member(self.catalyst_bucket, "catalyst_bucket", COMPANY_COUNT_BUCKETS)
        )
        object.__setattr__(
            self, "risk_bucket", _closed_member(self.risk_bucket, "risk_bucket", COMPANY_COUNT_BUCKETS)
        )
        object.__setattr__(self, "depth", _closed_member(self.depth, "depth", COMPANY_DEPTHS))

    def identity_tuple(self) -> tuple[str, str, str, str, str, str, str]:
        return (
            self.sector,
            self.thesis_status,
            self.coverage_status,
            self.freshness_status,
            self.catalyst_bucket,
            self.risk_bucket,
            self.depth,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "sector": self.sector,
            "thesis_status": self.thesis_status,
            "coverage_status": self.coverage_status,
            "freshness_status": self.freshness_status,
            "catalyst_bucket": self.catalyst_bucket,
            "risk_bucket": self.risk_bucket,
            "depth": self.depth,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | DriverCompanyBundle) -> DriverCompanyBundle:
        if isinstance(value, DriverCompanyBundle):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("driver company bundle must be DriverCompanyBundle or a mapping")
        leaked = sorted(str(key) for key in value if key in _FORBIDDEN_COMPANY_KEYS)
        if leaked:
            raise ValueError(f"driver company bundle forbids raw fields: {', '.join(leaked)}")
        unknown = sorted(str(key) for key in value if key not in _DRIVER_COMPANY_BUNDLE_KEYS)
        if unknown:
            raise ValueError(f"driver company bundle forbids unknown fields: {', '.join(unknown)}")
        return cls(
            schema_version=value.get("schema_version", DRIVER_COMPANY_BUNDLE_SCHEMA),
            sector=value.get("sector"),
            thesis_status=value.get("thesis_status"),
            coverage_status=value.get("coverage_status"),
            freshness_status=value.get("freshness_status"),
            catalyst_bucket=value.get("catalyst_bucket"),
            risk_bucket=value.get("risk_bucket"),
            depth=value.get("depth"),
        )


__all__ = [
    "COMPANY_COUNT_BUCKETS",
    "COMPANY_COVERAGE_STATUSES",
    "COMPANY_DEPTHS",
    "COMPANY_FRESHNESS_STATUSES",
    "COMPANY_PRODUCER_VERSION",
    "COMPANY_THESIS_STATUSES",
    "COMPANY_TRANSFORM_VERSION",
    "DRIVER_COMPANY_BUNDLE_SCHEMA",
    "DriverCompanyBundle",
    "company_signal_from_brief",
    "count_bucket",
    "worst_freshness",
]
