import sqlite3

import pytest

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


def test_situation_memory_search_empty_operators_and_ticker(tmp_path) -> None:
    store = SituationMemoryStore(tmp_path / "situation_memory.db")
    store.ingest_brief(_brief())

    assert store.search(query="") == store.search()
    assert store.search(query="AND OR NOT") == []
    assert store.search(query="INGA.AS") == []


def test_ingest_commit_failure_rolls_back_and_connection_stays_usable(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = SituationMemoryStore(tmp_path / "situation_memory.db")
    brief = _brief()
    real_conn = store._conn

    class _FailingCommit:
        def commit(self) -> None:
            raise sqlite3.OperationalError("database is locked")

        def __getattr__(self, name: str):
            return getattr(real_conn, name)

    monkeypatch.setattr(store, "_conn", _FailingCommit())
    with pytest.raises(sqlite3.OperationalError, match="database is locked"):
        store.ingest_brief(brief)
    monkeypatch.undo()

    assert store.count() == 0
    result = store.ingest_brief(brief)
    assert result == {"inserted": 2, "skipped": 0}
    assert store.count() == 2


def test_situation_memory_close_est_idempotent(tmp_path) -> None:
    store = SituationMemoryStore(tmp_path / "situation_memory.db")
    store.ingest_brief(_brief())
    store.close()
    store.close()


def test_apply_outcomes_est_idempotent_et_ne_touche_pas_q_value(tmp_path) -> None:
    store = SituationMemoryStore(tmp_path / "situation_memory.db")
    store.ingest_brief(_brief())
    note_id = store.load_notes()[0]["id"]
    payload = {
        "id": note_id,
        "verdict": "gagnant",
        "horizon_sessions": 5,
        "forward_return": 0.02,
        "evaluated_at": "2026-01-10T00:00:00+00:00",
        "coverage_n": 3,
    }
    store.apply_outcomes([payload])
    store.apply_outcomes([payload])
    store.update_outcome_scores({note_id: 0.08})

    assert store.count() == 2
    rows = store.load_outcomes()
    assert len(rows) == 1
    assert rows[0]["verdict"] == "gagnant"
    assert rows[0]["horizon_sessions"] == 5
    assert rows[0]["outcome_score"] == 0.08
    assert rows[0]["q_value"] is None


def test_load_pending_notes_ignore_les_notes_deja_jugees(tmp_path) -> None:
    store = SituationMemoryStore(tmp_path / "situation_memory.db")
    store.ingest_brief(_brief())
    first, second = store.load_notes()
    store.apply_outcomes(
        [
            {
                "id": first["id"],
                "verdict": "gagnant",
                "horizon_sessions": 5,
                "forward_return": 0.02,
                "evaluated_at": "2026-01-10T00:00:00+00:00",
                "coverage_n": 1,
            }
        ]
    )

    pending = store.load_pending_notes(limit=8)
    assert [row["id"] for row in pending] == [second["id"]]
    assert store.load_pending_notes(limit=0) == []
