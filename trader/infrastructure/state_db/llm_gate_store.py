"""Persistance SQLite de l'état de revue du gate de pertinence.

Le store reçoit une StateDb déjà ouverte (casys.db partagé). Il n'ouvre pas
de connexion séparée. Les migrations v5+v8 doivent être appliquées avant
construction.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from trader.infrastructure.state_db.connection import StateDb
from trader.infrastructure.state_db.migrations import LLM_GATE_MIGRATIONS


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


@dataclass(frozen=True)
class LlmGateSnapshot:
    """Durable gate state, split into the three in-memory projections."""

    last_llm_at: dict[tuple[str, str], datetime]
    last_wake_reasons: dict[tuple[str, str], tuple[str, ...]]
    last_wake_fingerprints: dict[tuple[str, str], dict[str, str]]


def _parse_context(
    reasons_json: object,
    fingerprints_json: object,
) -> tuple[tuple[str, ...], dict[str, str]] | None:
    """Parse one atomic context; legacy/corrupt values deliberately fail open."""
    try:
        reasons = json.loads(reasons_json)  # type: ignore[arg-type]
        fingerprints = json.loads(fingerprints_json)  # type: ignore[arg-type]
    except (json.JSONDecodeError, TypeError, UnicodeError):
        return None
    if not isinstance(reasons, list) or not all(isinstance(reason, str) for reason in reasons):
        return None
    if not isinstance(fingerprints, dict) or not all(
        isinstance(reason, str) and isinstance(fingerprint, str) for reason, fingerprint in fingerprints.items()
    ):
        return None
    return tuple(reasons), dict(fingerprints)


class LlmGateStore:
    """Atomic upsert / read of ``llm_gate_last_seen``.

    Args:
        db: StateDb ouverte avec ``LLM_GATE_MIGRATIONS`` appliquées.
    """

    def __init__(self, db: StateDb) -> None:
        self._db = db

    def load_all(self) -> dict[tuple[str, str], datetime]:
        """Compatibility projection of ``load_state().last_llm_at``."""
        return self.load_state().last_llm_at

    def load_state(self) -> LlmGateSnapshot:
        """Load timestamps and trusted context; corrupt context wakes fail-open."""
        rows = self._db.query_all(
            "SELECT state_dir, symbol, last_at, wake_reasons_json, wake_fingerprints_json FROM llm_gate_last_seen"
        )
        last_llm_at: dict[tuple[str, str], datetime] = {}
        last_wake_reasons: dict[tuple[str, str], tuple[str, ...]] = {}
        last_wake_fingerprints: dict[tuple[str, str], dict[str, str]] = {}
        for row in rows:
            state_dir = row["state_dir"]
            symbol = row["symbol"]
            if not isinstance(state_dir, str) or not isinstance(symbol, str):
                continue
            try:
                last_at = _parse_ts(row["last_at"])
            except (AttributeError, TypeError, ValueError):
                continue
            key = (state_dir, symbol)
            context = _parse_context(
                row["wake_reasons_json"],
                row["wake_fingerprints_json"],
            )
            if context is None:
                continue
            reasons, fingerprints = context
            last_llm_at[key] = last_at
            last_wake_reasons[key] = reasons
            last_wake_fingerprints[key] = fingerprints
        return LlmGateSnapshot(
            last_llm_at=last_llm_at,
            last_wake_reasons=last_wake_reasons,
            last_wake_fingerprints=last_wake_fingerprints,
        )

    def record(
        self,
        state_dir: str,
        symbol: str,
        at: datetime,
        *,
        wake_reasons: Sequence[str] = (),
        wake_fingerprints: Mapping[str, str] | None = None,
    ) -> None:
        """Atomically replace timestamp, reasons and fingerprints for a review."""
        last_at = _canon_dt(at)
        reasons_json = json.dumps(list(wake_reasons), separators=(",", ":"))
        fingerprints_json = json.dumps(
            dict(wake_fingerprints or {}),
            separators=(",", ":"),
            sort_keys=True,
        )
        with self._db.transaction() as cur:
            cur.execute(
                "INSERT INTO llm_gate_last_seen("
                "state_dir, symbol, last_at, wake_reasons_json, wake_fingerprints_json"
                ") VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT(state_dir, symbol) DO UPDATE SET"
                " last_at=excluded.last_at,"
                " wake_reasons_json=excluded.wake_reasons_json,"
                " wake_fingerprints_json=excluded.wake_fingerprints_json",
                (
                    state_dir,
                    symbol,
                    last_at,
                    reasons_json,
                    fingerprints_json,
                ),
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
    db.apply_migrations(list(LLM_GATE_MIGRATIONS))
    return LlmGateStore(db)


__all__ = ["LlmGateSnapshot", "LlmGateStore", "try_open_llm_gate_store"]
