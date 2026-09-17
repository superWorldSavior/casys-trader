from __future__ import annotations

from dataclasses import FrozenInstanceError
from typing import get_args

import pytest

from tests.package_layout._helpers import REPO_ROOT, _domain_import_violations
from trader.domain.company.intelligence import (
    AnalysisDepth,
    CompanyIntelligenceBrief,
    CoverageStatus,
    CompanyThesisStatus,
    Freshness,
)
from trader.domain.world_company import (
    COMPANY_COVERAGE_STATUSES,
    COMPANY_DEPTHS,
    COMPANY_FRESHNESS_STATUSES,
    COMPANY_THESIS_STATUSES,
    DRIVER_COMPANY_BUNDLE_SCHEMA,
    DriverCompanyBundle,
    _FRESHNESS_RANK,
    company_signal_from_brief,
    count_bucket,
    worst_freshness,
)

MODULE_PATH = REPO_ROOT / "trader" / "domain" / "world_company.py"


def _brief(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "brief_id": "company_micro:v1:CARL-B.CO:abc",
        "symbol": "CARL-B.CO",
        "as_of": "2026-09-05T17:26:49+00:00",
        "input_signature": "0" * 64,
        "depth": "screen",
        "issuer_identity": {"issuer_name": "Carlsberg A/S", "identity_status": "verified"},
        "coverage": {"status": "full"},
        "business": {"freshness": {"freshness": "fresh"}},
        "financial_snapshot": {"freshness": {"freshness": "stale"}},
        "earnings_and_guidance": {"freshness": {"freshness": "unknown"}},
        "company_thesis": {"status": "watch"},
        "catalysts": [
            {"point": "Guidance lifted.", "source_refs": ("r1",)},
            {"point": "JV planned.", "source_refs": ("r2",)},
            {"point": "IPO process.", "source_refs": ("r3",)},
        ],
        "risks": [{"point": "Beer declines.", "source_refs": ("r4",)}],
        "selection_view": {"posture": "insufficient_evidence", "confidence": "low"},
        "security_readiness": "not_decision_grade",
        "source_refs": ("s1",),
    }
    values.update(overrides)
    return values


def _bundle(**overrides: object) -> DriverCompanyBundle:
    values: dict[str, object] = {
        "sector": "eu_consumer",
        "thesis_status": "watch",
        "coverage_status": "full",
        "freshness_status": "stale",
        "catalyst_bucket": "many",
        "risk_bucket": "few",
        "depth": "screen",
    }
    values.update(overrides)
    return DriverCompanyBundle(**values)  # type: ignore[arg-type]


def test_count_buckets_are_closed() -> None:
    assert count_bucket(0) == "none"
    assert count_bucket(1) == "few"
    assert count_bucket(2) == "few"
    assert count_bucket(3) == "many"
    assert count_bucket(99) == "many"
    with pytest.raises(ValueError, match="non-negative"):
        count_bucket(-1)
    with pytest.raises(TypeError, match="integer"):
        count_bucket("3")  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="integer"):
        count_bucket(True)  # type: ignore[arg-type]


def test_worst_freshness_degrades_deterministically() -> None:
    assert worst_freshness(["fresh", "fresh"]) == "fresh"
    assert worst_freshness(["fresh", "unknown", "stale"]) == "stale"
    assert worst_freshness(["stale", "missing"]) == "missing"
    assert worst_freshness([]) == "unknown"
    with pytest.raises(ValueError, match="freshness"):
        worst_freshness(["fresh", "dusty"])
    with pytest.raises(TypeError, match="list or tuple"):
        worst_freshness("fresh")  # type: ignore[arg-type]


def test_company_vocab_mirrors_brief_contract() -> None:
    assert COMPANY_THESIS_STATUSES == frozenset(get_args(CompanyThesisStatus))
    assert COMPANY_COVERAGE_STATUSES == frozenset(get_args(CoverageStatus))
    assert COMPANY_FRESHNESS_STATUSES == frozenset(get_args(Freshness))
    assert COMPANY_DEPTHS == frozenset(get_args(AnalysisDepth))
    assert COMPANY_THESIS_STATUSES
    assert COMPANY_COVERAGE_STATUSES
    assert set(_FRESHNESS_RANK) == set(COMPANY_FRESHNESS_STATUSES)


def test_company_signal_from_brief_copies_closed_semantics() -> None:
    bundle = company_signal_from_brief(_brief(), sector="eu_consumer")
    assert bundle == _bundle()
    assert bundle.schema_version == DRIVER_COMPANY_BUNDLE_SCHEMA
    assert bundle.identity_tuple() == ("eu_consumer", "watch", "full", "stale", "many", "few", "screen")


def test_company_signal_accepts_brief_objects_and_requires_sector() -> None:
    parsed = CompanyIntelligenceBrief.from_mapping(_brief())
    assert parsed is not None
    assert company_signal_from_brief(parsed, sector="eu_consumer") == _bundle()
    with pytest.raises(ValueError, match="family"):
        company_signal_from_brief(_brief(), sector="EU Consumer!")
    with pytest.raises(ValueError, match="family"):
        company_signal_from_brief(_brief(), sector="   ")


def test_company_signal_ignores_selection_judgment() -> None:
    base = company_signal_from_brief(_brief(), sector="eu_consumer")
    altered = company_signal_from_brief(
        _brief(
            selection_view={"posture": "supports_selection", "confidence": "high"},
            security_readiness="conditional",
        ),
        sector="eu_consumer",
    )
    assert altered == base


def test_company_signal_rejects_unusable_briefs() -> None:
    with pytest.raises(ValueError, match="symbol"):
        company_signal_from_brief(_brief(symbol=""), sector="eu_consumer")
    with pytest.raises(TypeError, match="CompanyIntelligenceBrief or a mapping"):
        company_signal_from_brief(42, sector="eu_consumer")  # type: ignore[arg-type]


def test_company_bundle_rejects_open_and_raw_fields() -> None:
    bundle = _bundle()
    replayed = DriverCompanyBundle.from_mapping(bundle.to_dict())
    assert replayed == bundle
    with pytest.raises(ValueError, match="thesis_status"):
        _bundle(thesis_status="moonish")
    with pytest.raises(ValueError, match="coverage_status"):
        _bundle(coverage_status="sparse")
    with pytest.raises(ValueError, match="schema_version"):
        DriverCompanyBundle.from_mapping({**bundle.to_dict(), "schema_version": "driver_company_bundle.v2"})
    for raw in ("brief_id", "symbol", "posture", "selection_view", "security_readiness", "summary"):
        with pytest.raises(ValueError, match="raw fields"):
            DriverCompanyBundle.from_mapping({**bundle.to_dict(), raw: "leak"})
    with pytest.raises(ValueError, match="unknown fields"):
        DriverCompanyBundle.from_mapping({**bundle.to_dict(), "market_cap": "large"})
    with pytest.raises(FrozenInstanceError):
        bundle.sector = "eu_tech"  # type: ignore[misc]


def test_world_company_stays_stdlib_domain() -> None:
    source = MODULE_PATH.read_text(encoding="utf-8")
    assert "trader.application" not in source
    assert "trader.infrastructure" not in source
    assert "trader.runtime" not in source
    assert _domain_import_violations([MODULE_PATH], REPO_ROOT) == []
