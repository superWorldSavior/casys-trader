from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

import pytest

from tests.application.test_world_pattern_evaluation import COHORT_ID
from tests.package_layout._helpers import REPO_ROOT
from tests.state_db.test_world_pattern_formation_query import (
    SNAPSHOT_READY,
    _graph_episode,
    _market_episode,
    _news_about_triplet,
    _seed_labeled,
    _snapshot,
    _structural,
)
from trader.application.world_model.pattern_evaluation_request import PatternEvaluationScanRequest
from trader.domain.world_cohort import WorldCohortSlot
from trader.domain.world_episode import GRAPH_FEATURE_CONTRACT_ID, WorldEpisode, canonical_sha256
from trader.domain.world_graph import StructuralWorldRelationAsserted
from trader.infrastructure.state_db import world_pattern_evaluation_query as evaluation_query
from trader.infrastructure.state_db.world_graph_store import WorldGraphStore
from trader.infrastructure.state_db.world_knowledge_store import WorldKnowledgeStore
from trader.infrastructure.state_db.world_model_store import WorldModelStore
from trader.infrastructure.state_db.world_pattern_evaluation_query import SqlitePatternEvaluationSource


UTC = timezone.utc
NOT_BEFORE = datetime(2026, 9, 2, tzinfo=UTC)
AS_OF = datetime(2026, 9, 10, tzinfo=UTC)
POST = datetime(2026, 9, 3, tzinfo=UTC)
PRE = datetime(2026, 8, 25, tzinfo=UTC)
OTHER_COHORT = "world_cohort:v1:" + "d" * 64
_QUERY = REPO_ROOT / "trader" / "infrastructure" / "state_db" / "world_pattern_evaluation_query.py"


def _scan(**overrides: object) -> PatternEvaluationScanRequest:
    values: dict[str, object] = {
        "as_of": AS_OF,
        "not_before": NOT_BEFORE,
        "evaluation_cohort_id": COHORT_ID,
    }
    values.update(overrides)
    return PatternEvaluationScanRequest(**values)  # type: ignore[arg-type]


def _insert_evaluation_slot(
    db_path: Path,
    *,
    cohort_id: str,
    episode_id: str,
    as_of: datetime,
    recorded_at: datetime | None = None,
    symbol: str = "2330",
    extra_refs: dict[str, str] | None = None,
) -> str:
    refs = {GRAPH_FEATURE_CONTRACT_ID: episode_id}
    if extra_refs:
        refs = {**extra_refs, **refs}
    slot = WorldCohortSlot(
        cohort_id=cohort_id,
        manifest_sha256="a" * 64,
        venue="TW",
        symbol=symbol,
        bar_interval="1h",
        as_of_bar_ts=as_of,
        anchor_end_at=as_of,
        comparison_batch_id="batch:v1:eval",
        episode_refs_by_contract=refs,
        expected_lane_ids=("graph.pilot",),
        feature_contract_fingerprints={"graph.pilot": "b" * 64},
        feature_mask_fingerprints={"graph.pilot": "c" * 64},
        started_event_id="world_cohort_event:v1:" + "e" * 64,
    )
    payload = slot.to_dict()
    recorded = (recorded_at or as_of).isoformat()
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO world_cohort_slots (
                slot_id, cohort_id, event_id, manifest_sha256, venue, symbol, bar_interval,
                as_of_bar_ts, anchor_end_at, comparison_batch_id, started_event_id,
                payload_json, payload_sha256, recorded_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                slot.slot_id,
                slot.cohort_id,
                slot.started_event_id,
                slot.manifest_sha256,
                slot.venue,
                slot.symbol,
                slot.bar_interval,
                payload["as_of_bar_ts"],
                payload["anchor_end_at"],
                slot.comparison_batch_id,
                slot.started_event_id,
                json.dumps(payload),
                canonical_sha256(payload),
                recorded,
            ),
        )
        connection.commit()
    return slot.slot_id


def _add_graph_companion(seeded: dict[str, object], *, symbol: str, as_of: datetime) -> WorldEpisode:
    graph: WorldGraphStore = seeded["graph"]  # type: ignore[assignment]
    world: WorldModelStore = seeded["world"]  # type: ignore[assignment]
    market = _market_episode(symbol=symbol, as_of=as_of)
    world.append_episode(market)
    traded = _structural(symbol=symbol)
    graph.append_structural_relation_event(StructuralWorldRelationAsserted(relation=traded))
    about = seeded["knowledge"]
    snapshot = _snapshot(
        market,
        structural=(traded,),
        knowledge=() if about is None else (about,),
        knowledge_receipt_id=str(seeded["knowledge_receipt_id"] or ""),
        cutoff_at=as_of,
    )
    graph._clock = lambda: SNAPSHOT_READY
    graph.append(snapshot)
    episode = _graph_episode(market=market, snapshot=snapshot)
    world.append_episode(episode)
    return episode


def test_evaluation_query_is_readonly_and_never_names_outcomes() -> None:
    source = _QUERY.read_text(encoding="utf-8")
    assert "mode=ro" in source
    assert "query_only=ON" in source
    assert "world_cohort_slots" in source
    assert "WorldOutcome" not in source
    assert "world_outcome_events" not in source
    assert "StateDb(" not in source
    assert "WorldPatternStore(" not in source
    assert "WorldModelStore(" not in source
    assert "apply_current_world_model_schema" not in source
    assert "INSERT " not in source
    assert "UPDATE " not in source
    assert "DELETE " not in source


def test_missing_database_stays_absent(tmp_path: Path) -> None:
    missing = tmp_path / "absent" / "world_model.db"
    batch = SqlitePatternEvaluationSource(missing).load_evaluation_batch(_scan())
    assert batch.records == ()
    assert batch.rejection_counts == {"missing_db": 1}
    assert not missing.exists()


def test_scan_is_strictly_after_not_before_and_does_not_select_outcomes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pre = _seed_labeled(tmp_path, monkeypatch, as_of=PRE, persist_outcome=True, close=True)
    _insert_evaluation_slot(
        pre["db_path"],  # type: ignore[arg-type]
        cohort_id=COHORT_ID,
        episode_id=pre["episode"].episode_id,  # type: ignore[union-attr]
        as_of=PRE,
    )
    post_dir = tmp_path / "post"
    post_dir.mkdir()
    seeded = _seed_labeled(post_dir, monkeypatch, as_of=POST, persist_outcome=False, close=True)
    _insert_evaluation_slot(
        seeded["db_path"],  # type: ignore[arg-type]
        cohort_id=COHORT_ID,
        episode_id=seeded["episode"].episode_id,  # type: ignore[union-attr]
        as_of=POST,
    )
    statements: list[str] = []

    def traced(path: Path):
        uri = f"file:{quote(str(path.resolve()), safe='/')}?mode=ro"
        import sqlite3

        connection = sqlite3.connect(uri, uri=True)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        connection.set_trace_callback(lambda sql: statements.append(sql))
        return connection

    monkeypatch.setattr(evaluation_query, "_readonly_connection", traced)
    batch = SqlitePatternEvaluationSource(seeded["db_path"]).load_evaluation_batch(_scan())
    assert batch.records
    assert all(item.episode.observation.as_of_bar_ts > NOT_BEFORE for item in batch.records)
    assert any("world_cohort_slots" in sql for sql in statements)
    assert not any("world_outcome_events" in sql for sql in statements)
    assert all(not hasattr(item, "outcome") for item in batch.records)

    pre_batch = SqlitePatternEvaluationSource(tmp_path / "world_model.db").load_evaluation_batch(_scan())
    assert pre_batch.records == ()


def test_persist_replay_on_real_stores_does_not_read_outcomes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.application.test_world_pattern_evaluation import (
        COHORT_ID,
        EVAL_FP,
        _evaluating_hypothesis,
        _request,
    )
    from trader.application.world_model.pattern_evaluation import PatternEvaluationService
    from trader.application.world_model.pattern_path import project_pattern_path_details
    from trader.application.world_model.pattern_service import (
        RegisterPatternHypothesis,
        StartPatternEvaluation,
        WorldPatternService,
    )
    from trader.domain.world_pattern import PatternOccurrenceId
    from trader.infrastructure.state_db.world_model_store import WorldModelStore
    from trader.infrastructure.state_db.world_pattern_store import WorldPatternStore

    seeded = _seed_labeled(tmp_path, monkeypatch, as_of=POST, persist_outcome=False, close=True)
    db_path = seeded["db_path"]
    _insert_evaluation_slot(
        db_path,  # type: ignore[arg-type]
        cohort_id=COHORT_ID,
        episode_id=seeded["episode"].episode_id,  # type: ignore[union-attr]
        as_of=POST,
    )
    source = SqlitePatternEvaluationSource(db_path)
    batch = source.load_evaluation_batch(_scan())
    assert batch.records
    steps = project_pattern_path_details(batch.records[0])[0].steps
    hypothesis = _evaluating_hypothesis(steps=steps)
    world = WorldModelStore(db_path)
    store = WorldPatternStore(world._db)
    patterns = WorldPatternService(hypotheses=store, occurrences=store, availability=store)
    patterns.register(RegisterPatternHypothesis(spec=hypothesis.spec, registered_at=hypothesis.spec.formation_cutoff))
    patterns.start(
        StartPatternEvaluation(
            hypothesis_id=hypothesis.hypothesis_id,
            evaluation_cohort_id=COHORT_ID,
            started_at=hypothesis.spec.evaluation_start_not_before,
            evaluation_dataset_fingerprint=EVAL_FP,
        )
    )
    service = PatternEvaluationService(catalog=store, source=source)
    result = service.evaluate(_request())
    assert result.matches
    first = service.persist(result, patterns=patterns, predictions=world)
    second = service.persist(result, patterns=patterns, predictions=world)
    assert first.persisted_occurrence_ids == second.persisted_occurrence_ids
    loaded = store.load(PatternOccurrenceId(first.persisted_occurrence_ids[0]))
    assert loaded.active_outcome_link("elapsed_1d.v1") is None
    world.close()


def test_empty_evaluation_cohort_does_not_fall_back_to_in_window_episodes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seeded = _seed_labeled(tmp_path, monkeypatch, as_of=POST, persist_outcome=False, close=True)
    episode: WorldEpisode = seeded["episode"]  # type: ignore[assignment]
    batch = SqlitePatternEvaluationSource(seeded["db_path"]).load_evaluation_batch(_scan())
    assert batch.records == ()
    assert batch.rejection_counts == {"evaluation_cohort_empty": 1}
    assert episode.episode_id not in batch.source_evidence_ids


def test_in_window_graph_episode_outside_the_cohort_is_excluded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seeded = _seed_labeled(tmp_path, monkeypatch, as_of=POST, persist_outcome=False, close=False, symbol="2330")
    outsider = _add_graph_companion(seeded, symbol="2301", as_of=POST)
    member: WorldEpisode = seeded["episode"]  # type: ignore[assignment]
    db_path: Path = seeded["db_path"]  # type: ignore[assignment]
    seeded["world"].close()  # type: ignore[union-attr]
    seeded["graph"].close()  # type: ignore[union-attr]
    _insert_evaluation_slot(
        db_path,
        cohort_id=COHORT_ID,
        episode_id=member.episode_id,
        as_of=POST,
        symbol="2330",
    )
    _insert_evaluation_slot(
        db_path,
        cohort_id=OTHER_COHORT,
        episode_id=outsider.episode_id,
        as_of=POST,
        symbol="2301",
    )
    batch = SqlitePatternEvaluationSource(db_path).load_evaluation_batch(_scan())
    admitted = [item.episode.episode_id for item in batch.records]
    assert admitted == [member.episode_id]
    assert outsider.episode_id not in admitted
    assert outsider.episode_id not in batch.source_evidence_ids
    assert member.episode_id in batch.source_evidence_ids
    assert "evaluation_cohort_empty" not in batch.rejection_counts


def test_slot_recorded_after_as_of_does_not_admit_its_graph_companion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seeded = _seed_labeled(tmp_path, monkeypatch, as_of=POST, persist_outcome=False, close=True)
    episode: WorldEpisode = seeded["episode"]  # type: ignore[assignment]
    _insert_evaluation_slot(
        seeded["db_path"],  # type: ignore[arg-type]
        cohort_id=COHORT_ID,
        episode_id=episode.episode_id,
        as_of=POST,
        recorded_at=datetime(2026, 9, 11, tzinfo=UTC),
    )
    batch = SqlitePatternEvaluationSource(seeded["db_path"]).load_evaluation_batch(_scan())
    assert batch.records == ()
    assert batch.rejection_counts["slot_not_visible_as_of"] == 1
    assert episode.episode_id not in batch.source_evidence_ids


def _dummy_episode_id(index: int) -> str:
    return "world-episode:v1:" + f"{index:064x}"


def _episode_in_placeholder_counts(statements: list[str]) -> list[int]:
    widths: list[int] = []
    for sql in statements:
        compact = " ".join(sql.split())
        if "FROM world_episodes" not in compact or "episode_id IN" not in compact:
            continue
        match = re.search(r"episode_id IN \(([^)]*)\)", compact)
        assert match is not None
        body = match.group(1).strip()
        if not body:
            widths.append(0)
            continue
        widths.append(body.count(",") + 1)
    return widths


def _insert_dummy_episode_row(
    connection: sqlite3.Connection,
    *,
    episode_id: str,
    as_of: datetime,
    recorded_at: datetime,
) -> None:
    stamp = as_of.isoformat()
    connection.execute(
        """
        INSERT INTO world_episodes (
            episode_id, symbol, observed_at, available_at, as_of_bar_ts,
            feature_contract_version, training_eligible, payload_json, payload_sha256,
            source_evidence_json, source_evidence_sha256, recorded_at
        ) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?)
        """,
        (
            episode_id,
            "2330",
            stamp,
            stamp,
            stamp,
            GRAPH_FEATURE_CONTRACT_ID,
            "{}",
            "0" * 64,
            "[]",
            "1" * 64,
            recorded_at.isoformat(),
        ),
    )


def test_admitted_episode_in_list_is_chunked_beyond_sqlite_variable_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seeded = _seed_labeled(tmp_path, monkeypatch, as_of=POST, persist_outcome=False, close=False, symbol="2330")
    later = POST + timedelta(days=1)
    second = _add_graph_companion(seeded, symbol="2301", as_of=later)
    member: WorldEpisode = seeded["episode"]  # type: ignore[assignment]
    db_path: Path = seeded["db_path"]  # type: ignore[assignment]
    seeded["world"].close()  # type: ignore[union-attr]
    seeded["graph"].close()  # type: ignore[union-attr]

    dummy_count = 1099
    dummy_ids = [_dummy_episode_id(index) for index in range(1, dummy_count + 1)]
    dummy_as_of = [datetime(2026, 9, 5, tzinfo=UTC) + timedelta(seconds=index) for index in range(dummy_count)]
    with sqlite3.connect(db_path) as connection:
        for episode_id, as_of in zip(dummy_ids, dummy_as_of, strict=True):
            _insert_dummy_episode_row(connection, episode_id=episode_id, as_of=as_of, recorded_at=as_of)
        connection.commit()

    _insert_evaluation_slot(db_path, cohort_id=COHORT_ID, episode_id=member.episode_id, as_of=POST, symbol="2330")
    _insert_evaluation_slot(db_path, cohort_id=COHORT_ID, episode_id=second.episode_id, as_of=later, symbol="2301")
    with sqlite3.connect(db_path) as connection:
        for index, (episode_id, as_of) in enumerate(zip(dummy_ids, dummy_as_of, strict=True)):
            slot = WorldCohortSlot(
                cohort_id=COHORT_ID,
                manifest_sha256="a" * 64,
                venue="TW",
                symbol=f"D{index:04d}",
                bar_interval="1h",
                as_of_bar_ts=as_of,
                anchor_end_at=as_of,
                comparison_batch_id="batch:v1:eval",
                episode_refs_by_contract={GRAPH_FEATURE_CONTRACT_ID: episode_id},
                expected_lane_ids=("graph.pilot",),
                feature_contract_fingerprints={"graph.pilot": "b" * 64},
                feature_mask_fingerprints={"graph.pilot": "c" * 64},
                started_event_id="world_cohort_event:v1:" + "e" * 64,
            )
            payload = slot.to_dict()
            connection.execute(
                """
                INSERT INTO world_cohort_slots (
                    slot_id, cohort_id, event_id, manifest_sha256, venue, symbol, bar_interval,
                    as_of_bar_ts, anchor_end_at, comparison_batch_id, started_event_id,
                    payload_json, payload_sha256, recorded_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    slot.slot_id,
                    slot.cohort_id,
                    slot.started_event_id,
                    slot.manifest_sha256,
                    slot.venue,
                    slot.symbol,
                    slot.bar_interval,
                    payload["as_of_bar_ts"],
                    payload["anchor_end_at"],
                    slot.comparison_batch_id,
                    slot.started_event_id,
                    json.dumps(payload),
                    canonical_sha256(payload),
                    as_of.isoformat(),
                ),
            )
        connection.commit()

    admitted_count = dummy_count + 2
    assert admitted_count > 999
    assert admitted_count > 500

    statements: list[str] = []

    def traced(path: Path):
        uri = f"file:{quote(str(path.resolve()), safe='/')}?mode=ro"
        connection = sqlite3.connect(uri, uri=True)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        connection.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 999)
        connection.set_trace_callback(lambda sql: statements.append(sql))
        return connection

    monkeypatch.setattr(evaluation_query, "_readonly_connection", traced)
    batch = SqlitePatternEvaluationSource(db_path).load_evaluation_batch(_scan())

    assert "unavailable" not in batch.rejection_counts
    assert batch.rejection_counts.get("episode_malformed") == dummy_count
    admitted = [item.episode.episode_id for item in batch.records]
    assert admitted == [member.episode_id, second.episode_id]
    assert len(admitted) == len(set(admitted))
    in_widths = _episode_in_placeholder_counts(statements)
    assert in_widths
    assert all(width <= 500 for width in in_widths)
    assert max(in_widths) == 500
    assert sum(in_widths) == admitted_count
    assert len(in_widths) >= 3


def test_about_joins_frozen_news_signal_through_knowledge_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    knowledge_root = tmp_path / "world_knowledge"
    stamp = datetime(2026, 8, 20, 12, 5, tzinfo=UTC)
    store = WorldKnowledgeStore(knowledge_root, clock=lambda: stamp)
    relation, _envelope, signal = _news_about_triplet()
    store.append_artifact(_envelope, signal)
    seeded = _seed_labeled(tmp_path / "joined", monkeypatch, as_of=POST, persist_outcome=False, knowledge=relation)
    _insert_evaluation_slot(
        seeded["db_path"],  # type: ignore[arg-type]
        cohort_id=COHORT_ID,
        episode_id=seeded["episode"].episode_id,  # type: ignore[union-attr]
        as_of=POST,
    )
    batch = SqlitePatternEvaluationSource(
        seeded["db_path"], knowledge_root=knowledge_root  # type: ignore[arg-type]
    ).load_evaluation_batch(_scan())
    assert len(batch.records) == 1
    bindings = batch.records[0].driver_state_bindings
    assert len(bindings) == 1
    assert bindings[0].driver_state.signal_class == "news_state"
    assert bindings[0].driver_state.news == signal
