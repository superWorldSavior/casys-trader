from __future__ import annotations

from trader.domain.situation import NewsMacroBrief
from trader.domain.universe import (
    build_family_snapshot,
    candidate_scope_id,
    enrich_candidates_with_family,
    project_brief_to_universe_context,
)


def _brief() -> NewsMacroBrief:
    brief = NewsMacroBrief.from_mapping(
        {
            "brief_id": "brief-tw-1",
            "venue": "TW",
            "as_of": "2026-07-10T05:40:00+00:00",
            "valid_until": "2026-07-11T01:40:00+00:00",
            "input_refs": {
                "candidate_symbols": ["2330.TW", "2454.TW", "2317.TW"],
                "news_item_count": 80,
                "global_news_count": 2,
                "macro_series_labels": ["us_cpi"],
                "secret_raw_field": [{"title": "must never leak"}],
            },
            "zones": {
                "GLOBAL": [
                    {
                        "point": "Global liquidity is tightening",
                        "sources": ["DBnomics"],
                        "source_refs": ["macro_series:us_cpi"],
                        "severity": "watch",
                        "signal": "strong",
                    }
                ],
                "TW": [
                    {
                        "point": "Taiwan exporters face a stronger currency",
                        "sources": ["Reuters"],
                        "source_refs": ["news-global-1"],
                        "symbols": ["2330.TW", "NOT-IN-POOL"],
                        "severity": "risk",
                        "signal": "event",
                    }
                ],
            },
            "families": {
                "tw_semiconductors": [
                    {
                        "point": "Foundry demand remains firm",
                        "sources": ["Nikkei Asia"],
                        "source_refs": ["news-family-1"],
                        "symbols": ["2330.TW", "2454.TW"],
                        "signal": "strong",
                    }
                ],
                "tw_financials": [
                    {
                        "point": "Banks are sensitive to currency volatility",
                        "sources": ["Reuters"],
                        "source_refs": ["news-family-2"],
                        "symbols": ["2881.TW"],
                    }
                ],
            },
            "symbols": {
                "2330.TW": [
                    {
                        "point": "TSMC guides above expectations",
                        "sources": ["Reuters"],
                        "source_refs": ["news-symbol-1"],
                        "symbols": ["2330.TW"],
                        "severity": "watch",
                        "signal": "event",
                    }
                ],
                "2317.TW": [
                    {
                        "point": "Foxconn announces a routine board meeting",
                        "sources": ["Company filing"],
                        "source_refs": ["news-symbol-2"],
                        "symbols": ["2317.TW"],
                    }
                ],
            },
            "alerts": [
                {
                    "point": "Event risk is elevated into the open",
                    "sources": ["Reuters"],
                    "source_refs": ["news-alert-1"],
                    "symbols": ["2330.TW", "OUTSIDE"],
                    "severity": "risk",
                    "signal": "event",
                }
            ],
        }
    )
    assert brief is not None
    return brief


def test_project_brief_is_bounded_auditable_and_pool_scoped() -> None:
    context = project_brief_to_universe_context(
        _brief(),
        venue="TW",
        candidate_symbols=("2330.TW", "2454.TW"),
        active_at="2026-07-10T23:30:00+00:00",
        coverage_metadata={"candidates_with_news": 1, "news_cap_hit": True},
    )
    payload = context.to_dict()

    assert payload["status"] == "active"
    assert payload["brief_ref"] == {
        "date": "2026-07-10",
        "venue": "TW",
        "brief_id": "brief-tw-1",
        "as_of": "2026-07-10T05:40:00+00:00",
    }
    assert payload["metadata"] == {
        "brief_id": "brief-tw-1",
        "venue": "TW",
        "as_of": "2026-07-10T05:40:00+00:00",
        "valid_until": "2026-07-11T01:40:00+00:00",
    }
    assert "input_refs" not in payload
    assert "must never leak" not in str(payload)
    assert set(payload["families"]) == {"tw_semiconductors", "tw_financials"}
    assert set(payload["symbols"]) == {"2330.TW"}
    assert payload["zones"]["TW"][0]["symbols"] == ["2330.TW"]
    assert payload["alerts"][0]["symbols"] == ["2330.TW"]
    assert payload["symbols"]["2330.TW"][0]["sources"] == ["Reuters"]
    assert payload["symbols"]["2330.TW"][0]["source_refs"] == ["news-symbol-1"]
    assert payload["coverage"] == {
        "status": "partial",
        "candidate_count": 3,
        "candidates_with_news": 1,
        "news_items_injected": 80,
        "news_cap_hit": True,
        "global_headlines": "present",
        "macro_series": "present",
    }


def test_projection_prioritizes_risk_event_and_strong_under_caps() -> None:
    context = project_brief_to_universe_context(
        _brief(),
        venue="TW",
        candidate_symbols=("2330.TW", "2317.TW"),
        active_at="2026-07-10T23:30:00+00:00",
        max_points=2,
        max_text_chars=500,
    )
    payload = context.to_dict()
    points = [
        point["point"]
        for section in (payload["zones"], payload["families"], payload["symbols"])
        for rows in section.values()
        for point in rows
    ] + [point["point"] for point in payload["alerts"]]

    assert len(points) == 2
    assert "Taiwan exporters face a stronger currency" in points
    assert "Event risk is elevated into the open" in points
    assert payload["truncated"] is True
    assert payload["point_count"] == 2
    assert payload["text_chars"] <= 500
    # Family keys remain visible even when their lower-priority points are pruned.
    assert set(payload["families"]) == {"tw_semiconductors", "tw_financials"}


def test_projection_rejects_inactive_or_wrong_venue_brief_without_leaking_content() -> None:
    expired = project_brief_to_universe_context(
        _brief(),
        venue="TW",
        candidate_symbols=("2330.TW",),
        active_at="2026-07-11T01:40:00+00:00",
    )
    mismatch = project_brief_to_universe_context(
        _brief(),
        venue="EU",
        candidate_symbols=("SAP.DE",),
        active_at="2026-07-10T23:30:00+00:00",
    )

    assert expired.to_dict() == {
        "status": "inactive",
        "coverage": {
            "status": "missing",
            "candidate_count": 1,
            "candidates_with_news": "unknown",
            "news_items_injected": "unknown",
            "news_cap_hit": "unknown",
            "global_headlines": "unknown",
            "macro_series": "unknown",
        },
    }
    assert mismatch.to_dict()["status"] == "venue_mismatch"
    assert "TSMC" not in str(mismatch.to_dict())


def test_candidate_enrichment_family_snapshot_and_scope_id_are_stable() -> None:
    candidates = [
        {
            "symbol": "SAP.DE",
            "attractiveness": 0.7,
            "bias": "long",
            "candidate_source": "radar",
        },
        {
            "symbol": "ASML.AS",
            "attractiveness": 0.8,
            "bias": "neutral",
            "candidate_source": "fresh_news",
            "fresh_news": {"source_refs": ["u2", "u1"]},
        },
    ]
    enriched = enrich_candidates_with_family(candidates)
    snapshot = build_family_snapshot(enriched, baseline=("SAP.DE",), sticky=("ASML.AS", "UNKNOWN"))
    scope = candidate_scope_id("EU", candidates, ("SAP.DE",), "2026-07-10T15:30:00+00:00")

    assert [item["family"] for item in enriched] == ["eu_tech", "eu_tech"]
    assert snapshot["eu_tech"] == {
        "symbols": ["ASML.AS", "SAP.DE"],
        "candidate_count": 2,
        "baseline_symbols": ["SAP.DE"],
        "sticky_symbols": ["ASML.AS"],
        "radar_symbols": ["SAP.DE"],
        "challenger_symbols": ["ASML.AS"],
        "bias_counts": {"long": 1, "neutral": 1},
        "average_attractiveness": 0.75,
    }
    assert snapshot["unclassified"]["sticky_symbols"] == ["UNKNOWN"]
    assert scope == candidate_scope_id(
        "EU",
        list(reversed(candidates)),
        ("SAP.DE",),
        "2026-07-10T15:30:00+00:00",
    )
    assert scope != candidate_scope_id(
        "EU",
        candidates,
        ("ASML.AS", "SAP.DE"),
        "2026-07-10T15:30:00+00:00",
    )
    assert scope != candidate_scope_id(
        "EU",
        candidates,
        ("SAP.DE",),
        "2026-07-11T15:30:00+00:00",
    )
    modified = [dict(candidates[0]), {**candidates[1], "fresh_news": {"source_refs": ["u3"]}}]
    assert scope != candidate_scope_id(
        "EU",
        modified,
        ("SAP.DE",),
        "2026-07-10T15:30:00+00:00",
    )
