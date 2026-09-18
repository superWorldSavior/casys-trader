"""Prospective World-graph capture. Market and context capture stay frozen.

One graph companion is attached per market slot. Cutoff is the completed-bar
clock. Missing, unpublished, unmapped, or budget-exceeded graph is encoded as
snapshot missingness; it does not drop market or context episodes. Unmapped or
ambiguous ``WorldScopeResolution`` never invents an instrument root, venue, or
topology. A builder failure skips the graph companion (fail-open).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from trader.application.world_model.graph_features import encode_world_graph_features
from trader.application.world_model.graph_snapshot import WorldGraphSnapshotRequest
from trader.domain.world_episode import (
    MARKET_FEATURE_CONTRACT_ID,
    WorldEpisode,
    WorldObservation,
    completed_bar_cutoff,
)
from trader.domain.world_feature_contract import (
    GRAPH_FEATURE_CONTRACT_ID,
    WorldFeatureMask,
    graph_content_mask,
)
from trader.domain.world_graph import WorldGraphSnapshot, world_instrument_root_for_resolution
from trader.domain.world_scope import WorldMarketAnchorRef, WorldScopeMapping


@dataclass(frozen=True)
class WorldGraphCaptureConfig:
    """Injected graph capture config. Cohort id is constructor-injected, never file-loaded."""

    scope_mapping: WorldScopeMapping
    snapshot_service: object
    study_cohort_id: str | None = None
    feature_mask: WorldFeatureMask | None = None
    max_depth: int | None = None
    max_paths: int | None = None
    ontology_revision: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.scope_mapping, WorldScopeMapping):
            raise TypeError("scope_mapping must be WorldScopeMapping")
        if self.study_cohort_id is not None and not str(self.study_cohort_id).strip():
            raise ValueError("study_cohort_id must be a non-empty string when provided")
        ontology_revision = None if self.ontology_revision is None else str(self.ontology_revision).strip()
        if self.ontology_revision is not None and not ontology_revision:
            raise ValueError("ontology_revision must be a non-empty string when provided")
        object.__setattr__(self, "ontology_revision", ontology_revision)


def attach_world_graph(
    episodes: Sequence[WorldEpisode],
    config: WorldGraphCaptureConfig,
) -> tuple[WorldEpisode, ...]:
    """Project one graph episode per market episode without widening market or context."""

    if not isinstance(config, WorldGraphCaptureConfig):
        raise TypeError("config must be WorldGraphCaptureConfig")
    attached: list[WorldEpisode] = []
    for episode in episodes:
        companion = _attach_one(episode, config)
        if companion is not None:
            attached.append(companion)
    return tuple(attached)


def _attach_one(episode: WorldEpisode, config: WorldGraphCaptureConfig) -> WorldEpisode | None:
    observation = episode.observation
    if observation.feature_contract_version != MARKET_FEATURE_CONTRACT_ID:
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
        root = world_instrument_root_for_resolution(resolution, instrument=observation.symbol)
        request = WorldGraphSnapshotRequest(
            episode=episode,
            root_entity=root,
            cutoff_at=cutoff,
            scope_mapping=config.scope_mapping,
            scope_resolution=resolution,
            max_depth=config.max_depth,
            max_paths=config.max_paths,
            ontology_revision=config.ontology_revision,
        )
        bundle = config.snapshot_service.build(request)
        _persist_snapshot(config.snapshot_service, bundle.snapshot)
        mask = config.feature_mask if config.feature_mask is not None else graph_content_mask()
        encoded = encode_world_graph_features(bundle, mask=mask)
        graph_observation = WorldObservation(
            venue=observation.venue,
            symbol=observation.symbol,
            bar_interval=observation.bar_interval,
            as_of_bar_ts=observation.as_of_bar_ts,
            feature_contract_version=GRAPH_FEATURE_CONTRACT_ID,
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
            observation=graph_observation,
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


__all__ = [
    "WorldGraphCaptureConfig",
    "attach_world_graph",
]
