"""Tests — Task 1 : StateDb connexion WAL + transaction atomique."""
import pytest
from trader.state_db.connection import StateDb


def test_wal_and_transaction_atomicity(tmp_path):
    db_path = tmp_path / "test.db"
    db = StateDb(db_path)

    # 1. WAL activé
    row = db.execute("PRAGMA journal_mode").fetchone()
    assert row[0] == "wal"

    # 2. Créer une table pour les tests de transaction
    db.executescript("CREATE TABLE items (id INTEGER PRIMARY KEY, val TEXT)")

    # 3. Transaction avec exception → rollback
    with pytest.raises(ValueError):
        with db.transaction() as cur:
            cur.execute("INSERT INTO items(val) VALUES (?)", ("rollback_me",))
            raise ValueError("intentional error")

    count = db.execute("SELECT COUNT(*) FROM items").fetchone()
    assert count[0] == 0, "la transaction doit être rollback"

    # 4. Transaction normale → commit
    with db.transaction() as cur:
        cur.execute("INSERT INTO items(val) VALUES (?)", ("committed",))

    count = db.execute("SELECT COUNT(*) FROM items").fetchone()
    assert count[0] == 1, "la transaction doit être commitée"


def test_table_is_empty(tmp_path):
    db_path = tmp_path / "empty_test.db"
    db = StateDb(db_path)
    db.executescript("CREATE TABLE things (id INTEGER PRIMARY KEY)")
    assert db.table_is_empty("things") is True
    with db.transaction() as cur:
        cur.execute("INSERT INTO things(id) VALUES (1)")
    assert db.table_is_empty("things") is False
