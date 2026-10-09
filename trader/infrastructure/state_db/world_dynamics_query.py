"""Bounded, read-only market episode evidence for offline dynamics experiments.

The requested window concerns anchor timestamps. Actual availability, capture,
durable recording, and completed-bar clocks must all precede the as-of cutoff.
Only the current market capability is read; prediction archives and outcome
ledgers are unrelated to this query. No source availability is reconstructed.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from contextlib import closing
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from trader.domain.world_dynamics import EpisodeEvidence
from trader.domain.world_episode import (
    MARKET_FEATURE_CONTRACT_ID,
    WorldEpisode,
    is_eligible_completed_bar,
    parse_bar_interval,
    parse_utc_timestamp,
)
from trader.infrastructure.state_db.world_prediction_storage_lock import (
    PredictionStorageBusyError,
    shared_prediction_storage_lease,
)


MAX_EPISODE_LIMIT = 20_000
_READONLY_TIMEOUT_S = 5.0
_REQUIRED_COLUMNS = frozenset(
    {
        "episode_id",
        "venue",
        "symbol",
        "bar_interval",
        "feature_contract_version",
        "sampling_policy_version",
        "as_of_bar_ts",
        "observed_at",
        "available_at",
        "training_eligible",
        "training_reason",
        "payload_json",
        "payload_sha256",
        "recorded_at",
    }
)


class WorldDynamicsReadError(RuntimeError):
    """An explicit failure, never a silently shortened training series."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _readonly_connection(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(
        f"file:{quote(str(path.resolve()), safe='/')}?mode=ro",
        uri=True,
        timeout=_READONLY_TIMEOUT_S,
    )
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
    except Exception:
        connection.close()
        raise
    return connection


class SqliteWorldDynamicsQuery:
    """Read market observations within one explicit instrument/window only."""

    def __init__(self, db_path: str | Path) -> None:
        self.path = Path(db_path)

    def read_episodes(
        self,
        *,
        venue: str,
        symbol: str,
        bar_interval: str,
        market_contract_version: str,
        sampling_policy_version: str,
        start_at: datetime | str,
        as_of: datetime | str,
        limit: int,
    ) -> tuple[EpisodeEvidence, ...]:
        """Return evidence without deduplicating slots or inventing missing bars.

        ``limit`` bounds selected candidate rows, before payload eligibility
        checks. A larger window is refused rather than partially replayed.
        First-write/duplicate slot admission belongs to the application service.
        """

        start = parse_utc_timestamp(start_at, "start_at")
        cutoff = parse_utc_timestamp(as_of, "as_of")
        if start > cutoff:
            raise ValueError("start_at must not follow as_of")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_EPISODE_LIMIT:
            raise ValueError(f"limit must be an integer in [1, {MAX_EPISODE_LIMIT}]")
        for name, value in (
            ("venue", venue),
            ("symbol", symbol),
            ("bar_interval", bar_interval),
            ("sampling_policy_version", sampling_policy_version),
        ):
            if not isinstance(value, str) or not value.strip() or value != value.strip():
                raise ValueError(f"{name} must be a non-empty trimmed string")
        if parse_bar_interval(bar_interval) is None:
            raise ValueError("bar_interval must have a supported duration")
        if market_contract_version != MARKET_FEATURE_CONTRACT_ID:
            raise ValueError(f"market_contract_version must be {MARKET_FEATURE_CONTRACT_ID}")
        if not self.path.is_file():
            raise WorldDynamicsReadError("missing_db", f"market episode ledger is missing: {self.path}")
        try:
            with (
                shared_prediction_storage_lease(self.path, create=False),
                closing(_readonly_connection(self.path)) as connection,
            ):
                connection.execute("BEGIN")
                columns = {str(row["name"]) for row in connection.execute("PRAGMA table_info(world_episodes)")}
                if not _REQUIRED_COLUMNS <= columns:
                    raise WorldDynamicsReadError("schema_unavailable", "market episode schema is unavailable")
                rows = connection.execute(
                    """
                    SELECT episode_id, venue, symbol, bar_interval,
                           feature_contract_version, sampling_policy_version,
                           as_of_bar_ts, observed_at, available_at,
                           training_eligible, training_reason,
                           payload_json, payload_sha256, recorded_at
                    FROM world_episodes
                    WHERE venue = ? AND symbol = ? AND bar_interval = ?
                      AND feature_contract_version = ? AND sampling_policy_version = ?
                      AND training_eligible = 1
                      AND as_of_bar_ts >= ? AND as_of_bar_ts <= ?
                      AND available_at IS NOT NULL AND available_at <= ?
                      AND recorded_at <= ?
                    ORDER BY as_of_bar_ts ASC, recorded_at ASC, episode_id ASC
                    LIMIT ?
                    """,
                    (
                        venue,
                        symbol,
                        bar_interval,
                        market_contract_version,
                        sampling_policy_version,
                        start.isoformat(),
                        cutoff.isoformat(),
                        cutoff.isoformat(),
                        cutoff.isoformat(),
                        limit + 1,
                    ),
                ).fetchall()
                if len(rows) > limit:
                    raise WorldDynamicsReadError(
                        "limit_exceeded", f"market episode window exceeds limit={limit}; narrow the window"
                    )
                result: list[EpisodeEvidence] = []
                for row in rows:
                    evidence = _episode_evidence(row, start=start, cutoff=cutoff)
                    if evidence is not None:
                        result.append(evidence)
                return tuple(result)
        except WorldDynamicsReadError:
            raise
        except (PredictionStorageBusyError, OSError, sqlite3.Error) as exc:
            raise WorldDynamicsReadError("unavailable", f"market episode query failed: {type(exc).__name__}") from exc


def _episode_evidence(
    row: sqlite3.Row,
    *,
    start: datetime,
    cutoff: datetime,
) -> EpisodeEvidence | None:
    try:
        payload = json.loads(row["payload_json"])
        if not isinstance(payload, Mapping):
            raise ValueError("episode payload must be an object")
        episode = WorldEpisode.from_dict(payload)
        observation = episode.observation
        indexed = (
            ("episode_id", episode.episode_id),
            ("venue", observation.venue),
            ("symbol", observation.symbol),
            ("bar_interval", observation.bar_interval),
            ("feature_contract_version", observation.feature_contract_version),
            ("sampling_policy_version", observation.sampling_policy_version),
            ("training_eligible", int(episode.training_eligible)),
            ("training_reason", episode.training_reason),
        )
        if any(row[name] != expected for name, expected in indexed):
            raise ValueError("indexed episode identity disagrees with payload")
        stored_hash = str(row["payload_sha256"])
        if stored_hash.removeprefix("sha256:") != episode.payload_hash:
            raise ValueError("episode payload hash mismatch")
        for name, expected in (
            ("as_of_bar_ts", observation.as_of_bar_ts),
            ("observed_at", observation.as_of_bar_ts),
            ("available_at", observation.available_at),
        ):
            if parse_utc_timestamp(row[name], name) != expected:
                raise ValueError("indexed episode clock disagrees with payload")
        evidence = EpisodeEvidence(episode=episode, recorded_at=row["recorded_at"])
        end = observation.completed_bar_end_at
        if not episode.training_eligible:
            raise ValueError("selected episode is not training eligible")
        if not is_eligible_completed_bar(
            ts=observation.as_of_bar_ts,
            bar_interval=observation.bar_interval,
            timestamp_semantics=observation.anchor.timestamp_semantics,
            available_at=observation.available_at,
        ):
            raise ValueError("selected episode has no canonical completed bar proof")
        if (
            end is None
            or end > cutoff
            or not start <= observation.as_of_bar_ts <= cutoff
            or evidence.effective_available_at > cutoff
        ):
            return None
        return evidence
    except (TypeError, ValueError, KeyError, json.JSONDecodeError) as exc:
        raise WorldDynamicsReadError("invalid_episode", f"invalid market episode: {row['episode_id']}") from exc


__all__ = ["MAX_EPISODE_LIMIT", "SqliteWorldDynamicsQuery", "WorldDynamicsReadError"]
