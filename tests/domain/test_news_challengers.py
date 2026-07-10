from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from trader.domain.universe import news_challengers as challenger_module
from trader.domain.universe.news_challengers import select_news_challengers

UTC = timezone.utc
NOW = datetime(2026, 7, 10, 12, 0, tzinfo=UTC)


def _item(
    uuid: str,
    title: str,
    *,
    published_at: datetime | None = None,
    publisher: str = "Reuters",
    fetched_for: str = "WRONG",
) -> dict:
    return {
        "uuid": uuid,
        "symbol": fetched_for,
        "title": title,
        "publisher": publisher,
        "published_at": (published_at or NOW - timedelta(hours=1)).isoformat(),
    }


def _select(
    items,
    *,
    names: dict[str, str],
    aliases: dict[str, list[str]] | None = None,
    eligible: set[str] | None = None,
    radar: set[str] | None = None,
    seen: set[str] | None = None,
):
    return select_news_challengers(
        items,
        symbol_names=names,
        manual_aliases=aliases or {},
        eligible_symbols=eligible if eligible is not None else set(names),
        radar_symbols=radar or set(),
        seen_source_refs=seen or set(),
        as_of=NOW,
    )


def test_attribution_uses_title_entity_not_fetched_symbol() -> None:
    selection = _select(
        [_item("u1", "ASML announces quarterly results", fetched_for="2330.TW")],
        names={
            "2330.TW": "Taiwan Semiconductor Manufacturing Company Limited",
            "ASML.AS": "ASML Holding N.V.",
        },
    )

    assert [item.symbol for item in selection.challengers] == ["ASML.AS"]
    evidence = selection.challengers[0].evidence[0]
    assert evidence.fetched_for == "2330.TW"
    assert evidence.attribution_method in {"legal_name", "auto_alias"}


def test_query_symbol_is_never_an_attribution_fallback() -> None:
    selection = _select(
        [_item("u1", "Micron unveils a new investment plan", fetched_for="6488.TWO")],
        names={"6488.TWO": "GlobalWafers Co., Ltd."},
    )

    assert selection.challengers == ()
    assert selection.rejection_counts == {"no_direct_entity": 1}


def test_numeric_ticker_from_wrong_exchange_is_rejected() -> None:
    selection = _select(
        [_item("u1", "Formosa Petrochemical (TSE:6505) reports earnings")],
        names={"6505.TW": "Formosa Petrochemical Corporation"},
    )

    assert selection.challengers == ()
    assert selection.rejection_counts == {"no_direct_entity": 1}


def test_qualified_exchange_symbol_is_direct_evidence() -> None:
    selection = _select(
        [_item("u1", "Issuer (TPE:2882) reports quarterly earnings")],
        names={"2882.TW": "Cathay Financial Holding Co., Ltd."},
    )

    challenger = selection.challengers[0]
    assert challenger.symbol == "2882.TW"
    assert challenger.evidence[0].attribution_method == "qualified_symbol"


def test_longest_overlapping_alias_wins() -> None:
    selection = _select(
        [_item("u1", "Foxconn Technology revenue rises")],
        names={
            "2317.TW": "Hon Hai Precision Industry Co., Ltd.",
            "2354.TW": "Foxconn Technology Co., Ltd.",
        },
        aliases={"2317.TW": ["Foxconn"]},
    )

    assert [item.symbol for item in selection.challengers] == ["2354.TW"]


def test_disjoint_entities_make_attribution_ambiguous() -> None:
    selection = _select(
        [_item("u1", "ASML and Meta announce a partnership")],
        names={"ASML.AS": "ASML Holding N.V.", "META": "Meta Platforms, Inc."},
        aliases={"META": ["Meta"]},
    )

    assert selection.challengers == ()
    assert selection.rejection_counts == {"no_direct_entity": 1}


def test_real_archive_analyst_sources_do_not_become_challengers() -> None:
    selection = _select(
        [
            _item(
                "ubs-source",
                "Domino's Quarterly Results Likely Impacted by Macro Pressures, Promotions, UBS Says",
                publisher="MT Newswires",
            ),
            _item(
                "bac-source",
                "Costco shares fall after June sales update, Bank of America remains bullish",
                publisher="Proactive",
            ),
            _item(
                "ms-source",
                "AI Data Center Demand to Fuel Clean Tech Order Inflection, Morgan Stanley Says",
                publisher="MT Newswires",
            ),
            _item(
                "deere-direct",
                "Deere settles lawsuit by US FTC, states over equipment repair restrictions",
            ),
        ],
        names={
            "UBSG.SW": "UBS Group AG",
            "BAC": "Bank of America Corporation",
            "MS": "Morgan Stanley",
            "DE": "Deere & Company",
        },
    )

    assert [item.symbol for item in selection.challengers] == ["DE"]
    assert selection.rejection_counts == {"analyst_source_only": 3}


def test_company_centered_bank_headlines_remain_direct() -> None:
    selection = _select(
        [
            _item("ubs-results", "UBS profit rises after a strong quarter"),
            _item("bofa-results", "Bank of America reports earnings above expectations"),
            _item("ubs-self-report", "UBS says profit rose in the second quarter"),
        ],
        names={
            "UBSG.SW": "UBS Group AG",
            "BAC": "Bank of America Corporation",
        },
    )

    assert {item.symbol for item in selection.challengers} == {"UBSG.SW", "BAC"}
    assert selection.rejection_counts == {}


def test_explicit_analyst_role_markers_are_rejected() -> None:
    selection = _select(
        [
            _item("according", "Apple earnings remain strong according to UBS"),
            _item("by", "Nike outlook upgraded by UBS"),
            _item("rating", "UBS upgrades Nike rating"),
            _item("analysts-at", "Analysts at Morgan Stanley expect stronger sales"),
        ],
        names={
            "UBSG.SW": "UBS Group AG",
            "MS": "Morgan Stanley",
        },
    )

    assert selection.challengers == ()
    assert selection.rejection_counts == {"analyst_source_only": 4}


def test_freshness_accepts_exactly_72_hours_and_rejects_older() -> None:
    selection = _select(
        [
            _item("fresh", "FPCC invokes force majeure", published_at=NOW - timedelta(hours=72)),
            _item(
                "stale",
                "FPCC invokes force majeure",
                published_at=NOW - timedelta(hours=72, microseconds=1),
            ),
        ],
        names={"6505.TW": "Formosa Petrochemical Corporation"},
        aliases={"6505.TW": ["FPCC"]},
    )

    assert selection.challengers[0].source_refs == ("fresh",)
    assert selection.rejection_counts == {"stale": 1}


def test_score_threshold_is_inclusive_at_65() -> None:
    selection = _select(
        [
            _item(
                "u1",
                "Acme reports earnings",
                published_at=NOW - timedelta(hours=30),
                publisher="CNBC",
            )
        ],
        names={"ACME": "Acme Holdings Inc."},
    )

    assert selection.challengers[0].score == 65


def test_distinct_publishers_can_corroborate_but_same_publisher_cannot() -> None:
    rows = [
        _item(
            "u1",
            "Acme reports earnings",
            published_at=NOW - timedelta(hours=30),
            publisher="Local Wire One",
        ),
        _item(
            "u2",
            "Acme earnings update",
            published_at=NOW - timedelta(hours=31),
            publisher="Local Wire Two",
        ),
    ]
    names = {"ACME": "Acme Holdings Inc."}

    corroborated = _select(rows, names=names)
    same_publisher = _select(
        [{**row, "publisher": "Local Wire One"} for row in rows],
        names=names,
    )

    assert corroborated.challengers[0].score == 65
    assert same_publisher.challengers == ()
    assert same_publisher.rejection_counts == {"low_symbol_score": 1}


def test_duplicate_uuid_cannot_create_corroboration() -> None:
    selection = _select(
        [
            _item(
                "same",
                "Acme reports earnings",
                published_at=NOW - timedelta(hours=30),
                publisher="Local Wire One",
            ),
            _item(
                "same",
                "Acme earnings update",
                published_at=NOW - timedelta(hours=31),
                publisher="Local Wire Two",
            ),
        ],
        names={"ACME": "Acme Holdings Inc."},
    )

    assert selection.challengers == ()
    assert selection.rejection_counts == {"duplicate_source_ref": 1, "low_symbol_score": 1}


def test_seen_uuid_and_radar_symbol_are_excluded() -> None:
    selection = _select(
        [
            _item("seen", "Acme profit warning"),
            _item("radar", "Beta profit warning"),
        ],
        names={"ACME": "Acme Holdings Inc.", "BETA": "Beta Corporation"},
        radar={"BETA"},
        seen={"seen"},
    )

    assert selection.challengers == ()
    assert selection.rejection_counts == {"already_radar": 1, "already_seen": 1}


def test_all_qualifying_challengers_are_kept_without_global_cap() -> None:
    names = {f"SYM{idx:02d}": f"Issuer {idx} Corporation" for idx in range(12)}
    aliases = {symbol: [f"Brand {idx}"] for idx, symbol in enumerate(names)}
    rows = [
        _item(f"u{idx}", f"Brand {idx} announces an acquisition", publisher="Local Business")
        for idx in range(12)
    ]

    selection = _select(rows, names=names, aliases=aliases)

    assert len(selection.challengers) == 12
    assert {item.symbol for item in selection.challengers} == set(names)


def test_challenger_order_is_score_then_newest_date_then_symbol() -> None:
    names = {
        "A": "Alpha Corporation",
        "B": "Beta Corporation",
        "C": "Charlie Corporation",
        "D": "Delta Corporation",
    }
    rows = [
        _item("a", "Alpha issues a profit warning", published_at=NOW - timedelta(hours=5)),
        _item("b", "Beta announces an acquisition", published_at=NOW - timedelta(hours=1)),
        _item("d", "Delta announces an acquisition", published_at=NOW - timedelta(hours=2)),
        _item("c", "Charlie announces an acquisition", published_at=NOW - timedelta(hours=2)),
    ]

    selection = _select(rows, names=names)

    assert [item.symbol for item in selection.challengers] == ["A", "B", "C", "D"]


def test_candidate_contract_keeps_full_provenance_and_bounds_embedded_evidence() -> None:
    rows = [
        _item(
            f"u{idx}",
            "Acme reports earnings",
            published_at=NOW - timedelta(minutes=idx),
            publisher=f"Wire {idx}",
        )
        for idx in range(4)
    ]
    selection = _select(rows, names={"ACME": "Acme Holdings Inc."})

    candidate = selection.challengers[0].to_candidate()

    assert candidate["symbol"] == "ACME"
    assert candidate["candidate_source"] == "fresh_news"
    assert candidate["bias"] == "neutral"
    assert candidate["fresh_news"]["evidence_count"] == 4
    assert candidate["fresh_news"]["source_refs"] == ["u0", "u1", "u2", "u3"]
    assert len(candidate["fresh_news"]["evidence"]) == 3
    assert candidate["fresh_news"]["evidence"][0]["attribution"]["directness"] == "direct"


def test_repository_alias_file_has_unambiguous_manual_alias_lists() -> None:
    root = Path(__file__).resolve().parents[2]
    aliases = yaml.safe_load((root / "config" / "symbol_news_aliases.yaml").read_text(encoding="utf-8"))

    assert aliases["2317.TW"] == ["Hon Hai", "Foxconn", "FXCOF"]
    assert aliases["2330.TW"] == ["Taiwan Semiconductor", "TSMC"]
    assert all(isinstance(values, list) and values for values in aliases.values())


def test_attribution_catalog_avoids_per_item_regex_compilation_or_symbol_search(monkeypatch) -> None:
    names = {f"SYM{idx:03d}": f"Issuer {idx} Corporation" for idx in range(431)}
    names["TARGET"] = "Target Example Corporation"
    rows = [
        _item(f"u{idx}", "Target Example announces an acquisition")
        for idx in range(250)
    ]

    def forbidden_regex_call(*args, **kwargs):
        raise AssertionError("regex compilation/search must not run per item or known symbol")

    monkeypatch.setattr(challenger_module.re, "compile", forbidden_regex_call)
    monkeypatch.setattr(challenger_module.re, "search", forbidden_regex_call)

    selection = _select(rows, names=names)

    assert [item.symbol for item in selection.challengers] == ["TARGET"]
    assert selection.challengers[0].evidence_count == 250
