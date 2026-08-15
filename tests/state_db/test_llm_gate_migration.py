"""Tests TDD — LLM_GATE_MIGRATION (v5, table llm_gate_last_seen)."""

from __future__ import annotations

from pathlib import Path

from trader.infrastructure.state_db.connection import StateDb
from trader.infrastructure.state_db.migrations import LLM_GATE_MIGRATION


def test_llm_gate_migration_creates_table_and_primary_key(tmp_path: Path) -> None:
    db = StateDb(tmp_path / "casys.db")
    db.apply_migrations([LLM_GATE_MIGRATION])

    columns = {row["name"] for row in db.query_all("PRAGMA table_info(llm_gate_last_seen)")}
    assert columns == {"state_dir", "symbol", "last_at"}
    pk = {
        row["name"]
        for row in db.query_all("PRAGMA table_info(llm_gate_last_seen)")
        if row["pk"]
    }
    assert pk == {"state_dir", "symbol"}
    assert db.query_one("SELECT 1 FROM schema_migrations WHERE version=5") is not None


def test_llm_gate_migration_is_idempotent(tmp_path: Path) -> None:
    db = StateDb(tmp_path / "casys.db")
    db.apply_migrations([LLM_GATE_MIGRATION])
    db.apply_migrations([LLM_GATE_MIGRATION])

    rows = db.query_all("SELECT version FROM schema_migrations WHERE version=5")
    assert len(rows) == 1
