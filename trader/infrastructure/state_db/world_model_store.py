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
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
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
    CohortPhase,
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
    CURRENT_FEATURE_CONTRACT_IDS,
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
from trader.domain.world_graph import WorldEntityRef, WorldGraphSnapshot
from trader.domain.world_scope import WorldScopeMapping
from trader.domain.world_scope_lifecycle import WorldScopeMappingGeneration
from trader.infrastructure.state_db.availability_receipt import (
    UtcClock,
    _seal_world_availability_receipt,
    default_utc_clock,
)
from trader.infrastructure.state_db.connection import StateDb
from trader.infrastructure.state_db.world_prediction_storage_lock import (
    PredictionStorageLease,
    shared_prediction_storage_lease,
)
from trader.infrastructure.state_db.world_prediction_tiers import (
    cold_prediction_for_replay,
    ensure_cold_schema,
    prediction_count,
    read_prediction_identities,
    read_prediction_rows,
)

_MARKET_FEATURE_CONTRACT = "world_feature.market.v1"
_CONTEXT_FEATURE_CONTRACT = "world_feature.context.v1"
_GRAPH_FEATURE_CONTRACT = "world_feature.graph.v1"
_CURRENT_FEATURE_CONTRACTS = frozenset(CURRENT_FEATURE_CONTRACT_IDS)

__all__ = [
    "WORLD_MODEL_MIGRATIONS",
    "WORLD_MODEL_REQUIRED_TABLES",
    "WORLD_MODEL_SCHEMA_VERSION",
    "WORLD_MODEL_STORE_ID",
    "WORLD_PATTERN_LIFECYCLE_EVENTS_DDL",
    "WORLD_PREDICTION_IDENTITY_INDEX_DDL",
    "WORLD_PREDICTION_RECORDED_AT_INDEX_DDL",
    "WORLD_SCOPE_MAPPING_GENERATIONS_DDL",
    "WorldModelConflictError",
    "WorldModelSchemaMismatchError",
    "WorldModelStore",
    "apply_current_world_model_schema",
    "ensure_world_prediction_identity_index",
    "ensure_world_prediction_recorded_at_index",
    "ensure_world_pattern_lifecycle_events_schema",
    "ensure_world_scope_mapping_generations_schema",
]


class WorldModelConflictError(ValueError):
    """A deterministic world-model identifier was reused with new content."""


class WorldModelSchemaMismatchError(ValueError):
    """Opened a world-model database whose schema is not the current definition."""


WORLD_MODEL_STORE_ID = "world-model.db.v1"
_EVENT_SUBJECT_KIND = "world_cohort_event"
_PREDICTION_COHORT_FIELDS = (
    "study_cohort_id",
    "lane_id",
    "manifest_sha256",
    "feature_contract_fingerprint",
    "feature_mask_fingerprint",
)


def _assert_current_world_model_schema(db: StateDb) -> None:
    rows = db.query_all("SELECT version FROM schema_migrations ORDER BY version")
    versions = [int(row["version"]) for row in rows]
    expected = [version for version, _statements in WORLD_MODEL_MIGRATIONS]
    if versions != expected:
        raise WorldModelSchemaMismatchError(
            f"world-model schema versions {versions!r} are not the current {expected!r}"
        )
    tables = {
        str(row["name"])
        for row in db.query_all("SELECT name FROM sqlite_master WHERE type='table'")
        if row["name"] not in {"schema_migrations", "sqlite_sequence"}
    }
    missing = WORLD_MODEL_REQUIRED_TABLES - tables
    if missing:
        raise WorldModelSchemaMismatchError(
            "world-model schema is missing required tables: " + ", ".join(sorted(missing))
        )


def apply_current_world_model_schema(db: StateDb) -> None:
    db.apply_migrations(WORLD_MODEL_MIGRATIONS)
    ensure_world_pattern_lifecycle_events_schema(db)
    ensure_world_prediction_identity_index(db)
    ensure_world_prediction_recorded_at_index(db)
    ensure_world_scope_mapping_generations_schema(db)
    with db.transaction() as cur:
        ensure_cold_schema(cur)
    _assert_current_world_model_schema(db)


def _graph_snapshot_root_adapter_columns(root: WorldEntityRef | None) -> tuple[str, str]:
    """Project optional domain root onto TEXT NOT NULL index columns.

    Empty strings are the non-destructive stand-in for SQL NULL. They are not a
    WorldEntityRef; rehydration always reads payload_json.
    """

    if root is None:
        return "", ""
    return root.kind, root.entity_id


WORLD_MODEL_SCHEMA_VERSION = 1
WORLD_MODEL_REQUIRED_TABLES = frozenset(
    {
        "world_availability_receipts",
        "world_cohort_events",
        "world_cohort_manifests",
        "world_cohort_slots",
        "world_entity_events",
        "world_entity_identity_events",
        "world_episodes",
        "world_graph_snapshot_members",
        "world_graph_snapshots",
        "world_macro_graph_bridge_events",
        "world_ontology_revisions",
        "world_outcome_events",
        "world_pattern_hypothesis_events",
        "world_pattern_lifecycle_events",
        "world_pattern_occurrence_events",
        "world_pattern_outcome_links",
        "world_relation_events",
        "world_scope_mapping_generations",
        "world_shadow_predictions",
    }
)

WORLD_PATTERN_LIFECYCLE_EVENTS_DDL = (
    """
CREATE TABLE IF NOT EXISTS world_pattern_lifecycle_events (
                event_id                         TEXT PRIMARY KEY,
                event_type                       TEXT NOT NULL,
                evaluation_cohort_id             TEXT NOT NULL,
                started_event_id                 TEXT NOT NULL,
                manifest_sha256                  TEXT NOT NULL,
                formation_cutoff                 TEXT NOT NULL,
                evaluation_start_not_before      TEXT NOT NULL,
                formation_dataset_fingerprint    TEXT NOT NULL,
                evaluation_dataset_fingerprint   TEXT NOT NULL,
                selected_count                   INTEGER NOT NULL,
                payload_json                     TEXT NOT NULL,
                payload_sha256                   TEXT NOT NULL,
                recorded_at                      TEXT NOT NULL,
                UNIQUE(evaluation_cohort_id, started_event_id)
            )
            """,
    """
CREATE INDEX IF NOT EXISTS idx_world_pattern_lifecycle_events_cohort_start
            ON world_pattern_lifecycle_events(evaluation_cohort_id, started_event_id, event_id)
            """,
    """
CREATE INDEX IF NOT EXISTS idx_world_pattern_lifecycle_events_evaluation_ready
            ON world_pattern_lifecycle_events(evaluation_start_not_before, evaluation_cohort_id, event_id)
            """,
    """
CREATE TRIGGER IF NOT EXISTS world_pattern_lifecycle_events_no_delete
            BEFORE DELETE ON world_pattern_lifecycle_events
            BEGIN
                SELECT RAISE(ABORT, 'world_pattern_lifecycle_events are append-only');
            END
            """,
    """
CREATE TRIGGER IF NOT EXISTS world_pattern_lifecycle_events_no_update
            BEFORE UPDATE ON world_pattern_lifecycle_events
            BEGIN
                SELECT RAISE(ABORT, 'world_pattern_lifecycle_events are append-only');
            END
            """,
)


def ensure_world_pattern_lifecycle_events_schema(db: StateDb) -> None:
    """Idempotently add the lifecycle ledger to an already-applied v1 file."""

    with db.transaction() as cur:
        for statement in WORLD_PATTERN_LIFECYCLE_EVENTS_DDL:
            cur.execute(statement)


WORLD_PREDICTION_IDENTITY_INDEX_DDL = """
CREATE INDEX IF NOT EXISTS idx_world_predictions_identity
            ON world_shadow_predictions(episode_id, horizon_code, model_kind, model_version)
            """


def ensure_world_prediction_identity_index(db: StateDb) -> None:
    """Add the covering startup identity index to already-applied v1 ledgers."""

    table = db.query_one("SELECT 1 FROM sqlite_master WHERE type='table' AND name='world_shadow_predictions'")
    if table is None:
        # Preserve the schema validator's precise mismatch error for foreign or
        # incomplete ledgers instead of leaking an SQLite "no such table" error.
        return
    with db.transaction() as cur:
        cur.execute(WORLD_PREDICTION_IDENTITY_INDEX_DDL)


WORLD_PREDICTION_RECORDED_AT_INDEX_DDL = """
CREATE INDEX IF NOT EXISTS idx_world_predictions_recorded_at
            ON world_shadow_predictions(recorded_at, prediction_id)
            """


WORLD_SCOPE_MAPPING_GENERATIONS_DDL = (
    """
CREATE TABLE IF NOT EXISTS world_scope_mapping_generations (
                mapping_id        TEXT NOT NULL,
                mapping_sha256    TEXT NOT NULL,
                payload_json      TEXT NOT NULL,
                payload_sha256    TEXT NOT NULL,
                recorded_at       TEXT NOT NULL,
                PRIMARY KEY (mapping_id, mapping_sha256)
            )
            """,
    """
CREATE INDEX IF NOT EXISTS idx_world_scope_mapping_generations_hash
            ON world_scope_mapping_generations(mapping_sha256, mapping_id)
            """,
    """
CREATE TRIGGER IF NOT EXISTS world_scope_mapping_generations_no_delete
            BEFORE DELETE ON world_scope_mapping_generations
            BEGIN
                SELECT RAISE(ABORT, 'world_scope_mapping_generations are append-only');
            END
            """,
    """
CREATE TRIGGER IF NOT EXISTS world_scope_mapping_generations_no_update
            BEFORE UPDATE ON world_scope_mapping_generations
            BEGIN
                SELECT RAISE(ABORT, 'world_scope_mapping_generations are append-only');
            END
            """,
)


def ensure_world_scope_mapping_generations_schema(db: StateDb) -> None:
    """Idempotently add the mapping-generation ledger to an already-applied v1 file."""

    with db.transaction() as cur:
        for statement in WORLD_SCOPE_MAPPING_GENERATIONS_DDL:
            cur.execute(statement)


def ensure_world_prediction_recorded_at_index(db: StateDb) -> None:
    """Add the bounded recorded_at archive index to already-applied v1 ledgers."""

    table = db.query_one("SELECT 1 FROM sqlite_master WHERE type='table' AND name='world_shadow_predictions'")
    if table is None:
        return
    with db.transaction() as cur:
        cur.execute(WORLD_PREDICTION_RECORDED_AT_INDEX_DDL)


# This migration namespace belongs only to ``world_model.db``.  It must never
# be added to the central ``trader.infrastructure.state_db.migrations`` list.
# Live cutover archives the previous store; an old schema is rejected rather
# than migrated.
WORLD_MODEL_MIGRATIONS: list[tuple[int, list[str]]] = [
    (
        WORLD_MODEL_SCHEMA_VERSION,
        [
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
CREATE TABLE IF NOT EXISTS world_cohort_manifests (
                cohort_id         TEXT PRIMARY KEY,
                manifest_sha256   TEXT NOT NULL,
                payload_json      TEXT NOT NULL,
                payload_sha256    TEXT NOT NULL,
                recorded_at       TEXT NOT NULL
            )
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
CREATE TABLE IF NOT EXISTS world_entity_events (
                event_id          TEXT PRIMARY KEY,
                event_type        TEXT NOT NULL,
                entity_kind       TEXT NOT NULL,
                entity_id         TEXT NOT NULL,
                sequence          INTEGER NOT NULL,
                payload_json      TEXT NOT NULL,
                payload_sha256    TEXT NOT NULL,
                recorded_at       TEXT NOT NULL,
                UNIQUE(sequence)
            )
            """,
            """
CREATE TABLE IF NOT EXISTS world_entity_identity_events (
                event_id          TEXT PRIMARY KEY,
                event_type        TEXT NOT NULL,
                link_id           TEXT NOT NULL,
                sequence          INTEGER NOT NULL,
                payload_json      TEXT NOT NULL,
                payload_sha256    TEXT NOT NULL,
                recorded_at       TEXT NOT NULL,
                UNIQUE(sequence)
            )
            """,
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
CREATE TABLE IF NOT EXISTS world_graph_snapshot_members (
                snapshot_id       TEXT NOT NULL
                    REFERENCES world_graph_snapshots(snapshot_id),
                member_kind       TEXT NOT NULL
                    CHECK (member_kind IN (
                        'entity_revision',
                        'identity_link',
                        'structural_relation',
                        'knowledge_relation',
                        'artifact'
                    )),
                member_id         TEXT NOT NULL,
                content_sha256    TEXT,
                ordinal           INTEGER NOT NULL,
                payload_json      TEXT NOT NULL,
                payload_sha256    TEXT NOT NULL,
                recorded_at       TEXT NOT NULL,
                PRIMARY KEY (snapshot_id, member_kind, member_id, ordinal)
            )
            """,
            """
CREATE TABLE IF NOT EXISTS world_graph_snapshots (
                snapshot_id         TEXT PRIMARY KEY,
                root_episode_id     TEXT NOT NULL,
                root_entity_kind    TEXT NOT NULL,
                root_entity_id      TEXT NOT NULL,
                cutoff_at           TEXT NOT NULL,
                ontology_revision   TEXT NOT NULL,
                ontology_hash       TEXT NOT NULL,
                identity_map_hash   TEXT NOT NULL,
                scope_mapping_id    TEXT NOT NULL,
                scope_mapping_hash  TEXT NOT NULL,
                status              TEXT NOT NULL
                    CHECK (status IN ('complete', 'partial', 'missing', 'stale')),
                payload_json        TEXT NOT NULL,
                payload_sha256      TEXT NOT NULL,
                recorded_at         TEXT NOT NULL
            )
            """,
            """
CREATE TABLE IF NOT EXISTS world_macro_graph_bridge_events (
                event_id          TEXT PRIMARY KEY,
                event_type        TEXT NOT NULL,
                bridge_key        TEXT NOT NULL,
                run_id            TEXT,
                epoch             INTEGER,
                sequence          INTEGER NOT NULL,
                payload_json      TEXT NOT NULL,
                payload_sha256    TEXT NOT NULL,
                recorded_at       TEXT NOT NULL,
                UNIQUE(bridge_key, sequence)
            )
            """,
            """
CREATE TABLE IF NOT EXISTS world_ontology_revisions (
                event_id                TEXT PRIMARY KEY,
                event_type              TEXT NOT NULL,
                revision_id             TEXT NOT NULL,
                successor_revision_id   TEXT,
                sequence                INTEGER NOT NULL,
                payload_json            TEXT NOT NULL,
                payload_sha256          TEXT NOT NULL,
                recorded_at             TEXT NOT NULL,
                UNIQUE(sequence)
            )
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
CREATE TABLE IF NOT EXISTS world_pattern_hypothesis_events (
                event_id          TEXT PRIMARY KEY,
                hypothesis_id     TEXT NOT NULL,
                event_type        TEXT NOT NULL,
                sequence          INTEGER NOT NULL,
                payload_json      TEXT NOT NULL,
                payload_sha256    TEXT NOT NULL,
                recorded_at       TEXT NOT NULL,
                UNIQUE(hypothesis_id, sequence)
            )
            """,
            """
CREATE TABLE IF NOT EXISTS world_pattern_occurrence_events (
                event_id          TEXT PRIMARY KEY,
                occurrence_id     TEXT NOT NULL,
                hypothesis_id     TEXT NOT NULL,
                event_type        TEXT NOT NULL,
                sequence          INTEGER NOT NULL,
                cohort_id         TEXT NOT NULL,
                cutoff_at         TEXT NOT NULL,
                horizon_id        TEXT NOT NULL,
                payload_json      TEXT NOT NULL,
                payload_sha256    TEXT NOT NULL,
                recorded_at       TEXT NOT NULL,
                UNIQUE(occurrence_id, sequence)
            )
            """,
            """
CREATE TABLE IF NOT EXISTS world_pattern_outcome_links (
                link_id                      TEXT PRIMARY KEY,
                occurrence_id                TEXT NOT NULL,
                event_id                     TEXT NOT NULL,
                horizon_id                   TEXT NOT NULL,
                world_outcome_event_id       TEXT NOT NULL,
                world_outcome_content_sha256 TEXT NOT NULL,
                supersedes_link_id           TEXT,
                payload_json                 TEXT NOT NULL,
                payload_sha256               TEXT NOT NULL,
                recorded_at                  TEXT NOT NULL
            )
            """,
            """
CREATE TABLE IF NOT EXISTS world_relation_events (
                event_id          TEXT PRIMARY KEY,
                event_type        TEXT NOT NULL,
                family            TEXT NOT NULL
                    CHECK (family IN ('structural', 'knowledge')),
                relation_id       TEXT NOT NULL,
                relation_kind     TEXT,
                sequence          INTEGER NOT NULL,
                payload_json      TEXT NOT NULL,
                payload_sha256    TEXT NOT NULL,
                recorded_at       TEXT NOT NULL,
                UNIQUE(sequence)
            )
            """,
            """
CREATE TABLE IF NOT EXISTS world_shadow_predictions (
                prediction_id                  TEXT PRIMARY KEY,
                run_id                         TEXT NOT NULL,
                episode_id                     TEXT NOT NULL
                    REFERENCES world_episodes(episode_id),
                horizon_code                   TEXT NOT NULL,
                model_kind                     TEXT,
                model_version                  TEXT,
                predicted_at                   TEXT,
                input_sha256                   TEXT NOT NULL,
                prediction_json                TEXT NOT NULL,
                prediction_sha256              TEXT NOT NULL,
                payload_json                   TEXT NOT NULL,
                payload_sha256                 TEXT NOT NULL,
                recorded_at                    TEXT NOT NULL,
                study_cohort_id                TEXT,
                lane_id                        TEXT,
                manifest_sha256                TEXT,
                feature_contract_fingerprint   TEXT,
                feature_mask_fingerprint       TEXT
            )
            """,
            """
CREATE INDEX IF NOT EXISTS idx_world_availability_receipts_subject
            ON world_availability_receipts(subject_kind, subject_id, content_sha256)
            """,
            """
CREATE INDEX IF NOT EXISTS idx_world_cohort_events_cohort_sequence
            ON world_cohort_events(cohort_id, sequence, event_id)
            """,
            """
CREATE INDEX IF NOT EXISTS idx_world_cohort_slots_cohort_anchor
            ON world_cohort_slots(cohort_id, as_of_bar_ts, slot_id)
            """,
            """
CREATE INDEX IF NOT EXISTS idx_world_entity_events_entity
            ON world_entity_events(entity_kind, entity_id, sequence, event_id)
            """,
            """
CREATE INDEX IF NOT EXISTS idx_world_entity_identity_events_link
            ON world_entity_identity_events(link_id, sequence, event_id)
            """,
            """
CREATE INDEX IF NOT EXISTS idx_world_episodes_context_market_slot_candidates
            ON world_episodes(
                venue,
                symbol,
                bar_interval,
                feature_contract_version,
                sampling_policy_version,
                recorded_at,
                episode_id
            )
            WHERE feature_contract_version = 'world_feature.context.v1'
            """,
            """
CREATE INDEX IF NOT EXISTS idx_world_episodes_eligible_observed
            ON world_episodes(training_eligible, observed_at, episode_id)
            """,
            """
CREATE INDEX IF NOT EXISTS idx_world_episodes_graph_market_slot_candidates
            ON world_episodes(
                venue,
                symbol,
                bar_interval,
                feature_contract_version,
                sampling_policy_version,
                recorded_at,
                episode_id
            )
            WHERE feature_contract_version = 'world_feature.graph.v1'
            """,
            """
CREATE INDEX IF NOT EXISTS idx_world_graph_snapshot_members_snapshot
            ON world_graph_snapshot_members(snapshot_id, member_kind, member_id)
            """,
            """
CREATE INDEX IF NOT EXISTS idx_world_graph_snapshots_cutoff
            ON world_graph_snapshots(cutoff_at, root_episode_id, snapshot_id)
            """,
            """
CREATE INDEX IF NOT EXISTS idx_world_macro_graph_bridge_events_key
            ON world_macro_graph_bridge_events(bridge_key, sequence, event_id)
            """,
            """
CREATE INDEX IF NOT EXISTS idx_world_ontology_revisions_revision
            ON world_ontology_revisions(revision_id, sequence, event_id)
            """,
            """
CREATE INDEX IF NOT EXISTS idx_world_outcomes_observed
            ON world_outcome_events(status, training_eligible, label_available_at, outcome_event_id)
            """,
            """
CREATE INDEX IF NOT EXISTS idx_world_outcomes_pending
            ON world_outcome_events(episode_id, horizon_code, status, outcome_event_id)
            """,
            """
CREATE INDEX IF NOT EXISTS idx_world_outcomes_supersedes
            ON world_outcome_events(supersedes_outcome_event_id)
            """,
            """
CREATE INDEX IF NOT EXISTS idx_world_pattern_hypothesis_events_hypothesis
            ON world_pattern_hypothesis_events(hypothesis_id, sequence, event_id)
            """,
            """
CREATE INDEX IF NOT EXISTS idx_world_pattern_occurrence_events_cohort_cutoff
            ON world_pattern_occurrence_events(cohort_id, cutoff_at, occurrence_id)
            """,
            """
CREATE INDEX IF NOT EXISTS idx_world_pattern_occurrence_events_hypothesis
            ON world_pattern_occurrence_events(hypothesis_id, cutoff_at, occurrence_id)
            """,
            """
CREATE INDEX IF NOT EXISTS idx_world_pattern_outcome_links_horizon
            ON world_pattern_outcome_links(horizon_id, occurrence_id, link_id)
            """,
            """
CREATE INDEX IF NOT EXISTS idx_world_pattern_outcome_links_occurrence
            ON world_pattern_outcome_links(occurrence_id, horizon_id, link_id)
            """,
            """
CREATE INDEX IF NOT EXISTS idx_world_predictions_feature_fps
            ON world_shadow_predictions(feature_contract_fingerprint, feature_mask_fingerprint, prediction_id)
            """,
            """
CREATE INDEX IF NOT EXISTS idx_world_predictions_manifest
            ON world_shadow_predictions(manifest_sha256, prediction_id)
            """,
            WORLD_PREDICTION_IDENTITY_INDEX_DDL,
            WORLD_PREDICTION_RECORDED_AT_INDEX_DDL,
            """
CREATE INDEX IF NOT EXISTS idx_world_predictions_run_episode
            ON world_shadow_predictions(run_id, horizon_code, episode_id, prediction_id)
            """,
            """
CREATE INDEX IF NOT EXISTS idx_world_predictions_study_cohort
            ON world_shadow_predictions(study_cohort_id, lane_id, prediction_id)
            """,
            """
CREATE INDEX IF NOT EXISTS idx_world_relation_events_family
            ON world_relation_events(family, sequence, relation_id, event_id)
            """,
            """
CREATE TRIGGER IF NOT EXISTS world_availability_receipts_no_delete
            BEFORE DELETE ON world_availability_receipts
            BEGIN
                SELECT RAISE(ABORT, 'world_availability_receipts are append-only');
            END
            """,
            """
CREATE TRIGGER IF NOT EXISTS world_availability_receipts_no_update
            BEFORE UPDATE ON world_availability_receipts
            BEGIN
                SELECT RAISE(ABORT, 'world_availability_receipts are append-only');
            END
            """,
            """
CREATE TRIGGER IF NOT EXISTS world_cohort_events_no_delete
            BEFORE DELETE ON world_cohort_events
            BEGIN
                SELECT RAISE(ABORT, 'world_cohort_events are append-only');
            END
            """,
            """
CREATE TRIGGER IF NOT EXISTS world_cohort_events_no_update
            BEFORE UPDATE ON world_cohort_events
            BEGIN
                SELECT RAISE(ABORT, 'world_cohort_events are append-only');
            END
            """,
            """
CREATE TRIGGER IF NOT EXISTS world_cohort_manifests_no_delete
            BEFORE DELETE ON world_cohort_manifests
            BEGIN
                SELECT RAISE(ABORT, 'world_cohort_manifests are append-only');
            END
            """,
            """
CREATE TRIGGER IF NOT EXISTS world_cohort_manifests_no_update
            BEFORE UPDATE ON world_cohort_manifests
            BEGIN
                SELECT RAISE(ABORT, 'world_cohort_manifests are append-only');
            END
            """,
            """
CREATE TRIGGER IF NOT EXISTS world_cohort_slots_no_delete
            BEFORE DELETE ON world_cohort_slots
            BEGIN
                SELECT RAISE(ABORT, 'world_cohort_slots are append-only');
            END
            """,
            """
CREATE TRIGGER IF NOT EXISTS world_cohort_slots_no_update
            BEFORE UPDATE ON world_cohort_slots
            BEGIN
                SELECT RAISE(ABORT, 'world_cohort_slots are append-only');
            END
            """,
            """
CREATE TRIGGER IF NOT EXISTS world_entity_events_no_delete
            BEFORE DELETE ON world_entity_events
            BEGIN
                SELECT RAISE(ABORT, 'world_entity_events are append-only');
            END
            """,
            """
CREATE TRIGGER IF NOT EXISTS world_entity_events_no_update
            BEFORE UPDATE ON world_entity_events
            BEGIN
                SELECT RAISE(ABORT, 'world_entity_events are append-only');
            END
            """,
            """
CREATE TRIGGER IF NOT EXISTS world_entity_identity_events_no_delete
            BEFORE DELETE ON world_entity_identity_events
            BEGIN
                SELECT RAISE(ABORT, 'world_entity_identity_events are append-only');
            END
            """,
            """
CREATE TRIGGER IF NOT EXISTS world_entity_identity_events_no_update
            BEFORE UPDATE ON world_entity_identity_events
            BEGIN
                SELECT RAISE(ABORT, 'world_entity_identity_events are append-only');
            END
            """,
            """
CREATE TRIGGER IF NOT EXISTS world_episodes_context_canonical_first_write
            BEFORE INSERT ON world_episodes
            WHEN NEW.feature_contract_version = 'world_feature.context.v1'
            BEGIN
                SELECT RAISE(ABORT, 'context market slot already exists')
                WHERE EXISTS (
                    SELECT 1 FROM world_episodes AS existing
                    WHERE existing.symbol = NEW.symbol
                      AND existing.venue IS NEW.venue
                      AND existing.bar_interval IS NEW.bar_interval
                      AND existing.feature_contract_version = 'world_feature.context.v1'
                      AND existing.sampling_policy_version IS NEW.sampling_policy_version
                      AND existing.episode_id != NEW.episode_id
                      AND existing.as_of_bar_ts IS NEW.as_of_bar_ts
                );
            END
            """,
            """
CREATE TRIGGER IF NOT EXISTS world_episodes_graph_canonical_first_write
            BEFORE INSERT ON world_episodes
            WHEN NEW.feature_contract_version = 'world_feature.graph.v1'
            BEGIN
                SELECT RAISE(ABORT, 'graph market slot already exists')
                WHERE EXISTS (
                    SELECT 1 FROM world_episodes AS existing
                    WHERE existing.symbol = NEW.symbol
                      AND existing.venue IS NEW.venue
                      AND existing.bar_interval IS NEW.bar_interval
                      AND existing.feature_contract_version = 'world_feature.graph.v1'
                      AND existing.sampling_policy_version IS NEW.sampling_policy_version
                      AND existing.episode_id != NEW.episode_id
                      AND existing.as_of_bar_ts IS NEW.as_of_bar_ts
                );
                SELECT RAISE(ABORT, 'graph snapshot must be persisted first')
                WHERE json_extract(NEW.payload_json, '$.observation.graph_features.snapshot.snapshot_id') IS NOT NULL
                  AND NOT EXISTS (
                    SELECT 1 FROM world_graph_snapshots AS snapshot
                    WHERE snapshot.snapshot_id = json_extract(
                        NEW.payload_json,
                        '$.observation.graph_features.snapshot.snapshot_id'
                    )
                );
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
CREATE TRIGGER IF NOT EXISTS world_episodes_no_update
            BEFORE UPDATE ON world_episodes
            BEGIN
                SELECT RAISE(ABORT, 'world_episodes are append-only');
            END
            """,
            """
CREATE TRIGGER IF NOT EXISTS world_graph_snapshot_members_no_delete
            BEFORE DELETE ON world_graph_snapshot_members
            BEGIN
                SELECT RAISE(ABORT, 'world_graph_snapshot_members are append-only');
            END
            """,
            """
CREATE TRIGGER IF NOT EXISTS world_graph_snapshot_members_no_update
            BEFORE UPDATE ON world_graph_snapshot_members
            BEGIN
                SELECT RAISE(ABORT, 'world_graph_snapshot_members are append-only');
            END
            """,
            """
CREATE TRIGGER IF NOT EXISTS world_graph_snapshots_no_delete
            BEFORE DELETE ON world_graph_snapshots
            BEGIN
                SELECT RAISE(ABORT, 'world_graph_snapshots are append-only');
            END
            """,
            """
CREATE TRIGGER IF NOT EXISTS world_graph_snapshots_no_update
            BEFORE UPDATE ON world_graph_snapshots
            BEGIN
                SELECT RAISE(ABORT, 'world_graph_snapshots are append-only');
            END
            """,
            """
CREATE TRIGGER IF NOT EXISTS world_macro_graph_bridge_events_no_delete
            BEFORE DELETE ON world_macro_graph_bridge_events
            BEGIN
                SELECT RAISE(ABORT, 'world_macro_graph_bridge_events are append-only');
            END
            """,
            """
CREATE TRIGGER IF NOT EXISTS world_macro_graph_bridge_events_no_update
            BEFORE UPDATE ON world_macro_graph_bridge_events
            BEGIN
                SELECT RAISE(ABORT, 'world_macro_graph_bridge_events are append-only');
            END
            """,
            """
CREATE TRIGGER IF NOT EXISTS world_ontology_revisions_no_delete
            BEFORE DELETE ON world_ontology_revisions
            BEGIN
                SELECT RAISE(ABORT, 'world_ontology_revisions are append-only');
            END
            """,
            """
CREATE TRIGGER IF NOT EXISTS world_ontology_revisions_no_update
            BEFORE UPDATE ON world_ontology_revisions
            BEGIN
                SELECT RAISE(ABORT, 'world_ontology_revisions are append-only');
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
CREATE TRIGGER IF NOT EXISTS world_outcomes_no_update
            BEFORE UPDATE ON world_outcome_events
            BEGIN
                SELECT RAISE(ABORT, 'world_outcome_events are append-only');
            END
            """,
            """
CREATE TRIGGER IF NOT EXISTS world_pattern_hypothesis_events_no_delete
            BEFORE DELETE ON world_pattern_hypothesis_events
            BEGIN
                SELECT RAISE(ABORT, 'world_pattern_hypothesis_events are append-only');
            END
            """,
            """
CREATE TRIGGER IF NOT EXISTS world_pattern_hypothesis_events_no_update
            BEFORE UPDATE ON world_pattern_hypothesis_events
            BEGIN
                SELECT RAISE(ABORT, 'world_pattern_hypothesis_events are append-only');
            END
            """,
            """
CREATE TRIGGER IF NOT EXISTS world_pattern_occurrence_events_no_delete
            BEFORE DELETE ON world_pattern_occurrence_events
            BEGIN
                SELECT RAISE(ABORT, 'world_pattern_occurrence_events are append-only');
            END
            """,
            """
CREATE TRIGGER IF NOT EXISTS world_pattern_occurrence_events_no_update
            BEFORE UPDATE ON world_pattern_occurrence_events
            BEGIN
                SELECT RAISE(ABORT, 'world_pattern_occurrence_events are append-only');
            END
            """,
            """
CREATE TRIGGER IF NOT EXISTS world_pattern_outcome_links_no_delete
            BEFORE DELETE ON world_pattern_outcome_links
            BEGIN
                SELECT RAISE(ABORT, 'world_pattern_outcome_links are append-only');
            END
            """,
            """
CREATE TRIGGER IF NOT EXISTS world_pattern_outcome_links_no_update
            BEFORE UPDATE ON world_pattern_outcome_links
            BEGIN
                SELECT RAISE(ABORT, 'world_pattern_outcome_links are append-only');
            END
            """,
            """
CREATE TRIGGER IF NOT EXISTS world_predictions_no_delete
            BEFORE DELETE ON world_shadow_predictions
            BEGIN
                SELECT RAISE(ABORT, 'world_shadow_predictions are append-only');
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
CREATE TRIGGER IF NOT EXISTS world_relation_events_no_delete
            BEFORE DELETE ON world_relation_events
            BEGIN
                SELECT RAISE(ABORT, 'world_relation_events are append-only');
            END
            """,
            """
CREATE TRIGGER IF NOT EXISTS world_relation_events_no_update
            BEFORE UPDATE ON world_relation_events
            BEGIN
                SELECT RAISE(ABORT, 'world_relation_events are append-only');
            END
            """,
            """
CREATE TRIGGER IF NOT EXISTS world_episodes_current_feature_contract
            BEFORE INSERT ON world_episodes
            WHEN NEW.feature_contract_version IS NULL
               OR NEW.feature_contract_version NOT IN (
                    'world_feature.market.v1',
                    'world_feature.context.v1',
                    'world_feature.graph.v1'
               )
            BEGIN
                SELECT RAISE(ABORT, 'unsupported feature contract');
            END
            """,
            """
CREATE TRIGGER IF NOT EXISTS world_episodes_market_canonical_first_write
            BEFORE INSERT ON world_episodes
            WHEN NEW.feature_contract_version = 'world_feature.market.v1'
            BEGIN
                SELECT RAISE(ABORT, 'market slot already exists')
                WHERE EXISTS (
                    SELECT 1 FROM world_episodes AS existing
                    WHERE existing.symbol = NEW.symbol
                      AND existing.venue IS NEW.venue
                      AND existing.bar_interval IS NEW.bar_interval
                      AND existing.feature_contract_version = 'world_feature.market.v1'
                      AND existing.sampling_policy_version IS NEW.sampling_policy_version
                      AND existing.episode_id != NEW.episode_id
                      AND existing.as_of_bar_ts IS NEW.as_of_bar_ts
                );
            END
            """,
            """
CREATE INDEX IF NOT EXISTS idx_world_episodes_market_slot_candidates
            ON world_episodes(
                venue,
                symbol,
                bar_interval,
                feature_contract_version,
                sampling_policy_version,
                recorded_at,
                episode_id
            )
            WHERE feature_contract_version = 'world_feature.market.v1'
            """,
            *WORLD_PATTERN_LIFECYCLE_EVENTS_DDL,
            *WORLD_SCOPE_MAPPING_GENERATIONS_DDL,
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

_COLD_REPLAY_FIELDS = (
    "run_id",
    "episode_id",
    "horizon_code",
    "model_kind",
    "model_version",
    "predicted_at",
    "input_sha256",
    "prediction_sha256",
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


def _canonical_digest(value: Any, *, field: str) -> str | None:
    """Accept a live sha256 hex digest, with or without a ``sha256:`` prefix."""

    rendered = _text(value)
    if rendered is None:
        return None
    hex_part = rendered[7:] if rendered.lower().startswith("sha256:") else rendered
    if len(hex_part) != 64:
        raise ValueError(f"{field} must be a sha256 digest")
    try:
        int(hex_part, 16)
    except ValueError as exc:
        raise ValueError(f"{field} must be a sha256 digest") from exc
    return f"sha256:{hex_part.lower()}"


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
    """Read adapter: persist only the current DOWN/FLAT/UP classes."""

    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text not in PREDICTION_CLASSES:
        raise ValueError("move_class must be one of: DOWN, FLAT, UP")
    return text


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


class _LeasedWorldModelStateDb(StateDb):
    """Dedicated world-model SQLite connection whose shared storage lease outlives close.

    The lease is acquired before SQLite opens and released only after
    ``StateDb.close()``. Caller-supplied ``StateDb`` instances are never wrapped.
    """

    def __init__(self, db_path: str | Path, *, lease: PredictionStorageLease) -> None:
        self._storage_lease: PredictionStorageLease | None = lease
        try:
            super().__init__(db_path)
        except Exception:
            conn = getattr(self, "_conn", None)
            if conn is not None:
                self._conn = None
                try:
                    conn.close()
                except sqlite3.Error:
                    pass
            self._release_storage_lease()
            raise

    def close(self) -> None:
        try:
            super().close()
        finally:
            self._release_storage_lease()

    def _release_storage_lease(self) -> None:
        lease = self._storage_lease
        self._storage_lease = None
        if lease is not None:
            lease.close()


def _open_dedicated_state_db(path: str | Path) -> StateDb:
    """Open a newly-created WAL database safely when workers start together.

    ``StateDb`` enables WAL in its constructor, before its connection-level
    busy timeout is installed.  Two independent shadow workers may therefore
    race only at that first pragma.  Retrying this tiny initialization window
    here keeps concurrent evidence capture reliable without changing the
    shared StateDb behavior used by production stores.

    A shared prediction storage lease is acquired before SQLite opens and
    released only after the dedicated connection is closed.
    """

    last_error: sqlite3.OperationalError | None = None
    for attempt in range(12):
        lease = shared_prediction_storage_lease(path, create=True)
        try:
            return _LeasedWorldModelStateDb(path, lease=lease)
        except sqlite3.OperationalError as exc:
            lease.close()
            if "locked" not in str(exc).lower():
                raise
            last_error = exc
            # 25ms, 50ms, 75ms, ... capped at 150ms: normally a single retry,
            # and roughly 1.5 seconds maximum even for a stuck peer.
            time.sleep(min(0.025 * (attempt + 1), 0.15))
        except Exception:
            lease.close()
            raise
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
        self._storage_lease = None
        self._owns_db = False
        opened: StateDb | None = None
        try:
            if isinstance(db_or_path, StateDb):
                if db_or_path.path.name.casefold() == "casys.db":
                    raise ValueError("WorldModelStore requires a dedicated world_model.db, never casys.db")
                self._storage_lease = shared_prediction_storage_lease(db_or_path.path, create=True)
                opened = db_or_path
            else:
                path = Path(db_or_path)
                if path.name.casefold() == "casys.db":
                    raise ValueError("WorldModelStore requires a dedicated world_model.db, never casys.db")
                opened = _open_dedicated_state_db(path)
                self._owns_db = True
            self._db = opened
            self.path = self._db.path
            if self.path.name.casefold() == "casys.db":
                raise ValueError("WorldModelStore requires a dedicated world_model.db, never casys.db")
            self._clock = clock or default_utc_clock
            self._first_seen_at: dict[str, datetime] = {}
            # ``foreign_keys`` is connection-local and StateDb intentionally stays
            # generic, so set it before the world schema is used.
            self._db.query_one("PRAGMA foreign_keys=ON")
            apply_current_world_model_schema(self._db)
            self._prime_existing_receipts()
        except Exception:
            if self._owns_db and opened is not None:
                opened.close()
            if self._storage_lease is not None:
                self._storage_lease.close()
                self._storage_lease = None
            raise

    def close(self) -> None:
        """Close a store-owned connection and always release the storage lease."""

        try:
            if self._owns_db:
                self._db.close()
        finally:
            lease = self._storage_lease
            self._storage_lease = None
            if lease is not None:
                lease.close()

    @contextmanager
    def _prediction_read_tx(self) -> Iterator[sqlite3.Connection]:
        """Snapshot prediction reads without BEGIN IMMEDIATE."""

        with self._db._lock:
            self._db._ensure_open()
            conn = self._db._conn
            started = False
            if not conn.in_transaction:
                conn.execute("BEGIN DEFERRED")
                started = True
            try:
                yield conn
                if started:
                    conn.execute("COMMIT")
            except Exception:
                if started:
                    try:
                        conn.execute("ROLLBACK")
                    except Exception:
                        pass
                raise

    def integrity_check(self) -> list[str]:
        return self._db.integrity_check()

    def append_episode(self, episode: Any) -> bool:
        if isinstance(episode, WorldEpisode):
            canonical = episode
        else:
            mapping = _as_mapping(episode, name="episode")
            observation = mapping.get("observation")
            if isinstance(observation, Mapping):
                top = _text(mapping.get("feature_contract_version"))
                nested = _text(observation.get("feature_contract_version"))
                if top and nested and top != nested:
                    raise ValueError("feature_contract_version envelope contradicts nested observation")
            canonical = WorldEpisode.from_dict(mapping)
        payload = canonical.to_dict()
        observation = _mapping_or_empty(payload.get("observation"), name="episode.observation")
        contract = canonical.observation.feature_contract_version
        if contract not in _CURRENT_FEATURE_CONTRACTS:
            raise ValueError(
                "feature_contract_version must be one of: " + ", ".join(sorted(_CURRENT_FEATURE_CONTRACTS))
            )
        source_evidence = _first(
            payload,
            ("source_evidence",),
            ("evidence",),
            ("observation", "source_evidence"),
            ("observation", "evidence"),
        )
        source = _mapping_or_empty(source_evidence, name="episode.source_evidence")
        if not source and observation:
            # The domain object keeps its point-in-time proof in the
            # observation itself (anchor/freshness/availability).  Duplicate
            # that narrow evidence projection for indexed provenance without
            # inventing a reconstruction from later runtime state.
            source = {
                key: observation[key]
                for key in ("anchor", "freshness", "available_at", "captured_at", "context", "graph_features")
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
        capability = {
            _MARKET_FEATURE_CONTRACT: "market",
            _CONTEXT_FEATURE_CONTRACT: "context",
            _GRAPH_FEATURE_CONTRACT: "graph",
        }[contract]
        slot_fields = (
            values["venue"],
            values["symbol"],
            values["bar_interval"],
            values["as_of_bar_ts"],
            values["sampling_policy_version"],
        )
        if any(item is None or item == "" for item in slot_fields):
            raise ValueError(f"{capability} episode requires a complete market slot identity")
        if contract == _GRAPH_FEATURE_CONTRACT:
            snapshot = None
            graph_features = observation.get("graph_features")
            if isinstance(graph_features, Mapping):
                snapshot = graph_features.get("snapshot")
            if snapshot is not None:
                self.append_graph_snapshot(snapshot)
        existing = self.get_episode_by_capability_slot(
            venue=values["venue"],
            symbol=values["symbol"],
            bar_interval=values["bar_interval"],
            as_of_bar_ts=values["as_of_bar_ts"],
            feature_contract_version=contract,
            sampling_policy_version=values["sampling_policy_version"],
        )
        if existing is not None:
            if (
                existing["episode_id"] == values["episode_id"]
                and existing["payload_sha256"] == values["payload_sha256"]
            ):
                return False
            raise WorldModelConflictError(f"{capability} market slot already exists with different canonical content")
        try:
            return self._append(
                table="world_episodes",
                id_column="episode_id",
                columns=_EPISODE_COLUMNS,
                values=values,
            )
        except sqlite3.IntegrityError as exc:
            message = str(exc)
            if "unsupported feature contract" in message:
                raise ValueError("unsupported feature contract") from exc
            if f"{capability} market slot already exists" in message or "UNIQUE constraint failed" in message:
                raise WorldModelConflictError(
                    f"{capability} market slot already exists with different canonical content"
                ) from exc
            if capability == "graph" and "graph snapshot must be persisted first" in message:
                raise WorldModelConflictError("graph snapshot must be persisted first") from exc
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
        if live and not isinstance(prediction, WorldPrediction):
            supplied_input_hash = _canonical_digest(payload.get("input_sha256"), field="input_sha256")
            supplied_feature_hash = _canonical_digest(
                _first(payload, ("feature_hash",), ("prediction", "feature_hash")),
                field="feature_hash",
            )
        else:
            supplied_input_hash = _stored_sha256(payload.get("input_sha256"))
            supplied_feature_hash = _stored_sha256(_first(payload, ("feature_hash",), ("prediction", "feature_hash")))
        if (
            live
            and not isinstance(prediction, WorldPrediction)
            and (supplied_input_hash or supplied_feature_hash)
            and model_input
        ):
            raise ValueError("live compact prediction cannot include input")
        if (
            live
            and not isinstance(prediction, WorldPrediction)
            and supplied_input_hash is None
            and supplied_feature_hash is None
            and not model_input
        ):
            raise ValueError("live prediction requires input_sha256, feature_hash, or input")
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
            "input_sha256": supplied_input_hash or supplied_feature_hash or _canonical_sha256(model_input),
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
                with self._prediction_read_tx() as conn:
                    cold = cold_prediction_for_replay(conn, values["prediction_id"])
                if cold is None:
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

    def get_episode_by_capability_slot(
        self,
        *,
        venue: str,
        symbol: str,
        bar_interval: str,
        as_of_bar_ts: str,
        feature_contract_version: str,
        sampling_policy_version: str,
    ) -> dict[str, Any] | None:
        """Return the first canonical episode for one market slot and capability."""

        if feature_contract_version not in _CURRENT_FEATURE_CONTRACTS:
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

    def get_episode_by_context_slot(
        self,
        *,
        venue: str,
        symbol: str,
        bar_interval: str,
        as_of_bar_ts: str,
        feature_contract_version: str,
        sampling_policy_version: str,
    ) -> dict[str, Any] | None:
        if feature_contract_version != _CONTEXT_FEATURE_CONTRACT:
            return None
        return self.get_episode_by_capability_slot(
            venue=venue,
            symbol=symbol,
            bar_interval=bar_interval,
            as_of_bar_ts=as_of_bar_ts,
            feature_contract_version=feature_contract_version,
            sampling_policy_version=sampling_policy_version,
        )

    def get_episode_by_graph_slot(
        self,
        *,
        venue: str,
        symbol: str,
        bar_interval: str,
        as_of_bar_ts: str,
        feature_contract_version: str,
        sampling_policy_version: str,
    ) -> dict[str, Any] | None:
        """Return the first canonical graph episode for one market slot, if any."""

        if feature_contract_version != _GRAPH_FEATURE_CONTRACT:
            return None
        return self.get_episode_by_capability_slot(
            venue=venue,
            symbol=symbol,
            bar_interval=bar_interval,
            as_of_bar_ts=as_of_bar_ts,
            feature_contract_version=feature_contract_version,
            sampling_policy_version=sampling_policy_version,
        )

    def append_graph_snapshot(self, snapshot: Any) -> bool:
        """Persist a graph snapshot before the episode that roots on it."""

        parsed = snapshot if isinstance(snapshot, WorldGraphSnapshot) else WorldGraphSnapshot.from_mapping(snapshot)
        payload = parsed.to_dict()
        payload_json = canonical_json(payload)
        payload_sha256 = canonical_sha256(payload)
        existing = self._db.query_one(
            "SELECT snapshot_id, payload_sha256 FROM world_graph_snapshots WHERE snapshot_id=?",
            (parsed.snapshot_id,),
        )
        if existing is not None:
            if existing["payload_sha256"] != payload_sha256:
                raise WorldModelConflictError("graph snapshot already exists with different canonical content")
            return False
        recorded_at = _utc_now()
        root_kind, root_id = _graph_snapshot_root_adapter_columns(parsed.root_entity)
        try:
            with self._db.transaction() as cur:
                cur.execute(
                    """
                    INSERT INTO world_graph_snapshots(
                        snapshot_id, root_episode_id, root_entity_kind, root_entity_id, cutoff_at,
                        ontology_revision, ontology_hash, identity_map_hash, scope_mapping_id,
                        scope_mapping_hash, status, payload_json, payload_sha256, recorded_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        parsed.snapshot_id,
                        parsed.root_episode_id,
                        root_kind,
                        root_id,
                        payload["cutoff_at"],
                        parsed.ontology_revision,
                        parsed.ontology_hash,
                        parsed.identity_map_hash,
                        parsed.scope_mapping_id,
                        parsed.scope_mapping_hash,
                        parsed.status,
                        payload_json,
                        payload_sha256,
                        recorded_at,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            recovered = self._db.query_one(
                "SELECT payload_sha256 FROM world_graph_snapshots WHERE snapshot_id=?",
                (parsed.snapshot_id,),
            )
            if recovered is not None:
                if recovered["payload_sha256"] != payload_sha256:
                    raise WorldModelConflictError(
                        "graph snapshot already exists with different canonical content"
                    ) from exc
                return False
            raise WorldModelConflictError("conflicting graph snapshot") from exc
        return True

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
        horizon = _horizon_filter(horizon_code=horizon_code, horizon_id=horizon_id)
        columns = (*_PREDICTION_COLUMNS, "recorded_at", "episode_observed_at")
        with self._prediction_read_tx() as conn:
            rows = read_prediction_rows(
                conn,
                columns=columns,
                run_id=run_id,
                episode_ids=None if episode_id is None else (episode_id,),
                horizon_code=horizon,
                order="episode",
                limit=limit,
            )
        return [self._prediction_row(row) for row in rows]

    def list_prediction_identities(self) -> list[dict[str, Any]]:
        """Return only the stable deduplication key required at service startup."""

        with self._prediction_read_tx() as conn:
            return read_prediction_identities(conn)

    def counts(self) -> dict[str, int]:
        with self._prediction_read_tx() as conn:
            return {
                "episodes": int(conn.execute("SELECT COUNT(*) FROM world_episodes").fetchone()[0]),
                "outcome_events": int(conn.execute("SELECT COUNT(*) FROM world_outcome_events").fetchone()[0]),
                "predictions": prediction_count(conn),
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

    def list_collecting_cohort_ids(self) -> tuple[WorldCohortId, ...]:
        """Return reconstructed collecting cohort ids. Never registers, arms, or starts."""

        return tuple(WorldCohortId(cohort.cohort_id) for cohort in self.list_collecting_cohorts())

    def list_collecting_cohorts(self) -> tuple[WorldCohort, ...]:
        """Return reconstructed collecting aggregates. Never registers, arms, or starts."""

        rows = self._db.query_all("SELECT cohort_id FROM world_cohort_manifests ORDER BY cohort_id")
        collecting: list[WorldCohort] = []
        for row in rows:
            try:
                cohort = self.load(WorldCohortId(row["cohort_id"]))
            except (LookupError, TypeError, ValueError):
                continue
            if cohort.phase is CohortPhase.COLLECTING and cohort.started_event is not None:
                collecting.append(cohort)
        return tuple(collecting)

    def list_live_cohorts(self) -> tuple[WorldCohort, ...]:
        """Return REGISTERED, ARMED, or COLLECTING aggregates. Never mutates."""

        rows = self._db.query_all("SELECT cohort_id FROM world_cohort_manifests ORDER BY cohort_id")
        live: list[WorldCohort] = []
        for row in rows:
            try:
                cohort = self.load(WorldCohortId(row["cohort_id"]))
            except (LookupError, TypeError, ValueError):
                continue
            if cohort.phase in {CohortPhase.REGISTERED, CohortPhase.ARMED, CohortPhase.COLLECTING}:
                live.append(cohort)
        return tuple(live)

    def persist_mapping_generation(
        self,
        mapping: WorldScopeMapping | WorldScopeMappingGeneration,
    ) -> WorldScopeMappingGeneration:
        generation = WorldScopeMappingGeneration.from_mapping(mapping)
        payload = generation.mapping.to_dict()
        payload_json = canonical_json(payload)
        payload_sha256 = canonical_sha256(payload)
        existing = self._db.query_one(
            "SELECT payload_sha256 FROM world_scope_mapping_generations WHERE mapping_id=? AND mapping_sha256=?",
            (generation.mapping_id, generation.mapping_sha256),
        )
        if existing is not None:
            if existing["payload_sha256"] != payload_sha256:
                raise WorldModelConflictError(
                    "mapping generation already exists with different canonical content"
                )
            return generation
        recorded_at = _utc_now()
        try:
            with self._db.transaction() as cur:
                cur.execute(
                    """
                    INSERT INTO world_scope_mapping_generations(
                        mapping_id, mapping_sha256, payload_json, payload_sha256, recorded_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        generation.mapping_id,
                        generation.mapping_sha256,
                        payload_json,
                        payload_sha256,
                        recorded_at,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            recovered = self._db.query_one(
                "SELECT payload_sha256 FROM world_scope_mapping_generations WHERE mapping_id=? AND mapping_sha256=?",
                (generation.mapping_id, generation.mapping_sha256),
            )
            if recovered is not None:
                if recovered["payload_sha256"] != payload_sha256:
                    raise WorldModelConflictError(
                        "mapping generation already exists with different canonical content"
                    ) from exc
                return generation
            raise WorldModelConflictError("conflicting mapping generation") from exc
        return generation

    def load_mapping_generation(self, mapping_id: str, mapping_sha256: str) -> WorldScopeMappingGeneration | None:
        row = self._db.query_one(
            "SELECT payload_json FROM world_scope_mapping_generations WHERE mapping_id=? AND mapping_sha256=?",
            (mapping_id, mapping_sha256),
        )
        if row is None:
            return None
        return WorldScopeMappingGeneration.from_mapping(_json_load(row["payload_json"]))

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
            if table == "world_shadow_predictions":
                replayed = self._cold_prediction_replay(cur, values)
                if replayed is not None:
                    return replayed
            try:
                cur.execute(
                    f"INSERT INTO {table}({column_names}, recorded_at) "  # noqa: S608 -- constants only
                    f"VALUES ({placeholders}, ?) ON CONFLICT({id_column}) DO NOTHING",
                    (*stored_values, _utc_now()),
                )
            except sqlite3.IntegrityError as exc:
                if table == "world_shadow_predictions" and "cold storage" in str(exc):
                    replayed = self._cold_prediction_replay(cur, values)
                    if replayed is not None:
                        return replayed
                    raise WorldModelConflictError(
                        f"{id_column} {values[id_column]!r} already exists with different canonical content"
                    ) from exc
                raise
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

    def _cold_prediction_replay(self, cur: sqlite3.Cursor, values: Mapping[str, Any]) -> bool | None:
        cold = cold_prediction_for_replay(cur, values["prediction_id"])
        if cold is None:
            return None
        for field in _COLD_REPLAY_FIELDS:
            if cold.get(field) != values.get(field):
                raise WorldModelConflictError(
                    f"prediction_id {values['prediction_id']!r} already exists with different canonical content"
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
        # NOTE: source_evidence_json stays unparsed on purpose (raw column
        # remains available). No reader needs the parsed projection.
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
