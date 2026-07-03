"""Tests TDD — SCHEDULER_MIGRATION + import_scheduler_from_json.

Couvre :
- Schéma v3 : 4 tables (scheduler_meta, scheduler_symbol_wake,
  scheduler_stale_streaks, scheduler_watches) + 2 index.
- import_scheduler_from_json : peuplement tables, promotion legacy next_wake,
  canonicalisation UTC des timestamps, idempotence, backup, absent, JSON invalide.
- Sentinel d'idempotence (scheduler_meta toujours non-vide après import).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from trader.state_db.connection import StateDb
from trader.state_db.migrations import SCHEDULER_MIGRATION, import_scheduler_from_json


# ---------------------------------------------------------------------------
# Helpers fixture
# ---------------------------------------------------------------------------


def _scheduler_json(path: Path, data: dict) -> Path:
    path.write_text(json.dumps(data, indent=2))
    return path


def _rich_scheduler_data() -> dict:
    """Fixture riche : symboles + streaks + watches (armée + veille)."""
    return {
        "default_next_wake": "2026-07-03T10:00:00+00:00",
        "symbols": {
            "AAPL": "2026-07-03T11:00:00+00:00",
            "MSFT": "2026-07-03T12:00:00Z",
        },
        "stale_streaks": {
            "AAPL": 3,
            "MSFT": 1,
        },
        "indicator_watches": {
            "AAPL:watch001": {
                "id": "AAPL:watch001",
                "symbol": "AAPL",
                "on_trigger": "EXECUTE_ORDER",
                "order": {"intent": "OPEN_LONG", "qty": 50.0},
                "conditions": [{"indicator": "rsi", "op": "<=", "value": 30.0}],
                "expires_at": "2026-07-03T14:00:00+00:00",
            },
            "MSFT:watch002": {
                "id": "MSFT:watch002",
                "symbol": "MSFT",
                "on_trigger": "WAKE",
                "conditions": [],
                "expires_at": "2026-07-03T15:00:00+00:00",
            },
        },
    }


# ---------------------------------------------------------------------------
# Fixtures pytest
# ---------------------------------------------------------------------------


@pytest.fixture()
def db(tmp_path: Path) -> StateDb:
    return StateDb(tmp_path / "casys.db")


@pytest.fixture()
def scheduler_json(tmp_path: Path) -> Path:
    return _scheduler_json(tmp_path / "scheduler.json", _rich_scheduler_data())


# ---------------------------------------------------------------------------
# Schéma / constantes
# ---------------------------------------------------------------------------


class TestSchedulerMigrationSchema:
    def test_migration_version_is_3(self) -> None:
        version, _ = SCHEDULER_MIGRATION
        assert version == 3

    def test_migration_has_six_statements(self) -> None:
        """4 CREATE TABLE + 2 CREATE INDEX."""
        _, stmts = SCHEDULER_MIGRATION
        assert len(stmts) == 6

    def test_migration_creates_all_tables(self, db: StateDb) -> None:
        db.apply_migrations([SCHEDULER_MIGRATION])
        for table in ("scheduler_meta", "scheduler_symbol_wake",
                      "scheduler_stale_streaks", "scheduler_watches"):
            row = db.query_one(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
            )
            assert row is not None, f"Table {table} manquante"

    def test_migration_creates_indices(self, db: StateDb) -> None:
        db.apply_migrations([SCHEDULER_MIGRATION])
        for idx in ("idx_watches_symbol", "idx_watches_expires"):
            row = db.query_one(
                "SELECT name FROM sqlite_master WHERE type='index' AND name=?", (idx,)
            )
            assert row is not None, f"Index {idx} manquant"

    def test_migration_idempotent(self, db: StateDb) -> None:
        db.apply_migrations([SCHEDULER_MIGRATION])
        db.apply_migrations([SCHEDULER_MIGRATION])  # no-op, pas d'erreur
        rows = db.query_all(
            "SELECT version FROM schema_migrations WHERE version=3"
        )
        assert len(rows) == 1


# ---------------------------------------------------------------------------
# import_scheduler_from_json : cas nominaux
# ---------------------------------------------------------------------------


class TestImportSchedulerFromJson:
    def test_import_populates_meta_default_next_wake(
        self, db: StateDb, scheduler_json: Path
    ) -> None:
        import_scheduler_from_json(db, scheduler_json)
        row = db.query_one(
            "SELECT value FROM scheduler_meta WHERE key='default_next_wake'"
        )
        assert row is not None
        # Canonicalisé +00:00
        assert "+00:00" in row["value"]

    def test_import_populates_symbol_wake(
        self, db: StateDb, scheduler_json: Path
    ) -> None:
        import_scheduler_from_json(db, scheduler_json)
        rows = db.query_all("SELECT symbol, when_iso FROM scheduler_symbol_wake ORDER BY symbol")
        symbols = {r["symbol"]: r["when_iso"] for r in rows}
        assert set(symbols) == {"AAPL", "MSFT"}
        # Timestamps canonicalisés UTC
        for iso in symbols.values():
            assert "+00:00" in iso

    def test_import_populates_stale_streaks(
        self, db: StateDb, scheduler_json: Path
    ) -> None:
        import_scheduler_from_json(db, scheduler_json)
        rows = db.query_all("SELECT symbol, streak FROM scheduler_stale_streaks")
        streaks = {r["symbol"]: r["streak"] for r in rows}
        assert streaks == {"AAPL": 3, "MSFT": 1}

    def test_import_populates_watches(
        self, db: StateDb, scheduler_json: Path
    ) -> None:
        import_scheduler_from_json(db, scheduler_json)
        rows = db.query_all("SELECT id, on_trigger FROM scheduler_watches ORDER BY seq")
        assert len(rows) == 2
        ids = [r["id"] for r in rows]
        assert "AAPL:watch001" in ids
        assert "MSFT:watch002" in ids

    def test_import_watch_json_round_trip(
        self, db: StateDb, scheduler_json: Path
    ) -> None:
        data = _rich_scheduler_data()
        import_scheduler_from_json(db, scheduler_json)
        row = db.query_one(
            "SELECT watch_json FROM scheduler_watches WHERE id=?", ("AAPL:watch001",)
        )
        assert row is not None
        watch = json.loads(row["watch_json"])
        assert watch["id"] == "AAPL:watch001"
        assert watch["on_trigger"] == "EXECUTE_ORDER"
        assert isinstance(watch["order"], dict)

    def test_import_seq_preserves_insertion_order(
        self, db: StateDb, scheduler_json: Path
    ) -> None:
        import_scheduler_from_json(db, scheduler_json)
        rows = db.query_all("SELECT id, seq FROM scheduler_watches ORDER BY seq")
        seqs = [r["seq"] for r in rows]
        assert seqs == sorted(seqs), "seq doit être croissant"

    def test_import_canonicalises_z_suffix(
        self, db: StateDb, tmp_path: Path
    ) -> None:
        """timestamp Z est converti en +00:00 à l'import."""
        data = {
            "default_next_wake": "2026-07-03T10:00:00Z",
            "symbols": {"SPY": "2026-07-03T11:00:00Z"},
            "stale_streaks": {},
            "indicator_watches": {},
        }
        _scheduler_json(tmp_path / "scheduler.json", data)
        import_scheduler_from_json(db, tmp_path / "scheduler.json")

        meta_row = db.query_one(
            "SELECT value FROM scheduler_meta WHERE key='default_next_wake'"
        )
        assert meta_row["value"] == "2026-07-03T10:00:00+00:00"

        sym_row = db.query_one(
            "SELECT when_iso FROM scheduler_symbol_wake WHERE symbol='SPY'"
        )
        assert sym_row["when_iso"] == "2026-07-03T11:00:00+00:00"

    def test_import_creates_backup(
        self, db: StateDb, scheduler_json: Path
    ) -> None:
        import_scheduler_from_json(db, scheduler_json)
        assert not scheduler_json.exists(), "Le JSON source doit être renommé"
        baks = list(scheduler_json.parent.glob("scheduler.json.bak-*"))
        assert len(baks) == 1

    def test_reimport_is_noop(
        self, db: StateDb, scheduler_json: Path, tmp_path: Path
    ) -> None:
        import_scheduler_from_json(db, scheduler_json)

        # Deuxième import avec un JSON différent → no-op silencieux
        second = _scheduler_json(
            tmp_path / "other.json",
            {"default_next_wake": "2099-01-01T00:00:00+00:00", "symbols": {},
             "stale_streaks": {}, "indicator_watches": {}},
        )
        import_scheduler_from_json(db, second)

        # Données initiales toujours présentes
        rows = db.query_all("SELECT symbol FROM scheduler_symbol_wake")
        assert {r["symbol"] for r in rows} == {"AAPL", "MSFT"}
        # Second JSON NON backupé lors d'un no-op
        assert second.exists()

    def test_absent_json_starts_empty_with_sentinel(
        self, db: StateDb, tmp_path: Path
    ) -> None:
        absent = tmp_path / "scheduler.json"
        import_scheduler_from_json(db, absent)
        # Tables de données vides
        assert db.table_is_empty("scheduler_symbol_wake")
        assert db.table_is_empty("scheduler_stale_streaks")
        assert db.table_is_empty("scheduler_watches")
        # Mais scheduler_meta a le sentinel → idempotence garantie
        assert not db.table_is_empty("scheduler_meta")

    def test_absent_json_reimport_is_noop(
        self, db: StateDb, tmp_path: Path
    ) -> None:
        absent = tmp_path / "scheduler.json"
        import_scheduler_from_json(db, absent)

        # Créer un vrai scheduler.json maintenant, re-importer → toujours no-op
        data = {"default_next_wake": "2099-01-01T00:00:00+00:00",
                "symbols": {}, "stale_streaks": {}, "indicator_watches": {}}
        _scheduler_json(tmp_path / "scheduler.json", data)
        import_scheduler_from_json(db, tmp_path / "scheduler.json")

        # Le deuxième import NE doit PAS avoir importé les données du deuxième JSON
        assert db.table_is_empty("scheduler_symbol_wake")

    def test_invalid_json_raises_and_preserves_file(
        self, db: StateDb, tmp_path: Path
    ) -> None:
        bad = tmp_path / "scheduler.json"
        bad.write_text("{ not valid json !!!")

        with pytest.raises(json.JSONDecodeError):
            import_scheduler_from_json(db, bad)

        assert bad.exists(), "Fichier corrompu ne doit PAS être renommé"
        assert not list(bad.parent.glob("*.bak-*"))
        assert db.table_is_empty("scheduler_watches")

    def test_invalid_json_tables_remain_empty(
        self, db: StateDb, tmp_path: Path
    ) -> None:
        bad = tmp_path / "scheduler.json"
        bad.write_text("not json at all")

        with pytest.raises(json.JSONDecodeError):
            import_scheduler_from_json(db, bad)

        assert db.table_is_empty("scheduler_watches")
        # IMPORTANT : scheduler_meta DOIT rester vide sur erreur de parse
        # (le sentinel n'est inséré qu'APRÈS le parse réussi, en transaction)
        assert db.table_is_empty("scheduler_meta")


# ---------------------------------------------------------------------------
# Promotion legacy : next_wake → default_next_wake
# ---------------------------------------------------------------------------


class TestLegacyPromotion:
    def test_next_wake_promoted_to_default_next_wake(
        self, db: StateDb, tmp_path: Path
    ) -> None:
        """Vieux scheduler.json sans default_next_wake mais avec next_wake."""
        data = {
            "next_wake": "2026-07-03T08:00:00+00:00",
            "symbols": {},
            "stale_streaks": {},
            "indicator_watches": {},
        }
        _scheduler_json(tmp_path / "scheduler.json", data)
        import_scheduler_from_json(db, tmp_path / "scheduler.json")

        row = db.query_one(
            "SELECT value FROM scheduler_meta WHERE key='default_next_wake'"
        )
        assert row is not None
        assert row["value"] == "2026-07-03T08:00:00+00:00"

    def test_default_next_wake_takes_precedence_over_next_wake(
        self, db: StateDb, tmp_path: Path
    ) -> None:
        """Si les deux clés existent, default_next_wake prime."""
        data = {
            "next_wake": "2026-01-01T00:00:00+00:00",
            "default_next_wake": "2026-07-03T08:00:00+00:00",
            "symbols": {},
            "stale_streaks": {},
            "indicator_watches": {},
        }
        _scheduler_json(tmp_path / "scheduler.json", data)
        import_scheduler_from_json(db, tmp_path / "scheduler.json")

        row = db.query_one(
            "SELECT value FROM scheduler_meta WHERE key='default_next_wake'"
        )
        assert row["value"] == "2026-07-03T08:00:00+00:00"
