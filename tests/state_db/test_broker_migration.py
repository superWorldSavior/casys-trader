"""Tests TDD — BROKER_MIGRATION + import_broker_from_json."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from trader.state_db.connection import StateDb
from trader.state_db.migrations import BROKER_MIGRATION, import_broker_from_json


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def db(tmp_path: Path) -> StateDb:
    return StateDb(tmp_path / "casys.db")


@pytest.fixture()
def broker_json(tmp_path: Path) -> Path:
    """broker.json de test :
    - cash=95000
    - 2 positions (AAPL q=10, TSLA q=0)
    - 2 fills : un complet (AAPL) + un ancien sans commission/fx_rate (TSLA)
    """
    data = {
        "cash": 95000.0,
        "positions": {
            "AAPL": {"symbol": "AAPL", "quantity": 10.0, "avg_price": 150.0},
            "TSLA": {"symbol": "TSLA", "quantity": 0.0, "avg_price": 200.0},
        },
        "fills": [
            {
                "symbol": "AAPL",
                "side": "BUY",
                "quantity": 10.0,
                "price": 150.0,
                "ts": "2026-07-01T10:00:00+00:00",
                "commission": 1.5,
                "commission_currency": "USD",
                "commission_model": "ibkr",
                "fx_rate": 1.0,
            },
            {
                # Ancien fill sans commission/commission_currency/commission_model/fx_rate
                "symbol": "TSLA",
                "side": "SELL",
                "quantity": 5.0,
                "price": 200.0,
                "ts": "2026-06-30T09:00:00+00:00",
            },
        ],
    }
    path = tmp_path / "broker.json"
    path.write_text(json.dumps(data, indent=2))
    return path


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestBrokerMigrationSchema:
    def test_broker_migration_version(self) -> None:
        """BROKER_MIGRATION doit être version 1."""
        version, stmts = BROKER_MIGRATION
        assert version == 1

    def test_broker_migration_has_three_statements(self) -> None:
        """3 CREATE TABLE séparés (broker_state, broker_positions, broker_fills)."""
        _, stmts = BROKER_MIGRATION
        assert len(stmts) == 3

    def test_broker_migration_creates_tables(self, db: StateDb) -> None:
        """apply_migrations crée bien les 3 tables."""
        db.apply_migrations([BROKER_MIGRATION])
        for table in ("broker_state", "broker_positions", "broker_fills"):
            row = db.query_one(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            )
            assert row is not None, f"Table manquante : {table}"


class TestImportBrokerFromJson:
    def test_import_populates_broker_state(self, db: StateDb, broker_json: Path) -> None:
        """broker_state a id=1 et le cash du JSON."""
        import_broker_from_json(db, broker_json, starting_cash=100_000.0)

        row = db.query_one("SELECT cash FROM broker_state WHERE id=1")
        assert row is not None
        assert row["cash"] == pytest.approx(95000.0)

    def test_import_populates_positions_including_zero_qty(
        self, db: StateDb, broker_json: Path
    ) -> None:
        """2 positions importées, y compris TSLA à quantity=0 (historique)."""
        import_broker_from_json(db, broker_json, starting_cash=100_000.0)

        positions = db.query_all("SELECT * FROM broker_positions ORDER BY symbol")
        assert len(positions) == 2

        syms = {p["symbol"] for p in positions}
        assert syms == {"AAPL", "TSLA"}

        tsla = next(p for p in positions if p["symbol"] == "TSLA")
        assert tsla["quantity"] == pytest.approx(0.0)

    def test_import_populates_fills_and_patches_defaults(
        self, db: StateDb, broker_json: Path
    ) -> None:
        """2 fills importés ; l'ancien fill sans commission/fx_rate a les defaults."""
        import_broker_from_json(db, broker_json, starting_cash=100_000.0)

        fills = db.query_all("SELECT * FROM broker_fills ORDER BY seq")
        assert len(fills) == 2

        fill_aapl = next(f for f in fills if f["symbol"] == "AAPL")
        assert fill_aapl["commission"] == pytest.approx(1.5)
        assert fill_aapl["commission_model"] == "ibkr"
        assert fill_aapl["fx_rate"] == pytest.approx(1.0)

        fill_tsla = next(f for f in fills if f["symbol"] == "TSLA")
        assert fill_tsla["commission"] == pytest.approx(0.0)
        assert fill_tsla["commission_currency"] == "USD"
        assert fill_tsla["commission_model"] == "none"
        assert fill_tsla["fx_rate"] == pytest.approx(1.0)

    def test_import_creates_backup_and_removes_source(
        self, db: StateDb, broker_json: Path
    ) -> None:
        """Le fichier source est renommé en .bak-<ts>, le fichier original disparaît."""
        import_broker_from_json(db, broker_json, starting_cash=100_000.0)

        assert not broker_json.exists(), "Le JSON source doit être renommé (backup)"
        bak_files = list(broker_json.parent.glob("broker.json.bak-*"))
        assert len(bak_files) == 1, "Exactement un backup horodaté attendu"

    def test_reimport_is_noop_no_doublon_no_backup(
        self, db: StateDb, broker_json: Path, tmp_path: Path
    ) -> None:
        """Ré-import (broker_state non vide) → no-op : pas de doublon, pas de backup."""
        import_broker_from_json(db, broker_json, starting_cash=100_000.0)

        # Créer un 2e JSON pour tenter un ré-import
        broker_json_2 = tmp_path / "broker2.json"
        broker_json_2.write_text(
            json.dumps({"cash": 50000.0, "positions": {}, "fills": []})
        )

        # Ré-import → doit être un no-op complet
        import_broker_from_json(db, broker_json_2, starting_cash=100_000.0)

        # broker_state : toujours 1 ligne avec le cash original
        rows = db.query_all("SELECT * FROM broker_state")
        assert len(rows) == 1
        assert rows[0]["cash"] == pytest.approx(95000.0)

        # broker_fills : toujours 2 (pas de doublon)
        fills = db.query_all("SELECT * FROM broker_fills")
        assert len(fills) == 2

        # broker_json_2 n'a PAS été renommé (backup non créé)
        assert broker_json_2.exists(), "Le fichier source ne doit PAS être backupé lors d'un no-op"

    def test_json_absent_initialises_with_starting_cash(
        self, db: StateDb, tmp_path: Path
    ) -> None:
        """JSON absent + starting_cash=50000 → broker_state.cash==50000, positions/fills vides."""
        absent_path = tmp_path / "broker_absent.json"
        import_broker_from_json(db, absent_path, starting_cash=50_000.0)

        row = db.query_one("SELECT cash FROM broker_state WHERE id=1")
        assert row is not None
        assert row["cash"] == pytest.approx(50_000.0)

        assert db.query_all("SELECT * FROM broker_positions") == []
        assert db.query_all("SELECT * FROM broker_fills") == []


class TestImportBrokerInvalidJson:
    """FIX 4 — valider le JSON AVANT de renommer (fichier intact sur erreur)."""

    def test_invalid_json_raises_json_decode_error(self, db: StateDb, tmp_path: Path) -> None:
        """JSON corrompu → JSONDecodeError levée."""
        corrupted = tmp_path / "broker.json"
        corrupted.write_text("{ this is not valid json !!!}")

        with pytest.raises(json.JSONDecodeError):
            import_broker_from_json(db, corrupted, starting_cash=100_000.0)

    def test_invalid_json_file_preserved_after_error(self, db: StateDb, tmp_path: Path) -> None:
        """JSON corrompu → fichier original TOUJOURS présent après l'erreur (pas renommé)."""
        corrupted = tmp_path / "broker.json"
        corrupted.write_text("{ corrupted json }")

        with pytest.raises(json.JSONDecodeError):
            import_broker_from_json(db, corrupted, starting_cash=100_000.0)

        assert corrupted.exists(), "Le JSON corrompu ne doit PAS être renommé"
        bak_files = list(tmp_path.glob("broker.json.bak-*"))
        assert bak_files == [], "Aucun backup ne doit être créé sur erreur de parse"

    def test_invalid_json_tables_remain_empty(self, db: StateDb, tmp_path: Path) -> None:
        """JSON corrompu → broker_state reste vide (pas de demi-import)."""
        corrupted = tmp_path / "broker.json"
        corrupted.write_text("not json at all")

        with pytest.raises(json.JSONDecodeError):
            import_broker_from_json(db, corrupted, starting_cash=100_000.0)

        assert db.table_is_empty("broker_state")

    def test_retry_after_invalid_json_does_not_create_new_broker(
        self, db: StateDb, tmp_path: Path
    ) -> None:
        """Retry après JSON invalide → ne crée PAS un broker neuf (fichier toujours là)."""
        corrupted = tmp_path / "broker.json"
        corrupted.write_text("{{ invalid }}")

        # Premier essai : erreur
        with pytest.raises(json.JSONDecodeError):
            import_broker_from_json(db, corrupted, starting_cash=100_000.0)

        # Retry : le fichier est toujours là → nouvel essai échoue aussi (pas de broker neuf)
        with pytest.raises(json.JSONDecodeError):
            import_broker_from_json(db, corrupted, starting_cash=100_000.0)

        # Aucun broker_state créé
        assert db.table_is_empty("broker_state")
