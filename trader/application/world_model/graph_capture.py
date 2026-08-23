"""Prospective V3 World-graph capture. V1/V2 capture stays frozen.

One V3 companion is attached per V1 market slot. Cutoff is the completed-bar
clock. Missing, unpublished, unmapped, or budget-exceeded graph is encoded as
snapshot missingness; it does not drop V1/V2. A builder failure skips the V3
companion (fail-open).
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from trader.application.world_model.capture import (
    FEATURE_CONTRACT_VERSION,
    SAMPLING_POLICY_VERSION,
    capture_world_episodes,
)
from trader.application.world_model.graph_features import encode_world_graph_features
from trader.application.world_model.graph_snapshot import WorldGraphSnapshotRequest
from trader.domain.world_episode import (
    MARKET_FEATURE_CONTRACT_VERSION,
    WorldEpisode,
    WorldObservation,
    completed_bar_cutoff,
)
from trader.domain.world_feature_contract import (
    GRAPH_FEATURE_CONTRACT_VERSION,
    WorldFeatureMask,
    world_v3_graph_content_mask,
)
from trader.domain.world_graph import WorldEntityRef, WorldGraphSnapshot
from trader.domain.world_scope import WorldMarketAnchorRef, WorldScopeMapping, WorldScopeResolution


_MIC_RE = re.compile(r"^[A-Z0-9]{4}$")


@dataclass(frozen=True)
class WorldGraphCaptureConfig:
    """Injected V3 capture config. Cohort id is constructor-injected, never file-loaded."""

    scope_mapping: WorldScopeMapping
    snapshot_service: object
    study_cohort_id: str | None = None
    feature_mask: WorldFeatureMask | None = None
    max_depth: int | None = None
    max_paths: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.scope_mapping, WorldScopeMapping):
            raise TypeError("scope_mapping must be WorldScopeMapping")
        if self.study_cohort_id is not None and not str(self.study_cohort_id).strip():
            raise ValueError("study_cohort_id must be a non-empty string when provided")


def attach_world_graph(
    episodes: Sequence[WorldEpisode],
    config: WorldGraphCaptureConfig,
) -> tuple[WorldEpisode, ...]:
    """Project one V3 graph episode per V1 market episode without widening V1/V2."""

    if not isinstance(config, WorldGraphCaptureConfig):
        raise TypeError("config must be WorldGraphCaptureConfig")
    attached: list[WorldEpisode] = []
    for episode in episodes:
        companion = _attach_one(episode, config)
        if companion is not None:
            attached.append(companion)
    return tuple(attached)


def capture_world_episodes_with_graph(
    active_symbols: Sequence[object],
    tradable_symbols: Sequence[object] | None,
    bars_by_symbol: Mapping[object, Sequence[object]],
    market_metadata_by_symbol: Mapping[object, Mapping[str, object]] | None,
    config: WorldGraphCaptureConfig,
    *,
    source: str | None = None,
    interval: str | None = None,
    timestamp_semantics: str | None = None,
    captured_at: datetime | str | None = None,
) -> tuple[tuple[WorldEpisode, ...], tuple[WorldEpisode, ...]]:
    """Return ``(v1_episodes, v3_episodes)`` from one frozen market cohort."""

    v1_episodes = capture_world_episodes(
        active_symbols,
        tradable_symbols,
        bars_by_symbol,
        market_metadata_by_symbol,
        source=source,
        interval=interval,
        timestamp_semantics=timestamp_semantics,
        captured_at=captured_at,
        feature_contract_version=FEATURE_CONTRACT_VERSION,
        sampling_policy_version=SAMPLING_POLICY_VERSION,
    )
    try:
        v3_episodes = attach_world_graph(v1_episodes, config)
    except Exception:
        v3_episodes = ()
    return v1_episodes, v3_episodes


def _attach_one(episode: WorldEpisode, config: WorldGraphCaptureConfig) -> WorldEpisode | None:
    observation = episode.observation
    if observation.feature_contract_version != MARKET_FEATURE_CONTRACT_VERSION:
        return None
    cutoff = completed_bar_cutoff(
        as_of_bar_ts=observation.as_of_bar_ts,
        timestamp_semantics=observation.anchor.timestamp_semantics,
        bar_interval=observation.bar_interval,
    )
    if cutoff is None:
        return None
    if observation.available_at is None or cutoff > observation.available_at:
        return None
    try:
        resolution = config.scope_mapping.resolve(
            WorldMarketAnchorRef(market_venue=observation.venue, instrument=observation.symbol)
        )
        root = _root_entity(observation, config.scope_mapping, resolution)
        if root is None:
            return None
        request = WorldGraphSnapshotRequest(
            episode=episode,
            root_entity=root,
            cutoff_at=cutoff,
            scope_mapping=config.scope_mapping,
            scope_resolution=resolution,
            max_depth=config.max_depth,
            max_paths=config.max_paths,
        )
        bundle = config.snapshot_service.build(request)
        _persist_snapshot(config.snapshot_service, bundle.snapshot)
        mask = config.feature_mask if config.feature_mask is not None else world_v3_graph_content_mask()
        encoded = encode_world_graph_features(bundle, mask=mask)
        v3_observation = WorldObservation(
            venue=observation.venue,
            symbol=observation.symbol,
            bar_interval=observation.bar_interval,
            as_of_bar_ts=observation.as_of_bar_ts,
            feature_contract_version=GRAPH_FEATURE_CONTRACT_VERSION,
            sampling_policy_version=observation.sampling_policy_version,
            anchor=observation.anchor,
            available_at=observation.available_at,
            captured_at=observation.captured_at,
            freshness=observation.freshness,
            categorical_features=dict(observation.categorical_features),
            numeric_features=dict(observation.numeric_features),
            graph=bundle.snapshot,
            graph_features=encoded.to_observation_payload(),
        )
        return WorldEpisode(
            observation=v3_observation,
            training_eligible=episode.training_eligible,
            training_reason=episode.training_reason,
        )
    except Exception:
        return None


def _persist_snapshot(service: object, snapshot: WorldGraphSnapshot) -> None:
    persist = getattr(service, "persist", None)
    if not callable(persist):
        return
    try:
        persist(snapshot)
    except Exception:
        return


def _root_entity(
    observation: WorldObservation,
    mapping: WorldScopeMapping,
    resolution: WorldScopeResolution,
) -> WorldEntityRef | None:
    symbol = observation.symbol
    if resolution.status == "resolved":
        venue = next(scope for scope in resolution.scopes if scope.kind == "venue")
        return WorldEntityRef(kind="instrument", entity_id=f"{venue.entity_id}:symbol:{symbol}")
    venue_ids = {
        entry.venue.entity_id for entry in mapping.entries if entry.anchor.market_venue == observation.venue
    }
    if len(venue_ids) == 1:
        return WorldEntityRef(kind="instrument", entity_id=f"{next(iter(venue_ids))}:symbol:{symbol}")
    instrument_venues = {
        entry.venue.entity_id for entry in mapping.entries if entry.anchor.instrument == symbol
    }
    if len(instrument_venues) == 1:
        return WorldEntityRef(kind="instrument", entity_id=f"{next(iter(instrument_venues))}:symbol:{symbol}")
    venue = observation.venue
    if venue.startswith("mic:") and _MIC_RE.fullmatch(venue[4:]):
        return WorldEntityRef(kind="instrument", entity_id=f"{venue}:symbol:{symbol}")
    if _MIC_RE.fullmatch(venue):
        return WorldEntityRef(kind="instrument", entity_id=f"mic:{venue}:symbol:{symbol}")
    return None


__all__ = [
    "WorldGraphCaptureConfig",
    "attach_world_graph",
    "capture_world_episodes_with_graph",
]
