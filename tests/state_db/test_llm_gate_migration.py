"""Tests TDD — migrations v5+v8 du gate LLM."""

from __future__ import annotations

from pathlib import Path

from trader.infrastructure.state_db.connection import StateDb
from trader.infrastructure.state_db.migrations import (
    LLM_GATE_CONTEXT_MIGRATION,
    LLM_GATE_MIGRATION,
)


def test_llm_gate_migration_creates_table_and_primary_key(tmp_path: Path) -> None:
    db = StateDb(tmp_path / "casys.db")
    db.apply_migrations([LLM_GATE_MIGRATION])

    columns = {row["name"] for row in db.query_all("PRAGMA table_info(llm_gate_last_seen)")}
    assert columns == {"state_dir", "symbol", "last_at"}
    pk = {row["name"] for row in db.query_all("PRAGMA table_info(llm_gate_last_seen)") if row["pk"]}
    assert pk == {"state_dir", "symbol"}
    assert db.query_one("SELECT 1 FROM schema_migrations WHERE version=5") is not None


def test_llm_gate_migration_is_idempotent(tmp_path: Path) -> None:
    db = StateDb(tmp_path / "casys.db")
    db.apply_migrations([LLM_GATE_MIGRATION])
    db.apply_migrations([LLM_GATE_MIGRATION])

    rows = db.query_all("SELECT version FROM schema_migrations WHERE version=5")
    assert len(rows) == 1


def test_llm_gate_context_migration_preserves_legacy_rows(tmp_path: Path) -> None:
    db = StateDb(tmp_path / "casys.db")
    db.apply_migrations([LLM_GATE_MIGRATION])
    with db.transaction() as cur:
        cur.execute(
            "INSERT INTO llm_gate_last_seen(state_dir, symbol, last_at) VALUES (?, ?, ?)",
            ("/state", "SPY", "2026-08-20T10:00:00+00:00"),
        )

    db.apply_migrations([LLM_GATE_CONTEXT_MIGRATION])

    columns = {row["name"] for row in db.query_all("PRAGMA table_info(llm_gate_last_seen)")}
    assert columns == {
        "state_dir",
        "symbol",
        "last_at",
        "wake_reasons_json",
        "wake_fingerprints_json",
    }
    row = db.query_one("SELECT * FROM llm_gate_last_seen WHERE symbol='SPY'")
    assert row is not None
    assert row["last_at"] == "2026-08-20T10:00:00+00:00"
    assert row["wake_reasons_json"] is None
    assert row["wake_fingerprints_json"] is None
    assert db.query_one("SELECT 1 FROM schema_migrations WHERE version=8") is not None


def test_llm_gate_context_migration_is_idempotent(tmp_path: Path) -> None:
    db = StateDb(tmp_path / "casys.db")
    db.apply_migrations([LLM_GATE_MIGRATION, LLM_GATE_CONTEXT_MIGRATION])
    db.apply_migrations([LLM_GATE_MIGRATION, LLM_GATE_CONTEXT_MIGRATION])

    rows = db.query_all("SELECT version FROM schema_migrations WHERE version=8")
    assert len(rows) == 1
