"""Tests — UNIVERSE_SELECTION_MIGRATION + store d'attribution."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from trader.application.universe.selection_attribution import (
    EvaluatedSelection,
    persist_and_score,
)
from trader.domain.universe.selection_attribution import SELECTION_SEMANTICS_VERSION
from trader.infrastructure.state_db.connection import StateDb
from trader.infrastructure.state_db.migrations import (
    UNIVERSE_SELECTION_MIGRATION,
    UNIVERSE_SELECTION_MIGRATIONS,
    UNIVERSE_SELECTION_V7_MIGRATION,
)
from trader.infrastructure.state_db.universe_selection_store import UniverseSelectionStore


def _store(tmp_path: Path) -> UniverseSelectionStore:
    db = StateDb(tmp_path / "casys.db")
    db.apply_migrations(list(UNIVERSE_SELECTION_MIGRATIONS))
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
    db.apply_migrations(list(UNIVERSE_SELECTION_MIGRATIONS))
    db.apply_migrations(list(UNIVERSE_SELECTION_MIGRATIONS))

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
        "verdict_basis",
        "candidate_scope_id",
        "selector",
        "opportunity",
        "bench_median_opportunity",
        "allocation_excess",
        "bench_n",
    } <= columns
    versions = {row["version"] for row in db.query_all("SELECT version FROM schema_migrations")}
    assert {6, 7} <= versions
    assert len(db.query_all("SELECT version FROM schema_migrations WHERE version=6")) == 1
    assert len(db.query_all("SELECT version FROM schema_migrations WHERE version=7")) == 1


def test_v6_sql_n_est_pas_mutee() -> None:
    version, statements = UNIVERSE_SELECTION_MIGRATION
    assert version == 6
    assert UNIVERSE_SELECTION_V7_MIGRATION[0] == 7
    body = " ".join(statements)
    assert "UNIQUE (mandate_id, symbol, as_of, horizon_sessions)" in body
    assert "verdict_basis" not in body


def test_v6_avec_lignes_devient_v7_direction_et_unique_deux_bases(tmp_path: Path) -> None:
    db = StateDb(tmp_path / "casys.db")
    db.apply_migrations([UNIVERSE_SELECTION_MIGRATION])
    with db.transaction() as cur:
        cur.execute(
            """
            INSERT INTO universe_selection_outcomes(
                mandate_id, symbol, family, role, allowed_sides, as_of, venue,
                horizon_sessions, forward_return, verdict, flair_score, evaluated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "m-1",
                "AAPL",
                "us_tech",
                "core_candidate",
                '["long"]',
                "2026-01-01T08:00:00+00:00",
                "US",
                5,
                0.02,
                "gagnant",
                0.1,
                "2026-01-10T00:00:00+00:00",
            ),
        )

    db.apply_migrations([UNIVERSE_SELECTION_V7_MIGRATION])

    migrated = db.query_all(
        "SELECT mandate_id, symbol, verdict, verdict_basis, selector, opportunity, bench_n FROM universe_selection_outcomes"
    )
    assert len(migrated) == 1
    assert migrated[0]["verdict_basis"] == "direction"
    assert migrated[0]["verdict"] == "gagnant"
    assert migrated[0]["selector"] is None
    assert migrated[0]["opportunity"] is None
    assert migrated[0]["bench_n"] is None

    meta = db.query_all("SELECT name FROM sqlite_master WHERE type='table' AND name='universe_selection_metadata'")
    assert meta

    store = UniverseSelectionStore(db)
    store.upsert_outcomes(
        [
            {
                "mandate_id": "m-1",
                "symbol": "AAPL",
                "family": "us_tech",
                "role": "core_candidate",
                "allowed_sides": ["long"],
                "as_of": "2026-01-01T08:00:00+00:00",
                "venue": "US",
                "horizon_sessions": 5,
                "forward_return": 0.02,
                "verdict": "gagnant",
                "evaluated_at": "2026-01-10T00:00:00+00:00",
                "verdict_basis": "allocation",
                "candidate_scope_id": "scope-us",
                "selector": "agent",
                "opportunity": 0.02,
                "bench_median_opportunity": 0.01,
                "allocation_excess": 0.01,
                "bench_n": 8,
            }
        ]
    )
    rows = store.load_outcomes()
    assert len(rows) == 2
    bases = {row["verdict_basis"] for row in rows}
    assert bases == {"direction", "allocation"}
    allocation = next(row for row in rows if row["verdict_basis"] == "allocation")
    assert allocation["selector"] == "agent"
    assert allocation["opportunity"] == 0.02
    assert allocation["bench_n"] == 8


def test_semantics_bump_purge_les_outcomes_et_reste_idempotent(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.upsert_outcomes(
        [
            {
                "mandate_id": "m-1",
                "symbol": "AAPL",
                "family": "us_tech",
                "role": "core",
                "allowed_sides": ["long"],
                "as_of": "2026-01-01T08:00:00+00:00",
                "venue": "US",
                "horizon_sessions": 5,
                "forward_return": 0.02,
                "verdict": "gagnant",
                "evaluated_at": "2026-01-10T00:00:00+00:00",
                "verdict_basis": "direction",
            }
        ]
    )
    assert store.count() == 1

    first = store.ensure_selection_semantics()
    second = store.ensure_selection_semantics()
    assert first == second == SELECTION_SEMANTICS_VERSION
    assert store.count() == 0
    assert store.selection_semantics_version() == SELECTION_SEMANTICS_VERSION


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
