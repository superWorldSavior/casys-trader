"""Read-only unlabeled scan of graph companions for prospective pattern matching.

Opens the ledger with URI ``mode=ro`` and ``PRAGMA query_only=ON``. A missing
file stays missing. This adapter never creates, migrates, or writes the ledger,
never instantiates a store, and never reads outcome rows.
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from trader.application.world_model.pattern_evaluation_ports import (
    PatternEvaluationBatch,
    PatternEvaluationRecord,
)
from trader.application.world_model.pattern_evaluation_request import PatternEvaluationScanRequest
from trader.domain.world_cohort import WorldCohortSlot
from trader.domain.world_episode import GRAPH_FEATURE_CONTRACT_ID, WorldEpisode
from trader.infrastructure.state_db.world_pattern_formation_query import (
    _Ledger,
    _admitted_snapshot,
    _clocks_not_after,
    _embedded_snapshot_id,
    _hydrate_driver_bindings,
    _hydrate_members,
    _json_object,
    _load_macro_observation_corpus,
    _max_clock,
    _parse_clock,
    _placeholders,
    _readonly_connection,
    _table_names,
)

_READONLY_TIMEOUT_S = 5.0
_REQUIRED_TABLES = frozenset(
    {
        "world_availability_receipts",
        "world_cohort_slots",
        "world_episodes",
        "world_graph_snapshot_members",
        "world_graph_snapshots",
        "world_relation_events",
    }
)
_SNAPSHOT_SUBJECT_KIND = "world_graph_snapshot"
_RELATION_SUBJECT_KIND = "world_relation_event"


def _empty_batch(*reasons: str) -> PatternEvaluationBatch:
    counts: Counter[str] = Counter()
    for reason in reasons:
        counts[reason] += 1
    return PatternEvaluationBatch(records=(), rejection_counts=dict(counts), source_evidence_ids=())


class SqlitePatternEvaluationSource:
    """URI ``mode=ro`` loader of unlabeled graph-companion evaluation records."""

    def __init__(self, db_path: str | Path, *, macro_root: str | Path | None = None) -> None:
        self.path = Path(db_path)
        self.macro_root = None if macro_root is None else Path(macro_root)

    def load_evaluation_batch(self, request: Any) -> PatternEvaluationBatch:
        if not isinstance(request, PatternEvaluationScanRequest):
            raise TypeError("PatternEvaluationScanRequest is required")
        path = self.path
        if not path.exists():
            return _empty_batch("missing_db")
        try:
            with _readonly_connection(path) as connection:
                tables = _table_names(connection)
                missing = sorted(_REQUIRED_TABLES.difference(tables))
                if missing:
                    return _empty_batch("schema_unavailable")
                return _load_batch(connection, request, self.macro_root)
        except (OSError, sqlite3.Error):
            return _empty_batch("unavailable")


def _preload_evaluation_ledger(connection: sqlite3.Connection, *, macro_root: Path | None) -> _Ledger:
    snapshots = {
        str(row["snapshot_id"]): row for row in connection.execute("SELECT * FROM world_graph_snapshots")
    }
    receipts: dict[tuple[str, str, str], sqlite3.Row] = {}
    for row in connection.execute(
        """
        SELECT *
        FROM world_availability_receipts
        WHERE subject_kind IN (?, ?)
        """,
        (_SNAPSHOT_SUBJECT_KIND, _RELATION_SUBJECT_KIND),
    ):
        receipts[(str(row["subject_kind"]), str(row["subject_id"]), str(row["content_sha256"]))] = row
    members: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for row in connection.execute(
        """
        SELECT *
        FROM world_graph_snapshot_members
        WHERE member_kind IN ('structural_relation', 'knowledge_relation')
        ORDER BY snapshot_id ASC, member_kind ASC, ordinal ASC, member_id ASC
        """
    ):
        members[str(row["snapshot_id"])].append(row)
    relation_events: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for row in connection.execute(
        """
        SELECT *
        FROM world_relation_events
        WHERE event_type = 'world_relation_asserted'
        ORDER BY relation_id ASC, sequence ASC, event_id ASC
        """
    ):
        relation_events[str(row["relation_id"])].append(row)
    return _Ledger(
        snapshots=snapshots,
        receipts=receipts,
        members={key: tuple(rows) for key, rows in members.items()},
        relation_events={key: tuple(rows) for key, rows in relation_events.items()},
        outcomes={},
        macro_observations=_load_macro_observation_corpus(macro_root),
    )


def _load_batch(
    connection: sqlite3.Connection,
    request: PatternEvaluationScanRequest,
    macro_root: Path | None,
) -> PatternEvaluationBatch:
    as_of = request.as_of
    not_before = request.not_before
    rejections: Counter[str] = Counter()
    records: list[PatternEvaluationRecord] = []
    evidence: set[str] = set()
    admitted_ids = _admitted_graph_episode_ids(connection, request, rejections, evidence)
    if not admitted_ids:
        return PatternEvaluationBatch(
            records=(),
            rejection_counts={key: rejections[key] for key in sorted(rejections) if rejections[key]},
            source_evidence_ids=tuple(sorted(evidence)),
        )
    episode_rows = connection.execute(
        f"""
        SELECT *
        FROM world_episodes
        WHERE episode_id IN ({_placeholders(len(admitted_ids))})
          AND feature_contract_version = ?
          AND training_eligible = 1
          AND available_at IS NOT NULL
          AND available_at <= ?
          AND recorded_at <= ?
          AND as_of_bar_ts IS NOT NULL
          AND as_of_bar_ts <= ?
          AND as_of_bar_ts > ?
        ORDER BY as_of_bar_ts ASC, episode_id ASC
        """,
        (
            *admitted_ids,
            GRAPH_FEATURE_CONTRACT_ID,
            as_of.isoformat(),
            as_of.isoformat(),
            as_of.isoformat(),
            not_before.isoformat(),
        ),
    ).fetchall()
    found_ids = {str(row["episode_id"]) for row in episode_rows}
    for episode_id in admitted_ids:
        if episode_id not in found_ids:
            rejections["cohort_episode_not_in_scan_window"] += 1
    if not episode_rows:
        return PatternEvaluationBatch(
            records=(),
            rejection_counts={key: rejections[key] for key in sorted(rejections) if rejections[key]},
            source_evidence_ids=tuple(sorted(evidence)),
        )
    ledger = _preload_evaluation_ledger(connection, macro_root=macro_root)
    for episode_row in episode_rows:
        try:
            built = _record_for_episode(ledger, episode_row, request, rejections, evidence)
        except (OSError, sqlite3.Error):
            raise
        except (TypeError, ValueError, json.JSONDecodeError, KeyError):
            rejections["episode_malformed"] += 1
            continue
        if built is not None:
            records.append(built)
    records.sort(key=lambda item: (item.episode.observation.as_of_bar_ts, item.episode.episode_id))
    return PatternEvaluationBatch(
        records=tuple(records),
        rejection_counts={key: rejections[key] for key in sorted(rejections) if rejections[key]},
        source_evidence_ids=tuple(sorted(evidence)),
    )


def _admitted_graph_episode_ids(
    connection: sqlite3.Connection,
    request: PatternEvaluationScanRequest,
    rejections: Counter[str],
    evidence: set[str],
) -> tuple[str, ...]:
    """Admit GRAPH_FEATURE_CONTRACT_ID refs from cohort slots visible by as_of.

    An empty or missing cohort never falls back to unrelated episodes.
    """

    rows = connection.execute(
        """
        SELECT slot_id, cohort_id, as_of_bar_ts, recorded_at, payload_json
        FROM world_cohort_slots
        WHERE cohort_id = ?
        ORDER BY as_of_bar_ts ASC, slot_id ASC
        """,
        (request.evaluation_cohort_id,),
    ).fetchall()
    if not rows:
        rejections["evaluation_cohort_empty"] += 1
        return ()
    admitted: list[str] = []
    seen: set[str] = set()
    visible_without_graph = 0
    as_of = request.as_of
    for row in rows:
        try:
            recorded_at = _parse_clock(row["recorded_at"], "slot.recorded_at")
            slot_as_of = _parse_clock(row["as_of_bar_ts"], "slot.as_of_bar_ts")
            payload = _json_object(row["payload_json"], "slot.payload_json")
            slot = WorldCohortSlot.from_mapping(payload)
        except (TypeError, ValueError, json.JSONDecodeError, KeyError):
            rejections["slot_malformed"] += 1
            continue
        if str(row["slot_id"]) != slot.slot_id or str(row["cohort_id"]) != slot.cohort_id:
            rejections["slot_payload_mismatch"] += 1
            continue
        if slot.cohort_id != request.evaluation_cohort_id:
            rejections["slot_cohort_mismatch"] += 1
            continue
        if recorded_at is None or slot_as_of is None or not _clocks_not_after(as_of, recorded_at, slot_as_of):
            rejections["slot_not_visible_as_of"] += 1
            continue
        episode_id = slot.episode_refs_by_contract.get(GRAPH_FEATURE_CONTRACT_ID)
        if not isinstance(episode_id, str) or not episode_id.strip():
            rejections["slot_missing_graph_companion"] += 1
            visible_without_graph += 1
            continue
        evidence.add(slot.slot_id)
        if episode_id not in seen:
            seen.add(episode_id)
            admitted.append(episode_id)
    if not admitted and visible_without_graph:
        rejections["evaluation_cohort_without_graph_companion"] += 1
    return tuple(admitted)


def _record_for_episode(
    ledger: _Ledger,
    episode_row: sqlite3.Row,
    request: PatternEvaluationScanRequest,
    rejections: Counter[str],
    evidence: set[str],
) -> PatternEvaluationRecord | None:
    as_of = request.as_of
    not_before = request.not_before
    try:
        payload = _json_object(episode_row["payload_json"], "episode.payload_json")
        episode = WorldEpisode.from_dict(payload)
    except (TypeError, ValueError, json.JSONDecodeError):
        rejections["episode_malformed"] += 1
        return None
    if episode.episode_id != str(episode_row["episode_id"]):
        rejections["episode_payload_mismatch"] += 1
        return None
    if episode.observation.feature_contract_version != GRAPH_FEATURE_CONTRACT_ID:
        rejections["episode_not_graph_companion"] += 1
        return None
    if episode.training_eligible is not True or int(episode_row["training_eligible"]) != 1:
        rejections["episode_not_training_eligible"] += 1
        return None
    episode_available = _parse_clock(episode_row["available_at"], "episode.available_at")
    episode_recorded = _parse_clock(episode_row["recorded_at"], "episode.recorded_at")
    episode_as_of = _parse_clock(episode_row["as_of_bar_ts"], "episode.as_of_bar_ts")
    if episode_as_of != episode.observation.as_of_bar_ts or episode_available != episode.observation.available_at:
        rejections["episode_payload_mismatch"] += 1
        return None
    if episode_as_of is None or not (not_before < episode_as_of <= as_of):
        rejections["episode_outside_scan_window"] += 1
        return None
    if not _clocks_not_after(as_of, episode_available, episode_recorded, episode_as_of):
        rejections["episode_after_as_of"] += 1
        return None
    try:
        snapshot_id = _embedded_snapshot_id(episode.observation)
    except (TypeError, ValueError):
        rejections["embedded_snapshot_id_missing"] += 1
        return None
    snapshot_bundle = _admitted_snapshot(ledger, snapshot_id, as_of, rejections)
    if snapshot_bundle is None:
        return None
    snapshot, _snapshot_row, snapshot_receipt, snapshot_recorded, snapshot_receipt_recorded = snapshot_bundle
    if snapshot.cutoff_at <= not_before:
        rejections["snapshot_not_after_scan_bound"] += 1
        return None
    if snapshot.root_episode_id == episode.episode_id:
        rejections["snapshot_root_is_graph_episode"] += 1
        return None
    structural, knowledge, relation_ids = _hydrate_members(ledger, snapshot, rejections)
    recorded_at = _max_clock(
        episode_recorded,
        snapshot_recorded,
        snapshot_receipt_recorded,
        snapshot_receipt.ready_at,
    )
    available_at = _max_clock(episode_available, snapshot_receipt.ready_at)
    if recorded_at is None or available_at is None or not _clocks_not_after(as_of, recorded_at, available_at):
        rejections["record_clock_after_as_of"] += 1
        return None
    try:
        record = PatternEvaluationRecord(
            episode=episode,
            snapshot=snapshot,
            structural_relations=structural,
            knowledge_relations=knowledge,
            recorded_at=recorded_at,
            available_at=available_at,
            driver_state_bindings=_hydrate_driver_bindings(
                knowledge,
                snapshot,
                structural,
                ledger.macro_observations,
            ),
        )
    except (TypeError, ValueError):
        rejections["record_construction_failed"] += 1
        return None
    evidence.add(episode.episode_id)
    evidence.add(snapshot.snapshot_id)
    evidence.add(snapshot_receipt.receipt_id)
    evidence.update(relation_ids)
    return record


__all__ = ["SqlitePatternEvaluationSource"]
