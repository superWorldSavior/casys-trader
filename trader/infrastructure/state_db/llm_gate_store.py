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
from sqlite3 import Cursor

from trader.domain.planning.relevance_gate import (
    FRESH_PROBE_WAKE_FINGERPRINT_KEY,
)
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


def _utc_dt(at: datetime) -> datetime:
    if at.tzinfo is None:
        return at.replace(tzinfo=timezone.utc)
    return at.astimezone(timezone.utc)


def _encoded_context(
    wake_reasons: Sequence[str],
    wake_fingerprints: Mapping[str, str] | None,
) -> tuple[str, str]:
    return (
        json.dumps(list(wake_reasons), separators=(",", ":")),
        json.dumps(
            dict(wake_fingerprints or {}),
            separators=(",", ":"),
            sort_keys=True,
        ),
    )


def _upsert_gate_row(
    cur: Cursor,
    *,
    state_dir: str,
    symbol: str,
    last_at: str,
    reasons_json: str,
    fingerprints_json: str,
) -> None:
    cur.execute(
        "INSERT INTO llm_gate_last_seen("
        "state_dir, symbol, last_at, wake_reasons_json, wake_fingerprints_json"
        ") VALUES (?, ?, ?, ?, ?)"
        " ON CONFLICT(state_dir, symbol) DO UPDATE SET"
        " last_at=excluded.last_at,"
        " wake_reasons_json=excluded.wake_reasons_json,"
        " wake_fingerprints_json=excluded.wake_fingerprints_json",
        (state_dir, symbol, last_at, reasons_json, fingerprints_json),
    )


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
        reasons_json, fingerprints_json = _encoded_context(
            wake_reasons,
            wake_fingerprints,
        )
        with self._db.transaction() as cur:
            _upsert_gate_row(
                cur,
                state_dir=state_dir,
                symbol=symbol,
                last_at=last_at,
                reasons_json=reasons_json,
                fingerprints_json=fingerprints_json,
            )

    def record_with_fresh_probe_wake(
        self,
        state_dir: str,
        symbol: str,
        at: datetime,
        *,
        now: datetime,
        candidate_wake: datetime,
        wake_reasons: Sequence[str] = (),
        wake_fingerprints: Mapping[str, str] | None = None,
    ) -> tuple[str | None, dict[str, str]]:
        """Persist the stale replay marker and its scheduler wake atomically.

        A nearer future symbol wake remains authoritative. It is only typed as
        the replay probe when the existing fingerprint already owns that exact
        timestamp; otherwise no probe marker is fabricated.

        The scheduler migration must already be applied on this shared StateDb.
        """
        now_utc = _utc_dt(now)
        candidate_utc = _utc_dt(candidate_wake)
        if candidate_utc <= now_utc:
            raise ValueError("fresh probe candidate must be in the future")

        final_fingerprints = dict(wake_fingerprints or {})
        last_at = _canon_dt(at)
        candidate_iso = candidate_utc.isoformat()
        selected_probe_wake: str | None = candidate_iso
        should_write_candidate = True

        with self._db.transaction() as cur:
            row = cur.execute(
                "SELECT when_iso FROM scheduler_symbol_wake WHERE symbol=?",
                (symbol,),
            ).fetchone()
            if row is not None:
                current = _parse_ts(row["when_iso"])
                if now_utc < current <= candidate_utc:
                    should_write_candidate = False
                    selected_probe_wake = None
                    existing_marker = final_fingerprints.get(
                        FRESH_PROBE_WAKE_FINGERPRINT_KEY
                    )
                    if existing_marker:
                        try:
                            if _parse_ts(existing_marker) == current:
                                selected_probe_wake = current.isoformat()
                        except (AttributeError, TypeError, ValueError):
                            pass

            if selected_probe_wake is None:
                final_fingerprints.pop(
                    FRESH_PROBE_WAKE_FINGERPRINT_KEY,
                    None,
                )
            else:
                final_fingerprints[
                    FRESH_PROBE_WAKE_FINGERPRINT_KEY
                ] = selected_probe_wake
            reasons_json, fingerprints_json = _encoded_context(
                wake_reasons,
                final_fingerprints,
            )
            _upsert_gate_row(
                cur,
                state_dir=state_dir,
                symbol=symbol,
                last_at=last_at,
                reasons_json=reasons_json,
                fingerprints_json=fingerprints_json,
            )
            if should_write_candidate:
                cur.execute(
                    "INSERT INTO scheduler_symbol_wake(symbol, when_iso) VALUES (?, ?)"
                    " ON CONFLICT(symbol) DO UPDATE SET when_iso=excluded.when_iso",
                    (symbol, candidate_iso),
                )
        return selected_probe_wake, final_fingerprints


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
