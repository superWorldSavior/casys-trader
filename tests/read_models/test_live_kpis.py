from __future__ import annotations

import json
from pathlib import Path

import yaml

from trader.execution.broker import Order
from trader.infrastructure.state_db.broker_store import SqliteBroker
from trader.infrastructure.state_db.connection import open_state_db
from trader.infrastructure.state_db.migrations import import_broker_from_json
from trader.reporting.read_models.live_kpis import compute_live_kpis


def test_compute_live_kpis_read_model_projects_state_files(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "universe.yaml").write_text(
        yaml.dump({"starting_cash": 100_000.0, "symbols": ["SPY"]}),
        encoding="utf-8",
    )
    (state_dir / "history.jsonl").write_text(
        "\n".join(
            [
                json.dumps({"ts": "2026-01-01T10:00:00", "equity": 100_000.0}),
                json.dumps({"ts": "2026-01-02T10:00:00", "equity": 101_000.0}),
            ]
        ),
        encoding="utf-8",
    )
    (state_dir / "broker.json").write_text(
        json.dumps(
            {
                "cash": 95_000.0,
                "positions": {
                    "SPY": {"symbol": "SPY", "quantity": 5.0, "avg_price": 100.0},
                    "QQQ": {"symbol": "QQQ", "quantity": 0.0, "avg_price": 0.0},
                },
                "fills": [{"symbol": "SPY", "side": "BUY", "quantity": 5, "price": 100.0}],
            }
        ),
        encoding="utf-8",
    )

    kpis = compute_live_kpis(state_dir)

    assert kpis["equity"] == 101_000.0
    assert kpis["cash"] == 95_000.0
    assert kpis["num_trades"] == 1
    assert kpis["positions"] == [{"symbol": "SPY", "quantity": 5.0, "avg_price": 100.0}]


def test_compute_live_kpis_reads_broker_from_sqlite_when_db_exists(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "universe.yaml").write_text(
        yaml.dump({"starting_cash": 100_000.0, "symbols": ["SPY"]}),
        encoding="utf-8",
    )
    (state_dir / "history.jsonl").write_text(
        "\n".join(
            [
                json.dumps({"ts": "2026-01-01T10:00:00", "equity": 100_000.0}),
                json.dumps({"ts": "2026-01-02T10:00:00", "equity": 100_500.0}),
            ]
        ),
        encoding="utf-8",
    )

    db = open_state_db(state_dir / "casys.db")
    import_broker_from_json(db, state_dir / "_absent_broker.json", starting_cash=100_000.0)
    broker = SqliteBroker(db)
    broker.submit(Order("SPY", "BUY", 5.0), 100.0, "2026-01-01T10:00:00+00:00", dry_run=False)

    kpis = compute_live_kpis(state_dir)

    assert not (state_dir / "broker.json").exists()
    assert kpis["cash"] == 99_500.0
    assert kpis["num_trades"] == 1
    assert kpis["positions"] == [{"symbol": "SPY", "quantity": 5.0, "avg_price": 100.0}]
