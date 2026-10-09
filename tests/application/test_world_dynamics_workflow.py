from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

from trader.application.world_model import dynamics_workflow
from trader.application.world_model.dynamics_ports import DynamicsJournalResult
from trader.application.world_model.dynamics_workflow import (
    DynamicsCycleResult,
    DynamicsWorkflowConfig,
    WorldDynamicsShadowWorkflow,
)
from trader.domain.world_dynamics import ObservedDynamicsBar
from tests.application.test_world_dynamics_service import _series


class Clock:
    def __init__(self, current: datetime) -> None:
        self.current = current

    def __call__(self) -> datetime:
        return self.current


class MemoryJournal:
    """Test port preserves original receipts and implements strict retention."""

    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.rows: dict[tuple[str, ...], dict[datetime, ObservedDynamicsBar]] = {}

    def merge(self, bars: tuple[ObservedDynamicsBar, ...], *,
              max_series: int, max_bars_per_series: int) -> DynamicsJournalResult:
        new = duplicate = conflicting = evicted = 0
        rejected: set[tuple[str, ...]] = set()
        for candidate in bars:
            key = candidate.series_key
            if key not in self.rows:
                if len(self.rows) >= max_series:
                    rejected.add(key)
                    continue
                self.rows[key] = {}
            slots = self.rows[key]
            prior = slots.get(candidate.completed_end_at)
            if prior is not None:
                if prior.evidence_id == candidate.evidence_id:
                    duplicate += 1
                else:
                    conflicting += 1
                continue
            recorded = max(candidate.first_seen_at, self.clock())
            slots[candidate.completed_end_at] = replace(candidate, recorded_at=recorded)
            new += 1
        for slots in self.rows.values():
            for end_at in sorted(slots)[:-max_bars_per_series]:
                del slots[end_at]
                evicted += 1
        retained = tuple(bar for _, slots in sorted(self.rows.items()) for _, bar in sorted(slots.items()))
        return DynamicsJournalResult(retained, len(self.rows), new, duplicate, conflicting, evicted, len(rejected))


class Publisher:
    def __init__(self) -> None:
        self.publications: list[DynamicsCycleResult] = []

    def publish(self, publication: DynamicsCycleResult) -> None:
        self.publications.append(publication)


def _inputs(count: int = 24, *, symbols: tuple[str, ...] = ("2330.TW",),
            captured_at: datetime | None = None):
    rows = _series(count)
    captured = captured_at or rows[-1].effective_available_at
    episodes = tuple(replace(rows[-1].episode, observation=replace(
        rows[-1].episode.observation, symbol=symbol)) for symbol in symbols)
    bars = {symbol: [{**row.anchor.to_dict(), "interval": row.bar_interval,
                      "available_at": captured.isoformat()} for row in rows] for symbol in symbols}
    return {"episodes": episodes, "bars_by_symbol": bars, "now": captured}


def _workflow(inputs: dict[str, object], **config_changes: int):
    clock = Clock(inputs["now"] + timedelta(seconds=5))
    journal = MemoryJournal(clock)
    publisher = Publisher()
    config = DynamicsWorkflowConfig(min_support=8, max_bars_per_series=32, **config_changes)
    workflow = WorldDynamicsShadowWorkflow(journal=journal, publisher=publisher, config=config, clock=clock)
    return workflow, journal, publisher, clock


def test_automatic_bootstrap_uses_durable_clock_and_never_scores_old_download_as_forecast() -> None:
    inputs = _inputs()
    workflow, journal, publisher, clock = _workflow(inputs)
    publication = workflow.run(**inputs)
    summary = publication.summary
    assert summary["status"] == "ready" and summary["enabled"] is True
    assert summary["authority"] == "shadow_only" and summary["decision_effect"] == "none"
    assert summary["journal"]["new_bars"] == 24
    assert summary["replayed_series"] == 1 and len(journal.rows) == 1
    report = publication.reports[0]
    assert report.result.support == 23 and not report.result.evaluation_origins
    assert report.result.training_cutoff == clock.current
    assert report.request.as_of == clock.current
    assert report.scope["evidence_kind"] == "observed_market_bar"
    assert report.scope["availability_policy"] == "max(first_seen_at,recorded_at)"
    assert summary["series"][0]["new_transitions"] == 23
    assert publisher.publications == [publication]


def test_identical_retrieval_preserves_original_receipts_and_does_not_repeat_learning_or_rollout() -> None:
    inputs = _inputs()
    workflow, journal, publisher, clock = _workflow(inputs)
    with patch.object(dynamics_workflow, "run_dynamics_replay", wraps=dynamics_workflow.run_dynamics_replay) as replay:
        first = workflow.run(**inputs)
        saved = dict(next(iter(journal.rows.values())))
        later = inputs["now"] + timedelta(minutes=10)
        clock.current = later + timedelta(seconds=5)
        second = workflow.run(**_inputs(captured_at=later))
        assert replay.call_count == 1
    assert saved == next(iter(journal.rows.values()))
    assert second.summary["journal"]["new_bars"] == 0
    assert second.summary["journal"]["duplicate_bars"] == 24
    assert second.summary["replayed_series"] == 0
    assert second.summary["last_run_at"] == first.summary["last_run_at"]
    assert second.summary["series"][0]["execution"] == "reused_latest"
    assert second.summary["series"][0]["new_transitions"] == 0
    assert first.reports == second.reports
    assert len(publisher.publications) == 2


def test_unchanged_input_can_become_stale_without_resampling_a_path_into_the_past() -> None:
    inputs = _inputs()
    workflow, _, _, clock = _workflow(inputs)
    with patch.object(dynamics_workflow, "run_dynamics_replay", wraps=dynamics_workflow.run_dynamics_replay) as replay:
        first = workflow.run(**inputs)
        later = inputs["now"] + timedelta(hours=1)
        clock.current = later + timedelta(seconds=5)
        second = workflow.run(**_inputs(captured_at=later))
        assert replay.call_count == 1
    assert first.reports[0].result.trajectories
    assert second.summary["status"] == "stale_origin"
    assert not second.reports[0].result.trajectories
    assert second.reports[0].request.as_of == clock.current
    assert second.summary["last_run_at"] == first.summary["last_run_at"]


def test_new_bar_rebuilds_only_the_bounded_window_and_reports_one_new_transition() -> None:
    inputs = _inputs()
    workflow, _, _, clock = _workflow(inputs)
    workflow.config = replace(workflow.config, max_bars_per_series=24)
    first = workflow.run(**inputs)
    next_inputs = _inputs(25)
    clock.current = next_inputs["now"] + timedelta(seconds=5)
    second = workflow.run(**next_inputs)
    assert second.summary["journal"]["new_bars"] == 1
    assert second.summary["journal"]["evicted_bars"] == 1
    assert second.summary["journal"]["retained_bars"] == 24
    assert second.summary["series"][0]["new_transitions"] == 1
    assert second.reports[0].result.support == first.reports[0].result.support == 23
    assert second.reports[0].result.model_fingerprint != first.reports[0].result.model_fingerprint


def test_restart_recovers_pinned_scopes_before_selecting_candidates() -> None:
    inputs = _inputs(symbols=("B", "C", "D", "E"))
    workflow, journal, _, clock = _workflow(inputs)
    workflow.run(**inputs)
    restarted = WorldDynamicsShadowWorkflow(journal=journal, publisher=Publisher(),
                                            config=workflow.config, clock=clock)
    later_inputs = _inputs(symbols=("A", "B", "C", "D", "E"))
    publication = restarted.run(**later_inputs)
    assert {run.scope["symbol"] for run in publication.reports} == {"B", "C", "D", "E"}
    assert publication.summary["capture"]["exclusions"]["scope_limit"] == 1
    assert publication.summary["journal"]["accepted_series"] == 4
    assert publication.summary["journal"]["rejected_series"] == 0


def test_new_universe_never_expands_pinned_series_or_claims_stale_data_is_ready() -> None:
    inputs = _inputs(symbols=("B", "C", "D", "E"))
    workflow, journal, _, clock = _workflow(inputs)
    workflow.run(**inputs)
    later = inputs["now"] + timedelta(hours=1)
    clock.current = later + timedelta(seconds=5)
    publication = workflow.run(**_inputs(symbols=("F", "G", "H", "I"), captured_at=later))
    assert publication.summary["journal"]["rejected_series"] == 4
    assert publication.summary["journal"]["accepted_series"] == 4
    assert publication.summary["status"] == "stale_origin"
    assert len(journal.rows) == 4
    assert all(not run.result.trajectories for run in publication.reports)


def test_one_series_failure_is_published_and_does_not_prevent_another_series() -> None:
    inputs = _inputs(symbols=("A", "B"))
    workflow, _, publisher, _ = _workflow(inputs)
    original = dynamics_workflow.run_dynamics_replay

    def fail_one(bars, request):
        if bars[0].symbol == "A":
            raise ArithmeticError("sample overflow")
        return original(bars, request)

    with patch.object(dynamics_workflow, "run_dynamics_replay", side_effect=fail_one):
        publication = workflow.run(**inputs)
    assert publication.summary["status"] == "partial"
    assert publication.summary["errors"][0]["symbol"] == "A"
    assert publication.summary["errors"][0]["error"] == "ArithmeticError:sample overflow"
    assert len(publication.reports) == 1 and publication.reports[0].scope["symbol"] == "B"
    assert publisher.publications == [publication]


def test_empty_cycle_is_persisted_as_waiting_for_data() -> None:
    inputs = _inputs()
    workflow, _, publisher, _ = _workflow(inputs)
    publication = workflow.run(episodes=(), bars_by_symbol={}, now=inputs["now"])
    assert publication.summary["status"] == "waiting_for_data"
    assert publication.summary["series"] == [] and publication.summary["last_run_at"] is None
    assert publisher.publications == [publication]


def test_journal_contract_cannot_silently_expand_memory_bounds() -> None:
    inputs = _inputs()
    workflow, journal, _, _ = _workflow(inputs)
    original = journal.merge

    def oversized(bars, **kwargs):
        result = original(bars, **kwargs)
        if result.bars:
            return replace(result, bars=result.bars * 2)
        return result

    journal.merge = oversized
    with pytest.raises(ValueError, match="outside its configured bounds"):
        workflow.run(**inputs)


@pytest.mark.parametrize("options", [dict(max_series=0), dict(max_bars_per_series=1),
                                      dict(min_support=256, max_bars_per_series=256), dict(paths=10)])
def test_workflow_configuration_must_have_a_possible_bounded_training_window(options) -> None:
    with pytest.raises(ValueError):
        DynamicsWorkflowConfig(**options)
