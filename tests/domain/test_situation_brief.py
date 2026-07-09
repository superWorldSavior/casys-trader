from trader.domain.situation import NewsMacroBrief, SituationPoint


def test_situation_point_normalise_et_borne() -> None:
    point = SituationPoint.from_mapping(
        {
            "point": "x" * 250,
            "sources": ["u1", "u1", "u2"],
            "symbols": ["TSM", "ASML"],
            "severity": "panic",
            "signal": "strong",
        }
    )

    assert point is not None
    assert len(point.point) <= 200
    assert point.sources == ("u1", "u2")
    assert point.symbols == ("TSM", "ASML")
    assert point.severity == "info"
    assert point.signal == "strong"


def test_news_macro_brief_normalise_sections_et_alerts() -> None:
    payload = {
        "as_of": "2026-07-09T00:00:00+00:00",
        "valid_until": "2026-07-10T00:00:00+00:00",
        "zones": {
            "US": [{"point": f"zone {idx}", "sources": [f"z{idx}"]} for idx in range(8)],
            "": [{"point": "ignore"}],
        },
        "families": {"semis": [{"point": f"family {idx}"} for idx in range(6)]},
        "symbols": {"TSM": [{"point": f"symbol {idx}"} for idx in range(5)]},
        "alerts": [{"point": f"alert {idx}"} for idx in range(10)],
    }

    brief = NewsMacroBrief.from_mapping(payload)

    assert brief is not None
    assert len(brief.zones[0].points) == 5
    assert len(brief.families[0].points) == 3
    assert len(brief.symbols[0].points) == 3
    assert len(brief.alerts) == 8
    assert brief.to_dict()["zones"]["US"][0]["sources"] == ["z0"]


def test_news_macro_brief_requires_dates() -> None:
    assert NewsMacroBrief.from_mapping({"zones": {"US": [{"point": "x"}]}}) is None
