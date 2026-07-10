from datetime import datetime, timezone

from trader.domain.company import (
    CompanyEvidenceItem,
    CompanyEvidenceSnapshot,
    CompanyIntelligenceBrief,
    IssuerIdentity,
)


def _evidence(symbol: str = "SAP.DE") -> CompanyEvidenceItem:
    item = CompanyEvidenceItem.from_mapping(
        {
            "item_id": f"provider:{symbol}:2026Q2",
            "symbol": symbol,
            "provider": "fixture",
            "kind": "financial_statement",
            "source_ref": f"fixture:{symbol}:2026Q2",
            "source_name": "Fixture filing",
            "as_of": "2026-07-09T00:00:00+00:00",
            "period_end": "2026-06-30",
            "currency": "EUR",
            "payload": {"revenue": 100.0},
        }
    )
    assert item is not None
    return item


def _brief_payload() -> dict:
    freshness = {
        "source_as_of": "2026-06-30",
        "verified_at": "2026-07-09T00:00:00+00:00",
        "refresh_after": "2026-08-01T00:00:00+00:00",
        "freshness": "fresh",
    }
    point = {
        "point": "Cloud backlog still supports the medium-term revenue path.",
        "source_refs": ["fixture:SAP.DE:2026Q2", "invented:ref"],
        "evidence_label": "analyst_interpretation",
        "confidence": "medium",
    }
    return {
        "symbol": "SAP.DE",
        "as_of": "2026-07-09T01:00:00+00:00",
        "input_signature": "sig-1",
        "depth": "screen",
        "issuer_identity": {
            "issuer_name": "SAP SE",
            "instrument_type": "equity",
            "exchange": "XETRA",
            "identity_status": "verified",
        },
        "coverage": {"status": "full"},
        "business": {
            "summary": "Enterprise software vendor.",
            "source_refs": ["fixture:SAP.DE:2026Q2"],
            "points": [point],
            "freshness": freshness,
        },
        "financial_snapshot": {
            "summary": "Revenue and cash flow remain positive.",
            "source_refs": ["fixture:SAP.DE:2026Q2"],
            "metrics": [{"name": "revenue", "value": 100.0, "period": "2026Q2"}],
            "freshness": freshness,
        },
        "earnings_and_guidance": {
            "summary": "Guidance unchanged.",
            "source_refs": ["fixture:SAP.DE:2026Q2"],
            "freshness": freshness,
        },
        "company_thesis": {
            "status": "intact",
            "summary": "Cloud transition remains on track.",
            "pillars": [point],
            "confirming_evidence": [point],
            "disconfirming_evidence": [],
            "kill_criteria": [point],
            "next_proof_points": [point],
        },
        "catalysts": [point],
        "risks": [point],
        "open_questions": [point],
        "selection_view": {
            "posture": "supports_selection",
            "confidence": "medium",
            "reasons": ["Thesis is supported by the latest filing."],
        },
        "security_readiness": "conditional",
        "source_refs": ["fixture:SAP.DE:2026Q2", "invented:ref"],
    }


def test_evidence_snapshot_is_stable_and_symbol_scoped() -> None:
    identity = IssuerIdentity("SAP SE", exchange="XETRA", identity_status="verified")
    snapshot = CompanyEvidenceSnapshot.build(
        symbol="SAP.DE",
        as_of="2026-07-09T00:00:00+00:00",
        identity=identity,
        items=[_evidence(), _evidence("AAPL")],
        coverage={"status": "full"},
    )
    rebuilt = CompanyEvidenceSnapshot.from_mapping(snapshot.to_dict())

    assert rebuilt is not None
    assert [item.symbol for item in rebuilt.items] == ["SAP.DE"]
    assert rebuilt.input_signature == snapshot.input_signature
    assert rebuilt.source_catalog() == {"fixture:SAP.DE:2026Q2": "Fixture filing"}


def test_brief_forces_authoritative_envelope_and_filters_unknown_sources() -> None:
    brief = CompanyIntelligenceBrief.from_mapping(_brief_payload())
    assert brief is not None

    filtered = brief.restrict_sources({"fixture:SAP.DE:2026Q2"})

    assert filtered.symbol == "SAP.DE"
    assert filtered.brief_id.startswith("company_micro:v1:SAP.DE:")
    assert filtered.source_refs == ("fixture:SAP.DE:2026Q2",)
    assert filtered.business.points[0].source_refs == ("fixture:SAP.DE:2026Q2",)
    assert filtered.selection_view.posture == "supports_selection"
    assert "selected_hotlist" not in filtered.to_dict()


def test_brief_freshness_is_evaluated_per_section() -> None:
    brief = CompanyIntelligenceBrief.from_mapping(_brief_payload())
    assert brief is not None

    assert brief.freshness_status(datetime(2026, 7, 10, tzinfo=timezone.utc)) == "fresh"
    assert brief.freshness_status(datetime(2026, 8, 2, tzinfo=timezone.utc)) == "stale"


def test_invalid_selection_authority_is_not_part_of_domain_contract() -> None:
    payload = _brief_payload()
    payload["selection_view"] = {
        "posture": "selected_hotlist",
        "confidence": "high",
        "reasons": ["invalid"],
    }
    brief = CompanyIntelligenceBrief.from_mapping(payload)

    assert brief is not None
    assert brief.selection_view.posture == "insufficient_evidence"
