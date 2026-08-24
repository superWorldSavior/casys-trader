"""Persistance SQLite des verdicts d'attribution des sélections d'univers.

Le store reçoit une StateDb déjà ouverte (casys.db partagé). Il n'ouvre pas
de connexion séparée. Les migrations v6+v7+v8 doivent être appliquées avant
construction. La v6 n'est pas mutée : la v7 recrée la table. La v8 est
additive (``direction_source``).
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from trader.domain.universe.selection_attribution import (
    SELECTION_SEMANTICS_KEY,
    SELECTION_SEMANTICS_VERSION,
)
from trader.infrastructure.state_db.connection import StateDb
from trader.infrastructure.state_db.migrations import UNIVERSE_SELECTION_MIGRATIONS


def _sides_to_json(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)):
        return json.dumps(list(value), ensure_ascii=False)
    return "[]"


def _sides_from_json(raw: object) -> list[str]:
    if isinstance(raw, list):
        return [str(item) for item in raw]
    try:
        parsed = json.loads(str(raw or "[]"))
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    return [str(item) for item in parsed]


def _optional_float(value: object) -> float | None:
    if value is None:
        return None
    return float(value)


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    return int(value)


def _row_to_dict(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "mandate_id": str(row["mandate_id"]),
        "symbol": str(row["symbol"]),
        "family": str(row["family"] or ""),
        "role": str(row["role"] or ""),
        "allowed_sides": _sides_from_json(row["allowed_sides"]),
        "as_of": str(row["as_of"]),
        "venue": str(row["venue"] or ""),
        "horizon_sessions": int(row["horizon_sessions"]),
        "forward_return": _optional_float(row["forward_return"]),
        "verdict": str(row["verdict"]),
        "flair_score": _optional_float(row["flair_score"]),
        "evaluated_at": str(row["evaluated_at"]),
        "verdict_basis": str(row["verdict_basis"] or "direction"),
        "candidate_scope_id": str(row["candidate_scope_id"] or ""),
        "selector": str(row["selector"] or ""),
        "opportunity": _optional_float(row["opportunity"]),
        "bench_median_opportunity": _optional_float(row["bench_median_opportunity"]),
        "allocation_excess": _optional_float(row["allocation_excess"]),
        "bench_n": _optional_int(row["bench_n"]),
        "direction_source": str(row["direction_source"] or ""),
    }


class UniverseSelectionStore:
    """Upsert / lecture de ``universe_selection_outcomes``.

    Args:
        db: StateDb ouverte avec les migrations v6+v7+v8 appliquées.
    """

    def __init__(self, db: StateDb) -> None:
        self._db = db

    def upsert_outcomes(self, rows: Sequence[Mapping[str, Any]]) -> None:
        if not rows:
            return
        with self._db.transaction() as cur:
            for row in rows:
                cur.execute(
                    """
                    INSERT INTO universe_selection_outcomes(
                        mandate_id, symbol, family, role, allowed_sides, as_of, venue,
                        horizon_sessions, forward_return, verdict, flair_score, evaluated_at,
                        verdict_basis, candidate_scope_id, selector,
                        opportunity, bench_median_opportunity, allocation_excess, bench_n,
                        direction_source
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(mandate_id, symbol, as_of, horizon_sessions, verdict_basis)
                    DO UPDATE SET
                        family=excluded.family,
                        role=excluded.role,
                        allowed_sides=excluded.allowed_sides,
                        venue=excluded.venue,
                        forward_return=excluded.forward_return,
                        verdict=excluded.verdict,
                        evaluated_at=excluded.evaluated_at,
                        candidate_scope_id=excluded.candidate_scope_id,
                        selector=excluded.selector,
                        opportunity=excluded.opportunity,
                        bench_median_opportunity=excluded.bench_median_opportunity,
                        allocation_excess=excluded.allocation_excess,
                        bench_n=excluded.bench_n,
                        direction_source=excluded.direction_source
                    """,
                    (
                        str(row.get("mandate_id") or ""),
                        str(row.get("symbol") or ""),
                        str(row.get("family") or ""),
                        str(row.get("role") or ""),
                        _sides_to_json(row.get("allowed_sides")),
                        str(row.get("as_of") or ""),
                        str(row.get("venue") or ""),
                        int(row.get("horizon_sessions") or 0),
                        row.get("forward_return"),
                        str(row.get("verdict") or ""),
                        str(row.get("evaluated_at") or ""),
                        str(row.get("verdict_basis") or "direction"),
                        str(row.get("candidate_scope_id") or "") or None,
                        str(row.get("selector") or "") or None,
                        row.get("opportunity"),
                        row.get("bench_median_opportunity"),
                        row.get("allocation_excess"),
                        row.get("bench_n"),
                        str(row.get("direction_source") or "") or None,
                    ),
                )

    def load_outcomes(self) -> list[dict[str, Any]]:
        rows = self._db.query_all(
            """
            SELECT id, mandate_id, symbol, family, role, allowed_sides, as_of, venue,
                   horizon_sessions, forward_return, verdict, flair_score, evaluated_at,
                   verdict_basis, candidate_scope_id, selector,
                   opportunity, bench_median_opportunity, allocation_excess, bench_n,
                   direction_source
              FROM universe_selection_outcomes
             ORDER BY id
            """
        )
        return [_row_to_dict(row) for row in rows]

    def update_flair_scores(self, scores: Mapping[int, float]) -> None:
        if not scores:
            return
        with self._db.transaction() as cur:
            for row_id, score in scores.items():
                cur.execute(
                    "UPDATE universe_selection_outcomes SET flair_score=? WHERE id=?",
                    (float(score), int(row_id)),
                )

    def count(self) -> int:
        row = self._db.query_one("SELECT COUNT(*) AS n FROM universe_selection_outcomes")
        return 0 if row is None else int(row["n"])

    def selection_semantics_version(self) -> str | None:
        row = self._db.query_one(
            "SELECT value FROM universe_selection_metadata WHERE key=?",
            (SELECTION_SEMANTICS_KEY,),
        )
        if row is None:
            return None
        return str(row["value"])

    def ensure_selection_semantics(self, *, now: datetime | None = None) -> str:
        """Idempotent bump: purge derived outcomes when the semantics version changes."""
        clock = now or datetime.now(timezone.utc)
        updated_at = clock.isoformat()
        with self._db.transaction() as cur:
            current = cur.execute(
                "SELECT value FROM universe_selection_metadata WHERE key=?",
                (SELECTION_SEMANTICS_KEY,),
            ).fetchone()
            if current is not None and str(current[0]) == SELECTION_SEMANTICS_VERSION:
                return SELECTION_SEMANTICS_VERSION
            cur.execute("DELETE FROM universe_selection_outcomes")
            cur.execute(
                """
                INSERT INTO universe_selection_metadata(key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value=excluded.value,
                    updated_at=excluded.updated_at
                """,
                (SELECTION_SEMANTICS_KEY, SELECTION_SEMANTICS_VERSION, updated_at),
            )
        return SELECTION_SEMANTICS_VERSION


def try_open_universe_selection_store(state_dir: str | Path) -> UniverseSelectionStore:
    """Open casys.db and apply v6+v7+v8. Does not bump or purge semantics.

    Readers (digest, ``summary``) must not wipe derived outcomes. The
    rejudge path calls ``ensure_selection_semantics`` itself.
    """
    from trader.infrastructure.state_db.connection import open_state_db

    db = open_state_db(Path(state_dir) / "casys.db")
    db.apply_migrations(list(UNIVERSE_SELECTION_MIGRATIONS))
    return UniverseSelectionStore(db)


__all__ = ["UniverseSelectionStore", "try_open_universe_selection_store"]
