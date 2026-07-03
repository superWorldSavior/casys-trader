"""Tests — Task 2 : schema_migrations + apply_migrations."""
import pytest
from trader.state_db.connection import StateDb


_MIGRATION_1 = "CREATE TABLE foo (id INTEGER PRIMARY KEY, name TEXT)"
_MIGRATION_2 = "CREATE TABLE bar (id INTEGER PRIMARY KEY, val REAL)"


def test_apply_migrations_creates_tables_and_records(tmp_path):
    db = StateDb(tmp_path / "mig.db")
    migrations = [(1, _MIGRATION_1), (2, _MIGRATION_2)]

    db.apply_migrations(migrations)

    # Tables créées
    tables = {
        row[0]
        for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('foo','bar')"
        ).fetchall()
    }
    assert tables == {"foo", "bar"}

    # schema_migrations : 2 lignes
    count = db.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0]
    assert count == 2

    versions = {
        row[0]
        for row in db.execute("SELECT version FROM schema_migrations").fetchall()
    }
    assert versions == {1, 2}

    # applied_at renseigné et canonicalisé (+00:00)
    for row in db.execute("SELECT applied_at FROM schema_migrations").fetchall():
        assert "+00:00" in row[0], f"applied_at non canonicalisé : {row[0]}"


def test_apply_migrations_idempotent(tmp_path):
    db = StateDb(tmp_path / "mig2.db")
    migrations = [(1, _MIGRATION_1), (2, _MIGRATION_2)]

    db.apply_migrations(migrations)
    db.apply_migrations(migrations)  # ré-appliquer → no-op, sans erreur

    count = db.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0]
    assert count == 2, "ré-appliquer ne doit pas dupliquer les lignes"


def test_apply_migrations_multi_statement(tmp_path):
    """Une migration peut contenir plusieurs statements séparés par ';'."""
    db = StateDb(tmp_path / "multi.db")
    multi_sql = (
        "CREATE TABLE alpha (id INTEGER PRIMARY KEY);"
        "CREATE TABLE beta (id INTEGER PRIMARY KEY)"
    )
    db.apply_migrations([(1, multi_sql)])

    tables = {
        row[0]
        for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('alpha','beta')"
        ).fetchall()
    }
    assert tables == {"alpha", "beta"}
