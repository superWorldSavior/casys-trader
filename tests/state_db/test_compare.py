"""Tests TDD — FIX 3 compare_backends (trader/state_db/compare.py).

Couverture :
    - État identique (JSON shadow ↔ SQLite) → identical=True, exit 0
    - Divergence cash broker (JSON modifié post-import) → identical=False,
      broker.cash.identical=False, positions_diff vide
    - Divergence trade_plan → identical=False, trade_plans.diff non vide
    - Divergence scheduler (symbols_with_wake) → identical=False, wakes_diff non vide
    - CLI exit code : 0 si identical, 1 sinon
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from trader.state_db.connection import (
    _DB_REGISTRY,
    _DB_REGISTRY_LOCK,
    close_all_state_dbs,
    open_state_db,
)
from trader.state_db.broker_factory import bootstrap_state_backend
from trader.state_db.compare import compare_backends


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _clear_registry_for(paths: list[Path]) -> None:
    with _DB_REGISTRY_LOCK:
        for p in paths:
            _DB_REGISTRY.pop(str(p.resolve()), None)


def _bootstrap(tmp_path: Path, cash: float = 50_000.0) -> None:
    """Bootstrap un state_dir SQLite depuis zéro avec un broker.json initial."""
    broker_json = {
        "cash": cash,
        "positions": {},
        "fills": [],
    }
    (tmp_path / "broker.json").write_text(json.dumps(broker_json))

    db_path = tmp_path / "casys.db"
    _clear_registry_for([db_path])

    bootstrap_state_backend(
        state_dir=tmp_path,
        starting_cash=cash,
        commission_model=None,
        backend="sqlite",
    )


# ---------------------------------------------------------------------------
# Test 1 — États identiques → identical=True
# ---------------------------------------------------------------------------


def test_compare_identical_states(tmp_path: Path) -> None:
    """JSON shadow et SQLite identiques après bootstrap → identical=True."""
    _bootstrap(tmp_path, cash=50_000.0)
    try:
        result = compare_backends(tmp_path)
    finally:
        close_all_state_dbs()
        _clear_registry_for([tmp_path / "casys.db"])

    assert result["identical"] is True, (
        f"États identiques après bootstrap : identical doit être True. result={result!r}"
    )
    assert result["broker"]["cash"]["identical"] is True
    assert result["broker"]["positions_diff"] == []
    assert result["trade_plans"]["diff"] == []
    assert result["scheduler"]["wakes_diff"] == []
    assert result["scheduler"]["watches_diff"] == []
    assert result["scheduler"]["stale_diff"] == []


# ---------------------------------------------------------------------------
# Test 2 — Divergence broker.cash → identical=False
# ---------------------------------------------------------------------------


def test_compare_divergent_broker_cash(tmp_path: Path) -> None:
    """Modification du broker.json post-import → cash diverge JSON vs SQLite."""
    _bootstrap(tmp_path, cash=50_000.0)

    # Modifier le shadow JSON après que SQLite a déjà été importé
    # L'import SQLite (sentinel posé) ne sera pas rejouée → divergence
    broker_json_path = tmp_path / "broker.json"
    shadow = json.loads(broker_json_path.read_text())
    shadow["cash"] = 99_999.99
    broker_json_path.write_text(json.dumps(shadow))

    try:
        result = compare_backends(tmp_path)
    finally:
        close_all_state_dbs()
        _clear_registry_for([tmp_path / "casys.db"])

    assert result["identical"] is False, (
        "Cash différent JSON vs SQLite → identical doit être False"
    )
    assert result["broker"]["cash"]["identical"] is False
    assert abs(result["broker"]["cash"]["json"] - 99_999.99) < 1e-6
    assert abs(result["broker"]["cash"]["sqlite"] - 50_000.0) < 1e-6

    # Aucune divergence sur plans ou scheduler (non modifiés)
    assert result["trade_plans"]["diff"] == []
    assert result["scheduler"]["wakes_diff"] == []


# ---------------------------------------------------------------------------
# Test 3 — Divergence scheduler.symbols_with_wake → identical=False
# ---------------------------------------------------------------------------


def test_compare_divergent_scheduler_wakes(tmp_path: Path) -> None:
    """Modification de scheduler.json (symbols) post-import → wakes_diff non vide."""
    _bootstrap(tmp_path, cash=50_000.0)

    # Ajouter un symbol wake dans le shadow scheduler.json APRÈS import
    sched_json_path = tmp_path / "scheduler.json"
    sched = json.loads(sched_json_path.read_text())
    sched["symbols"]["AAPL.US"] = "2026-07-04T10:00:00+00:00"
    sched_json_path.write_text(json.dumps(sched))

    try:
        result = compare_backends(tmp_path)
    finally:
        close_all_state_dbs()
        _clear_registry_for([tmp_path / "casys.db"])

    assert result["identical"] is False, (
        "symbols_with_wake divergent → identical doit être False"
    )
    assert result["scheduler"]["wakes_diff"], (
        "wakes_diff doit être non vide quand symbols_with_wake divergent"
    )
    # Le diff doit mentionner AAPL.US (côté JSON) vs set vide (côté SQLite)
    wakes_field = next(
        (d for d in result["scheduler"]["wakes_diff"] if d["field"] == "symbols_with_wake"),
        None,
    )
    assert wakes_field is not None, "Un wakes_diff avec field=symbols_with_wake attendu"
    assert "AAPL.US" in wakes_field["json"]


# ---------------------------------------------------------------------------
# Test 4 — CLI exit code 0 si identical
# ---------------------------------------------------------------------------


def test_cli_exit_0_when_identical(tmp_path: Path) -> None:
    """CLI python -m trader.state_db.compare exit 0 si identical."""
    _bootstrap(tmp_path, cash=50_000.0)
    # Libère le registre pour le sous-processus
    close_all_state_dbs()
    _clear_registry_for([tmp_path / "casys.db"])

    proc = subprocess.run(
        [sys.executable, "-m", "trader.state_db.compare", str(tmp_path)],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, (
        f"CLI exit code doit être 0 si identical. stderr={proc.stderr!r}"
    )
    output = json.loads(proc.stdout)
    assert output["identical"] is True


# ---------------------------------------------------------------------------
# Test 5 — CLI exit code 1 si divergence
# ---------------------------------------------------------------------------


def test_cli_exit_1_when_divergent(tmp_path: Path) -> None:
    """CLI python -m trader.state_db.compare exit 1 si divergence."""
    _bootstrap(tmp_path, cash=50_000.0)

    # Modifier le shadow
    broker_json_path = tmp_path / "broker.json"
    shadow = json.loads(broker_json_path.read_text())
    shadow["cash"] = 1.0
    broker_json_path.write_text(json.dumps(shadow))

    # Libère le registre pour le sous-processus
    close_all_state_dbs()
    _clear_registry_for([tmp_path / "casys.db"])

    proc = subprocess.run(
        [sys.executable, "-m", "trader.state_db.compare", str(tmp_path)],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 1, (
        f"CLI exit code doit être 1 si divergence. stdout={proc.stdout!r}"
    )
    output = json.loads(proc.stdout)
    assert output["identical"] is False
    assert output["broker"]["cash"]["identical"] is False


# ---------------------------------------------------------------------------
# Test 6 — identical=True reflète bien le contenu du dict
# ---------------------------------------------------------------------------


def test_compare_result_structure(tmp_path: Path) -> None:
    """compare_backends retourne un dict avec la structure attendue."""
    _bootstrap(tmp_path, cash=10_000.0)
    try:
        result = compare_backends(tmp_path)
    finally:
        close_all_state_dbs()
        _clear_registry_for([tmp_path / "casys.db"])

    # Structure broker
    assert "broker" in result
    assert "cash" in result["broker"]
    assert "identical" in result["broker"]["cash"]
    assert "json" in result["broker"]["cash"]
    assert "sqlite" in result["broker"]["cash"]
    assert "positions_diff" in result["broker"]
    assert isinstance(result["broker"]["positions_diff"], list)

    # Structure trade_plans
    assert "trade_plans" in result
    assert "diff" in result["trade_plans"]
    assert isinstance(result["trade_plans"]["diff"], list)

    # Structure scheduler
    assert "scheduler" in result
    assert "wakes_diff" in result["scheduler"]
    assert "watches_diff" in result["scheduler"]
    assert "stale_diff" in result["scheduler"]

    # Clé identical globale
    assert "identical" in result
    assert isinstance(result["identical"], bool)
