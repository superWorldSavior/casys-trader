from trader.domain.situation import NewsMacroBrief
from trader.infrastructure.state_db.situation_memory_store import SituationMemoryStore


def _brief() -> NewsMacroBrief:
    brief = NewsMacroBrief.from_mapping(
        {
            "brief_id": "2026-07-09T07:00:00+00:00|EU",
            "venue": "EU",
            "as_of": "2026-07-09T07:00:00+00:00",
            "valid_until": "2026-07-10T07:00:00+00:00",
            "zones": {
                "EU": [
                    {
                        "point": "Risk-off pressure broadens across cyclicals",
                        "sources": ["Reuters"],
                        "source_refs": ["u1"],
                        "symbols": ["AIR.PA"],
                        "signal": "strong",
                        "direction": "risk_off",
                    }
                ]
            },
            "families": {
                "eu_industrials": [
                    {
                        "point": "Industrial exporters face weaker demand signal",
                        "sources": ["Bloomberg"],
                        "source_refs": ["u2"],
                        "symbols": ["AIR.PA"],
                        "severity": "watch",
                        "direction": "bearish",
                    }
                ]
            },
        }
    )
    assert brief is not None
    return brief


def test_situation_memory_ingests_brief_idempotently(tmp_path) -> None:
    store = SituationMemoryStore(tmp_path / "situation_memory.db")
    brief = _brief()

    first = store.ingest_brief(brief)
    second = store.ingest_brief(brief)

    assert first == {"inserted": 2, "skipped": 0}
    assert second == {"inserted": 0, "skipped": 2}
    assert store.count() == 2


def test_situation_memory_searches_fts_and_filters(tmp_path) -> None:
    store = SituationMemoryStore(tmp_path / "situation_memory.db")
    store.ingest_brief(_brief())

    rows = store.search(query="cyclicals", venue="EU", symbol="AIR.PA", active_at="2026-07-09T08:00:00+00:00")
    family_rows = store.search(family="eu_industrials", active_at="2026-07-09T08:00:00+00:00")
    expired = store.search(active_at="2026-07-11T08:00:00+00:00")

    assert len(rows) == 1
    assert rows[0]["brief_id"] == "2026-07-09T07:00:00+00:00|EU"
    assert rows[0]["sources"] == ["Reuters"]
    assert rows[0]["source_refs"] == ["u1"]
    assert rows[0]["direction"] == "risk_off"
    assert len(family_rows) == 1
    assert family_rows[0]["section_name"] == "eu_industrials"
    assert expired == []


def test_situation_memory_search_hyphen_ne_plante_pas(tmp_path) -> None:
    store = SituationMemoryStore(tmp_path / "situation_memory.db")
    store.ingest_brief(_brief())

    assert store.search(query="take-profit", venue="EU") == []
    rows = store.search(query="risk-off cyclicals", venue="EU")
    assert len(rows) == 1
    assert rows[0]["brief_id"] == "2026-07-09T07:00:00+00:00|EU"
