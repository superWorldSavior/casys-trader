"""Dedicated immutable SQLite ledger for shadow world-model evidence.

This store deliberately lives outside ``casys.db``.  It records market-world
episodes, future-label evidence, and shadow predictions without granting any
of them authority over trading.  Rows are append-only: an exact replay is a
no-op, while reusing a deterministic identifier with different canonical
content fails closed.
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import time
from collections.abc import Mapping, Sequence
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from trader.domain.world_availability import (
    AvailabilityEvidence,
    WorldAvailabilityReceipt,
    WorldAvailabilitySubjectRef,
    WorldStorageLocator,
    _attest_verified_store_receipt,
)
from trader.domain.world_cohort import (
    WORLD_COHORT_EVENTS,
    WorldCohort,
    WorldCohortEvent,
    WorldCohortEventEnvelope,
    WorldCohortId,
    WorldCohortManifest,
    WorldCohortRegistered,
    WorldCohortSlot,
    WorldCohortSlotAdmitted,
    parse_world_cohort_event,
    world_cohort_event_payload_hash,
)
from trader.domain.world_episode import (
    OUTCOME_STATUSES,
    PREDICTION_CLASSES,
    PREDICTION_STATUSES,
    WorldEpisode,
    WorldOutcome,
    WorldPrediction,
    canonical_json,
    canonical_sha256,
    parse_utc_timestamp,
)
from trader.infrastructure.state_db.availability_receipt import (
    UtcClock,
    _seal_world_availability_receipt,
    default_utc_clock,
)
from trader.infrastructure.state_db.connection import StateDb

_LEGACY_MOVE_CLASS = {"up": "UP", "down": "DOWN", "flat": "FLAT"}
_V2_FEATURE_CONTRACT = "market_ohlcv_context.v2"

__all__ = [
    "WORLD_MODEL_MIGRATIONS",
    "WORLD_MODEL_STORE_ID",
    "WorldModelConflictError",
    "WorldModelStore",
]


class WorldModelConflictError(ValueError):
    """A deterministic world-model identifier was reused with new content."""


WORLD_MODEL_STORE_ID = "world-model.db.v1"
_EVENT_SUBJECT_KIND = "world_cohort_event"
_PREDICTION_COHORT_FIELDS = (
    "study_cohort_id",
    "lane_id",
    "manifest_sha256",
    "feature_contract_fingerprint",
    "feature_mask_fingerprint",
)


def _append_only_trigger_sql(table: str) -> list[str]:
    return [
        f"""
            CREATE TRIGGER IF NOT EXISTS {table}_no_update
            BEFORE UPDATE ON {table}
            BEGIN
                SELECT RAISE(ABORT, '{table} are append-only');
            END
            """,
        f"""
            CREATE TRIGGER IF NOT EXISTS {table}_no_delete
            BEFORE DELETE ON {table}
            BEGIN
                SELECT RAISE(ABORT, '{table} are append-only');
            END
            """,
    ]


_V2_CANONICAL_FIRST_WRITE_TRIGGER = """
            CREATE TRIGGER IF NOT EXISTS world_episodes_v2_canonical_first_write
            BEFORE INSERT ON world_episodes
            WHEN NEW.feature_contract_version = 'market_ohlcv_context.v2'
            BEGIN
                SELECT RAISE(ABORT, 'V2 market slot already exists')
                WHERE EXISTS (
                    SELECT 1 FROM world_episodes AS existing
                    WHERE existing.symbol = NEW.symbol
                      AND existing.venue IS NEW.venue
                      AND existing.bar_interval IS NEW.bar_interval
                      AND existing.feature_contract_version = 'market_ohlcv_context.v2'
                      AND existing.sampling_policy_version IS NEW.sampling_policy_version
                      AND existing.episode_id != NEW.episode_id
                      AND existing.as_of_bar_ts IS NEW.as_of_bar_ts
                );
            END
            """

_V2_SLOT_CANDIDATE_INDEX = """
            CREATE INDEX IF NOT EXISTS idx_world_episodes_v2_market_slot_candidates
            ON world_episodes(
                venue,
                symbol,
                bar_interval,
                feature_contract_version,
                sampling_policy_version,
                recorded_at,
                episode_id
            )
            WHERE feature_contract_version = 'market_ohlcv_context.v2'
            """


def _canonical_v2_episode(payload: Mapping[str, Any], observation: Mapping[str, Any]) -> WorldEpisode | None:
    """Rebuild a V2 episode from its canonical projection, or return None for non-V2."""

    top = _text(payload.get("feature_contract_version"))
    nested = _text(observation.get("feature_contract_version"))
    has_context = observation.get("context") is not None
    is_v2 = top == _V2_FEATURE_CONTRACT or nested == _V2_FEATURE_CONTRACT or has_context
    if top and nested and top != nested:
        if is_v2:
            raise ValueError("feature_contract_version envelope contradicts nested observation")
        return None
    if not is_v2:
        return None
    return WorldEpisode.from_dict(payload)


# This migration namespace belongs only to ``world_model.db``.  It must never
# be added to the central ``trader.infrastructure.state_db.migrations`` list.
WORLD_MODEL_MIGRATIONS: list[tuple[int, list[str]]] = [
    (
        1,
        [
            """
            CREATE TABLE IF NOT EXISTS world_episodes (
                episode_id              TEXT PRIMARY KEY,
                capture_id              TEXT,
                venue                   TEXT,
                symbol                  TEXT NOT NULL,
                observed_at             TEXT NOT NULL,
                available_at            TEXT,
                as_of_bar_ts            TEXT,
                bar_interval            TEXT,
                feature_contract_version TEXT,
                sampling_policy_version TEXT,
                training_eligible       INTEGER NOT NULL CHECK (training_eligible IN (0, 1)),
                training_reason         TEXT,
                payload_json            TEXT NOT NULL,
                payload_sha256          TEXT NOT NULL,
                source_evidence_json    TEXT NOT NULL,
                source_evidence_sha256  TEXT NOT NULL,
                recorded_at             TEXT NOT NULL
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_world_episodes_eligible_observed
            ON world_episodes(training_eligible, observed_at, episode_id)
            """,
            """
            CREATE TABLE IF NOT EXISTS world_outcome_events (
                outcome_event_id            TEXT PRIMARY KEY,
                episode_id                  TEXT NOT NULL
                    REFERENCES world_episodes(episode_id),
                horizon_code                TEXT NOT NULL,
                label_schema_version        TEXT NOT NULL,
                status                      TEXT NOT NULL,
                move_class                  TEXT,
                training_eligible           INTEGER NOT NULL CHECK (training_eligible IN (0, 1)),
                label_available_at          TEXT,
                sealed_at                   TEXT,
                supersedes_outcome_event_id TEXT
                    REFERENCES world_outcome_events(outcome_event_id),
                label_json                  TEXT NOT NULL,
                evidence_json               TEXT NOT NULL,
                evidence_sha256             TEXT NOT NULL,
                payload_json                TEXT NOT NULL,
                payload_sha256              TEXT NOT NULL,
                recorded_at                 TEXT NOT NULL
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_world_outcomes_pending
            ON world_outcome_events(episode_id, horizon_code, status, outcome_event_id)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_world_outcomes_observed
            ON world_outcome_events(status, training_eligible, label_available_at, outcome_event_id)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_world_outcomes_supersedes
            ON world_outcome_events(supersedes_outcome_event_id)
            """,
            """
            CREATE TABLE IF NOT EXISTS world_shadow_predictions (
                prediction_id              TEXT PRIMARY KEY,
                run_id                     TEXT NOT NULL,
                episode_id                 TEXT NOT NULL
                    REFERENCES world_episodes(episode_id),
                horizon_code               TEXT NOT NULL,
                model_kind                 TEXT,
                model_version              TEXT,
                predicted_at               TEXT,
                input_sha256               TEXT NOT NULL,
                prediction_json            TEXT NOT NULL,
                prediction_sha256          TEXT NOT NULL,
                payload_json               TEXT NOT NULL,
                payload_sha256             TEXT NOT NULL,
                recorded_at                TEXT NOT NULL
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_world_predictions_run_episode
            ON world_shadow_predictions(run_id, horizon_code, episode_id, prediction_id)
            """,
            """
            CREATE TRIGGER IF NOT EXISTS world_episodes_no_update
            BEFORE UPDATE ON world_episodes
            BEGIN
                SELECT RAISE(ABORT, 'world_episodes are append-only');
            END
            """,
            """
            CREATE TRIGGER IF NOT EXISTS world_episodes_no_delete
            BEFORE DELETE ON world_episodes
            BEGIN
                SELECT RAISE(ABORT, 'world_episodes are append-only');
            END
            """,
            """
            CREATE TRIGGER IF NOT EXISTS world_outcomes_no_update
            BEFORE UPDATE ON world_outcome_events
            BEGIN
                SELECT RAISE(ABORT, 'world_outcome_events are append-only');
            END
            """,
            """
            CREATE TRIGGER IF NOT EXISTS world_outcomes_no_delete
            BEFORE DELETE ON world_outcome_events
            BEGIN
                SELECT RAISE(ABORT, 'world_outcome_events are append-only');
            END
            """,
            """
            CREATE TRIGGER IF NOT EXISTS world_predictions_no_update
            BEFORE UPDATE ON world_shadow_predictions
            BEGIN
                SELECT RAISE(ABORT, 'world_shadow_predictions are append-only');
            END
            """,
            """
            CREATE TRIGGER IF NOT EXISTS world_predictions_no_delete
            BEFORE DELETE ON world_shadow_predictions
            BEGIN
                SELECT RAISE(ABORT, 'world_shadow_predictions are append-only');
            END
            """,
        ],
    ),
    (
        2,
        [
            "DROP INDEX IF EXISTS idx_world_episodes_v2_market_slot",
            "DROP TRIGGER IF EXISTS world_episodes_v2_canonical_first_write",
            _V2_CANONICAL_FIRST_WRITE_TRIGGER,
            _V2_SLOT_CANDIDATE_INDEX,
        ],
    ),
    (
        3,
        [
            "DROP INDEX IF EXISTS idx_world_episodes_v2_market_slot",
            "DROP TRIGGER IF EXISTS world_episodes_v2_canonical_first_write",
            _V2_CANONICAL_FIRST_WRITE_TRIGGER,
            _V2_SLOT_CANDIDATE_INDEX,
        ],
    ),
    (
        4,
        [
            "DROP INDEX IF EXISTS idx_world_episodes_v2_market_slot",
            "DROP TRIGGER IF EXISTS world_episodes_v2_canonical_first_write",
            _V2_CANONICAL_FIRST_WRITE_TRIGGER,
            _V2_SLOT_CANDIDATE_INDEX,
        ],
    ),
    (
        5,
        [
            """
            CREATE TABLE IF NOT EXISTS world_cohort_manifests (
                cohort_id         TEXT PRIMARY KEY,
                manifest_sha256   TEXT NOT NULL,
                payload_json      TEXT NOT NULL,
                payload_sha256    TEXT NOT NULL,
                recorded_at       TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS world_cohort_events (
                event_id          TEXT PRIMARY KEY,
                cohort_id         TEXT NOT NULL
                    REFERENCES world_cohort_manifests(cohort_id),
                event_type        TEXT NOT NULL,
                sequence          INTEGER NOT NULL,
                manifest_sha256   TEXT NOT NULL,
                payload_json      TEXT NOT NULL,
                payload_sha256    TEXT NOT NULL,
                recorded_at       TEXT NOT NULL,
                UNIQUE(cohort_id, sequence)
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_world_cohort_events_cohort_sequence
            ON world_cohort_events(cohort_id, sequence, event_id)
            """,
            """
            CREATE TABLE IF NOT EXISTS world_cohort_slots (
                slot_id              TEXT PRIMARY KEY,
                cohort_id            TEXT NOT NULL
                    REFERENCES world_cohort_manifests(cohort_id),
                event_id             TEXT NOT NULL
                    REFERENCES world_cohort_events(event_id),
                manifest_sha256      TEXT NOT NULL,
                venue                TEXT NOT NULL,
                symbol               TEXT NOT NULL,
                bar_interval         TEXT NOT NULL,
                as_of_bar_ts         TEXT NOT NULL,
                anchor_end_at        TEXT NOT NULL,
                comparison_batch_id  TEXT NOT NULL,
                started_event_id     TEXT NOT NULL,
                payload_json         TEXT NOT NULL,
                payload_sha256       TEXT NOT NULL,
                recorded_at          TEXT NOT NULL
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_world_cohort_slots_cohort_anchor
            ON world_cohort_slots(cohort_id, as_of_bar_ts, slot_id)
            """,
            """
            CREATE TABLE IF NOT EXISTS world_availability_receipts (
                receipt_id             TEXT PRIMARY KEY,
                subject_kind           TEXT NOT NULL,
                subject_id             TEXT NOT NULL,
                content_sha256         TEXT NOT NULL,
                scope                  TEXT NOT NULL,
                storage_locator_json   TEXT NOT NULL,
                ready_at               TEXT NOT NULL,
                receipt_sha256         TEXT NOT NULL,
                payload_json           TEXT NOT NULL,
                recorded_at            TEXT NOT NULL,
                UNIQUE(subject_kind, subject_id, content_sha256)
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_world_availability_receipts_subject
            ON world_availability_receipts(subject_kind, subject_id, content_sha256)
            """,
            *_append_only_trigger_sql("world_cohort_manifests"),
            *_append_only_trigger_sql("world_cohort_events"),
            *_append_only_trigger_sql("world_cohort_slots"),
            *_append_only_trigger_sql("world_availability_receipts"),
            "ALTER TABLE world_shadow_predictions ADD COLUMN study_cohort_id TEXT",
            "ALTER TABLE world_shadow_predictions ADD COLUMN lane_id TEXT",
            "ALTER TABLE world_shadow_predictions ADD COLUMN manifest_sha256 TEXT",
            "ALTER TABLE world_shadow_predictions ADD COLUMN feature_contract_fingerprint TEXT",
            "ALTER TABLE world_shadow_predictions ADD COLUMN feature_mask_fingerprint TEXT",
            """
            CREATE INDEX IF NOT EXISTS idx_world_predictions_study_cohort
            ON world_shadow_predictions(study_cohort_id, lane_id, prediction_id)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_world_predictions_manifest
            ON world_shadow_predictions(manifest_sha256, prediction_id)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_world_predictions_feature_fps
            ON world_shadow_predictions(feature_contract_fingerprint, feature_mask_fingerprint, prediction_id)
            """,
        ],
    ),
]


_EPISODE_COLUMNS = (
    "episode_id",
    "capture_id",
    "venue",
    "symbol",
    "observed_at",
    "available_at",
    "as_of_bar_ts",
    "bar_interval",
    "feature_contract_version",
    "sampling_policy_version",
    "training_eligible",
    "training_reason",
    "payload_json",
    "payload_sha256",
    "source_evidence_json",
    "source_evidence_sha256",
)

_OUTCOME_COLUMNS = (
    "outcome_event_id",
    "episode_id",
    "horizon_code",
    "label_schema_version",
    "status",
    "move_class",
    "training_eligible",
    "label_available_at",
    "sealed_at",
    "supersedes_outcome_event_id",
    "label_json",
    "evidence_json",
    "evidence_sha256",
    "payload_json",
    "payload_sha256",
)

_PREDICTION_COLUMNS = (
    "prediction_id",
    "run_id",
    "episode_id",
    "horizon_code",
    "model_kind",
    "model_version",
    "predicted_at",
    "input_sha256",
    "prediction_json",
    "prediction_sha256",
    "payload_json",
    "payload_sha256",
    "study_cohort_id",
    "lane_id",
    "manifest_sha256",
    "feature_contract_fingerprint",
    "feature_mask_fingerprint",
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _aware_utc(value: datetime, *, field_name: str) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError(f"{field_name} must return a timezone-aware datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _cohort_scope(cohort_id: str) -> str:
    return f"world_cohort:{cohort_id}"


def _as_mapping(value: Any, *, name: str) -> dict[str, Any]:
    """Accept the domain object's stable payload contract or a mapping."""

    if isinstance(value, Mapping):
        return {str(key): item for key, item in value.items()}
    to_payload = getattr(value, "to_payload", None)
    if callable(to_payload):
        rendered = to_payload()
        if isinstance(rendered, Mapping):
            return {str(key): item for key, item in rendered.items()}
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        rendered = to_dict()
        if isinstance(rendered, Mapping):
            return {str(key): item for key, item in rendered.items()}
    if is_dataclass(value) and not isinstance(value, type):
        rendered = asdict(value)
        if isinstance(rendered, Mapping):
            return {str(key): item for key, item in rendered.items()}
    raise TypeError(f"{name} must be a mapping or expose to_dict()")


def _json_ready(value: Any) -> Any:
    """Strictly normalize the small set of non-JSON domain scalar types."""

    if isinstance(value, Mapping):
        return {str(key): _json_ready(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_ready(item) for item in value]
    if isinstance(value, Enum):
        return _json_ready(value.value)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError("world-model timestamps must be timezone-aware")
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("world-model payloads must not contain non-finite floats")
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if is_dataclass(value) and not isinstance(value, type):
        return _json_ready(asdict(value))
    raise TypeError(f"world-model payload contains non-serializable {type(value).__name__}")


def _canonical_json(value: Any) -> str:
    """Use the domain canonicalizer when present, with a strict local fallback."""

    try:
        from trader.domain.world_episode import canonical_json
    except (ImportError, ModuleNotFoundError):
        canonical_json = None
    if callable(canonical_json):
        rendered = canonical_json(value)
        if not isinstance(rendered, str):
            raise TypeError("domain canonical_json must return str")
        return rendered
    return json.dumps(
        _json_ready(value),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _canonical_sha256(value: Any) -> str:
    try:
        from trader.domain.world_episode import canonical_sha256
    except (ImportError, ModuleNotFoundError):
        canonical_sha256 = None
    if callable(canonical_sha256):
        rendered = str(canonical_sha256(value))
        return rendered if rendered.startswith("sha256:") else f"sha256:{rendered}"
    return "sha256:" + hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _stored_sha256(value: Any) -> str | None:
    """Normalize a caller-provided digest to the store's tagged convention."""

    rendered = _text(value)
    if rendered is None:
        return None
    return rendered if rendered.startswith("sha256:") else f"sha256:{rendered}"


def _nested(mapping: Mapping[str, Any], *path: str) -> Any:
    current: Any = mapping
    for key in path:
        if not isinstance(current, Mapping) or key not in current:
            return None
        current = current[key]
    return current


def _first(mapping: Mapping[str, Any], *paths: tuple[str, ...]) -> Any:
    for path in paths:
        value = _nested(mapping, *path)
        if value is not None:
            return value
    return None


def _text(value: Any) -> str | None:
    if isinstance(value, Enum):
        value = value.value
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _required_text(value: Any, *, field: str) -> str:
    result = _text(value)
    if result is None:
        raise ValueError(f"world-model record requires {field}")
    return result


def _canonical_move_class(value: str | None) -> str | None:
    """Read adapter: persist lowercase historical directions as DOWN/FLAT/UP."""

    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return _LEGACY_MOVE_CLASS.get(text.lower(), text.upper() if text.upper() in PREDICTION_CLASSES else text)


def _assert_live_prediction(payload: Mapping[str, Any], nested: Mapping[str, Any]) -> None:
    recommendation = nested.get("recommendation", payload.get("recommendation", "NO_GO"))
    if recommendation != "NO_GO":
        raise ValueError("recommendation must remain NO_GO for a shadow prediction")
    authority = nested.get("authority", payload.get("authority", "shadow_only"))
    if authority != "shadow_only":
        raise ValueError("authority must remain shadow_only")
    decision_effect = nested.get("decision_effect", payload.get("decision_effect", "none"))
    if decision_effect != "none":
        raise ValueError("decision_effect must remain none")
    status = str(nested.get("status") or payload.get("status") or "warming_up").strip().lower()
    if status not in PREDICTION_STATUSES:
        allowed = ", ".join(sorted(PREDICTION_STATUSES))
        raise ValueError(f"status must be one of: {allowed}")
    probabilities = nested.get("probabilities", payload.get("probabilities"))
    if isinstance(probabilities, Mapping):
        keys = set(probabilities)
        if keys and keys != set(PREDICTION_CLASSES):
            raise ValueError("probabilities must contain exactly DOWN, FLAT, and UP")
        if status == "shadow_only" and keys != set(PREDICTION_CLASSES):
            raise ValueError("shadow_only prediction requires DOWN/FLAT/UP probabilities")
    predicted_class = nested.get("predicted_class", payload.get("predicted_class"))
    if predicted_class is not None:
        rendered = str(predicted_class).strip()
        if rendered not in PREDICTION_CLASSES:
            raise ValueError("predicted_class must be one of: DOWN, FLAT, UP")


def _assert_live_outcome(payload: Mapping[str, Any], label: Mapping[str, Any]) -> None:
    status = str(payload.get("status") or label.get("status") or "observed").strip().lower()
    if status not in OUTCOME_STATUSES:
        allowed = ", ".join(sorted(OUTCOME_STATUSES))
        raise ValueError(f"status must be one of: {allowed}")
    move = _first(
        payload,
        ("move_class",),
        ("direction",),
        ("label", "move_class"),
        ("label", "direction"),
    )
    if move is not None:
        rendered = str(move).strip()
        if rendered not in PREDICTION_CLASSES:
            raise ValueError("move_class must be one of: DOWN, FLAT, UP")
    source = _first(
        payload,
        ("source_raw_sha256",),
        ("evidence", "source_raw_sha256"),
        ("label", "source_raw_sha256"),
    )
    eligible = payload.get("training_eligible")
    if eligible is True and (status != "observed" or not _text(source)):
        raise ValueError("only an observed outcome with immutable source evidence is trainable")


def _horizon_filter(
    *,
    horizon_code: str | None,
    horizon_id: str | None,
) -> str | None:
    """Normalize the public ``horizon_id``/``horizon_code`` aliases.

    The domain calls this stable identifier ``horizon_id`` while the indexed
    SQL column is deliberately named ``horizon_code``.  Accepting both at the
    read boundary keeps callers from having to know that storage detail, but
    refuses contradictory filters rather than silently broadening a query.
    """

    if horizon_code is None and horizon_id is None:
        return None
    code = _required_text(horizon_code, field="horizon_code") if horizon_code is not None else None
    identifier = _required_text(horizon_id, field="horizon_id") if horizon_id is not None else None
    if code is not None and identifier is not None and code != identifier:
        raise ValueError("horizon_code and horizon_id must match when both are supplied")
    return code or identifier


def _bool(value: Any, *, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes"}:
            return True
        if normalized in {"0", "false", "no"}:
            return False
    raise ValueError(f"expected boolean-like value, got {value!r}")


def _mapping_or_empty(value: Any, *, name: str) -> dict[str, Any]:
    if value is None:
        return {}
    return _as_mapping(value, name=name)


def _json_load(text: str) -> Any:
    return json.loads(text)


def _open_dedicated_state_db(path: str | Path) -> StateDb:
    """Open a newly-created WAL database safely when workers start together.

    ``StateDb`` enables WAL in its constructor, before its connection-level
    busy timeout is installed.  Two independent shadow workers may therefore
    race only at that first pragma.  Retrying this tiny initialization window
    here keeps concurrent evidence capture reliable without changing the
    shared StateDb behavior used by production stores.
    """

    last_error: sqlite3.OperationalError | None = None
    for attempt in range(12):
        try:
            return StateDb(path)
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower():
                raise
            last_error = exc
            # 25ms, 50ms, 75ms, ... capped at 150ms: normally a single retry,
            # and roughly 1.5 seconds maximum even for a stuck peer.
            time.sleep(min(0.025 * (attempt + 1), 0.15))
    assert last_error is not None
    raise last_error


class WorldModelStore:
    """Append-only persistence boundary for the shadow world model.

    ``db_or_path`` accepts a supplied :class:`StateDb` for a daemon-owned
    connection, or a standalone path for a dedicated CLI/test connection.
    Supplying ``casys.db`` is refused to keep world-model writes away from the
    broker and execution state.
    """

    def __init__(self, db_or_path: StateDb | str | Path, *, clock: UtcClock | None = None) -> None:
        if isinstance(db_or_path, StateDb):
            self._db = db_or_path
            self._owns_db = False
        else:
            if Path(db_or_path).name.casefold() == "casys.db":
                raise ValueError("WorldModelStore requires a dedicated world_model.db, never casys.db")
            self._db = _open_dedicated_state_db(db_or_path)
            self._owns_db = True
        self.path = self._db.path
        if self.path.name.casefold() == "casys.db":
            raise ValueError("WorldModelStore requires a dedicated world_model.db, never casys.db")
        self._clock = clock or default_utc_clock
        self._first_seen_at: dict[str, datetime] = {}
        # ``foreign_keys`` is connection-local and StateDb intentionally stays
        # generic, so set it before the world schema is used.
        self._db.query_one("PRAGMA foreign_keys=ON")
        self._db.apply_migrations(WORLD_MODEL_MIGRATIONS)
        self._prime_existing_receipts()

    def close(self) -> None:
        """Close only a connection this store created itself."""

        if self._owns_db:
            self._db.close()

    def integrity_check(self) -> list[str]:
        return self._db.integrity_check()

    def append_episode(self, episode: Any) -> bool:
        payload = _as_mapping(episode, name="episode")
        observation = _mapping_or_empty(payload.get("observation"), name="episode.observation")
        canonical_v2 = _canonical_v2_episode(payload, observation)
        if canonical_v2 is not None:
            payload = canonical_v2.to_dict()
            observation = _mapping_or_empty(payload.get("observation"), name="episode.observation")
        source_evidence = _first(
            payload,
            ("source_evidence",),
            ("evidence",),
            ("observation", "source_evidence"),
            ("observation", "evidence"),
        )
        source = _mapping_or_empty(source_evidence, name="episode.source_evidence")
        if not source and observation:
            # The domain V1 object keeps its point-in-time proof in the
            # observation itself (anchor/freshness/availability).  Duplicate
            # that narrow evidence projection for indexed provenance without
            # inventing a reconstruction from later runtime state.
            source = {
                key: observation[key]
                for key in ("anchor", "freshness", "available_at", "captured_at", "context")
                if key in observation
            }
        values = {
            "episode_id": _required_text(_first(payload, ("episode_id",), ("id",)), field="episode_id"),
            "capture_id": _text(_first(payload, ("capture_id",), ("observation", "capture_id"))),
            "venue": _text(_first(payload, ("venue",), ("observation", "venue"))),
            "symbol": _required_text(_first(payload, ("symbol",), ("observation", "symbol")), field="symbol"),
            "observed_at": _required_text(
                _first(
                    payload,
                    ("observed_at",),
                    ("observation", "observed_at"),
                    ("observation", "as_of_bar_ts"),
                ),
                field="observed_at",
            ),
            "available_at": _text(_first(payload, ("available_at",), ("observation", "available_at"))),
            "as_of_bar_ts": _text(_first(payload, ("as_of_bar_ts",), ("observation", "as_of_bar_ts"))),
            "bar_interval": _text(_first(payload, ("bar_interval",), ("observation", "bar_interval"), ("interval",))),
            "feature_contract_version": _text(
                _first(
                    payload,
                    ("feature_contract_version",),
                    ("observation", "feature_contract_version"),
                )
            ),
            "sampling_policy_version": _text(
                _first(
                    payload,
                    ("sampling_policy_version",),
                    ("observation", "sampling_policy_version"),
                )
            ),
            # Missing flags are non-trainable by default.  This prevents a
            # historical/ledger reconstruction from accidentally entering a
            # causal cohort.
            "training_eligible": int(_bool(payload.get("training_eligible"), default=False)),
            "training_reason": _text(_first(payload, ("training_reason",), ("training_eligibility_reason",))),
            "payload_json": _canonical_json(payload),
            "payload_sha256": _canonical_sha256(payload),
            "source_evidence_json": _canonical_json(source),
            "source_evidence_sha256": _canonical_sha256(source),
        }
        if values["feature_contract_version"] == _V2_FEATURE_CONTRACT:
            slot_fields = (
                values["venue"],
                values["symbol"],
                values["bar_interval"],
                values["as_of_bar_ts"],
                values["sampling_policy_version"],
            )
            if any(item is None or item == "" for item in slot_fields):
                raise ValueError("V2 episode requires a complete market slot identity")
            existing = self.get_episode_by_v2_slot(
                venue=values["venue"],
                symbol=values["symbol"],
                bar_interval=values["bar_interval"],
                as_of_bar_ts=values["as_of_bar_ts"],
                feature_contract_version=values["feature_contract_version"],
                sampling_policy_version=values["sampling_policy_version"],
            )
            if existing is not None:
                if (
                    existing["episode_id"] == values["episode_id"]
                    and existing["payload_sha256"] == values["payload_sha256"]
                ):
                    return False
                raise WorldModelConflictError("V2 market slot already exists with different canonical content")
        try:
            return self._append(
                table="world_episodes",
                id_column="episode_id",
                columns=_EPISODE_COLUMNS,
                values=values,
            )
        except sqlite3.IntegrityError as exc:
            message = str(exc)
            if values["feature_contract_version"] == _V2_FEATURE_CONTRACT and (
                "UNIQUE constraint failed" in message or "V2 market slot already exists" in message
            ):
                raise WorldModelConflictError("V2 market slot already exists with different canonical content") from exc
            raise

    def append_outcome_event(self, outcome: Any) -> bool:
        """Append a canonical live outcome.  Mapping payloads must satisfy the live contract."""

        return self._append_outcome_event(outcome, live=True)

    def append_legacy_outcome_event(self, outcome: Any) -> bool:
        """Persist a historical or fixture mapping without the live canonical contract."""

        return self._append_outcome_event(outcome, live=False)

    def append_legacy_outcome(self, outcome: Any) -> bool:
        return self.append_legacy_outcome_event(outcome)

    def _append_outcome_event(self, outcome: Any, *, live: bool) -> bool:
        payload = _as_mapping(outcome, name="outcome")
        episode_id = _required_text(payload.get("episode_id"), field="episode_id")
        episode = self._db.query_one("SELECT training_eligible FROM world_episodes WHERE episode_id=?", (episode_id,))
        if episode is None:
            raise ValueError(f"outcome references unknown episode_id {episode_id!r}")
        label_candidate = _first(payload, ("label",), ("outcome",), ("market_transition",))
        label = _mapping_or_empty(label_candidate, name="outcome.label")
        if not label:
            # ``WorldOutcome.to_dict`` is already the canonical label payload.
            # Keep only its exogenous target/proof fields in the indexed label
            # projection; the full event remains in payload_json.
            label = {
                key: payload[key]
                for key in (
                    "target_at",
                    "anchor_close",
                    "endpoint_close",
                    "simple_return",
                    "log_return",
                    "endpoint_bar_ts",
                    "source",
                    "source_raw_sha256",
                    "direction",
                    "direction_band",
                    "label_semantics_version",
                    "reason",
                )
                if key in payload
            }
        evidence_candidate = _first(payload, ("evidence",), ("source_evidence",), ("label_evidence",))
        evidence = _mapping_or_empty(evidence_candidate, name="outcome.evidence")
        if not evidence:
            evidence = {
                key: payload[key]
                for key in (
                    "anchor_bar",
                    "anchor_evidence_id",
                    "target_bar",
                    "target_evidence_id",
                    "source",
                    "source_raw_sha256",
                    "availability_provenance",
                    "endpoint_bar_ts",
                    "target_at",
                )
                if key in payload
            }
        horizon_code = _required_text(
            _first(payload, ("horizon_code",), ("horizon_id",), ("horizon", "horizon_id"), ("horizon",)),
            field="horizon_code",
        )
        supersedes_outcome_event_id = _text(
            _first(
                payload,
                ("supersedes_outcome_event_id",),
                ("supersedes_outcome_id",),
                ("supersedes_event_id",),
                ("label", "supersedes_outcome_event_id"),
                ("label", "supersedes_outcome_id"),
                ("label", "supersedes_event_id"),
            )
        )
        if supersedes_outcome_event_id is not None:
            predecessor = self._db.query_one(
                "SELECT episode_id, horizon_code FROM world_outcome_events WHERE outcome_event_id=?",
                (supersedes_outcome_event_id,),
            )
            if predecessor is None:
                raise ValueError(
                    f"superseding outcome references an unknown outcome_event_id {supersedes_outcome_event_id!r}"
                )
            if predecessor["episode_id"] != episode_id or predecessor["horizon_code"] != horizon_code:
                raise ValueError("superseding outcome must keep the same episode_id and horizon_code")
        values = {
            "outcome_event_id": _required_text(
                _first(payload, ("outcome_event_id",), ("outcome_id",), ("event_id",), ("id",)),
                field="outcome_event_id",
            ),
            "episode_id": episode_id,
            "horizon_code": horizon_code,
            "label_schema_version": _text(_first(payload, ("label_schema_version",), ("schema_version",)))
            or "world-label-v1",
            "status": _text(payload.get("status")) or "observed",
            "move_class": _text(
                _first(payload, ("move_class",), ("direction",), ("label", "move_class"), ("label", "direction"))
            ),
            "training_eligible": int(_bool(payload.get("training_eligible"), default=False)),
            "label_available_at": _text(
                _first(payload, ("label_available_at",), ("available_at",), ("label", "available_at"))
            ),
            "sealed_at": _text(_first(payload, ("sealed_at",), ("computed_at",), ("label", "sealed_at"))),
            "supersedes_outcome_event_id": supersedes_outcome_event_id,
            "label_json": _canonical_json(label),
            "evidence_json": _canonical_json(evidence),
            "evidence_sha256": _canonical_sha256(evidence),
            "payload_json": _canonical_json(payload),
            "payload_sha256": _canonical_sha256(payload),
        }
        if live and not isinstance(outcome, WorldOutcome):
            existing = self._db.query_one(
                "SELECT outcome_event_id FROM world_outcome_events WHERE outcome_event_id=?",
                (values["outcome_event_id"],),
            )
            if existing is None:
                _assert_live_outcome(payload, label)
        return self._append(
            table="world_outcome_events",
            id_column="outcome_event_id",
            columns=_OUTCOME_COLUMNS,
            values=values,
        )

    def append_outcome(self, outcome: Any) -> bool:
        """Compatibility spelling for callers that do not need event wording."""

        return self.append_outcome_event(outcome)

    def append_prediction(self, prediction: Any) -> bool:
        """Append a canonical live prediction.  Mapping payloads must satisfy the live contract."""

        return self._append_prediction(prediction, live=True)

    def append_legacy_prediction(self, prediction: Any) -> bool:
        """Persist a historical or fixture mapping without the live canonical contract."""

        return self._append_prediction(prediction, live=False)

    def _append_prediction(self, prediction: Any, *, live: bool) -> bool:
        payload = _as_mapping(prediction, name="prediction")
        episode_id = _required_text(payload.get("episode_id"), field="episode_id")
        episode = self._db.query_one("SELECT 1 FROM world_episodes WHERE episode_id=?", (episode_id,))
        if episode is None:
            raise ValueError(f"prediction references unknown episode_id {episode_id!r}")
        predicted = _mapping_or_empty(
            _first(payload, ("prediction",), ("prediction_payload",)), name="prediction.prediction"
        )
        if not predicted:
            predicted = {
                key: payload[key]
                for key in (
                    "predicted_return",
                    "predicted_class",
                    "distribution",
                    "probabilities",
                    "status",
                    "support",
                    "backoff_tier",
                    "tier",
                    "exact_support",
                    "coarse_support",
                    "global_support",
                    "horizon_id",
                    "horizon_code",
                    "feature_hash",
                    "training_cutoff",
                    "model_fingerprint",
                    "comparison_batch_id",
                    "comparison_cohort_fingerprint",
                    "recommendation",
                    "authority",
                    "decision_effect",
                    "non_authoritative",
                )
                if key in payload
            }
        model_input = _mapping_or_empty(
            _first(payload, ("input",), ("model_input",), ("features",)), name="prediction.input"
        )
        supplied_input_hash = _stored_sha256(payload.get("input_sha256"))
        values = {
            "prediction_id": _required_text(_first(payload, ("prediction_id",), ("id",)), field="prediction_id"),
            "run_id": _required_text(_first(payload, ("run_id",), ("model_run_id",), ("model_id",)), field="run_id"),
            "episode_id": episode_id,
            "horizon_code": _required_text(
                _first(
                    payload,
                    ("horizon_code",),
                    ("horizon_id",),
                    ("prediction", "horizon_code"),
                    ("prediction", "horizon_id"),
                ),
                field="horizon_code",
            ),
            "model_kind": _text(_first(payload, ("model_kind",), ("kind",), ("model_id",))),
            "model_version": _text(_first(payload, ("model_version",), ("version",))),
            "predicted_at": _text(_first(payload, ("predicted_at",), ("available_at",), ("created_at",))),
            "input_sha256": supplied_input_hash
            or _stored_sha256(payload.get("feature_hash"))
            or _canonical_sha256(model_input),
            "prediction_json": _canonical_json(predicted),
            "prediction_sha256": _canonical_sha256(predicted),
            "payload_json": _canonical_json(payload),
            "payload_sha256": _canonical_sha256(payload),
            "study_cohort_id": _text(_first(payload, ("study_cohort_id",), ("prediction", "study_cohort_id"))),
            "lane_id": _text(_first(payload, ("lane_id",), ("prediction", "lane_id"))),
            "manifest_sha256": _text(_first(payload, ("manifest_sha256",), ("prediction", "manifest_sha256"))),
            "feature_contract_fingerprint": _text(
                _first(
                    payload,
                    ("feature_contract_fingerprint",),
                    ("prediction", "feature_contract_fingerprint"),
                )
            ),
            "feature_mask_fingerprint": _text(
                _first(
                    payload,
                    ("feature_mask_fingerprint",),
                    ("prediction", "feature_mask_fingerprint"),
                )
            ),
        }
        if live and not isinstance(prediction, WorldPrediction):
            existing = self._db.query_one(
                "SELECT prediction_id FROM world_shadow_predictions WHERE prediction_id=?",
                (values["prediction_id"],),
            )
            if existing is None:
                _assert_live_prediction(payload, predicted)
        return self._append(
            table="world_shadow_predictions",
            id_column="prediction_id",
            columns=_PREDICTION_COLUMNS,
            values=values,
        )

    def get_episode(self, episode_id: str) -> dict[str, Any] | None:
        row = self._db.query_one("SELECT * FROM world_episodes WHERE episode_id=?", (episode_id,))
        return None if row is None else self._episode_row(row)

    def get_episode_by_v2_slot(
        self,
        *,
        venue: str,
        symbol: str,
        bar_interval: str,
        as_of_bar_ts: str,
        feature_contract_version: str,
        sampling_policy_version: str,
    ) -> dict[str, Any] | None:
        """Return the first canonical V2 episode for one market slot, if any."""

        if feature_contract_version != _V2_FEATURE_CONTRACT:
            return None
        incoming = parse_utc_timestamp(as_of_bar_ts, "as_of_bar_ts")
        rows = self._db.query_all(
            """
            SELECT * FROM world_episodes
            WHERE venue IS ? AND symbol=? AND bar_interval IS ?
              AND feature_contract_version=? AND sampling_policy_version IS ?
            ORDER BY recorded_at ASC, episode_id ASC
            """,
            (
                venue,
                symbol,
                bar_interval,
                feature_contract_version,
                sampling_policy_version,
            ),
        )
        for row in rows:
            raw = row["as_of_bar_ts"]
            if raw in (None, ""):
                continue
            try:
                existing = parse_utc_timestamp(raw, "as_of_bar_ts")
            except (TypeError, ValueError):
                continue
            if existing == incoming:
                return self._episode_row(row)
        return None

    def list_eligible_episodes(self, *, limit: int | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM world_episodes WHERE training_eligible=1 ORDER BY observed_at, episode_id"
        rows = self._db.query_all(*self._with_limit(sql, (), limit))
        return [self._episode_row(row) for row in rows]

    def list_pending_episodes(
        self,
        *,
        horizon_code: str | None = None,
        horizon_id: str | None = None,
        limit: int | None = None,
        training_eligible: bool | None = None,
    ) -> list[dict[str, Any]]:
        conditions: list[str] = []
        params: list[Any] = []
        horizon = _horizon_filter(horizon_code=horizon_code, horizon_id=horizon_id)
        if training_eligible is not None:
            conditions.append("e.training_eligible=?")
            params.append(int(training_eligible))
        if horizon is None:
            conditions.append(
                "NOT EXISTS (SELECT 1 FROM world_outcome_events o "
                "WHERE o.episode_id=e.episode_id "
                "AND o.status IN ('observed', 'missing', 'unknown') "
                "AND NOT EXISTS (SELECT 1 FROM world_outcome_events successor "
                "WHERE successor.supersedes_outcome_event_id=o.outcome_event_id))"
            )
        else:
            conditions.append(
                "NOT EXISTS (SELECT 1 FROM world_outcome_events o "
                "WHERE o.episode_id=e.episode_id AND o.horizon_code=? "
                "AND o.status IN ('observed', 'missing', 'unknown') "
                "AND NOT EXISTS (SELECT 1 FROM world_outcome_events successor "
                "WHERE successor.supersedes_outcome_event_id=o.outcome_event_id))"
            )
            params.append(horizon)
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        sql = f"SELECT e.* FROM world_episodes e{where} ORDER BY e.observed_at, e.episode_id"
        rows = self._db.query_all(*self._with_limit(sql, tuple(params), limit))
        return [self._episode_row(row) for row in rows]

    def list_outcome_events(
        self,
        *,
        episode_id: str | None = None,
        horizon_code: str | None = None,
        horizon_id: str | None = None,
        status: str | None = None,
        training_eligible: bool | None = None,
        active_only: bool = False,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        conditions: list[str] = []
        params: list[Any] = []
        horizon = _horizon_filter(horizon_code=horizon_code, horizon_id=horizon_id)
        if episode_id is not None:
            conditions.append("o.episode_id=?")
            params.append(episode_id)
        if horizon is not None:
            conditions.append("o.horizon_code=?")
            params.append(horizon)
        if status is not None:
            conditions.append("o.status=?")
            params.append(_required_text(status, field="status"))
        if training_eligible is not None:
            conditions.append("o.training_eligible=?")
            params.append(int(training_eligible))
        if active_only:
            conditions.append(
                "NOT EXISTS (SELECT 1 FROM world_outcome_events successor "
                "WHERE successor.supersedes_outcome_event_id=o.outcome_event_id)"
            )
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        sql = (
            "SELECT o.*, e.observed_at AS episode_observed_at, "
            "e.training_eligible AS episode_training_eligible, "
            "e.venue AS episode_venue, e.symbol AS episode_symbol, "
            "e.bar_interval AS episode_bar_interval, e.as_of_bar_ts AS episode_as_of_bar_ts "
            "FROM world_outcome_events o JOIN world_episodes e ON e.episode_id=o.episode_id"
            f"{where} ORDER BY e.observed_at, o.horizon_code, "
            "COALESCE(o.label_available_at, o.sealed_at, ''), o.outcome_event_id"
        )
        rows = self._db.query_all(*self._with_limit(sql, tuple(params), limit))
        return [self._outcome_row(row) for row in rows]

    def list_observed_outcomes(
        self,
        *,
        episode_id: str | None = None,
        horizon_code: str | None = None,
        horizon_id: str | None = None,
        training_eligible: bool | None = None,
        active_only: bool = True,
        include_superseded: bool = False,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """List causal observed labels, using current revision leaves by default.

        A correction is immutable evidence too, but a baseline must not learn
        both the superseded label and its replacement.  Audit callers can ask
        for the complete revision history with ``include_superseded=True``.
        """

        return self.list_outcome_events(
            episode_id=episode_id,
            horizon_code=horizon_code,
            horizon_id=horizon_id,
            status="observed",
            training_eligible=training_eligible,
            active_only=False if include_superseded else active_only,
            limit=limit,
        )

    def list_predictions(
        self,
        *,
        run_id: str | None = None,
        episode_id: str | None = None,
        horizon_code: str | None = None,
        horizon_id: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        conditions: list[str] = []
        params: list[Any] = []
        horizon = _horizon_filter(horizon_code=horizon_code, horizon_id=horizon_id)
        if run_id is not None:
            conditions.append("p.run_id=?")
            params.append(run_id)
        if episode_id is not None:
            conditions.append("p.episode_id=?")
            params.append(episode_id)
        if horizon is not None:
            conditions.append("p.horizon_code=?")
            params.append(horizon)
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        sql = (
            "SELECT p.*, e.observed_at AS episode_observed_at "
            "FROM world_shadow_predictions p JOIN world_episodes e ON e.episode_id=p.episode_id"
            f"{where} ORDER BY p.run_id, e.observed_at, p.episode_id, p.prediction_id"
        )
        rows = self._db.query_all(*self._with_limit(sql, tuple(params), limit))
        return [self._prediction_row(row) for row in rows]

    def counts(self) -> dict[str, int]:
        return {
            "episodes": int(self._db.query_one("SELECT COUNT(*) FROM world_episodes")[0]),
            "outcome_events": int(self._db.query_one("SELECT COUNT(*) FROM world_outcome_events")[0]),
            "predictions": int(self._db.query_one("SELECT COUNT(*) FROM world_shadow_predictions")[0]),
        }

    def register(self, manifest: WorldCohortManifest, event: WorldCohortRegistered) -> WorldCohortEventEnvelope:
        if not isinstance(manifest, WorldCohortManifest):
            raise TypeError("manifest must be WorldCohortManifest")
        if not isinstance(event, WorldCohortRegistered):
            raise TypeError("event must be WorldCohortRegistered")
        if event.cohort_id != manifest.cohort_id or event.manifest_sha256 != manifest.manifest_sha256:
            raise ValueError("registered event does not match the durable manifest")
        sequence = self._persist_cohort_subject(manifest, event)
        receipt = self._commit_availability_receipt(event)
        return self._bind_envelope(event, sequence=sequence, receipt=receipt)

    def append_event(self, event: WorldCohortEvent, *, expected_sequence: int) -> WorldCohortEventEnvelope:
        if not isinstance(expected_sequence, int) or isinstance(expected_sequence, bool) or expected_sequence < 0:
            raise ValueError("expected_sequence must be a non-negative int")
        parsed = self._require_cohort_event(event)
        sequence = self._persist_cohort_event(parsed, expected_sequence=expected_sequence)
        receipt = self._commit_availability_receipt(parsed)
        return self._bind_envelope(parsed, sequence=sequence, receipt=receipt)

    def load(self, cohort_id: WorldCohortId) -> WorldCohort:
        if not isinstance(cohort_id, WorldCohortId):
            raise TypeError("cohort_id must be WorldCohortId")
        manifest = self._load_manifest(cohort_id.value)
        if manifest is None:
            raise LookupError(cohort_id.value)
        events = self._load_events(cohort_id.value)
        if not events:
            raise ValueError("tamper: manifest without registered event")
        return WorldCohort.reconstruct(manifest, events)

    def list_slots(self, cohort_id: WorldCohortId) -> tuple[WorldCohortSlot, ...]:
        return self.load(cohort_id).admitted_slots

    def envelope_for(self, event: WorldCohortEvent) -> WorldCohortEventEnvelope:
        parsed = self._require_cohort_event(event)
        row = self._event_row(parsed.event_id)
        if row is None:
            raise LookupError(parsed.event_id)
        stored = self._rehydrate_event_row(row)
        incoming_hash = world_cohort_event_payload_hash(parsed)
        if incoming_hash != row["payload_sha256"]:
            raise WorldModelConflictError(
                f"event_id {parsed.event_id!r} already exists with different canonical content"
            )
        return self._bind_envelope(stored, sequence=int(row["sequence"]), receipt=self._lookup_receipt(stored))

    def _require_cohort_event(self, event: WorldCohortEvent | Mapping[str, Any]) -> WorldCohortEvent:
        if isinstance(event, WORLD_COHORT_EVENTS):
            return event
        if isinstance(event, Mapping):
            return parse_world_cohort_event(event)
        raise TypeError("event must be a world cohort event")

    def _persist_cohort_subject(self, manifest: WorldCohortManifest, event: WorldCohortRegistered) -> int:
        payload_json = canonical_json(event.to_dict())
        payload_sha256 = world_cohort_event_payload_hash(event)
        manifest_json = canonical_json(manifest.to_dict())
        manifest_payload_sha256 = canonical_sha256(manifest.to_dict())
        recorded_at = _utc_now()
        with self._db.transaction() as cur:
            existing = cur.execute(
                "SELECT manifest_sha256, payload_sha256 FROM world_cohort_manifests WHERE cohort_id=?",
                (manifest.cohort_id,),
            ).fetchone()
            if existing is not None:
                if existing["manifest_sha256"] != manifest.manifest_sha256:
                    raise WorldModelConflictError("cohort_id reused with a different hash")
                event_row = cur.execute(
                    "SELECT event_id, payload_sha256, sequence FROM world_cohort_events WHERE event_id=?",
                    (event.event_id,),
                ).fetchone()
                if event_row is None:
                    raise ValueError("tamper: manifest without registered event")
                if event_row["payload_sha256"] != payload_sha256:
                    raise WorldModelConflictError(
                        f"event_id {event.event_id!r} already exists with different canonical content"
                    )
                return int(event_row["sequence"])
            cur.execute(
                """
                INSERT INTO world_cohort_manifests(
                    cohort_id, manifest_sha256, payload_json, payload_sha256, recorded_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    manifest.cohort_id,
                    manifest.manifest_sha256,
                    manifest_json,
                    manifest_payload_sha256,
                    recorded_at,
                ),
            )
            cur.execute(
                """
                INSERT INTO world_cohort_events(
                    event_id, cohort_id, event_type, sequence, manifest_sha256,
                    payload_json, payload_sha256, recorded_at
                ) VALUES (?, ?, ?, 1, ?, ?, ?, ?)
                """,
                (
                    event.event_id,
                    event.cohort_id,
                    event.event_type,
                    event.manifest_sha256,
                    payload_json,
                    payload_sha256,
                    recorded_at,
                ),
            )
            return 1

    def _persist_cohort_event(self, event: WorldCohortEvent, *, expected_sequence: int) -> int:
        payload_json = canonical_json(event.to_dict())
        payload_sha256 = world_cohort_event_payload_hash(event)
        recorded_at = _utc_now()
        try:
            with self._db.transaction() as cur:
                existing = cur.execute(
                    "SELECT payload_sha256, sequence FROM world_cohort_events WHERE event_id=?",
                    (event.event_id,),
                ).fetchone()
                if existing is not None:
                    if existing["payload_sha256"] != payload_sha256:
                        raise WorldModelConflictError(
                            f"event_id {event.event_id!r} already exists with different canonical content"
                        )
                    return int(existing["sequence"])
                manifest = cur.execute(
                    "SELECT manifest_sha256 FROM world_cohort_manifests WHERE cohort_id=?",
                    (event.cohort_id,),
                ).fetchone()
                if manifest is None:
                    raise LookupError(event.cohort_id)
                if manifest["manifest_sha256"] != event.manifest_sha256:
                    raise WorldModelConflictError("event manifest hash does not match the durable manifest")
                count_row = cur.execute(
                    "SELECT COUNT(*) FROM world_cohort_events WHERE cohort_id=?",
                    (event.cohort_id,),
                ).fetchone()
                persisted_count = int(count_row[0])
                # CAS on persisted count, not MAX(sequence)+1: a stale snapshot must not append.
                if persisted_count != expected_sequence:
                    raise WorldModelConflictError("conflicting sequence")
                sequence = expected_sequence + 1
                cur.execute(
                    """
                    INSERT INTO world_cohort_events(
                        event_id, cohort_id, event_type, sequence, manifest_sha256,
                        payload_json, payload_sha256, recorded_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event.event_id,
                        event.cohort_id,
                        event.event_type,
                        sequence,
                        event.manifest_sha256,
                        payload_json,
                        payload_sha256,
                        recorded_at,
                    ),
                )
                if isinstance(event, WorldCohortSlotAdmitted):
                    self._insert_slot_projection(cur, event, recorded_at=recorded_at)
                return sequence
        except sqlite3.IntegrityError:
            existing = self._event_row(event.event_id)
            if existing is not None:
                if existing["payload_sha256"] != payload_sha256:
                    raise WorldModelConflictError(
                        f"event_id {event.event_id!r} already exists with different canonical content"
                    ) from None
                return int(existing["sequence"])
            raise WorldModelConflictError("conflicting sequence") from None

    def _insert_slot_projection(
        self,
        cur: sqlite3.Cursor,
        event: WorldCohortSlotAdmitted,
        *,
        recorded_at: str,
    ) -> None:
        slot = event.slot
        slot_payload = slot.to_dict()
        payload_json = canonical_json(slot_payload)
        payload_sha256 = canonical_sha256(slot_payload)
        existing = cur.execute(
            "SELECT payload_sha256 FROM world_cohort_slots WHERE slot_id=?",
            (slot.slot_id,),
        ).fetchone()
        if existing is not None:
            if existing["payload_sha256"] != payload_sha256:
                raise WorldModelConflictError(
                    f"slot_id {slot.slot_id!r} already exists with different canonical content"
                )
            return
        cur.execute(
            """
            INSERT INTO world_cohort_slots(
                slot_id, cohort_id, event_id, manifest_sha256, venue, symbol, bar_interval,
                as_of_bar_ts, anchor_end_at, comparison_batch_id, started_event_id,
                payload_json, payload_sha256, recorded_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                slot.slot_id,
                slot.cohort_id,
                event.event_id,
                slot.manifest_sha256,
                slot.venue,
                slot.symbol,
                slot.bar_interval,
                slot_payload["as_of_bar_ts"],
                slot_payload["anchor_end_at"],
                slot.comparison_batch_id,
                slot.started_event_id,
                payload_json,
                payload_sha256,
                recorded_at,
            ),
        )

    def _commit_availability_receipt(self, event: WorldCohortEvent) -> WorldAvailabilityReceipt:
        existing = self._lookup_receipt(event)
        if existing is not None:
            return existing
        subject = WorldAvailabilitySubjectRef(
            kind=_EVENT_SUBJECT_KIND,
            subject_id=event.event_id,
            content_sha256=world_cohort_event_payload_hash(event),
        )
        locator = WorldStorageLocator(
            kind="sqlite",
            store_id=WORLD_MODEL_STORE_ID,
            table="world_cohort_events",
            row_id=event.event_id,
        )
        ready_at = _aware_utc(self._clock(), field_name="clock")
        receipt = _seal_world_availability_receipt(
            subject,
            scope=_cohort_scope(event.cohort_id),
            storage_locator=locator,
            ready_at=ready_at,
        )
        payload = receipt.to_dict()
        recorded_at = _utc_now()
        try:
            with self._db.transaction() as cur:
                cur.execute(
                    """
                    INSERT INTO world_availability_receipts(
                        receipt_id, subject_kind, subject_id, content_sha256, scope,
                        storage_locator_json, ready_at, receipt_sha256, payload_json, recorded_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        receipt.receipt_id,
                        subject.kind,
                        subject.subject_id,
                        subject.content_sha256,
                        receipt.scope,
                        canonical_json(locator.to_dict()),
                        payload["ready_at"],
                        receipt.receipt_sha256,
                        canonical_json(payload),
                        recorded_at,
                    ),
                )
        except sqlite3.IntegrityError:
            recovered = self._lookup_receipt(event)
            if recovered is None:
                raise WorldModelConflictError("conflicting availability receipt for the same subject") from None
            return recovered
        written = self._lookup_receipt(event)
        if written is None:
            raise ValueError("availability receipt readback failed")
        self._remember_receipt(written, seen_at=ready_at)
        return written

    def _bind_envelope(
        self,
        event: WorldCohortEvent,
        *,
        sequence: int,
        receipt: WorldAvailabilityReceipt | None,
    ) -> WorldCohortEventEnvelope:
        evidence = None
        if receipt is not None:
            first_seen = self._remember_receipt(receipt)
            evidence = AvailabilityEvidence(receipt=receipt, first_seen_at=first_seen)
        return WorldCohortEventEnvelope.bind(event, sequence=sequence, evidence=evidence)

    def _load_manifest(self, cohort_id: str) -> WorldCohortManifest | None:
        row = self._db.query_one(
            "SELECT payload_json, payload_sha256, manifest_sha256 FROM world_cohort_manifests WHERE cohort_id=?",
            (cohort_id,),
        )
        if row is None:
            return None
        try:
            payload = _json_load(row["payload_json"])
            manifest = WorldCohortManifest.from_mapping(payload)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("tamper: existing manifest cannot be rehydrated") from exc
        if canonical_sha256(manifest.to_dict()) != row["payload_sha256"]:
            raise ValueError("tamper: cohort manifest payload hash mismatch")
        if manifest.manifest_sha256 != row["manifest_sha256"]:
            raise ValueError("tamper: cohort manifest hash mismatch")
        return manifest

    def _load_events(self, cohort_id: str) -> tuple[WorldCohortEvent, ...]:
        rows = self._db.query_all(
            "SELECT * FROM world_cohort_events WHERE cohort_id=? ORDER BY sequence ASC, event_id ASC",
            (cohort_id,),
        )
        return tuple(self._rehydrate_event_row(row) for row in rows)

    def _event_row(self, event_id: str) -> Any:
        return self._db.query_one("SELECT * FROM world_cohort_events WHERE event_id=?", (event_id,))

    def _rehydrate_event_row(self, row: Any) -> WorldCohortEvent:
        try:
            payload = _json_load(row["payload_json"])
            event = parse_world_cohort_event(payload)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("tamper: existing event cannot be rehydrated") from exc
        digest = world_cohort_event_payload_hash(event)
        if digest != row["payload_sha256"]:
            raise ValueError("tamper: cohort event payload hash mismatch")
        if event.event_id != row["event_id"]:
            raise ValueError("tamper: cohort event id mismatch")
        return event

    def _lookup_receipt(self, event: WorldCohortEvent) -> WorldAvailabilityReceipt | None:
        digest = world_cohort_event_payload_hash(event)
        row = self._db.query_one(
            """
            SELECT * FROM world_availability_receipts
            WHERE subject_kind=? AND subject_id=? AND content_sha256=?
            """,
            (_EVENT_SUBJECT_KIND, event.event_id, digest),
        )
        if row is None:
            return None
        return self._parse_receipt_row(row)

    def _parse_receipt_row(self, row: Any) -> WorldAvailabilityReceipt | None:
        try:
            payload = _json_load(row["payload_json"])
            receipt = WorldAvailabilityReceipt.from_mapping(payload)
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        if (
            receipt.receipt_id != row["receipt_id"]
            or receipt.receipt_sha256 != row["receipt_sha256"]
            or receipt.subject.kind != row["subject_kind"]
            or receipt.subject.subject_id != row["subject_id"]
            or receipt.subject.content_sha256 != row["content_sha256"]
        ):
            return None
        locator = receipt.storage_locator
        if (
            locator.kind != "sqlite"
            or locator.store_id != WORLD_MODEL_STORE_ID
            or locator.table != "world_cohort_events"
        ):
            return None
        try:
            return _attest_verified_store_receipt(
                receipt,
                expected_subject=receipt.subject,
                expected_scope=receipt.scope,
                expected_locator=locator,
            )
        except (TypeError, ValueError):
            return None

    def _prime_existing_receipts(self) -> None:
        rows = self._db.query_all("SELECT * FROM world_availability_receipts")
        for row in rows:
            parsed = self._parse_receipt_row(row)
            if parsed is None:
                continue
            self._remember_receipt(parsed)

    def _remember_receipt(
        self,
        receipt: WorldAvailabilityReceipt,
        *,
        seen_at: datetime | None = None,
    ) -> datetime:
        digest = receipt.receipt_sha256
        if not digest:
            raise ValueError("availability receipt is missing receipt_sha256")
        existing = self._first_seen_at.get(digest)
        if existing is not None:
            return existing
        stamped = _aware_utc(seen_at if seen_at is not None else self._clock(), field_name="clock")
        self._first_seen_at[digest] = stamped
        return stamped

    def _append(
        self,
        *,
        table: str,
        id_column: str,
        columns: Sequence[str],
        values: Mapping[str, Any],
    ) -> bool:
        """Insert one immutable row or verify that a replay is exact."""

        column_names = ", ".join(columns)
        placeholders = ", ".join("?" for _ in columns)
        stored_values = tuple(values[column] for column in columns)
        with self._db.transaction() as cur:
            cur.execute(
                f"INSERT INTO {table}({column_names}, recorded_at) "  # noqa: S608 -- constants only
                f"VALUES ({placeholders}, ?) ON CONFLICT({id_column}) DO NOTHING",
                (*stored_values, _utc_now()),
            )
            if cur.rowcount == 1:
                return True
            row = cur.execute(
                f"SELECT {column_names} FROM {table} WHERE {id_column}=?",  # noqa: S608 -- constants only
                (values[id_column],),
            ).fetchone()
            actual = None if row is None else tuple(row[column] for column in columns)
            if actual != stored_values:
                raise WorldModelConflictError(
                    f"{id_column} {values[id_column]!r} already exists with different canonical content"
                )
            return False

    @staticmethod
    def _with_limit(
        sql: str,
        params: tuple[Any, ...],
        limit: int | None,
    ) -> tuple[str, tuple[Any, ...]]:
        if limit is None:
            return sql, params
        normalized = int(limit)
        if normalized < 0:
            raise ValueError("limit must be >= 0")
        return f"{sql} LIMIT ?", (*params, normalized)

    @staticmethod
    def _episode_row(row: Any) -> dict[str, Any]:
        result = dict(row)
        result["training_eligible"] = bool(result["training_eligible"])
        episode = _json_load(result["payload_json"])
        result["episode"] = episode
        # Return the original domain projection at the top level as well as
        # under ``episode``.  This keeps generic runtime/labeler callers from
        # needing to learn the SQLite envelope while DB scalar columns retain
        # their query-friendly authority.
        if isinstance(episode, Mapping):
            for key, value in episode.items():
                result.setdefault(key, value)
        result["source_evidence"] = _json_load(result["source_evidence_json"])
        return result

    @staticmethod
    def _outcome_row(row: Any) -> dict[str, Any]:
        result = dict(row)
        result["training_eligible"] = bool(result["training_eligible"])
        if "episode_training_eligible" in result:
            result["episode_training_eligible"] = bool(result["episode_training_eligible"])
        result["outcome"] = _json_load(result["payload_json"])
        result["label"] = _json_load(result["label_json"])
        result["evidence"] = _json_load(result["evidence_json"])
        result["move_class"] = _canonical_move_class(result.get("move_class"))
        label = result["label"]
        if isinstance(label, dict):
            if "move_class" in label:
                label["move_class"] = _canonical_move_class(label.get("move_class"))
            if "direction" in label:
                label["direction"] = _canonical_move_class(label.get("direction"))
        return result

    @staticmethod
    def _prediction_row(row: Any) -> dict[str, Any]:
        result = dict(row)
        result["prediction"] = _json_load(result["prediction_json"])
        result["prediction_record"] = _json_load(result["payload_json"])
        record = result["prediction_record"]
        nested = result["prediction"]
        for field in _PREDICTION_COHORT_FIELDS:
            column_value = result.get(field)
            json_value = record.get(field) if isinstance(record, Mapping) else None
            if column_value not in (None, ""):
                result[field] = column_value
                if isinstance(record, dict):
                    record[field] = column_value
                if isinstance(nested, dict):
                    nested[field] = column_value
            elif json_value not in (None, ""):
                result[field] = json_value
        return result
