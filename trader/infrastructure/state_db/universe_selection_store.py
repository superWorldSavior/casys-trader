"""Persistance SQLite des verdicts d'attribution des sélections d'univers.

Le store reçoit une StateDb déjà ouverte (casys.db partagé). Il n'ouvre pas
de connexion séparée. La migration v6 doit être appliquée avant construction.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from trader.infrastructure.state_db.connection import StateDb
from trader.infrastructure.state_db.migrations import UNIVERSE_SELECTION_MIGRATION


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
        "forward_return": None if row["forward_return"] is None else float(row["forward_return"]),
        "verdict": str(row["verdict"]),
        "flair_score": None if row["flair_score"] is None else float(row["flair_score"]),
        "evaluated_at": str(row["evaluated_at"]),
    }


class UniverseSelectionStore:
    """Upsert / lecture de ``universe_selection_outcomes``.

    Args:
        db: StateDb ouverte avec ``UNIVERSE_SELECTION_MIGRATION`` appliquée.
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
                        horizon_sessions, forward_return, verdict, flair_score, evaluated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)
                    ON CONFLICT(mandate_id, symbol, as_of, horizon_sessions) DO UPDATE SET
                        family=excluded.family,
                        role=excluded.role,
                        allowed_sides=excluded.allowed_sides,
                        venue=excluded.venue,
                        forward_return=excluded.forward_return,
                        verdict=excluded.verdict,
                        evaluated_at=excluded.evaluated_at
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
                    ),
                )

    def load_outcomes(self) -> list[dict[str, Any]]:
        rows = self._db.query_all(
            """
            SELECT id, mandate_id, symbol, family, role, allowed_sides, as_of, venue,
                   horizon_sessions, forward_return, verdict, flair_score, evaluated_at
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


def try_open_universe_selection_store(state_dir: str | Path) -> UniverseSelectionStore:
    """Ouvre le store sur casys.db et applique la migration v6 si besoin."""
    from trader.infrastructure.state_db.connection import open_state_db

    db = open_state_db(Path(state_dir) / "casys.db")
    db.apply_migrations([UNIVERSE_SELECTION_MIGRATION])
    return UniverseSelectionStore(db)


__all__ = ["UniverseSelectionStore", "try_open_universe_selection_store"]
