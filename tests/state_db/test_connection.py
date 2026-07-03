"""Tests — StateDb connexion WAL + transaction atomique + helpers lecture."""
import pytest
from trader.state_db.connection import StateDb


def test_wal_and_transaction_atomicity(tmp_path):
    db_path = tmp_path / "test.db"
    db = StateDb(db_path)

    # 1. WAL activé
    row = db.query_one("PRAGMA journal_mode")
    assert row[0] == "wal"

    # 2. Créer une table pour les tests de transaction
    db.executescript("CREATE TABLE items (id INTEGER PRIMARY KEY, val TEXT)")

    # 3. Transaction avec exception → rollback
    with pytest.raises(ValueError):
        with db.transaction() as cur:
            cur.execute("INSERT INTO items(val) VALUES (?)", ("rollback_me",))
            raise ValueError("intentional error")

    count = db.query_one("SELECT COUNT(*) FROM items")
    assert count[0] == 0, "la transaction doit être rollback"

    # 4. Transaction normale → commit
    with db.transaction() as cur:
        cur.execute("INSERT INTO items(val) VALUES (?)", ("committed",))

    count = db.query_one("SELECT COUNT(*) FROM items")
    assert count[0] == 1, "la transaction doit être commitée"


def test_table_is_empty(tmp_path):
    db_path = tmp_path / "empty_test.db"
    db = StateDb(db_path)
    db.executescript("CREATE TABLE things (id INTEGER PRIMARY KEY)")
    assert db.table_is_empty("things") is True
    with db.transaction() as cur:
        cur.execute("INSERT INTO things(id) VALUES (1)")
    assert db.table_is_empty("things") is False


def test_query_one_and_query_all(tmp_path):
    """query_one/query_all fetchent correctement sous lock (FIX 2)."""
    db = StateDb(tmp_path / "q.db")
    db.executescript("CREATE TABLE t (id INTEGER PRIMARY KEY, val TEXT)")

    with db.transaction() as cur:
        cur.execute("INSERT INTO t VALUES (1, 'alpha')")
        cur.execute("INSERT INTO t VALUES (2, 'beta')")

    # query_one — row présente
    row = db.query_one("SELECT val FROM t WHERE id = ?", (1,))
    assert row is not None
    assert row["val"] == "alpha"

    # query_one — row absente → None
    missing = db.query_one("SELECT val FROM t WHERE id = ?", (999,))
    assert missing is None

    # query_all — ordre préservé
    rows = db.query_all("SELECT val FROM t ORDER BY id")
    assert [r["val"] for r in rows] == ["alpha", "beta"]


def test_transaction_commit_and_rollback_in_autocommit(tmp_path):
    """En mode autocommit (isolation_level=None), transaction() gère correctement
    BEGIN IMMEDIATE / COMMIT / ROLLBACK via cursor (FIX 1)."""
    db = StateDb(tmp_path / "tx.db")
    db.executescript("CREATE TABLE t (x INTEGER)")

    # Rollback : l'insert ne doit pas persister
    with pytest.raises(RuntimeError):
        with db.transaction() as cur:
            cur.execute("INSERT INTO t VALUES (42)")
            raise RuntimeError("abort")

    assert db.query_one("SELECT COUNT(*) FROM t")[0] == 0, "rollback attendu"

    # Commit : l'insert doit persister
    with db.transaction() as cur:
        cur.execute("INSERT INTO t VALUES (99)")

    row = db.query_one("SELECT x FROM t")
    assert row is not None
    assert row["x"] == 99
