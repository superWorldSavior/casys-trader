"""Persistance SQLite de la cadence last_llm_at du gate de pertinence.

Le store reçoit une StateDb déjà ouverte (casys.db partagé). Il n'ouvre pas
de connexion séparée. La migration v5 doit être appliquée avant construction.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from trader.infrastructure.state_db.connection import StateDb
from trader.infrastructure.state_db.migrations import LLM_GATE_MIGRATION


def _canon_dt(at: datetime) -> str:
    """Canonicalise un datetime → ISO UTC +00:00 (comparaison SQL lexicale sûre)."""
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    else:
        at = at.astimezone(timezone.utc)
    return at.isoformat()


def _parse_ts(raw: str) -> datetime:
    value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class LlmGateStore:
    """Upsert / lecture de ``llm_gate_last_seen``.

    Args:
        db: StateDb ouverte avec ``LLM_GATE_MIGRATION`` appliquée.
    """

    def __init__(self, db: StateDb) -> None:
        self._db = db

    def load_all(self) -> dict[tuple[str, str], datetime]:
        """Retourne toutes les paires ``(state_dir, symbol) → last_at``."""
        rows = self._db.query_all(
            "SELECT state_dir, symbol, last_at FROM llm_gate_last_seen"
        )
        return {(row["state_dir"], row["symbol"]): _parse_ts(row["last_at"]) for row in rows}

    def record(self, state_dir: str, symbol: str, at: datetime) -> None:
        """Enregistre (ou remplace) le timestamp de revue LLM d'un symbole."""
        last_at = _canon_dt(at)
        with self._db.transaction() as cur:
            cur.execute(
                "INSERT INTO llm_gate_last_seen(state_dir, symbol, last_at)"
                " VALUES (?, ?, ?)"
                " ON CONFLICT(state_dir, symbol) DO UPDATE SET last_at=excluded.last_at",
                (state_dir, symbol, last_at),
            )


def try_open_llm_gate_store(
    state_dir: str | Path,
    backend: str,
) -> LlmGateStore | None:
    """Ouvre le store sur casys.db, ou None si le backend n'est pas sqlite."""
    if str(backend).lower() != "sqlite":
        return None
    from trader.infrastructure.state_db.connection import open_state_db

    db = open_state_db(Path(state_dir) / "casys.db")
    db.apply_migrations([LLM_GATE_MIGRATION])
    return LlmGateStore(db)


__all__ = ["LlmGateStore", "try_open_llm_gate_store"]
