"""Bounded automatic dynamics maintenance over already-fetched market bars.

The caller owns background execution and failure isolation. This workflow has
no threads, provider calls, trading callbacks, or knowledge of output paths.
Durable first-known evidence is its restart checkpoint: small models rebuild
deterministically when that bounded evidence window changes.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone

from trader.application.world_model.dynamics_capture import capture_dynamics_bars
from trader.application.world_model.dynamics_ports import DynamicsJournal, DynamicsPublisher
from trader.application.world_model.dynamics_service import (
    DynamicsReplayRequest,
    DynamicsReplayResult,
    run_dynamics_replay,
)
from trader.domain.world_dynamics import NextBarTransition, ObservedDynamicsBar
from trader.domain.world_episode import canonical_sha256, parse_bar_interval, parse_utc_timestamp


@dataclass(frozen=True)
class DynamicsWorkflowConfig:
    max_series: int = 4
    max_bars_per_series: int = 256
    steps: int = 4
    paths: int = 20
    min_support: int = 40
    max_evaluation_origins: int = 8
    seed: int = 0

    def __post_init__(self) -> None:
        for name, upper, lower in (("max_series", 4, 1), ("max_bars_per_series", 256, 2)):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or not lower <= value <= upper:
                raise ValueError(f"{name} must be an integer in [{lower}, {upper}]")
        self.request(datetime(1970, 1, 1, tzinfo=timezone.utc))
        if self.min_support >= self.max_bars_per_series:
            raise ValueError("min_support must be smaller than max_bars_per_series")

    def request(self, as_of: datetime) -> DynamicsReplayRequest:
        return DynamicsReplayRequest(
            as_of=as_of, steps=self.steps, paths=self.paths, min_support=self.min_support,
            max_evaluation_origins=self.max_evaluation_origins, seed=self.seed,
        )


@dataclass(frozen=True)
class DynamicsScopeRun:
    scope: dict[str, str]
    request: DynamicsReplayRequest
    result: DynamicsReplayResult


@dataclass(frozen=True)
class DynamicsCycleResult:
    summary: dict[str, object]
    reports: tuple[DynamicsScopeRun, ...]

    def to_dict(self) -> dict[str, object]:
        """The runner exposes only the lightweight operational summary."""
        return dict(self.summary)


@dataclass(frozen=True)
class _CachedScope:
    evidence_fingerprint: str
    transition_ids: frozenset[str]
    run: DynamicsScopeRun


def _scope(bar: ObservedDynamicsBar) -> dict[str, str]:
    return {
        "venue": bar.venue, "symbol": bar.symbol, "bar_interval": bar.bar_interval,
        "source": bar.anchor.source, "timestamp_semantics": bar.anchor.timestamp_semantics,
        "market_contract_version": bar.feature_contract_version,
        "sampling_policy_version": bar.sampling_policy_version,
        "evidence_kind": bar.kind,
        "availability_policy": "max(first_seen_at,recorded_at)",
        "missing_bars_policy": "exclude_gaps_no_fetch_or_backfill",
    }


def _transition_ids(bars: tuple[ObservedDynamicsBar, ...]) -> frozenset[str]:
    transitions: set[str] = set()
    for source, target in zip(bars, bars[1:]):
        try:
            transitions.add(NextBarTransition(source, target).transition_id)
        except ValueError:
            # Overnight closures and absent intervals never become next bars.
            continue
    return frozenset(transitions)


class WorldDynamicsShadowWorkflow:
    """Capture, journal, rebuild and publish up to a fixed number of series."""

    def __init__(
        self, *, journal: DynamicsJournal, publisher: DynamicsPublisher,
        config: DynamicsWorkflowConfig | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.journal = journal
        self.publisher = publisher
        self.config = config or DynamicsWorkflowConfig()
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._recovered = False
        self._pinned_series: set[tuple[str, ...]] = set()
        self._cache: dict[tuple[str, ...], _CachedScope] = {}
        self._last_run_at: datetime | None = None

    def run(
        self, *, episodes: Iterable[object], bars_by_symbol: Mapping[str, object],
        now: datetime | str,
    ) -> DynamicsCycleResult:
        captured_at = parse_utc_timestamp(now, "now")
        limits = {
            "max_series": self.config.max_series,
            "max_bars_per_series": self.config.max_bars_per_series,
        }
        if not self._recovered:
            restored = self.journal.merge((), **limits)
            self._pinned_series = {bar.series_key for bar in restored.bars}
            self._recovered = True
        capture = capture_dynamics_bars(
            episodes=episodes, bars_by_symbol=bars_by_symbol, captured_at=captured_at,
            preferred_series=tuple(self._pinned_series), **limits,
        )
        journal = self.journal.merge(capture.bars, **limits)
        self._pinned_series = {bar.series_key for bar in journal.bars}
        # Only returned durable clocks authorize training. Retrieval of an old
        # history now must never manufacture an earlier forecast or evaluation.
        as_of = max(
            captured_at, parse_utc_timestamp(self.clock(), "clock"),
            *(bar.effective_available_at for bar in journal.bars),
            *((self._last_run_at,) if self._last_run_at is not None else ()),
        )
        grouped: dict[tuple[str, ...], list[ObservedDynamicsBar]] = {}
        for bar in journal.bars:
            grouped.setdefault(bar.series_key, []).append(bar)
        if len(grouped) > self.config.max_series or any(
            len(rows) > self.config.max_bars_per_series for rows in grouped.values()
        ):
            raise ValueError("dynamics journal returned evidence outside its configured bounds")
        for key in tuple(self._cache):
            if key not in grouped:
                del self._cache[key]
        reports: list[DynamicsScopeRun] = []
        series: list[dict[str, object]] = []
        errors: list[dict[str, object]] = []
        replayed_series = 0
        for key, rows in sorted(grouped.items()):
            bars = tuple(sorted(rows, key=lambda bar: (bar.completed_end_at, bar.evidence_id)))
            scope = _scope(bars[0])
            fingerprint = canonical_sha256({
                "evidence": [(bar.evidence_id, bar.payload_hash) for bar in bars],
                "configuration": asdict(self.config),
            })
            cached = self._cache.get(key)
            unchanged = cached is not None and cached.evidence_fingerprint == fingerprint
            transition_ids = cached.transition_ids if unchanged else _transition_ids(bars)
            new_transitions = len(transition_ids - cached.transition_ids) if cached is not None else len(transition_ids)
            if unchanged:
                assert cached is not None
                run = cached.run
                interval = parse_bar_interval(run.scope["bar_interval"])
                latest = run.result.latest_origin_end_at
                if run.result.status == "ready" and latest is not None and interval is not None and as_of - latest >= interval:
                    run = replace(
                        run, request=replace(run.request, as_of=as_of),
                        result=replace(run.result, status="stale_origin", trajectories=()),
                    )
                    self._cache[key] = replace(cached, run=run)
            else:
                try:
                    request = self.config.request(as_of)
                    result = run_dynamics_replay(bars, request)
                except (ArithmeticError, TypeError, ValueError) as exc:
                    error = {**scope, "status": "error", "error": f"{type(exc).__name__}:{exc}"}
                    series.append(error)
                    errors.append(error)
                    continue
                run = DynamicsScopeRun(scope, request, result)
                self._cache[key] = _CachedScope(fingerprint, transition_ids, run)
                replayed_series += 1
                self._last_run_at = as_of
            reports.append(run)
            series.append({
                **scope, "status": run.result.status,
                "execution": "reused_latest" if unchanged else "replayed",
                "as_of": run.request.as_of.isoformat(),
                "origin_end_at": run.result.latest_origin_end_at.isoformat() if run.result.latest_origin_end_at else None,
                "bars": run.result.unique_anchors, "window_transitions": run.result.transitions,
                "new_transitions": new_transitions,
                "support": run.result.support, "paths": len(run.result.trajectories),
                "model_id": run.result.model_id, "fingerprint": run.result.model_fingerprint,
                "training_cutoff": run.result.training_cutoff.isoformat() if run.result.training_cutoff else None,
            })
        statuses = {run.result.status for run in reports}
        if errors:
            status, reason = "partial", "series_replay_error"
        elif "ready" in statuses:
            status, reason = "ready", "shadow_trajectories_available"
        elif statuses and statuses <= {"stale_origin"}:
            status, reason = "stale_origin", "no_fresh_completed_origin"
        elif statuses:
            status, reason = "warming_up", "insufficient_adjacent_transition_support"
        else:
            status, reason = "waiting_for_data", "no_completed_market_bars"
        summary: dict[str, object] = {
            "schema_version": "world_dynamics_status.v1", "enabled": True,
            "status": status, "reason": reason,
            "authority": "shadow_only", "decision_effect": "none", "recommendation": "NO_GO",
            "causal_claim": False, "pnl_claim": False,
            "captured_at": captured_at.isoformat(), "updated_at": as_of.isoformat(),
            "last_run_at": self._last_run_at.isoformat() if self._last_run_at else None,
            "bounds": asdict(self.config),
            "scope_policy": "persisted_first_series_pinned_until_explicit_reset",
            "training_policy": "deterministic_rebuild_of_retained_first_known_evidence",
            "capture": {"bars": len(capture.bars), "selected_series": capture.selected_series,
                        "exclusions": dict(capture.exclusions)},
            "journal": {
                "retained_bars": len(journal.bars), "accepted_series": journal.accepted_series,
                "new_bars": journal.new_bars, "duplicate_bars": journal.duplicate_bars,
                "conflicting_bars": journal.conflicting_bars, "evicted_bars": journal.evicted_bars,
                "rejected_series": journal.rejected_series,
            },
            "replayed_series": replayed_series, "series": series, "errors": errors,
        }
        publication = DynamicsCycleResult(summary, tuple(reports))
        self.publisher.publish(publication)
        return publication
