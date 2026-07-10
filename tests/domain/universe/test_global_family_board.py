from trader.domain.universe import UniverseSituationContext, build_global_family_board


def test_global_family_board_compares_venue_families_without_allocating() -> None:
    scopes = {
        "EU": {
            "candidate_scope_id": "scope-eu",
            "as_of": "2026-07-10T05:30:00+00:00",
            "parent_candidate_scope_id": "close-eu",
            "parent_close_at": "2026-07-09T15:30:00+00:00",
            "candidates": [
                {
                    "symbol": "BNP.PA",
                    "family": "eu_financials",
                    "attractiveness": 0.9,
                    "bias": "long",
                    "candidate_source": "radar",
                },
                {
                    "symbol": "AIR.PA",
                    "family": "eu_industrials",
                    "attractiveness": 0.4,
                    "bias": "long",
                    "candidate_source": "fresh_news",
                },
            ],
            "default_hotlist": ["BNP.PA"],
            "sticky_context_at_close": [],
        },
        "US": {
            "candidate_scope_id": "scope-us",
            "as_of": "2026-07-10T12:00:00+00:00",
            "candidates": [
                {
                    "symbol": "MSFT",
                    "family": "us_tech",
                    "attractiveness": 0.8,
                    "bias": "long",
                    "candidate_source": "radar",
                }
            ],
            "default_hotlist": ["MSFT"],
            "sticky_context_at_close": [],
        },
    }
    situations = {
        "EU": UniverseSituationContext(
            status="active",
            brief_ref={"venue": "EU", "brief_id": "brief-eu"},
            families={
                "eu_financials": [
                    {
                        "point": "European banks benefit from steeper curves.",
                        "direction": "bullish",
                        "signal": "strong",
                        "severity": "watch",
                        "sources": ["Reuters"],
                        "source_refs": ["eu-bank-1"],
                    }
                ]
            },
        ),
        "US": UniverseSituationContext(
            status="active",
            brief_ref={"venue": "US", "brief_id": "brief-us"},
            families={},
        ),
    }

    board = build_global_family_board(
        as_of="2026-07-10T12:05:00+00:00",
        scopes=scopes,
        situations=situations,
    )

    assert board["status"] == "partial"
    assert board["role"] == "comparative_context_not_capital_allocation"
    assert "allocation" not in board
    assert board["coverage"]["missing_scope_venues"] == ["TW"]
    assert board["coverage"]["stale_scope_venues"] == []
    assert board["venues"]["EU"]["scope_freshness"] == "fresh"
    eu_families = board["venues"]["EU"]["families"]
    assert eu_families["eu_financials"]["radar_rank_within_venue"] == 1
    assert eu_families["eu_industrials"]["challenger_count"] == 1
    assert eu_families["eu_financials"]["situation"]["direction_counts"] == {
        "bullish": 1
    }
    assert board["venues"]["US"]["families"]["us_tech"][
        "situation_status"
    ] == "not_reported"
    assert board["board_id"].startswith("global_family_board:v1:")


def test_global_family_board_id_ignores_observation_time_but_tracks_inputs() -> None:
    scope = {
        "candidate_scope_id": "scope-us",
        "as_of": "2026-07-10T12:00:00+00:00",
        "candidates": [
            {
                "symbol": "MSFT",
                "family": "us_tech",
                "attractiveness": 0.8,
                "bias": "long",
            }
        ],
        "default_hotlist": ["MSFT"],
    }
    first = build_global_family_board(
        as_of="2026-07-10T12:05:00+00:00",
        scopes={"US": scope},
        situations={},
    )
    second = build_global_family_board(
        as_of="2026-07-10T12:10:00+00:00",
        scopes={"US": scope},
        situations={},
    )
    changed = build_global_family_board(
        as_of="2026-07-10T12:10:00+00:00",
        scopes={"US": {**scope, "default_hotlist": []}},
        situations={},
    )

    assert first["board_id"] == second["board_id"]
    assert first["as_of"] != second["as_of"]
    assert changed["board_id"] != first["board_id"]
