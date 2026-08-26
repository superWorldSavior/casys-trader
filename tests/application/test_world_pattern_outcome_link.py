from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

import pytest

from tests.application.test_world_pattern_evaluation import (
    COHORT_ID,
    EVAL_STARTED,
    FORMATION,
    MemoryCatalog,
    MemorySource,
    _matched_pair,
    _request,
)
from tests.application.test_world_pattern_service import _MemoryPatternStore
from trader.application.world_model.pattern_evaluation import PatternEvaluationService
from trader.application.world_model.pattern_outcome_link import (
    PatternOutcomeLinkRequest,
    PatternOutcomeLinkService,
)
from trader.application.world_model.pattern_ports import PatternPayloadConflict
from trader.application.world_model.pattern_service import (
    RegisterPatternHypothesis,
    StartPatternEvaluation,
    WorldPatternService,
)
from trader.domain.world_episode import WorldOutcome
from trader.domain.world_pattern import (
    PATTERN_EVALUATION_HORIZON_IDS,
    PatternOccurrence,
    PatternOccurrenceId,
    PatternOutcomeLinked,
    PatternOutcomeLinkSuperseded,
)


UTC = timezone.utc
LINK_AS_OF = datetime(2026, 9, 12, tzinfo=UTC)
DURATIONS = {
    "elapsed_4h.v1": 4 * 60 * 60,
    "elapsed_1d.v1": 24 * 60 * 60,
    "elapsed_3d.v1": 3 * 24 * 60 * 60,
}


@dataclass
class MemoryOccurrenceCatalog:
    occurrences: tuple[PatternOccurrence, ...]

    def list_recorded_occurrences(
        self,
        *,
        evaluation_cohort_id: str | None = None,
        hypothesis_ids: tuple[str, ...] | None = None,
        occurrence_ids: tuple[str, ...] | None = None,
    ) -> tuple[PatternOccurrence, ...]:
        items = self.occurrences
        if evaluation_cohort_id is not None:
            items = tuple(item for item in items if item.spec.cohort_id == evaluation_cohort_id)
        if hypothesis_ids is not None:
            wanted = frozenset(hypothesis_ids)
            items = tuple(item for item in items if item.hypothesis_id in wanted)
        if occurrence_ids is not None:
            wanted = frozenset(occurrence_ids)
            items = tuple(item for item in items if item.occurrence_id in wanted)
        return items


@dataclass
class MemoryLeaves:
    outcomes: tuple[WorldOutcome, ...]

    def load_active_observed_leaves(
        self,
        *,
        episode_ids,
        horizon_ids,
        as_of,
    ) -> tuple[WorldOutcome, ...]:
        allowed_episodes = frozenset(episode_ids)
        allowed_horizons = frozenset(horizon_ids)
        return tuple(
            item
            for item in self.outcomes
            if item.episode_id in allowed_episodes
            and item.horizon.horizon_id in allowed_horizons
            and item.available_at is not None
            and item.available_at <= as_of
        )


def _leaf(
    episode_id: str,
    horizon_id: str,
    *,
    digest: str,
    supersedes_event_id: str | None = None,
) -> WorldOutcome:
    target = datetime(2026, 9, 11, tzinfo=UTC)
    return WorldOutcome(
        episode_id=episode_id,
        horizon={"horizon_id": horizon_id, "duration_seconds": DURATIONS[horizon_id]},
        status="observed",
        target_at=target,
        available_at=target,
        computed_at=target,
        anchor_close=100.0,
        endpoint_close=102.0,
        endpoint_bar_ts=target,
        source="analysis_bars",
        source_raw_sha256=digest,
        supersedes_event_id=supersedes_event_id,
    )


def _recorded_occurrence() -> tuple[PatternOccurrence, _MemoryPatternStore, WorldPatternService]:
    hypothesis, record = _matched_pair()
    store = _MemoryPatternStore()
    patterns = WorldPatternService(hypotheses=store, occurrences=store, availability=store)
    patterns.register(RegisterPatternHypothesis(spec=hypothesis.spec, registered_at=FORMATION))
    patterns.start(
        StartPatternEvaluation(
            hypothesis_id=hypothesis.hypothesis_id,
            evaluation_cohort_id=COHORT_ID,
            started_at=EVAL_STARTED,
            evaluation_dataset_fingerprint=_request().evaluation_dataset_fingerprint,
        )
    )
    result = PatternEvaluationService(MemoryCatalog((hypothesis,)), MemorySource((record,))).evaluate(_request())
    match = result.matches[0]

    class _Writer:
        def append_prediction(self, prediction):
            return True

    PatternEvaluationService(MemoryCatalog((hypothesis,)), MemorySource((record,))).persist(
        result, patterns=patterns, predictions=_Writer()
    )
    occurrence = store.load(PatternOccurrenceId(match.occurrence.occurrence_id))
    return occurrence, store, patterns


def test_preview_exposes_available_4h_and_keeps_missing_horizons_pending() -> None:
    occurrence, _store, _patterns = _recorded_occurrence()
    episode_id = occurrence.spec.forecast.episode_id
    only_4h = _leaf(episode_id, "elapsed_4h.v1", digest="1" * 64)
    result = PatternOutcomeLinkService(
        catalog=MemoryOccurrenceCatalog((occurrence,)),
        outcomes=MemoryLeaves((only_4h,)),
    ).preview(PatternOutcomeLinkRequest(as_of=LINK_AS_OF, occurrence_ids=(occurrence.occurrence_id,)))
    status = result.statuses[0]
    assert status.available == {"elapsed_4h.v1": only_4h.event_id}
    assert status.linked == {}
    assert status.pending == ("elapsed_1d.v1", "elapsed_3d.v1")
    assert result.available_count == 1
    assert result.pending_count == 2


def test_link_transitions_horizons_independently_and_never_fabricates() -> None:
    occurrence, store, patterns = _recorded_occurrence()
    episode_id = occurrence.spec.forecast.episode_id
    leaf_4h = _leaf(episode_id, "elapsed_4h.v1", digest="1" * 64)
    leaf_1d = _leaf(episode_id, "elapsed_1d.v1", digest="2" * 64)
    request = PatternOutcomeLinkRequest(as_of=LINK_AS_OF, occurrence_ids=(occurrence.occurrence_id,))
    first = PatternOutcomeLinkService(
        catalog=MemoryOccurrenceCatalog((occurrence,)),
        outcomes=MemoryLeaves((leaf_4h,)),
    ).link(request, patterns=patterns)
    assert first.statuses[0].linked == {"elapsed_4h.v1": leaf_4h.event_id}
    assert first.statuses[0].pending == ("elapsed_1d.v1", "elapsed_3d.v1")
    assert first.statuses[0].available == {}
    updated = store.load(PatternOccurrenceId(occurrence.occurrence_id))
    second = PatternOutcomeLinkService(
        catalog=MemoryOccurrenceCatalog((updated,)),
        outcomes=MemoryLeaves((leaf_4h, leaf_1d)),
    ).link(request, patterns=patterns)
    assert set(second.statuses[0].linked) == {"elapsed_4h.v1", "elapsed_1d.v1"}
    assert second.statuses[0].pending == ("elapsed_3d.v1",)
    final = store.load(PatternOccurrenceId(occurrence.occurrence_id))
    assert final.active_outcome_link("elapsed_3d.v1") is None
    assert PATTERN_EVALUATION_HORIZON_IDS[2] == "elapsed_3d.v1"


def test_link_replay_of_the_same_active_leaf_is_idempotent() -> None:
    occurrence, store, patterns = _recorded_occurrence()
    episode_id = occurrence.spec.forecast.episode_id
    leaf_4h = _leaf(episode_id, "elapsed_4h.v1", digest="1" * 64)
    request = PatternOutcomeLinkRequest(as_of=LINK_AS_OF, occurrence_ids=(occurrence.occurrence_id,))
    first = PatternOutcomeLinkService(
        catalog=MemoryOccurrenceCatalog((occurrence,)),
        outcomes=MemoryLeaves((leaf_4h,)),
    ).link(request, patterns=patterns)
    after_first = store.load(PatternOccurrenceId(occurrence.occurrence_id))
    replay = PatternOutcomeLinkService(
        catalog=MemoryOccurrenceCatalog((after_first,)),
        outcomes=MemoryLeaves((leaf_4h,)),
    ).link(request, patterns=patterns)
    after_replay = store.load(PatternOccurrenceId(occurrence.occurrence_id))
    first_link = after_first.active_outcome_link("elapsed_4h.v1")
    replay_link = after_replay.active_outcome_link("elapsed_4h.v1")
    assert first_link is not None and replay_link is not None
    assert replay.statuses[0].linked == first.statuses[0].linked == {"elapsed_4h.v1": leaf_4h.event_id}
    assert replay_link.link_id == first_link.link_id
    assert len(after_replay.events) == len(after_first.events)
    assert sum(1 for event in after_replay.events if isinstance(event, PatternOutcomeLinked)) == 1
    assert not any(isinstance(event, PatternOutcomeLinkSuperseded) for event in after_replay.events)


def test_link_supersedes_when_canonical_correction_targets_the_current_leaf() -> None:
    occurrence, store, patterns = _recorded_occurrence()
    episode_id = occurrence.spec.forecast.episode_id
    first_leaf = _leaf(episode_id, "elapsed_4h.v1", digest="1" * 64)
    request = PatternOutcomeLinkRequest(as_of=LINK_AS_OF, occurrence_ids=(occurrence.occurrence_id,))
    PatternOutcomeLinkService(
        catalog=MemoryOccurrenceCatalog((occurrence,)),
        outcomes=MemoryLeaves((first_leaf,)),
    ).link(request, patterns=patterns)
    after_first = store.load(PatternOccurrenceId(occurrence.occurrence_id))
    current = after_first.active_outcome_link("elapsed_4h.v1")
    assert current is not None
    preview = PatternOutcomeLinkService(
        catalog=MemoryOccurrenceCatalog((after_first,)),
        outcomes=MemoryLeaves((first_leaf,)),
    ).preview(request)
    assert preview.statuses[0].linked == {"elapsed_4h.v1": first_leaf.event_id}
    assert preview.statuses[0].available == {}
    correction = _leaf(
        episode_id,
        "elapsed_4h.v1",
        digest="9" * 64,
        supersedes_event_id=first_leaf.event_id,
    )
    waiting = PatternOutcomeLinkService(
        catalog=MemoryOccurrenceCatalog((after_first,)),
        outcomes=MemoryLeaves((correction,)),
    ).preview(request)
    assert waiting.statuses[0].linked == {"elapsed_4h.v1": first_leaf.event_id}
    assert waiting.statuses[0].available == {"elapsed_4h.v1": correction.event_id}
    superseded = PatternOutcomeLinkService(
        catalog=MemoryOccurrenceCatalog((after_first,)),
        outcomes=MemoryLeaves((correction,)),
    ).link(request, patterns=patterns)
    final = store.load(PatternOccurrenceId(occurrence.occurrence_id))
    leaf = final.active_outcome_link("elapsed_4h.v1")
    assert leaf is not None
    assert leaf.world_outcome_event_id == correction.event_id
    assert leaf.supersedes_link_id == current.link_id
    assert superseded.statuses[0].linked == {"elapsed_4h.v1": correction.event_id}
    assert isinstance(final.events[-1], PatternOutcomeLinkSuperseded)


def test_link_applies_a_two_hop_correction_chain_without_skipping_a_hop() -> None:
    occurrence, store, patterns = _recorded_occurrence()
    episode_id = occurrence.spec.forecast.episode_id
    first_leaf = _leaf(episode_id, "elapsed_4h.v1", digest="1" * 64)
    request = PatternOutcomeLinkRequest(as_of=LINK_AS_OF, occurrence_ids=(occurrence.occurrence_id,))
    PatternOutcomeLinkService(
        catalog=MemoryOccurrenceCatalog((occurrence,)),
        outcomes=MemoryLeaves((first_leaf,)),
    ).link(request, patterns=patterns)
    after_first = store.load(PatternOccurrenceId(occurrence.occurrence_id))
    second_leaf = _leaf(
        episode_id,
        "elapsed_4h.v1",
        digest="2" * 64,
        supersedes_event_id=first_leaf.event_id,
    )
    PatternOutcomeLinkService(
        catalog=MemoryOccurrenceCatalog((after_first,)),
        outcomes=MemoryLeaves((second_leaf,)),
    ).link(request, patterns=patterns)
    after_second = store.load(PatternOccurrenceId(occurrence.occurrence_id))
    third_leaf = _leaf(
        episode_id,
        "elapsed_4h.v1",
        digest="3" * 64,
        supersedes_event_id=second_leaf.event_id,
    )
    PatternOutcomeLinkService(
        catalog=MemoryOccurrenceCatalog((after_second,)),
        outcomes=MemoryLeaves((third_leaf,)),
    ).link(request, patterns=patterns)
    final = store.load(PatternOccurrenceId(occurrence.occurrence_id))
    leaf = final.active_outcome_link("elapsed_4h.v1")
    assert leaf is not None
    assert leaf.world_outcome_event_id == third_leaf.event_id
    assert sum(1 for event in final.events if isinstance(event, PatternOutcomeLinkSuperseded)) == 2


def test_link_rejects_a_correction_that_does_not_supersede_the_current_leaf() -> None:
    occurrence, store, patterns = _recorded_occurrence()
    episode_id = occurrence.spec.forecast.episode_id
    first_leaf = _leaf(episode_id, "elapsed_4h.v1", digest="1" * 64)
    request = PatternOutcomeLinkRequest(as_of=LINK_AS_OF, occurrence_ids=(occurrence.occurrence_id,))
    PatternOutcomeLinkService(
        catalog=MemoryOccurrenceCatalog((occurrence,)),
        outcomes=MemoryLeaves((first_leaf,)),
    ).link(request, patterns=patterns)
    after_first = store.load(PatternOccurrenceId(occurrence.occurrence_id))
    skipped = _leaf(episode_id, "elapsed_4h.v1", digest="2" * 64)
    jumped = _leaf(
        episode_id,
        "elapsed_4h.v1",
        digest="3" * 64,
        supersedes_event_id=skipped.event_id,
    )
    with pytest.raises((ValueError, PatternPayloadConflict), match="supersed"):
        PatternOutcomeLinkService(
            catalog=MemoryOccurrenceCatalog((after_first,)),
            outcomes=MemoryLeaves((jumped,)),
        ).link(request, patterns=patterns)
    still = store.load(PatternOccurrenceId(occurrence.occurrence_id))
    still_link = still.active_outcome_link("elapsed_4h.v1")
    assert still_link is not None
    assert still_link.world_outcome_event_id == first_leaf.event_id
