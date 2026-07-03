"""Tests — schema_migrations + apply_migrations (signature list[str] par migration)."""
import pytest
from trader.state_db.connection import StateDb


_MIGRATION_1 = "CREATE TABLE foo (id INTEGER PRIMARY KEY, name TEXT)"
_MIGRATION_2 = "CREATE TABLE bar (id INTEGER PRIMARY KEY, val REAL)"


def test_apply_migrations_creates_tables_and_records(tmp_path):
    db = StateDb(tmp_path / "mig.db")
    # FIX 3 : chaque migration = (version, list[str])
    migrations = [(1, [_MIGRATION_1]), (2, [_MIGRATION_2])]

    db.apply_migrations(migrations)

    # Tables créées
    tables = {
        row[0]
        for row in db.query_all(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('foo','bar')"
        )
    }
    assert tables == {"foo", "bar"}

    # schema_migrations : 2 lignes
    count = db.query_one("SELECT COUNT(*) FROM schema_migrations")[0]
    assert count == 2

    versions = {row[0] for row in db.query_all("SELECT version FROM schema_migrations")}
    assert versions == {1, 2}

    # applied_at renseigné et canonicalisé (+00:00)
    for row in db.query_all("SELECT applied_at FROM schema_migrations"):
        assert "+00:00" in row[0], f"applied_at non canonicalisé : {row[0]}"


def test_apply_migrations_idempotent(tmp_path):
    db = StateDb(tmp_path / "mig2.db")
    migrations = [(1, [_MIGRATION_1]), (2, [_MIGRATION_2])]

    db.apply_migrations(migrations)
    db.apply_migrations(migrations)  # ré-appliquer → no-op, sans erreur

    count = db.query_one("SELECT COUNT(*) FROM schema_migrations")[0]
    assert count == 2, "ré-appliquer ne doit pas dupliquer les lignes"


def test_apply_migrations_multi_statement(tmp_path):
    """Une migration peut contenir plusieurs statements (liste explicite, FIX 3)."""
    db = StateDb(tmp_path / "multi.db")
    db.apply_migrations([(1, [
        "CREATE TABLE alpha (id INTEGER PRIMARY KEY)",
        "CREATE TABLE beta (id INTEGER PRIMARY KEY)",
    ])])

    tables = {
        row[0]
        for row in db.query_all(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('alpha','beta')"
        )
    }
    assert tables == {"alpha", "beta"}


def test_apply_migrations_race_idempotent(tmp_path):
    """Ré-run immédiat de la même version avec BEGIN IMMEDIATE → no-op propre (FIX 3 robustesse race)."""
    db = StateDb(tmp_path / "race.db")
    migrations = [(1, ["CREATE TABLE race_tbl (id INTEGER PRIMARY KEY)"])]

    db.apply_migrations(migrations)
    # Simuler un second processus qui re-tente la même version
    db.apply_migrations(migrations)
    db.apply_migrations(migrations)

    count = db.query_one("SELECT COUNT(*) FROM schema_migrations")[0]
    assert count == 1, "une seule ligne pour version=1 même après plusieurs runs"
