"""Tests TDD — FIX 1 cycle de vie connexion + FIX 2 integrity_check.

Couverture :
    FIX 1 :
        - StateDb.close() : PRAGMA wal_checkpoint(TRUNCATE) + ferme connexion
        - StateDb.close() idempotent (2e appel silencieux)
        - close_all_state_dbs() : ferme toutes les instances + vide _DB_REGISTRY
        - Après close_all_state_dbs(), open_state_db(même chemin) → nouvelle instance

    FIX 2 :
        - StateDb.integrity_check() → ['ok'] sur une base saine
        - bootstrap_state_backend log une erreur [state_db] integrity_check ÉCHEC
          si integrity_check retourne une valeur != ['ok']
"""
from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import patch

import pytest

from trader.state_db.connection import (
    StateDb,
    _DB_REGISTRY,
    _DB_REGISTRY_LOCK,
    close_all_state_dbs,
    open_state_db,
)


# ---------------------------------------------------------------------------
# Helpers d'isolation registry
# ---------------------------------------------------------------------------


def _clear_registry_for(paths: list[Path]) -> None:
    with _DB_REGISTRY_LOCK:
        for p in paths:
            _DB_REGISTRY.pop(str(p.resolve()), None)


# ---------------------------------------------------------------------------
# FIX 1 — StateDb.close() idempotent
# ---------------------------------------------------------------------------


def test_close_idempotent(tmp_path: Path) -> None:
    """StateDb.close() peut être appelé plusieurs fois sans lever d'exception."""
    db = StateDb(tmp_path / "close_test.db")
    db.executescript("CREATE TABLE t (x INTEGER)")
    with db.transaction() as cur:
        cur.execute("INSERT INTO t VALUES (42)")

    # Premier appel
    db.close()
    assert db._conn is None, "close() doit mettre _conn à None"

    # Deuxième appel — ne doit PAS lever
    db.close()
    assert db._conn is None, "close() idempotent : _conn reste None"


def test_close_sets_conn_to_none(tmp_path: Path) -> None:
    """Après close(), StateDb._conn est None."""
    db = StateDb(tmp_path / "none_test.db")
    assert db._conn is not None
    db.close()
    assert db._conn is None


def test_close_checkpoints_wal(tmp_path: Path) -> None:
    """close() n'échoue pas sur une base WAL (regression guard)."""
    db = StateDb(tmp_path / "wal_test.db")
    db.executescript("CREATE TABLE t (x INTEGER)")
    with db.transaction() as cur:
        cur.execute("INSERT INTO t VALUES (1)")
    # Si WAL checkpoint échoue, close() propagerait l'exception (ici best-effort)
    db.close()  # ne doit pas lever


# ---------------------------------------------------------------------------
# FIX 1 — close_all_state_dbs() + cache vidé
# ---------------------------------------------------------------------------


def test_close_all_clears_registry(tmp_path: Path) -> None:
    """close_all_state_dbs() vide _DB_REGISTRY."""
    db_path = tmp_path / "registry_test.db"
    _clear_registry_for([db_path])
    try:
        open_state_db(db_path)
        key = str(db_path.resolve())
        with _DB_REGISTRY_LOCK:
            assert key in _DB_REGISTRY

        close_all_state_dbs()

        with _DB_REGISTRY_LOCK:
            assert key not in _DB_REGISTRY
    finally:
        _clear_registry_for([db_path])


def test_close_all_reopen_returns_new_instance(tmp_path: Path) -> None:
    """Après close_all_state_dbs(), open_state_db(même chemin) → nouvelle instance."""
    db_path = tmp_path / "reopen_test.db"
    _clear_registry_for([db_path])
    try:
        db1 = open_state_db(db_path)
        close_all_state_dbs()
        db2 = open_state_db(db_path)

        assert db1 is not db2, (
            "close_all_state_dbs vide le cache → open_state_db crée une nouvelle instance"
        )
        # La nouvelle instance doit être fonctionnelle
        assert db2._conn is not None
        row = db2.query_one("PRAGMA journal_mode")
        assert row[0] == "wal"
    finally:
        close_all_state_dbs()
        _clear_registry_for([db_path])


def test_close_all_closes_all_connections(tmp_path: Path) -> None:
    """close_all_state_dbs() ferme bien les connexions (_conn → None sur chaque db)."""
    path_a = tmp_path / "a.db"
    path_b = tmp_path / "b.db"
    _clear_registry_for([path_a, path_b])
    try:
        db_a = open_state_db(path_a)
        db_b = open_state_db(path_b)

        close_all_state_dbs()

        assert db_a._conn is None, "db_a._conn doit être None après close_all"
        assert db_b._conn is None, "db_b._conn doit être None après close_all"
    finally:
        _clear_registry_for([path_a, path_b])


# ---------------------------------------------------------------------------
# FIX 2 — integrity_check()
# ---------------------------------------------------------------------------


def test_integrity_check_healthy_db_returns_ok(tmp_path: Path) -> None:
    """integrity_check() sur une base saine retourne ['ok']."""
    db = StateDb(tmp_path / "healthy.db")
    db.executescript("CREATE TABLE t (x INTEGER)")
    with db.transaction() as cur:
        cur.execute("INSERT INTO t VALUES (1)")

    result = db.integrity_check()
    assert result == ["ok"], f"integrity_check doit retourner ['ok'], got {result!r}"
    db.close()


def test_integrity_check_returns_list_of_str(tmp_path: Path) -> None:
    """integrity_check() retourne bien une list[str]."""
    db = StateDb(tmp_path / "type_test.db")
    result = db.integrity_check()
    assert isinstance(result, list)
    assert all(isinstance(s, str) for s in result)
    db.close()


def test_bootstrap_logs_error_on_integrity_failure(tmp_path: Path, caplog) -> None:
    """bootstrap_state_backend log une erreur si integrity_check != ['ok'].

    Simule la corruption en mockant StateDb.integrity_check au niveau classe.
    """
    from trader.state_db.broker_factory import bootstrap_state_backend

    db_path = tmp_path / "casys.db"
    _clear_registry_for([db_path])

    corrupted_result = ["*** in database main ***", "Page 1 is never used"]

    with patch.object(
        StateDb,
        "integrity_check",
        return_value=corrupted_result,
    ):
        with caplog.at_level(logging.ERROR, logger="trader.state_db.broker_factory"):
            bootstrap_state_backend(
                state_dir=tmp_path,
                starting_cash=50_000.0,
                commission_model=None,
                backend="sqlite",
            )

    error_messages = [r.message for r in caplog.records if r.levelno == logging.ERROR]
    assert any(
        "integrity_check ÉCHEC" in msg for msg in error_messages
    ), f"Log attendu '[state_db] integrity_check ÉCHEC' non trouvé. Records: {error_messages!r}"

    # Nettoyage — le bootstrap a quand même réussi (log only, pas de crash)
    close_all_state_dbs()
    _clear_registry_for([db_path])


def test_bootstrap_no_error_on_healthy_db(tmp_path: Path, caplog) -> None:
    """bootstrap_state_backend ne log PAS d'erreur sur une base saine."""
    from trader.state_db.broker_factory import bootstrap_state_backend

    db_path = tmp_path / "casys.db"
    _clear_registry_for([db_path])

    try:
        with caplog.at_level(logging.ERROR, logger="trader.state_db.broker_factory"):
            bootstrap_state_backend(
                state_dir=tmp_path,
                starting_cash=50_000.0,
                commission_model=None,
                backend="sqlite",
            )

        error_messages = [r.message for r in caplog.records if r.levelno == logging.ERROR]
        assert not any(
            "integrity_check" in msg for msg in error_messages
        ), f"Erreur inattendue sur base saine: {error_messages!r}"
    finally:
        close_all_state_dbs()
        _clear_registry_for([db_path])


# ---------------------------------------------------------------------------
# FIX 3 — _ensure_open : RuntimeError après close()
# ---------------------------------------------------------------------------


def test_ensure_open_raises_query_one_after_close(tmp_path: Path) -> None:
    """query_one lève RuntimeError('StateDb fermé') après close()."""
    db = StateDb(tmp_path / "eo_test.db")
    db.close()

    with pytest.raises(RuntimeError, match="StateDb fermé"):
        db.query_one("SELECT 1")


def test_ensure_open_raises_query_all_after_close(tmp_path: Path) -> None:
    """query_all lève RuntimeError('StateDb fermé') après close()."""
    db = StateDb(tmp_path / "eo_all.db")
    db.close()

    with pytest.raises(RuntimeError, match="StateDb fermé"):
        db.query_all("SELECT 1")


def test_ensure_open_raises_transaction_after_close(tmp_path: Path) -> None:
    """transaction() lève RuntimeError('StateDb fermé') après close()."""
    db = StateDb(tmp_path / "eo_tx.db")
    db.close()

    with pytest.raises(RuntimeError, match="StateDb fermé"):
        with db.transaction() as cur:
            cur.execute("SELECT 1")


def test_ensure_open_raises_integrity_check_after_close(tmp_path: Path) -> None:
    """integrity_check() lève RuntimeError('StateDb fermé') après close()."""
    db = StateDb(tmp_path / "eo_ic.db")
    db.close()

    with pytest.raises(RuntimeError, match="StateDb fermé"):
        db.integrity_check()


def test_ensure_open_close_remains_idempotent(tmp_path: Path) -> None:
    """close() reste idempotent malgré _ensure_open : 2e appel silencieux."""
    db = StateDb(tmp_path / "eo_idem.db")
    db.close()
    db.close()  # Ne doit pas lever
