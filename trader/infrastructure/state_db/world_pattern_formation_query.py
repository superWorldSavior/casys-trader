"""Read-only point-in-time adapter from world_model.db onto PatternFormationSource.

Opens the ledger with URI ``mode=ro`` and ``PRAGMA query_only=ON``. A missing
file stays missing. This module never creates, migrates, or writes the ledger,
never instantiates a store or ``StateDb``, and never reconstructs pattern paths.

Supporting rows are preloaded in a constant number of SELECTs, then evaluated
in process. Admission still uses the same PIT, receipt, hash, and leaf rules.
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

from trader.application.world_model.pattern_discovery_ports import (
    PatternDriverStateBinding,
    PatternFormationBatch,
    PatternFormationRecord,
    TimeSpecificMarketAnchor,
)
from trader.application.world_model.pattern_formation_request import PatternFormationRequest
from trader.domain.world_availability import (
    AvailabilityEvidence,
    PointInTimeEligibilityPolicy,
    WorldAvailabilityReceipt,
    WorldAvailabilitySubjectRef,
    _attest_verified_store_receipt,
    world_subject_content_sha256,
)
from trader.domain.world_driver import DRIVER_OVERLAY_RELATION_KINDS, DriverState
from trader.domain.world_episode import (
    GRAPH_FEATURE_CONTRACT_ID,
    PREDICTION_CLASSES,
    WorldEpisode,
    WorldOutcome,
    canonical_sha256,
    parse_utc_timestamp,
)
from trader.domain.world_graph import (
    KnowledgeArtifactRef,
    KnowledgeWorldRelation,
    KnowledgeWorldRelationAsserted,
    StructuralWorldRelation,
    StructuralWorldRelationAsserted,
    WorldEntityRef,
    WorldGraphSnapshot,
    WorldKnowledgeRelationRef,
    WorldObservationRef,
    parse_world_relation_event,
    reconstruct_macro_observes_provenance,
    world_observation_ref_for_observation_id,
)
from trader.domain.world_macro import (
    MACRO_TRANSFORM_VERSION,
    MACRO_WORLD_OBSERVATION_SUBJECT_KIND,
    MacroWorldObservation,
    is_admitted_macro_producer,
)
from trader.infrastructure.state_db._jsonl_store import read_jsonl_objects
from trader.infrastructure.state_db.availability_receipt import load_receipts, parse_world_availability_receipt
from trader.infrastructure.state_db.world_macro_store import WORLD_MACRO_STORE_ID
from trader.infrastructure.state_db.world_model_store import WORLD_MODEL_STORE_ID

_READONLY_TIMEOUT_S = 5.0
_REQUIRED_TABLES = frozenset(
    {
        "world_availability_receipts",
        "world_episodes",
        "world_graph_snapshot_members",
        "world_graph_snapshots",
        "world_outcome_events",
        "world_relation_events",
    }
)
_ADMITTED_SNAPSHOT_STATUSES = frozenset({"complete", "partial"})
_PREDICTION_CLASS_SET = frozenset(PREDICTION_CLASSES)
_SNAPSHOT_SUBJECT_KIND = "world_graph_snapshot"
_RELATION_SUBJECT_KIND = "world_relation_event"
_SNAPSHOT_TABLE = "world_graph_snapshots"
_RELATION_TABLE = "world_relation_events"
_PIT = PointInTimeEligibilityPolicy()

_ReceiptKey = tuple[str, str, str]
_SnapshotBundle = tuple[WorldGraphSnapshot, sqlite3.Row, WorldAvailabilityReceipt, datetime, datetime]
_MacroJoin = tuple[MacroWorldObservation, WorldAvailabilityReceipt]
_RECEIPT_REF_PREFIX = "world-availability-receipt:v1:"


def _empty_batch(*reasons: str) -> PatternFormationBatch:
    counts: Counter[str] = Counter()
    for reason in reasons:
        counts[reason] += 1
    return PatternFormationBatch(records=(), rejection_counts=dict(counts), source_evidence_ids=())


def _readonly_connection(path: Path) -> sqlite3.Connection:
    uri = f"file:{quote(str(path.resolve()), safe='/')}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=_READONLY_TIMEOUT_S)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def _table_names(connection: sqlite3.Connection) -> set[str]:
    return {str(row[0]) for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _json_object(text: Any, field_name: str) -> dict[str, Any]:
    payload = json.loads(text)
    if not isinstance(payload, Mapping):
        raise ValueError(f"{field_name} must be a JSON object")
    return dict(payload)


def _parse_clock(value: Any, field_name: str) -> datetime | None:
    if value in (None, ""):
        return None
    return parse_utc_timestamp(value, field_name)


def _clocks_not_after(cutoff: datetime, *values: datetime | None) -> bool:
    return all(value is not None and value <= cutoff for value in values)


def _max_clock(*values: datetime | None) -> datetime | None:
    present = [value for value in values if value is not None]
    return max(present) if present else None


def _embedded_snapshot_id(observation: Any) -> str:
    features = getattr(observation, "graph_features", None)
    if isinstance(features, Mapping):
        nested = features.get("snapshot")
        if isinstance(nested, Mapping):
            raw = nested.get("snapshot_id")
            if isinstance(raw, str) and raw.strip():
                return raw.strip()
        raw = features.get("snapshot_id")
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
    graph = getattr(observation, "graph", None)
    snapshot_id = getattr(graph, "snapshot_id", None)
    if isinstance(snapshot_id, str) and snapshot_id.strip():
        return snapshot_id.strip()
    if isinstance(graph, Mapping):
        raw = graph.get("snapshot_id")
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
    raise ValueError("graph episode must embed snapshot_id on observation.graph_features.snapshot")


def _placeholders(count: int) -> str:
    return ",".join("?" for _ in range(count))


class _Ledger:
    """Indexed point-in-time rows loaded by a constant SELECT budget."""

    def __init__(
        self,
        *,
        snapshots: Mapping[str, sqlite3.Row],
        receipts: Mapping[_ReceiptKey, sqlite3.Row],
        members: Mapping[str, tuple[sqlite3.Row, ...]],
        relation_events: Mapping[str, tuple[sqlite3.Row, ...]],
        outcomes: Mapping[str, tuple[sqlite3.Row, ...]],
        macro_observations: Mapping[str, _MacroJoin],
    ) -> None:
        self.snapshots = snapshots
        self.receipts = receipts
        self.members = members
        self.relation_events = relation_events
        self.outcomes = outcomes
        self.macro_observations = macro_observations
        self._snapshot_eval: dict[str, tuple[str, _SnapshotBundle | None]] = {}
        self._parsed_events: dict[str, Any] = {}


class SqlitePatternFormationSource:
    """URI ``mode=ro`` loader of labeled graph-companion formation records."""

    def __init__(self, db_path: str | Path, *, macro_root: str | Path | None = None) -> None:
        self.path = Path(db_path)
        self.macro_root = None if macro_root is None else Path(macro_root)

    def load_formation_batch(self, request: PatternFormationRequest) -> PatternFormationBatch:
        if not isinstance(request, PatternFormationRequest):
            raise TypeError("PatternFormationRequest is required")
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


def _preload_ledger(
    connection: sqlite3.Connection,
    *,
    horizons: Sequence[str],
    cutoff: datetime,
    macro_root: Path | None,
) -> _Ledger:
    snapshots = {
        str(row["snapshot_id"]): row for row in connection.execute("SELECT * FROM world_graph_snapshots")
    }
    receipts: dict[_ReceiptKey, sqlite3.Row] = {}
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
    outcomes: dict[str, list[sqlite3.Row]] = defaultdict(list)
    outcome_sql = f"""
        SELECT *
        FROM world_outcome_events
        WHERE horizon_code IN ({_placeholders(len(horizons))})
          AND recorded_at <= ?
        ORDER BY episode_id ASC, horizon_code ASC, recorded_at ASC, outcome_event_id ASC
        """
    for row in connection.execute(outcome_sql, (*horizons, cutoff.isoformat())):
        outcomes[str(row["episode_id"])].append(row)
    return _Ledger(
        snapshots=snapshots,
        receipts=receipts,
        members={key: tuple(rows) for key, rows in members.items()},
        relation_events={key: tuple(rows) for key, rows in relation_events.items()},
        outcomes={key: tuple(rows) for key, rows in outcomes.items()},
        macro_observations=_load_macro_observation_corpus(macro_root),
    )


def _load_batch(
    connection: sqlite3.Connection,
    request: PatternFormationRequest,
    macro_root: Path | None,
) -> PatternFormationBatch:
    cutoff = request.formation_cutoff
    rejections: Counter[str] = Counter()
    records: list[PatternFormationRecord] = []
    evidence: set[str] = set()
    episode_rows = connection.execute(
        """
        SELECT *
        FROM world_episodes
        WHERE feature_contract_version = ?
          AND training_eligible = 1
          AND available_at IS NOT NULL
          AND available_at <= ?
          AND recorded_at <= ?
          AND as_of_bar_ts IS NOT NULL
          AND as_of_bar_ts <= ?
        ORDER BY as_of_bar_ts ASC, episode_id ASC
        """,
        (GRAPH_FEATURE_CONTRACT_ID, cutoff.isoformat(), cutoff.isoformat(), cutoff.isoformat()),
    ).fetchall()
    if not episode_rows:
        return PatternFormationBatch(records=(), rejection_counts={}, source_evidence_ids=())
    ledger = _preload_ledger(connection, horizons=request.horizons, cutoff=cutoff, macro_root=macro_root)
    for episode_row in episode_rows:
        try:
            built = _record_for_episode(ledger, episode_row, request, rejections, evidence)
        except (OSError, sqlite3.Error):
            raise
        except (TypeError, ValueError, json.JSONDecodeError, KeyError):
            rejections["episode_malformed"] += 1
            continue
        records.extend(built)
    records.sort(
        key=lambda item: (
            item.market_anchor.as_of_bar_ts,
            item.episode.episode_id,
            item.market_anchor.horizon_id,
            item.snapshot.snapshot_id,
            item.outcome.event_id,
        )
    )
    return PatternFormationBatch(
        records=tuple(records),
        rejection_counts={key: rejections[key] for key in sorted(rejections) if rejections[key]},
        source_evidence_ids=tuple(sorted(evidence)),
    )


def _record_for_episode(
    ledger: _Ledger,
    episode_row: sqlite3.Row,
    request: PatternFormationRequest,
    rejections: Counter[str],
    evidence: set[str],
) -> tuple[PatternFormationRecord, ...]:
    cutoff = request.formation_cutoff
    try:
        payload = _json_object(episode_row["payload_json"], "episode.payload_json")
        episode = WorldEpisode.from_dict(payload)
    except (TypeError, ValueError, json.JSONDecodeError):
        rejections["episode_malformed"] += 1
        return ()
    if episode.episode_id != str(episode_row["episode_id"]):
        rejections["episode_payload_mismatch"] += 1
        return ()
    if episode.observation.feature_contract_version != GRAPH_FEATURE_CONTRACT_ID:
        rejections["episode_not_graph_companion"] += 1
        return ()
    if episode.training_eligible is not True or int(episode_row["training_eligible"]) != 1:
        rejections["episode_not_training_eligible"] += 1
        return ()
    episode_available = _parse_clock(episode_row["available_at"], "episode.available_at")
    episode_recorded = _parse_clock(episode_row["recorded_at"], "episode.recorded_at")
    episode_as_of = _parse_clock(episode_row["as_of_bar_ts"], "episode.as_of_bar_ts")
    if episode_as_of != episode.observation.as_of_bar_ts or episode_available != episode.observation.available_at:
        rejections["episode_payload_mismatch"] += 1
        return ()
    if not _clocks_not_after(cutoff, episode_available, episode_recorded, episode_as_of):
        rejections["episode_after_formation_cutoff"] += 1
        return ()
    try:
        snapshot_id = _embedded_snapshot_id(episode.observation)
    except (TypeError, ValueError):
        rejections["embedded_snapshot_id_missing"] += 1
        return ()
    snapshot_bundle = _admitted_snapshot(ledger, snapshot_id, cutoff, rejections)
    if snapshot_bundle is None:
        return ()
    snapshot, snapshot_row, snapshot_receipt, snapshot_recorded, snapshot_receipt_recorded = snapshot_bundle
    if snapshot.root_episode_id == episode.episode_id:
        rejections["snapshot_root_is_graph_episode"] += 1
        return ()
    structural, knowledge, relation_ids = _hydrate_members(ledger, snapshot, rejections)
    outcomes = _active_outcomes_as_of(
        ledger.outcomes.get(episode.episode_id, ()),
        episode.episode_id,
        request.horizons,
        cutoff,
        rejections,
    )
    built: list[PatternFormationRecord] = []
    for outcome, outcome_row in outcomes:
        outcome_recorded = _parse_clock(outcome_row["recorded_at"], "outcome.recorded_at")
        recorded_at = _max_clock(
            episode_recorded,
            snapshot_recorded,
            snapshot_receipt_recorded,
            snapshot_receipt.ready_at,
            outcome_recorded,
        )
        available_at = _max_clock(
            episode_available,
            snapshot_receipt.ready_at,
            outcome.available_at,
            _parse_clock(outcome_row["label_available_at"], "outcome.label_available_at"),
        )
        if recorded_at is None or available_at is None or not _clocks_not_after(cutoff, recorded_at, available_at):
            rejections["record_clock_after_formation_cutoff"] += 1
            continue
        try:
            record = PatternFormationRecord(
                episode=episode,
                snapshot=snapshot,
                structural_relations=structural,
                knowledge_relations=knowledge,
                outcome=outcome,
                recorded_at=recorded_at,
                available_at=available_at,
                market_anchor=TimeSpecificMarketAnchor(
                    venue=episode.observation.venue,
                    symbol=episode.observation.symbol,
                    bar_interval=episode.observation.bar_interval,
                    as_of_bar_ts=episode.observation.as_of_bar_ts,
                    horizon_id=outcome.horizon.horizon_id,
                ),
                driver_state_bindings=_hydrate_driver_bindings(
                    knowledge,
                    snapshot,
                    structural,
                    ledger.macro_observations,
                ),
            )
        except (TypeError, ValueError):
            rejections["record_construction_failed"] += 1
            continue
        built.append(record)
        evidence.add(episode.episode_id)
        evidence.add(snapshot.snapshot_id)
        evidence.add(outcome.event_id)
        evidence.add(snapshot_receipt.receipt_id)
        evidence.update(relation_ids)
    return tuple(built)


def _resolve_macro_path(root: Path, relative: str) -> Path | None:
    text = str(relative or "").strip()
    if not text or ".." in text:
        return None
    candidate_rel = Path(text)
    if candidate_rel.is_absolute() or candidate_rel.anchor or ".." in candidate_rel.parts:
        return None
    resolved_root = root.resolve()
    candidate = (resolved_root / candidate_rel).resolve()
    if not candidate.is_relative_to(resolved_root):
        return None
    return candidate


def _index_macro_history_payloads(history: Path) -> dict[str, dict[str, Any]]:
    if not history.is_file():
        return {}
    indexed: dict[str, dict[str, Any]] = {}
    for row in read_jsonl_objects(history):
        indexed.setdefault(world_subject_content_sha256(row), dict(row))
    return indexed


def _iter_macro_observation_receipts(root: Path) -> tuple[WorldAvailabilityReceipt, ...]:
    receipts: list[WorldAvailabilityReceipt] = []
    for path in sorted(root.rglob("*.jsonl")):
        if not path.is_file() or path.parent.name != "availability_receipts":
            continue
        for row in load_receipts(path):
            parsed = parse_world_availability_receipt(row)
            if parsed is None:
                continue
            if parsed.subject.kind != MACRO_WORLD_OBSERVATION_SUBJECT_KIND:
                continue
            if parsed.storage_locator.store_id != WORLD_MACRO_STORE_ID:
                continue
            receipts.append(parsed)
    return tuple(receipts)


def _load_macro_observation_corpus(root: Path | None) -> Mapping[str, _MacroJoin]:
    """Read-only attested observation corpus. Never creates directories or files."""

    if root is None or not root.exists() or not root.is_dir():
        return {}
    grouped: dict[str, list[WorldAvailabilityReceipt]] = {}
    locators: dict[str, None] = {}
    for receipt in _iter_macro_observation_receipts(root):
        grouped.setdefault(receipt.subject.subject_id, []).append(receipt)
        locator = receipt.storage_locator
        if locator.kind == "jsonl" and locator.store_id == WORLD_MACRO_STORE_ID and locator.path:
            locators[str(locator.path)] = None
    payloads_by_locator = {
        locator_path: _index_macro_history_payloads(history)
        for locator_path in locators
        if (history := _resolve_macro_path(root, locator_path)) is not None
    }
    corpus: dict[str, _MacroJoin] = {}
    for receipts in grouped.values():
        if len(receipts) != 1:
            continue
        receipt = receipts[0]
        locator = receipt.storage_locator
        if locator.kind != "jsonl" or locator.store_id != WORLD_MACRO_STORE_ID or not locator.path:
            continue
        payload = payloads_by_locator.get(str(locator.path), {}).get(receipt.subject.content_sha256)
        if payload is None:
            continue
        if str(payload.get("observation_id") or "") != receipt.subject.subject_id:
            continue
        try:
            observation = MacroWorldObservation.from_mapping(payload)
            attested = _attest_verified_store_receipt(
                receipt,
                expected_subject=receipt.subject,
                expected_scope=receipt.scope,
                expected_locator=locator,
            )
        except (TypeError, ValueError):
            continue
        if observation.observation_id != attested.subject.subject_id:
            continue
        if observation.content_sha256 != attested.subject.content_sha256:
            continue
        if attested.scope != f"{observation.scope.kind}:{observation.scope.entity_id}":
            continue
        corpus[observation.observation_id] = (observation, attested)
    return corpus


def _unique_evidence(*values: str | None) -> tuple[str, ...]:
    seen: list[str] = []
    for raw in values:
        if not isinstance(raw, str):
            continue
        text = raw.strip()
        if not text or text in seen:
            continue
        seen.append(text)
    return tuple(seen)


def _receipt_ref_from_source_refs(refs: Sequence[str]) -> tuple[str, str] | None:
    for token in refs:
        if not isinstance(token, str) or not token.startswith(_RECEIPT_REF_PREFIX) or "/" not in token:
            continue
        left, _separator, digest = token.rpartition("/")
        if not left or len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            continue
        return left, digest
    return None


def _missing_observes_binding(
    relation: KnowledgeWorldRelation,
    missingness: str,
    *evidence: str | None,
) -> PatternDriverStateBinding:
    return PatternDriverStateBinding(
        relation_id=relation.relation_id,
        driver_state=DriverState.missing(
            missingness=missingness,
            source_family="macro_observation",
            artifact_kind="macro_world_observation",
        ),
        evidence_refs=_unique_evidence(*evidence),
    )


def _bind_about_relation(relation: KnowledgeWorldRelation) -> PatternDriverStateBinding:
    source = relation.source
    if isinstance(source, KnowledgeArtifactRef):
        return PatternDriverStateBinding(
            relation_id=relation.relation_id,
            driver_state=DriverState.unspecified(source_family="knowledge_artifact"),
            evidence_refs=_unique_evidence(source.artifact_id, source.content_sha256, *relation.source_refs),
        )
    return PatternDriverStateBinding(
        relation_id=relation.relation_id,
        driver_state=DriverState.missing(
            missingness="artifact_unjoined",
            source_family="knowledge_artifact",
        ),
        evidence_refs=_unique_evidence(*relation.source_refs),
    )


def _bind_observes_relation(
    relation: KnowledgeWorldRelation,
    snapshot: WorldGraphSnapshot,
    structural: Sequence[StructuralWorldRelation],
    corpus: Mapping[str, _MacroJoin],
) -> PatternDriverStateBinding:
    cutoff = snapshot.cutoff_at
    try:
        provenance = reconstruct_macro_observes_provenance(
            relation,
            root=snapshot.root_entity if isinstance(snapshot.root_entity, WorldEntityRef) else None,
            structural_relations=structural,
        )
    except (TypeError, ValueError):
        return _missing_observes_binding(relation, "observation_unjoined", *relation.source_refs)
    evidence = (
        provenance.observation_id,
        f"{provenance.observation_id}/{provenance.observation_sha256}",
        *relation.source_refs,
    )
    joined = corpus.get(provenance.observation_id)
    if joined is None:
        return _missing_observes_binding(relation, "observation_unjoined", *evidence)
    observation, receipt = joined
    evidence = (*evidence, receipt.receipt_id)
    if (
        observation.content_sha256 != provenance.observation_sha256
        or receipt.subject.content_sha256 != observation.content_sha256
        or receipt.subject.content_sha256 != provenance.observation_sha256
    ):
        return _missing_observes_binding(relation, "observation_hash_mismatch", *evidence)
    if not is_admitted_macro_producer(observation.producer_version):
        return _missing_observes_binding(relation, "producer_not_admitted", *evidence)
    if observation.transform_version != MACRO_TRANSFORM_VERSION:
        return _missing_observes_binding(relation, "observation_unjoined", *evidence)
    if not isinstance(relation.target, WorldEntityRef):
        return _missing_observes_binding(relation, "observation_unjoined", *evidence)
    if observation.scope.kind != relation.target.kind or observation.scope.entity_id != relation.target.entity_id:
        return _missing_observes_binding(relation, "observation_unjoined", *evidence)
    if relation.effective_from != observation.cutoff_at or relation.effective_until != observation.valid_until:
        return _missing_observes_binding(relation, "observation_unjoined", *evidence)
    if not relation.effective_at(cutoff):
        return _missing_observes_binding(relation, "observation_unjoined", *evidence)
    if observation.cutoff_at > cutoff:
        return _missing_observes_binding(relation, "observation_unjoined", *evidence)
    if observation.valid_until is not None and cutoff >= observation.valid_until:
        return _missing_observes_binding(relation, "observation_unjoined", *evidence)
    if receipt.ready_at is None or receipt.ready_at > cutoff:
        return _missing_observes_binding(relation, "observation_unjoined", *evidence)
    source_receipt = _receipt_ref_from_source_refs(relation.source_refs)
    if source_receipt is not None and source_receipt != (receipt.receipt_id, receipt.receipt_sha256):
        return _missing_observes_binding(relation, "observation_unjoined", *evidence)
    if not isinstance(relation.source, WorldObservationRef):
        return _missing_observes_binding(relation, "observation_unjoined", *evidence)
    if world_observation_ref_for_observation_id(observation.observation_id) != relation.source:
        return _missing_observes_binding(relation, "observation_unjoined", *evidence)
    try:
        driver = DriverState.from_observation(observation)
    except (TypeError, ValueError):
        return _missing_observes_binding(relation, "observation_unjoined", *evidence)
    return PatternDriverStateBinding(
        relation_id=relation.relation_id,
        driver_state=driver,
        evidence_refs=_unique_evidence(*evidence),
    )


def _hydrate_driver_bindings(
    knowledge: Sequence[KnowledgeWorldRelation],
    snapshot: WorldGraphSnapshot,
    structural: Sequence[StructuralWorldRelation],
    corpus: Mapping[str, _MacroJoin],
) -> tuple[PatternDriverStateBinding, ...]:
    admitted = {ref.relation_id for ref in snapshot.knowledge_relation_refs}
    bindings: list[PatternDriverStateBinding] = []
    for relation in knowledge:
        if relation.kind not in DRIVER_OVERLAY_RELATION_KINDS or relation.relation_id not in admitted:
            continue
        if relation.kind == "ABOUT":
            bindings.append(_bind_about_relation(relation))
            continue
        bindings.append(_bind_observes_relation(relation, snapshot, structural, corpus))
    bindings.sort(key=lambda item: item.relation_id)
    return tuple(bindings)


def _admitted_snapshot(
    ledger: _Ledger,
    snapshot_id: str,
    cutoff: datetime,
    rejections: Counter[str],
) -> _SnapshotBundle | None:
    cached = ledger._snapshot_eval.get(snapshot_id)
    if cached is None:
        cached = _evaluate_snapshot(ledger, snapshot_id, cutoff)
        ledger._snapshot_eval[snapshot_id] = cached
    reason, bundle = cached
    if bundle is None:
        rejections[reason] += 1
        return None
    return bundle


def _evaluate_snapshot(
    ledger: _Ledger,
    snapshot_id: str,
    cutoff: datetime,
) -> tuple[str, _SnapshotBundle | None]:
    row = ledger.snapshots.get(snapshot_id)
    if row is None:
        return "snapshot_missing", None
    try:
        payload = _json_object(row["payload_json"], "snapshot.payload_json")
        snapshot = WorldGraphSnapshot.from_mapping(payload)
        recorded_at = parse_utc_timestamp(row["recorded_at"], "snapshot.recorded_at")
        cutoff_at = parse_utc_timestamp(row["cutoff_at"], "snapshot.cutoff_at")
    except (TypeError, ValueError, json.JSONDecodeError):
        return "snapshot_malformed", None
    if snapshot.snapshot_id != str(row["snapshot_id"]) or snapshot.root_episode_id != str(row["root_episode_id"]):
        return "snapshot_payload_mismatch", None
    if snapshot.cutoff_at != cutoff_at or snapshot.status != str(row["status"]):
        return "snapshot_payload_mismatch", None
    if canonical_sha256(snapshot.to_dict()) != str(row["payload_sha256"]):
        return "snapshot_payload_mismatch", None
    if snapshot.status not in _ADMITTED_SNAPSHOT_STATUSES:
        return "snapshot_not_admitted", None
    if not _clocks_not_after(cutoff, snapshot.cutoff_at, recorded_at):
        return "snapshot_after_formation_cutoff", None
    payload_digest = str(row["payload_sha256"])
    receipt_row = ledger.receipts.get((_SNAPSHOT_SUBJECT_KIND, snapshot.snapshot_id, payload_digest))
    receipt = _store_attested_receipt(receipt_row, expected_table=_SNAPSHOT_TABLE)
    if receipt is None or receipt_row is None:
        return "snapshot_receipt_unproven", None
    receipt_recorded = _parse_clock(receipt_row["recorded_at"], "snapshot_receipt.recorded_at")
    if not _clocks_not_after(cutoff, receipt.ready_at, receipt_recorded):
        return "snapshot_receipt_unproven", None
    if (
        receipt.subject.kind != _SNAPSHOT_SUBJECT_KIND
        or receipt.subject.subject_id != snapshot.snapshot_id
        or receipt.subject.content_sha256 != payload_digest
    ):
        return "snapshot_receipt_unproven", None
    return "ok", (snapshot, row, receipt, recorded_at, receipt_recorded)


def _store_attested_receipt(row: sqlite3.Row | None, *, expected_table: str) -> WorldAvailabilityReceipt | None:
    if row is None:
        return None
    try:
        payload = _json_object(row["payload_json"], "receipt.payload_json")
        receipt = WorldAvailabilityReceipt.from_mapping(payload)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if (
        receipt.receipt_id != str(row["receipt_id"])
        or receipt.receipt_sha256 != str(row["receipt_sha256"])
        or receipt.subject.kind != str(row["subject_kind"])
        or receipt.subject.subject_id != str(row["subject_id"])
        or receipt.subject.content_sha256 != str(row["content_sha256"])
    ):
        return None
    locator = receipt.storage_locator
    if locator.kind != "sqlite" or locator.store_id != WORLD_MODEL_STORE_ID or locator.table != expected_table:
        return None
    try:
        return _attest_verified_store_receipt(
            receipt,
            expected_subject=WorldAvailabilitySubjectRef(
                kind=receipt.subject.kind,
                subject_id=receipt.subject.subject_id,
                content_sha256=receipt.subject.content_sha256,
            ),
            expected_scope=receipt.scope,
            expected_locator=locator,
        )
    except (TypeError, ValueError):
        return None


def _hydrate_members(
    ledger: _Ledger,
    snapshot: WorldGraphSnapshot,
    rejections: Counter[str],
) -> tuple[tuple[StructuralWorldRelation, ...], tuple[KnowledgeWorldRelation, ...], set[str]]:
    structural: list[StructuralWorldRelation] = []
    knowledge: list[KnowledgeWorldRelation] = []
    evidence: set[str] = set()
    seen_structural: set[str] = set()
    seen_knowledge: set[str] = set()
    for member in ledger.members.get(snapshot.snapshot_id, ()):
        try:
            relation, event_id, receipt_id = _hydrate_relation_member(ledger, snapshot, member, rejections)
        except (TypeError, ValueError, json.JSONDecodeError, KeyError):
            rejections["relation_malformed"] += 1
            continue
        if relation is None:
            continue
        if isinstance(relation, StructuralWorldRelation):
            if relation.relation_id in seen_structural:
                continue
            seen_structural.add(relation.relation_id)
            structural.append(relation)
        else:
            if relation.relation_id in seen_knowledge:
                continue
            seen_knowledge.add(relation.relation_id)
            knowledge.append(relation)
        evidence.add(event_id)
        evidence.add(receipt_id)
    structural.sort(key=lambda item: item.relation_id)
    knowledge.sort(key=lambda item: item.relation_id)
    return tuple(structural), tuple(knowledge), evidence


def _parse_relation_event(ledger: _Ledger, row: sqlite3.Row) -> Any | BaseException:
    event_id = str(row["event_id"])
    cached = ledger._parsed_events.get(event_id)
    if cached is not None:
        return cached
    try:
        parsed = parse_world_relation_event(_json_object(row["payload_json"], "relation.payload_json"))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        ledger._parsed_events[event_id] = exc
        return exc
    ledger._parsed_events[event_id] = parsed
    return parsed


def _hydrate_relation_member(
    ledger: _Ledger,
    snapshot: WorldGraphSnapshot,
    member: sqlite3.Row,
    rejections: Counter[str],
) -> tuple[StructuralWorldRelation | KnowledgeWorldRelation | None, str, str]:
    member_kind = str(member["member_kind"])
    relation_id = str(member["member_id"])
    member_hash = str(member["content_sha256"] or "")
    matched: list[tuple[Any, sqlite3.Row]] = []
    for row in ledger.relation_events.get(relation_id, ()):
        event = _parse_relation_event(ledger, row)
        if isinstance(event, BaseException):
            rejections["relation_malformed"] += 1
            continue
        relation = getattr(event, "relation", None)
        if relation is None or getattr(relation, "content_sha256", None) != member_hash:
            continue
        if getattr(relation, "relation_id", None) != relation_id:
            continue
        matched.append((event, row))
    if not matched:
        rejections["relation_content_mismatch"] += 1
        return None, "", ""
    if len(matched) > 1:
        rejections["relation_ambiguous"] += 1
        return None, "", ""
    event, row = matched[0]
    if not isinstance(event, (StructuralWorldRelationAsserted, KnowledgeWorldRelationAsserted)):
        rejections["relation_unproven"] += 1
        return None, "", ""
    relation = event.relation
    expected_family = "structural" if member_kind == "structural_relation" else "knowledge"
    if str(row["family"]) != expected_family or event.family != expected_family:
        rejections["relation_content_mismatch"] += 1
        return None, "", ""
    event_recorded = _parse_clock(row["recorded_at"], "relation.recorded_at")
    if event_recorded is None or event_recorded > snapshot.cutoff_at:
        rejections["relation_unproven"] += 1
        return None, "", ""
    receipt_row = ledger.receipts.get((_RELATION_SUBJECT_KIND, str(row["event_id"]), str(row["payload_sha256"])))
    receipt = _store_attested_receipt(receipt_row, expected_table=_RELATION_TABLE)
    if receipt is None or receipt_row is None:
        rejections["relation_unproven"] += 1
        return None, "", ""
    receipt_recorded = _parse_clock(receipt_row["recorded_at"], "relation_receipt.recorded_at")
    first_seen = receipt_recorded or receipt.ready_at
    if first_seen is None:
        rejections["relation_unproven"] += 1
        return None, "", ""
    evidence = AvailabilityEvidence(receipt=receipt, first_seen_at=first_seen)
    if not _PIT.is_eligible(evidence=evidence, cutoff_at=snapshot.cutoff_at):
        rejections["relation_unproven"] += 1
        return None, "", ""
    if not relation.effective_at(snapshot.cutoff_at):
        rejections["relation_not_effective"] += 1
        return None, "", ""
    if member_kind == "knowledge_relation":
        try:
            member_payload = _json_object(member["payload_json"], "member.payload_json")
            ref = WorldKnowledgeRelationRef.from_mapping(member_payload)
        except (TypeError, ValueError, json.JSONDecodeError):
            rejections["relation_malformed"] += 1
            return None, "", ""
        if ref.availability_receipt_id != receipt.receipt_id:
            rejections["knowledge_receipt_mismatch"] += 1
            return None, "", ""
        if ref.relation_id != relation.relation_id or ref.content_sha256 != relation.content_sha256:
            rejections["relation_content_mismatch"] += 1
            return None, "", ""
    return relation, str(row["event_id"]), receipt.receipt_id


def _active_outcomes_as_of(
    rows: Sequence[sqlite3.Row],
    episode_id: str,
    horizons: Sequence[str],
    cutoff: datetime,
    rejections: Counter[str],
) -> tuple[tuple[WorldOutcome, sqlite3.Row], ...]:
    visible_ids = {str(row["outcome_event_id"]) for row in rows}
    superseded: set[str] = set()
    for row in rows:
        predecessor = row["supersedes_outcome_event_id"]
        if predecessor in (None, ""):
            continue
        predecessor_id = str(predecessor)
        if predecessor_id in visible_ids:
            superseded.add(predecessor_id)
    leaves_by_horizon: dict[str, list[sqlite3.Row]] = {horizon: [] for horizon in horizons}
    for row in rows:
        if str(row["outcome_event_id"]) in superseded:
            continue
        horizon = str(row["horizon_code"])
        if horizon in leaves_by_horizon:
            leaves_by_horizon[horizon].append(row)
    selected: list[tuple[WorldOutcome, sqlite3.Row]] = []
    for horizon in horizons:
        leaves = leaves_by_horizon[horizon]
        if not leaves:
            continue
        if len(leaves) > 1:
            rejections["outcome_ambiguous_leaf"] += 1
            continue
        parsed = _parse_authoritative_outcome(leaves[0], episode_id, horizon, cutoff, rejections)
        if parsed is not None:
            selected.append(parsed)
    return tuple(selected)


def _indexed_outcome_is_active_observed(row: sqlite3.Row, cutoff: datetime) -> bool:
    label_available = _parse_clock(row["label_available_at"], "outcome.label_available_at")
    computed = _parse_clock(row["sealed_at"], "outcome.sealed_at")
    recorded_at = _parse_clock(row["recorded_at"], "outcome.recorded_at")
    move_class = str(row["move_class"] or "")
    return (
        str(row["status"]) == "observed"
        and int(row["training_eligible"]) == 1
        and move_class in _PREDICTION_CLASS_SET
        and _clocks_not_after(cutoff, label_available, computed, recorded_at)
    )


def _parse_authoritative_outcome(
    row: sqlite3.Row,
    episode_id: str,
    horizon: str,
    cutoff: datetime,
    rejections: Counter[str],
) -> tuple[WorldOutcome, sqlite3.Row] | None:
    if not _indexed_outcome_is_active_observed(row, cutoff):
        rejections["outcome_not_active_observed"] += 1
        return None
    try:
        payload = _json_object(row["payload_json"], "outcome.payload_json")
        outcome = WorldOutcome.from_dict(payload)
    except (TypeError, ValueError, json.JSONDecodeError):
        rejections["outcome_malformed"] += 1
        return None
    label_available = _parse_clock(row["label_available_at"], "outcome.label_available_at")
    computed = _parse_clock(row["sealed_at"], "outcome.sealed_at")
    move_class = str(row["move_class"] or "")
    if (
        outcome.episode_id != episode_id
        or outcome.episode_id != str(row["episode_id"])
        or outcome.horizon.horizon_id != horizon
        or outcome.horizon.horizon_id != str(row["horizon_code"])
        or outcome.status != str(row["status"])
        or outcome.event_id != str(row["outcome_event_id"])
        or bool(int(row["training_eligible"])) != bool(outcome.training_eligible)
        or outcome.available_at != label_available
        or outcome.computed_at != computed
        or (outcome.supersedes_event_id or None) != (row["supersedes_outcome_event_id"] or None)
        or outcome.direction != move_class
    ):
        rejections["outcome_payload_mismatch"] += 1
        return None
    if (
        outcome.status != "observed"
        or outcome.training_eligible is not True
        or int(row["training_eligible"]) != 1
        or move_class not in _PREDICTION_CLASS_SET
        or not _clocks_not_after(cutoff, label_available, computed, _parse_clock(row["recorded_at"], "outcome.recorded_at"))
    ):
        rejections["outcome_not_active_observed"] += 1
        return None
    return outcome, row


__all__ = ["SqlitePatternFormationSource"]
