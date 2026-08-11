"""Tests de la migration additive v4 des traces de processus."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from trader.infrastructure.state_db.connection import StateDb
from trader.infrastructure.state_db.migrations import (
    BROKER_MIGRATION,
    PROCESS_TRACE_MIGRATION,
    import_broker_from_json,
)


def test_v4_upgrades_a_v1_broker_without_rewriting_legacy_fills(tmp_path: Path) -> None:
    db = StateDb(tmp_path / "casys.db")
    db.apply_migrations([BROKER_MIGRATION])
    with db.transaction() as cur:
        cur.execute(
            """INSERT INTO broker_fills(
                symbol, side, quantity, price, ts, commission,
                commission_currency, commission_model, fx_rate
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("AAPL", "BUY", 1.0, 200.0, "2026-08-09T00:00:00+00:00", 0.0, "USD", "none", 1.0),
        )

    db.apply_migrations([PROCESS_TRACE_MIGRATION])

    columns = {row["name"] for row in db.query_all("PRAGMA table_info(broker_fills)")}
    assert {"process_instance_id", "attempt_id", "decision_id"} <= columns
    legacy_fill = db.query_one("SELECT * FROM broker_fills WHERE symbol='AAPL'")
    assert legacy_fill is not None
    assert legacy_fill["process_instance_id"] is None
    assert legacy_fill["attempt_id"] is None
    assert legacy_fill["decision_id"] is None


def test_v4_creates_process_event_table_and_indexes(tmp_path: Path) -> None:
    db = StateDb(tmp_path / "casys.db")
    db.apply_migrations([BROKER_MIGRATION, PROCESS_TRACE_MIGRATION])

    columns = {row["name"] for row in db.query_all("PRAGMA table_info(process_events)")}
    assert {
        "seq",
        "event_id",
        "process_type",
        "process_version",
        "process_instance_id",
        "attempt_id",
        "runtime_run_id",
        "work_object_type",
        "work_object_key",
        "event_type",
        "ts",
        "caused_by_json",
        "terminal_result",
        "outcome_code",
        "effect_status",
        "effect_refs_json",
        "version_pins_json",
    } == columns
    indexes = {row["name"] for row in db.query_all("PRAGMA index_list(process_events)")}
    assert {"idx_process_events_instance", "idx_process_events_work"} <= indexes


def test_v4_allows_legacy_nulls_but_rejects_a_second_fill_for_one_instance(
    tmp_path: Path,
) -> None:
    db = StateDb(tmp_path / "casys.db")
    db.apply_migrations([BROKER_MIGRATION, PROCESS_TRACE_MIGRATION])
    fill_values = ("AAPL", "BUY", 1.0, 200.0, "2026-08-09T00:00:00+00:00")
    with db.transaction() as cur:
        cur.execute(
            """INSERT INTO broker_fills(symbol, side, quantity, price, ts)
               VALUES (?, ?, ?, ?, ?)""",
            fill_values,
        )
        cur.execute(
            """INSERT INTO broker_fills(
                   symbol, side, quantity, price, ts, process_instance_id
               ) VALUES (?, ?, ?, ?, ?, ?)""",
            (*fill_values, "instance-1"),
        )

    with pytest.raises(sqlite3.IntegrityError):
        with db.transaction() as cur:
            cur.execute(
                """INSERT INTO broker_fills(
                       symbol, side, quantity, price, ts, process_instance_id
                   ) VALUES (?, ?, ?, ?, ?, ?)""",
                (*fill_values, "instance-1"),
            )


def test_v4_is_idempotent(tmp_path: Path) -> None:
    db = StateDb(tmp_path / "casys.db")
    migrations = [BROKER_MIGRATION, PROCESS_TRACE_MIGRATION]

    db.apply_migrations(migrations)
    db.apply_migrations(migrations)

    rows = db.query_all("SELECT version FROM schema_migrations WHERE version=4")
    assert len(rows) == 1


def test_broker_boot_applies_v4_before_an_existing_import_sentinel(tmp_path: Path) -> None:
    """Le sentinel broker ne doit pas empêcher l'upgrade d'une base déjà importée."""

    db = StateDb(tmp_path / "casys.db")
    db.apply_migrations([BROKER_MIGRATION])
    db.executescript("""CREATE TABLE state_imports(store TEXT PRIMARY KEY, imported_at TEXT);""")
    with db.transaction() as cur:
        cur.execute("INSERT INTO broker_state(id, cash) VALUES (1, 100000.0)")
        cur.execute("INSERT INTO state_imports(store, imported_at) VALUES ('broker', '2026-08-09T00:00:00+00:00')")

    import_broker_from_json(
        db,
        tmp_path / "already-imported-broker.json",
        starting_cash=1.0,
    )

    assert db.query_one("SELECT 1 FROM schema_migrations WHERE version=4") is not None
    assert db.query_one("SELECT name FROM sqlite_master WHERE type='table' AND name='process_events'") is not None
    assert db.query_one("SELECT cash FROM broker_state WHERE id=1")["cash"] == 100000.0


def test_json_import_preserves_optional_process_correlation(tmp_path: Path) -> None:
    db = StateDb(tmp_path / "casys.db")
    broker_json = tmp_path / "broker.json"
    broker_json.write_text(
        json.dumps(
            {
                "cash": 100000.0,
                "positions": {},
                "fills": [
                    {
                        "symbol": "AAPL",
                        "side": "BUY",
                        "quantity": 1.0,
                        "price": 200.0,
                        "ts": "2026-08-09T00:00:00+00:00",
                        "process_instance_id": "instance-1",
                        "attempt_id": "attempt-1",
                        "decision_id": "decision-1",
                    },
                    {
                        "symbol": "MSFT",
                        "side": "BUY",
                        "quantity": 1.0,
                        "price": 400.0,
                        "ts": "2026-08-09T00:01:00+00:00",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    import_broker_from_json(db, broker_json, starting_cash=1.0)

    linked = db.query_one("SELECT * FROM broker_fills WHERE symbol='AAPL'")
    legacy = db.query_one("SELECT * FROM broker_fills WHERE symbol='MSFT'")
    assert linked is not None
    assert linked["process_instance_id"] == "instance-1"
    assert linked["attempt_id"] == "attempt-1"
    assert linked["decision_id"] == "decision-1"
    assert legacy is not None
    assert legacy["process_instance_id"] is None
    assert legacy["attempt_id"] is None
    assert legacy["decision_id"] is None
