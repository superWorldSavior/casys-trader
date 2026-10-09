"""Causal replay and recursive simulations, separate from live predictor lanes."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
import hashlib
from typing import Sequence

import numpy as np

from trader.application.world_model.dynamics_model import (
    EmpiricalDynamicsBaseline,
    OnlineOHLCVDynamicsModel,
)
from trader.application.world_model.dynamics_ports import OHLCVDynamicsModel
from trader.domain.world_dynamics import (
    EpisodeEvidence,
    NextBarTransition,
    SimulatedBar,
    WorldTrajectory,
)
from trader.domain.world_episode import AnchorBar, parse_bar_interval, parse_utc_timestamp


@dataclass(frozen=True)
class DynamicsReplayRequest:
    as_of: datetime | str
    steps: int = 4
    paths: int = 200
    min_support: int = 40
    seed: int = 0
    max_evaluation_origins: int = 64

    def __post_init__(self) -> None:
        object.__setattr__(self, "as_of", parse_utc_timestamp(self.as_of, "as_of"))
        for field, lower, upper in (
            ("steps", 1, 12), ("paths", 20, 1000), ("min_support", 2, 256),
            ("max_evaluation_origins", 1, 128),
        ):
            value = getattr(self, field)
            if isinstance(value, bool) or not isinstance(value, int) or not lower <= value <= upper:
                raise ValueError(f"{field} must be an integer in [{lower}, {upper}]")
        if isinstance(self.seed, bool) or not isinstance(self.seed, int) or self.seed < 0:
            raise ValueError("seed must be a nonnegative integer")


@dataclass(frozen=True)
class HorizonScore:
    model_id: str
    step: int
    origins: int
    return_crps: float
    return_mae: float
    central_80_coverage: float
    central_80_width: float


@dataclass(frozen=True)
class EvaluationOrigin:
    episode_id: str
    episode_hash: str
    prediction_at: datetime
    training_cutoff: datetime
    support: int
    model_fingerprint: str
    baseline_fingerprint: str
    target_episode_ids: tuple[str, ...]
    gru_return_crps: tuple[float, ...]
    baseline_return_crps: tuple[float, ...]


@dataclass(frozen=True)
class DynamicsReplayResult:
    status: str
    read_episodes: int
    unique_anchors: int
    transitions: int
    exclusions: tuple[tuple[str, int], ...]
    scores: tuple[HorizonScore, ...]
    evaluation_origins: tuple[EvaluationOrigin, ...]
    trajectories: tuple[WorldTrajectory, ...]
    model_id: str
    model_fingerprint: str
    model_diagnostics: tuple[tuple[str, object], ...]
    support: int
    residual_support: int
    training_cutoff: datetime | None
    latest_origin_id: str | None
    latest_origin_end_at: datetime | None


@dataclass(frozen=True)
class _SampledPath:
    seed: int
    bars: tuple[SimulatedBar, ...]


def _series(evidence: EpisodeEvidence) -> tuple[str, ...]:
    observation = evidence.episode.observation
    return (
        observation.venue, observation.symbol, observation.bar_interval,
        observation.feature_contract_version, observation.sampling_policy_version,
        observation.anchor.source, observation.anchor.timestamp_semantics,
    )


def _end(evidence: EpisodeEvidence) -> datetime:
    end_at = evidence.episode.observation.completed_bar_end_at
    if end_at is None:
        raise ValueError("dynamics requires a provable completed bar")
    return end_at


def _canonical_anchors(
    evidence: Sequence[EpisodeEvidence], cutoff: datetime, exclusions: Counter[str],
) -> tuple[EpisodeEvidence, ...]:
    slots: dict[tuple[tuple[str, ...], datetime], list[EpisodeEvidence]] = {}
    for row in evidence:
        if not row.episode.training_eligible:
            exclusions["ineligible_episode"] += 1
            continue
        if row.effective_available_at > cutoff or _end(row) > cutoff:
            exclusions["not_available_at_cutoff"] += 1
            continue
        slots.setdefault((_series(row), _end(row)), []).append(row)
    selected: list[EpisodeEvidence] = []
    for rows in slots.values():
        rows.sort(key=lambda row: (row.effective_available_at, row.episode.episode_id))
        first_clock = rows[0].effective_available_at

        def values(row: EpisodeEvidence) -> tuple[float, ...]:
            return tuple(getattr(row.episode.observation.anchor, field)
                         for field in ("open", "high", "low", "close", "volume"))

        first_values = {values(row) for row in rows if row.effective_available_at == first_clock}
        if len(first_values) != 1:
            exclusions["conflicting_bar_slot"] += len(rows)
            continue
        # Immutable first-known admission. A correction arriving later must
        # never erase what earlier replay origins actually knew and learned.
        canonical = rows[0]
        selected.append(canonical)
        for revision in rows[1:]:
            reason = ("duplicate_identical_bar" if values(revision) == values(canonical)
                      else "conflicting_later_bar_revision")
            exclusions[reason] += 1
    selected.sort(key=lambda row: (_end(row), _series(row), row.episode.episode_id))
    return tuple(selected)


def _history(
    origin: EpisodeEvidence,
    slots: dict[tuple[tuple[str, ...], datetime], EpisodeEvidence],
    interval: timedelta,
) -> tuple[AnchorBar, ...]:
    chain = [origin]
    cursor = _end(origin)
    # Twenty causal bars also bound input preparation independently of ledger size.
    for _ in range(19):
        cursor -= interval
        earlier = slots.get((_series(origin), cursor))
        if earlier is None or earlier.effective_available_at > origin.effective_available_at:
            break
        chain.append(earlier)
    return tuple(row.episode.observation.anchor for row in reversed(chain))


def _paths(
    model: OHLCVDynamicsModel, history: Sequence[AnchorBar], origin: EpisodeEvidence,
    request: DynamicsReplayRequest, interval: timedelta,
) -> tuple[_SampledPath, ...]:
    paths: list[_SampledPath] = []
    for path_index in range(request.paths):
        seed_material = f"{request.seed}:{origin.episode.episode_id}:{model.model_id}:{path_index}".encode()
        seed = int.from_bytes(hashlib.sha256(seed_material).digest()[:8], "big")
        rng = np.random.Generator(np.random.PCG64(seed))
        held: list[AnchorBar | SimulatedBar] = list(history)
        simulated: list[SimulatedBar] = []
        for step in range(1, request.steps + 1):
            bar = model.sample_next(
                held, rng, end_at=_end(origin) + step * interval, step_index=step,
            )
            simulated.append(bar)
            held.append(bar)
        paths.append(_SampledPath(seed, tuple(simulated)))
    return tuple(paths)


def _sample_scores(samples: np.ndarray, actual: float) -> tuple[float, float, float, float]:
    ordered = np.sort(samples)
    count = len(ordered)
    pairwise_half = float(np.sum((2 * np.arange(count) - count + 1) * ordered) / (count * count))
    crps = float(np.mean(np.abs(samples - actual))) - pairwise_half
    low, high = np.quantile(samples, [0.1, 0.9])
    return crps, abs(float(np.mean(samples)) - actual), float(low <= actual <= high), float(high - low)


def run_dynamics_replay(
    evidence: Sequence[EpisodeEvidence], request: DynamicsReplayRequest, *,
    model: OHLCVDynamicsModel | None = None,
    baseline: OHLCVDynamicsModel | None = None,
) -> DynamicsReplayResult:
    """Train only labels already known at each origin, then score its future.

    This retrospective replay is exploratory. It does not alter cohort manifests,
    predictor ledgers, availability receipts, or prospective experiment authority.
    """

    model = model or OnlineOHLCVDynamicsModel(minimum_support=request.min_support, seed=request.seed)
    baseline = baseline or EmpiricalDynamicsBaseline(minimum_support=request.min_support)
    exclusions: Counter[str] = Counter()
    anchors = _canonical_anchors(evidence, request.as_of, exclusions)
    series = {_series(row) for row in anchors}
    if len(series) > 1:
        raise ValueError("dynamics replay requires one exact instrument/source/contract series")
    interval = parse_bar_interval(anchors[0].episode.observation.bar_interval) if anchors else None
    if anchors and interval is None:
        raise ValueError("invalid bar interval")
    slots = {(_series(row), _end(row)): row for row in anchors}
    transitions: list[NextBarTransition] = []
    for source, target in zip(anchors, anchors[1:]):
        if _end(target) - _end(source) != interval:
            exclusions["nonadjacent_bar"] += 1
            continue
        try:
            transitions.append(NextBarTransition(source, target))
        except ValueError:
            exclusions["invalid_transition"] += 1
    transitions.sort(key=lambda item: (item.label_available_at, item.transition_id))
    histories = {row.episode.episode_id: _history(row, slots, interval) for row in anchors} if interval else {}
    # Split by completed-bar time, never by response/label. Select a bounded,
    # evenly spaced set of late origins before inspecting future outcomes.
    heldout = anchors[max(1, int(len(anchors) * 0.7)):]
    if len(heldout) > request.max_evaluation_origins:
        indices = np.linspace(0, len(heldout) - 1, request.max_evaluation_origins, dtype=int)
        heldout = tuple(heldout[int(index)] for index in indices)
    selected_ids = {row.episode.episode_id for row in heldout}
    observations = sorted(anchors, key=lambda row: (row.effective_available_at, _end(row)))
    trained = 0
    training_cutoff: datetime | None = None
    measurements: dict[tuple[str, int], list[tuple[float, float, float, float]]] = {}
    evaluations: list[EvaluationOrigin] = []

    def mature(cutoff: datetime) -> None:
        nonlocal trained, training_cutoff
        while trained < len(transitions) and transitions[trained].label_available_at <= cutoff:
            transition = transitions[trained]
            held = histories[transition.source.episode.episode_id]
            target = transition.target.episode.observation.anchor
            model.update(held, target)
            baseline.update(held, target)
            training_cutoff = transition.label_available_at
            trained += 1

    for origin in observations:
        mature(origin.effective_available_at)
        if origin.episode.episode_id not in selected_ids:
            continue
        if model.support < request.min_support or baseline.support < request.min_support:
            exclusions["evaluation_insufficient_support"] += 1
            continue
        future = tuple(slots.get((_series(origin), _end(origin) + step * interval))
                       for step in range(1, request.steps + 1))
        if any(row is None for row in future):
            exclusions["evaluation_incomplete_future"] += 1
            continue
        targets = tuple(row for row in future if row is not None)
        if any(_end(row) <= origin.effective_available_at for row in targets):
            exclusions["evaluation_target_already_completed"] += 1
            continue
        if any(row.effective_available_at <= origin.effective_available_at for row in targets):
            exclusions["evaluation_future_already_known"] += 1
            continue
        crps_by_model: list[tuple[float, ...]] = []
        for challenger in (model, baseline):
            simulated = _paths(challenger, histories[origin.episode.episode_id], origin, request, interval)
            crps_values: list[float] = []
            for step, target in enumerate(targets, start=1):
                anchor_close = origin.episode.observation.anchor.close
                samples = np.array([path.bars[step - 1].close / anchor_close - 1 for path in simulated])
                actual = target.episode.observation.anchor.close / anchor_close - 1
                values = _sample_scores(samples, actual)
                measurements.setdefault((challenger.model_id, step), []).append(values)
                crps_values.append(values[0])
            crps_by_model.append(tuple(crps_values))
        assert training_cutoff is not None
        evaluations.append(EvaluationOrigin(
            episode_id=origin.episode.episode_id, episode_hash=origin.episode.payload_hash,
            prediction_at=origin.effective_available_at, training_cutoff=training_cutoff,
            support=model.support, model_fingerprint=model.fingerprint(),
            baseline_fingerprint=baseline.fingerprint(),
            target_episode_ids=tuple(row.episode.episode_id for row in targets),
            gru_return_crps=crps_by_model[0], baseline_return_crps=crps_by_model[1],
        ))
    mature(request.as_of)
    scores = tuple(HorizonScore(
        model_id, step, len(values), *(float(value) for value in np.mean(values, axis=0)),
    ) for (model_id, step), values in sorted(measurements.items()))
    trajectories: tuple[WorldTrajectory, ...] = ()
    latest = anchors[-1] if anchors else None
    if latest is None:
        status = "no_data"
    elif model.support < request.min_support:
        status = "insufficient_support"
    elif request.as_of - _end(latest) >= interval:
        status = "stale_origin"
    else:
        assert training_cutoff is not None and interval is not None
        status = "ready"
        simulated = _paths(model, histories[latest.episode.episode_id], latest, request, interval)
        observation = latest.episode.observation
        trajectories = tuple(WorldTrajectory(
            origin_episode_id=latest.episode.episode_id, origin_episode_hash=latest.episode.payload_hash,
            symbol=observation.symbol, venue=observation.venue, bar_interval=observation.bar_interval,
            origin_end_at=_end(latest), prediction_at=request.as_of, training_cutoff=training_cutoff,
            model_id=model.model_id, model_fingerprint=model.fingerprint(), seed=path.seed, bars=path.bars,
        ) for path in simulated)
    return DynamicsReplayResult(
        status, len(evidence), len(anchors), len(transitions), tuple(sorted((key, count)
        for key, count in exclusions.items() if count)), scores, tuple(evaluations), trajectories,
        model.model_id, model.fingerprint(), tuple(sorted(model.diagnostics().items())),
        model.support, model.residual_support, training_cutoff,
        latest.episode.episode_id if latest else None, _end(latest) if latest else None,
    )
