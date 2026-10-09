"""Capture already-fetched bars without inventing historical availability.

These values are journal candidates, never WorldEpisodes. A journal must
replace provisional recorded_at with its actual durable clock before replay.
Only the small market metadata surface of a real scope episode is consumed.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from trader.domain.world_dynamics import (
    OBSERVED_DYNAMICS_FEATURE_CONTRACT_VERSION,
    OBSERVED_DYNAMICS_SAMPLING_POLICY_VERSION,
    ObservedDynamicsBar,
)
from trader.domain.world_episode import (
    AnchorBar,
    WorldEpisode,
    completed_bar_cutoff,
    is_eligible_completed_bar,
    parse_utc_timestamp,
)

MAX_PROVIDER_ROWS_PER_SERIES = 8192


@dataclass(frozen=True)
class DynamicsCaptureResult:
    bars: tuple[ObservedDynamicsBar, ...]
    selected_series: int
    exclusions: tuple[tuple[str, int], ...]


def _scope_key(episode: WorldEpisode) -> tuple[str, ...]:
    observation = episode.observation
    return (
        observation.venue, observation.symbol, observation.bar_interval,
        OBSERVED_DYNAMICS_FEATURE_CONTRACT_VERSION,
        OBSERVED_DYNAMICS_SAMPLING_POLICY_VERSION,
        observation.anchor.source, observation.anchor.timestamp_semantics,
    )


def capture_dynamics_bars(
    *, episodes: Iterable[object], bars_by_symbol: Mapping[str, object],
    captured_at: datetime | str, max_series: int = 4, max_bars_per_series: int = 256,
    preferred_series: Sequence[tuple[str, ...]] = (),
) -> DynamicsCaptureResult:
    """Capture at most the latest bounded completed bars per admitted scope.

    Existing pinned scopes take priority. Provider input is validated before
    chronological retention; ambiguous equal-time OHLCV slots are excluded,
    and identical duplicates do not multiply training support. Oversized
    snapshots are refused explicitly rather than scanned without a bound.
    """

    captured = parse_utc_timestamp(captured_at, "captured_at")
    for name, value, upper in (("max_series", max_series, 4),
                               ("max_bars_per_series", max_bars_per_series, 256)):
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= upper:
            raise ValueError(f"{name} must be an integer in [1, {upper}]")
    exclusions: Counter[str] = Counter()
    scopes: dict[tuple[str, ...], WorldEpisode] = {}
    by_symbol: dict[str, set[tuple[str, ...]]] = {}
    for episode in episodes:
        if type(episode) is not WorldEpisode or not episode.training_eligible:
            exclusions["ineligible_scope_episode"] += 1
            continue
        key = _scope_key(episode)
        scopes.setdefault(key, episode)
        by_symbol.setdefault(episode.observation.symbol, set()).add(key)
    ambiguous = {symbol for symbol, keys in by_symbol.items() if len(keys) > 1}
    for key in tuple(scopes):
        if key[1] in ambiguous:
            exclusions["ambiguous_scope_metadata"] += 1
            del scopes[key]
    preferred = set(preferred_series)
    keys = sorted(scopes, key=lambda key: (key not in preferred, key))
    selected = keys[:max_series]
    exclusions["scope_limit"] += max(0, len(keys) - len(selected))
    captured_bars: list[ObservedDynamicsBar] = []
    for key in selected:
        episode = scopes[key]
        observation = episode.observation
        raw_bars = bars_by_symbol.get(observation.symbol, ())
        if not isinstance(raw_bars, Sequence) or isinstance(raw_bars, (str, bytes, bytearray)):
            exclusions["invalid_bar_snapshot"] += 1
            continue
        if len(raw_bars) > MAX_PROVIDER_ROWS_PER_SERIES:
            exclusions["provider_row_limit"] += 1
            continue
        slots: dict[datetime, list[ObservedDynamicsBar]] = {}
        for payload in raw_bars:
            if not isinstance(payload, Mapping):
                exclusions["invalid_market_bar"] += 1
                continue
            if any(payload.get(name) != expected for name, expected in (
                ("source", observation.anchor.source),
                ("interval", observation.bar_interval),
                ("timestamp_semantics", observation.anchor.timestamp_semantics),
            )):
                exclusions["bar_series_mismatch"] += 1
                continue
            try:
                available = parse_utc_timestamp(payload.get("available_at"), "available_at")
                anchor = AnchorBar(
                    ts=payload.get("ts"), open=payload.get("open"), high=payload.get("high"),
                    low=payload.get("low"), close=payload.get("close"), volume=payload.get("volume"),
                    source=payload.get("source"), timestamp_semantics=payload.get("timestamp_semantics"),
                )
                end_at = completed_bar_cutoff(
                    as_of_bar_ts=anchor.ts, timestamp_semantics=anchor.timestamp_semantics,
                    bar_interval=observation.bar_interval,
                )
                if end_at is None or not is_eligible_completed_bar(
                    ts=anchor.ts, bar_interval=observation.bar_interval,
                    timestamp_semantics=anchor.timestamp_semantics, end_at=end_at,
                    available_at=available,
                ):
                    exclusions["noncanonical_or_unavailable_bar"] += 1
                    continue
                if available > captured or end_at > captured:
                    exclusions["future_or_incomplete_bar"] += 1
                    continue
                candidate = ObservedDynamicsBar(
                    venue=observation.venue, symbol=observation.symbol,
                    bar_interval=observation.bar_interval, anchor=anchor,
                    first_seen_at=captured, recorded_at=captured,
                )
            except (TypeError, ValueError):
                exclusions["invalid_market_bar"] += 1
                continue
            slots.setdefault(end_at, []).append(candidate)
        admitted: list[ObservedDynamicsBar] = []
        for end_at in sorted(slots):
            rows = slots[end_at]
            if len({row.evidence_id for row in rows}) > 1:
                exclusions["ambiguous_bar_slot"] += len(rows)
                continue
            admitted.append(rows[0])
            exclusions["duplicate_identical_bar"] += len(rows) - 1
        exclusions["history_retention_limit"] += max(0, len(admitted) - max_bars_per_series)
        captured_bars.extend(admitted[-max_bars_per_series:])
    return DynamicsCaptureResult(
        bars=tuple(captured_bars), selected_series=len(selected),
        exclusions=tuple(sorted((reason, count) for reason, count in exclusions.items() if count)),
    )
