"""Tests — UNIVERSE_SELECTION_MIGRATION + store d'attribution."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from trader.application.universe.selection_attribution import (
    EvaluatedSelection,
    persist_and_score,
)
from trader.infrastructure.state_db.connection import StateDb
from trader.infrastructure.state_db.migrations import UNIVERSE_SELECTION_MIGRATION
from trader.infrastructure.state_db.universe_selection_store import UniverseSelectionStore


def _store(tmp_path: Path) -> UniverseSelectionStore:
    db = StateDb(tmp_path / "casys.db")
    db.apply_migrations([UNIVERSE_SELECTION_MIGRATION])
    return UniverseSelectionStore(db)


def _evaluated(
    *,
    mandate_id: str,
    symbol: str,
    family: str,
    verdict: str,
    forward_return: float,
    as_of: str = "2026-01-01T08:00:00+00:00",
    role: str = "core_candidate",
    venue: str = "US",
) -> EvaluatedSelection:
    return EvaluatedSelection(
        mandate_id=mandate_id,
        symbol=symbol,
        role=role,
        allowed_sides=("long",),
        as_of=as_of,
        venue=venue,
        family=family,
        horizon_sessions=5,
        forward_return=forward_return,
        verdict=verdict,
    )


def test_universe_selection_migration_creates_table_and_is_idempotent(tmp_path: Path) -> None:
    db = StateDb(tmp_path / "casys.db")
    db.apply_migrations([UNIVERSE_SELECTION_MIGRATION])
    db.apply_migrations([UNIVERSE_SELECTION_MIGRATION])

    columns = {row["name"] for row in db.query_all("PRAGMA table_info(universe_selection_outcomes)")}
    assert {
        "id",
        "mandate_id",
        "symbol",
        "family",
        "role",
        "allowed_sides",
        "as_of",
        "venue",
        "horizon_sessions",
        "forward_return",
        "verdict",
        "flair_score",
        "evaluated_at",
    } <= columns
    assert db.query_one("SELECT 1 FROM schema_migrations WHERE version=6") is not None
    versions = db.query_all("SELECT version FROM schema_migrations WHERE version=6")
    assert len(versions) == 1


def test_deux_evaluations_successives_ne_dupliquent_pas(tmp_path: Path) -> None:
    store = _store(tmp_path)
    now = datetime(2026, 1, 10, tzinfo=timezone.utc)
    item = _evaluated(
        mandate_id="m-1",
        symbol="AAPL",
        family="us_tech",
        verdict="gagnant",
        forward_return=0.02,
    )

    persist_and_score(store, [item], now=now)
    persist_and_score(store, [item], now=now)

    assert store.count() == 1
    rows = store.load_outcomes()
    assert len(rows) == 1
    assert rows[0]["mandate_id"] == "m-1"
    assert rows[0]["symbol"] == "AAPL"
    assert rows[0]["verdict"] == "gagnant"
    assert rows[0]["flair_score"] is not None


def test_flair_persiste_lift_famille_gagnante_superieur(tmp_path: Path) -> None:
    store = _store(tmp_path)
    now = datetime(2026, 1, 10, tzinfo=timezone.utc)
    evaluated = [
        _evaluated(
            mandate_id=f"m-win-{index}",
            symbol=f"WIN{index}",
            family="alpha",
            verdict="gagnant",
            forward_return=0.02,
        )
        for index in range(3)
    ] + [
        _evaluated(
            mandate_id=f"m-loss-{index}",
            symbol=f"LOSS{index}",
            family="beta",
            verdict="perdant",
            forward_return=-0.02,
        )
        for index in range(3)
    ]

    persist_and_score(store, evaluated, now=now)
    rows = store.load_outcomes()
    alpha = [row["flair_score"] for row in rows if row["family"] == "alpha"]
    beta = [row["flair_score"] for row in rows if row["family"] == "beta"]
    assert alpha and beta
    assert sum(alpha) / len(alpha) > sum(beta) / len(beta)
