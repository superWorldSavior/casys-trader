"""Tests — SITUATION_MEMORY_OUTCOME_MIGRATION (situation_memory.db)."""

from __future__ import annotations

from pathlib import Path

from trader.infrastructure.state_db.connection import StateDb
from trader.infrastructure.state_db.migrations import SITUATION_MEMORY_OUTCOME_MIGRATION
from trader.infrastructure.state_db.situation_memory_store import SituationMemoryStore

_NOTES_DDL = """
CREATE TABLE situation_notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    note_key TEXT UNIQUE NOT NULL,
    brief_id TEXT NOT NULL,
    point TEXT,
    outcome_score REAL,
    q_value REAL
)
"""


def test_situation_outcome_migration_ajoute_les_colonnes_et_est_idempotente(tmp_path: Path) -> None:
    db = StateDb(tmp_path / "situation_memory.db")
    db.executescript(_NOTES_DDL)
    db.apply_migrations([SITUATION_MEMORY_OUTCOME_MIGRATION])
    db.apply_migrations([SITUATION_MEMORY_OUTCOME_MIGRATION])

    columns = {row["name"] for row in db.query_all("PRAGMA table_info(situation_notes)")}
    assert {
        "verdict",
        "horizon_sessions",
        "forward_return",
        "evaluated_at",
        "coverage_n",
    } <= columns
    versions = db.query_all("SELECT version FROM schema_migrations WHERE version=1")
    assert len(versions) == 1


def test_store_ajoute_les_colonnes_sans_casser_l_ouverture(tmp_path: Path) -> None:
    store = SituationMemoryStore(tmp_path / "situation_memory.db")
    columns = {
        str(row[1])
        for row in store._conn.execute("PRAGMA table_info(situation_notes)")
    }
    assert {
        "verdict",
        "horizon_sessions",
        "forward_return",
        "evaluated_at",
        "coverage_n",
    } <= columns
    store.close()
    again = SituationMemoryStore(tmp_path / "situation_memory.db")
    again.close()
