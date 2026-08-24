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
    UNIVERSE_SELECTION_V8_MIGRATION,
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
        "direction_source",
    } <= columns
    versions = {row["version"] for row in db.query_all("SELECT version FROM schema_migrations")}
    assert {6, 7, 8} <= versions
    assert len(db.query_all("SELECT version FROM schema_migrations WHERE version=6")) == 1
    assert len(db.query_all("SELECT version FROM schema_migrations WHERE version=7")) == 1
    assert len(db.query_all("SELECT version FROM schema_migrations WHERE version=8")) == 1


def test_v6_sql_n_est_pas_mutee() -> None:
    version, statements = UNIVERSE_SELECTION_MIGRATION
    assert version == 6
    assert UNIVERSE_SELECTION_V7_MIGRATION[0] == 7
    assert UNIVERSE_SELECTION_V8_MIGRATION[0] == 8
    body = " ".join(statements)
    assert "UNIQUE (mandate_id, symbol, as_of, horizon_sessions)" in body
    assert "verdict_basis" not in body
    v8_body = " ".join(UNIVERSE_SELECTION_V8_MIGRATION[1]).upper()
    assert "ALTER TABLE" in v8_body
    assert "ADD COLUMN" in v8_body
    assert "DIRECTION_SOURCE" in v8_body
    assert "CREATE TABLE" not in v8_body


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

    db.apply_migrations([UNIVERSE_SELECTION_V8_MIGRATION])
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


def test_v8_ajoute_direction_source_sans_recreer_ni_purger(tmp_path: Path) -> None:
    db = StateDb(tmp_path / "casys.db")
    db.apply_migrations([UNIVERSE_SELECTION_MIGRATION, UNIVERSE_SELECTION_V7_MIGRATION])
    with db.transaction() as cur:
        cur.execute(
            """
            INSERT INTO universe_selection_outcomes(
                mandate_id, symbol, family, role, allowed_sides, as_of, venue,
                horizon_sessions, forward_return, verdict, flair_score, evaluated_at,
                verdict_basis
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                "direction",
            ),
        )

    db.apply_migrations([UNIVERSE_SELECTION_V8_MIGRATION])

    columns = {row["name"] for row in db.query_all("PRAGMA table_info(universe_selection_outcomes)")}
    assert "direction_source" in columns
    remaining = db.query_all("SELECT mandate_id, verdict, direction_source FROM universe_selection_outcomes")
    assert len(remaining) == 1
    assert remaining[0]["mandate_id"] == "m-1"
    assert remaining[0]["verdict"] == "gagnant"
    assert remaining[0]["direction_source"] is None
    indexes = db.query_all("SELECT sql FROM sqlite_master WHERE type='table' AND name='universe_selection_outcomes'")
    assert "verdict_basis" in (indexes[0]["sql"] or "")
    assert "UNIQUE (mandate_id, symbol, as_of, horizon_sessions, verdict_basis)" in (indexes[0]["sql"] or "")


def test_try_open_ne_purge_pas_quand_la_metadata_manque(tmp_path: Path) -> None:
    from trader.infrastructure.state_db.connection import close_all_state_dbs, open_state_db
    from trader.infrastructure.state_db.universe_selection_store import (
        try_open_universe_selection_store,
    )

    close_all_state_dbs()
    db = open_state_db(tmp_path / "casys.db")
    db.apply_migrations(list(UNIVERSE_SELECTION_MIGRATIONS))
    store = UniverseSelectionStore(db)
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
    assert store.selection_semantics_version() is None

    reopened = try_open_universe_selection_store(tmp_path)
    assert reopened.count() == 1
    assert reopened.selection_semantics_version() is None
    close_all_state_dbs()


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


def test_store_persiste_direction_source(tmp_path: Path) -> None:
    store = _store(tmp_path)
    now = datetime(2026, 1, 10, tzinfo=timezone.utc)
    persist_and_score(
        store,
        [
            EvaluatedSelection(
                mandate_id="m-soft",
                symbol="AIR.PA",
                role="core_candidate",
                allowed_sides=("long", "short"),
                as_of="2026-01-01T08:00:00+00:00",
                venue="EU",
                family="eu_tech",
                horizon_sessions=5,
                forward_return=0.02,
                verdict="gagnant",
                verdict_basis="direction",
                direction_source="directional_view",
            )
        ],
        now=now,
    )
    rows = store.load_outcomes()
    assert len(rows) == 1
    assert rows[0]["direction_source"] == "directional_view"
    assert SELECTION_SEMANTICS_VERSION == "bench_v2"


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


def test_flair_baseline_ne_contamine_pas_les_scores_agent(tmp_path: Path) -> None:
    store = _store(tmp_path)
    now = datetime(2026, 1, 10, tzinfo=timezone.utc)
    agent_losses = [
        EvaluatedSelection(
            mandate_id=f"m-agent-{index}",
            symbol=f"AGT{index}",
            role="core_candidate",
            allowed_sides=("long",),
            as_of="2026-01-01T08:00:00+00:00",
            venue="EU",
            family="eu_tech",
            horizon_sessions=5,
            forward_return=-0.02,
            verdict="perdant",
            verdict_basis="allocation",
            selector="agent",
        )
        for index in range(3)
    ]
    baseline_wins = [
        EvaluatedSelection(
            mandate_id=f"m-fb-{index}",
            symbol=f"FB{index}",
            role="fallback_selection",
            allowed_sides=(),
            as_of="2026-01-01T08:00:00+00:00",
            venue="EU",
            family="eu_tech",
            horizon_sessions=5,
            forward_return=0.02,
            verdict="gagnant",
            verdict_basis="allocation",
            selector="baseline_fallback",
        )
        for index in range(3)
    ]
    persist_and_score(store, agent_losses + baseline_wins, now=now)
    rows = store.load_outcomes()
    agent_scores = [row["flair_score"] for row in rows if row["selector"] == "agent"]
    assert agent_scores
    assert all(score == 0.0 for score in agent_scores)
