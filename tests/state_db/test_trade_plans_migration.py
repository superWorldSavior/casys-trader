"""Tests TDD — TRADE_PLANS_MIGRATION + import_trade_plans_from_json."""
from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

import pytest

from trader.planning.trade_plan import (
    ProfitProtection,
    TakeProfit,
    TradePlan,
    TrailingStop,
)
from trader.state_db.connection import StateDb
from trader.state_db.migrations import (
    TRADE_PLANS_MIGRATION,
    import_trade_plans_from_json,
)
from trader.state_db.trade_plan_store import row_to_plan


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _rich_plan_dict(plan_id: str = "AAPL-001", symbol: str = "AAPL") -> dict:
    """Fixture dict d'un plan riche (tous les sous-objets non None)."""
    return {
        "id": plan_id,
        "symbol": symbol,
        "side": "LONG",
        "quantity": 10.0,
        "remaining_quantity": 10.0,
        "entry_price": 100.0,
        "opened_at": "2026-07-01T10:00:00+00:00",
        "reference_volatility": 0.02,
        "hard_stop_price": 95.0,
        "take_profits": [
            {"name": "tp1", "price": 110.0, "fraction": 0.5, "quantity": 5.0, "after_fill": ""},
            {"name": "tp2", "price": 120.0, "fraction": 0.5, "quantity": 5.0, "after_fill": "hold"},
        ],
        "trailing_stop": {
            "enabled_after": "tp1",
            "trail_type": "percent",
            "trail_value": 2.0,
            "trail_floored": False,
        },
        "max_hold_minutes": 120.0,
        "high_watermark": 105.0,
        "low_watermark": 98.0,
        "filled_take_profits": ["tp1"],
        "profit_protection": {
            "enabled": True,
            "arm_at_r": 0.5,
            "trigger_on_giveback_pct": 0.4,
            "close_fraction": 1.0 / 3.0,
            "move_stop_to": "breakeven",
            "min_hold_minutes": 10.0,
            "lock_r": 1.5,
            "triggered": False,
        },
        "exit_watch": {"type": "indicator", "on_trigger": "WAKE"},
        "llm_provider": "openai",
        "llm_model": "gpt-4o",
        "llm_fallback_reason": None,
        "llm_confidence": 0.85,
        "last_llm_review": {"ts": "2026-07-01T10:05:00+00:00", "action": "HOLD"},
        "entry_thesis": "Breakout from consolidation",
        "entry_decision_id": "dec-001",
        "entry_context": {"price": 100.0, "session": "US"},
    }


def _plans_json(plans: list[dict], path: Path) -> Path:
    path.write_text(json.dumps({"plans": plans}, indent=2))
    return path


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def db(tmp_path: Path) -> StateDb:
    return StateDb(tmp_path / "casys.db")


@pytest.fixture()
def plans_json(tmp_path: Path) -> Path:
    return _plans_json([_rich_plan_dict()], tmp_path / "trade_plans.json")


# ---------------------------------------------------------------------------
# Schéma / constantes
# ---------------------------------------------------------------------------


class TestTradePlansMigrationSchema:
    def test_migration_version_is_2(self) -> None:
        version, _ = TRADE_PLANS_MIGRATION
        assert version == 2

    def test_migration_has_two_statements(self) -> None:
        """CREATE TABLE + CREATE INDEX."""
        _, stmts = TRADE_PLANS_MIGRATION
        assert len(stmts) == 2

    def test_migration_creates_table_and_index(self, db: StateDb) -> None:
        db.apply_migrations([TRADE_PLANS_MIGRATION])
        row = db.query_one(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='trade_plans'"
        )
        assert row is not None, "Table trade_plans manquante"
        idx = db.query_one(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_trade_plans_symbol'"
        )
        assert idx is not None, "Index idx_trade_plans_symbol manquant"


# ---------------------------------------------------------------------------
# Import depuis JSON
# ---------------------------------------------------------------------------


class TestImportTradePlansFromJson:
    def test_import_populates_table(self, db: StateDb, plans_json: Path) -> None:
        import_trade_plans_from_json(db, plans_json)
        rows = db.query_all("SELECT id FROM trade_plans")
        assert len(rows) == 1
        assert rows[0]["id"] == "AAPL-001"

    def test_round_trip_rich_plan(self, db: StateDb, tmp_path: Path) -> None:
        """Import d'un plan riche → open_plans() retourne un plan asdict-identique."""
        rich = _rich_plan_dict()
        _plans_json([rich], tmp_path / "trade_plans.json")
        import_trade_plans_from_json(db, tmp_path / "trade_plans.json")

        rows = db.query_all("SELECT * FROM trade_plans ORDER BY seq")
        assert len(rows) == 1
        plan = row_to_plan(rows[0])

        # Le round-trip doit être exact via asdict
        from trader.planning.trade_plan import trade_plan_from_dict

        original = trade_plan_from_dict(rich)
        assert asdict(plan) == asdict(original)

    def test_import_seq_preserves_order(self, db: StateDb, tmp_path: Path) -> None:
        """seq assigné dans l'ordre d'apparition dans le JSON."""
        plans = [
            _rich_plan_dict("P1", "AAPL"),
            _rich_plan_dict("P2", "MSFT"),
            _rich_plan_dict("P3", "TSLA"),
        ]
        _plans_json(plans, tmp_path / "trade_plans.json")
        import_trade_plans_from_json(db, tmp_path / "trade_plans.json")

        rows = db.query_all("SELECT id, seq FROM trade_plans ORDER BY seq")
        ids = [r["id"] for r in rows]
        assert ids == ["P1", "P2", "P3"]
        seqs = [r["seq"] for r in rows]
        assert seqs == sorted(seqs), "seq doit être croissant"

    def test_import_creates_backup(self, db: StateDb, plans_json: Path) -> None:
        import_trade_plans_from_json(db, plans_json)
        assert not plans_json.exists(), "Le JSON source doit être renommé"
        baks = list(plans_json.parent.glob("trade_plans.json.bak-*"))
        assert len(baks) == 1

    def test_reimport_is_noop(self, db: StateDb, plans_json: Path, tmp_path: Path) -> None:
        """Ré-import (table non vide) → no-op silencieux, pas de doublon."""
        import_trade_plans_from_json(db, plans_json)

        second = _plans_json([_rich_plan_dict("OTHER")], tmp_path / "other.json")
        import_trade_plans_from_json(db, second)

        rows = db.query_all("SELECT id FROM trade_plans")
        assert len(rows) == 1
        assert rows[0]["id"] == "AAPL-001"
        assert second.exists(), "second JSON ne doit PAS être backupé lors d'un no-op"

    def test_absent_json_starts_empty(self, db: StateDb, tmp_path: Path) -> None:
        absent = tmp_path / "trade_plans.json"
        import_trade_plans_from_json(db, absent)
        assert db.table_is_empty("trade_plans")

    def test_invalid_json_raises_and_preserves_file(
        self, db: StateDb, tmp_path: Path
    ) -> None:
        bad = tmp_path / "trade_plans.json"
        bad.write_text("{ not valid json !!!")

        with pytest.raises(json.JSONDecodeError):
            import_trade_plans_from_json(db, bad)

        assert bad.exists(), "Fichier corrompu ne doit PAS être renommé"
        assert not list(bad.parent.glob("*.bak-*")), "Aucun backup sur erreur"
        assert db.table_is_empty("trade_plans"), "Table doit rester vide sur erreur de parse"

    def test_invalid_json_tables_remain_empty(self, db: StateDb, tmp_path: Path) -> None:
        bad = tmp_path / "trade_plans.json"
        bad.write_text("not json at all")

        with pytest.raises(json.JSONDecodeError):
            import_trade_plans_from_json(db, bad)

        assert db.table_is_empty("trade_plans")
