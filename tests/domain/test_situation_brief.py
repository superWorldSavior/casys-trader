from trader.domain.situation import NewsMacroBrief, SituationPoint


def test_situation_point_normalise_et_borne() -> None:
    point = SituationPoint.from_mapping(
        {
            "point": "x" * 250,
            "sources": ["u1", "u1", "u2"],
            "symbols": ["TSM", "ASML"],
            "severity": "panic",
            "signal": "strong",
            "direction": "risk_off",
        }
    )

    assert point is not None
    assert len(point.point) <= 200
    assert point.sources == ()
    assert point.source_refs == ("u1", "u2")
    assert point.symbols == ("TSM", "ASML")
    assert point.severity == "info"
    assert point.signal == "strong"
    assert point.direction == "risk_off"


def test_news_macro_brief_normalise_sections_et_alerts() -> None:
    payload = {
        "as_of": "2026-07-09T00:00:00+00:00",
        "valid_until": "2026-07-10T00:00:00+00:00",
        "venue": "US",
        "input_refs": {"news_item_uuids": ["u1"]},
        "zones": {
            "US": [
                {
                    "point": f"zone {idx}",
                    "sources": ["Reuters"],
                    "source_refs": [f"z{idx}"],
                }
                for idx in range(8)
            ],
            "": [{"point": "ignore"}],
        },
        "families": {"semis": [{"point": f"family {idx}"} for idx in range(6)]},
        "symbols": {"TSM": [{"point": f"symbol {idx}"} for idx in range(5)]},
        "alerts": [{"point": f"alert {idx}"} for idx in range(10)],
    }

    brief = NewsMacroBrief.from_mapping(payload)

    assert brief is not None
    assert brief.venue == "US"
    assert brief.brief_id == "2026-07-09T00:00:00+00:00|US"
    assert brief.ref(date="2026-07-09") == {
        "date": "2026-07-09",
        "venue": "US",
        "brief_id": "2026-07-09T00:00:00+00:00|US",
        "as_of": "2026-07-09T00:00:00+00:00",
    }
    assert len(brief.zones[0].points) == 5
    assert len(brief.families[0].points) == 3
    assert len(brief.symbols[0].points) == 3
    assert len(brief.alerts) == 8
    assert brief.to_dict()["zones"]["US"][0]["sources"] == ["Reuters"]
    assert brief.to_dict()["zones"]["US"][0]["source_refs"] == ["z0"]
    assert brief.to_dict()["input_refs"] == {"news_item_uuids": ["u1"]}


def test_news_macro_brief_requires_dates() -> None:
    assert NewsMacroBrief.from_mapping({"zones": {"US": [{"point": "x"}]}}) is None
