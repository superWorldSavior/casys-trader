"""Append-only World scope-mapping generations.

``world_scope_mapping.v1`` is the schema/contract id. Content hash is the
generation identity. Existing rows are never rewritten; a content change
records a new immutable mapping object for a future cohort pin.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from trader.domain.world_scope import WORLD_SCOPE_MAPPING_SCHEMA, WorldScopeMapping, WorldScopeMappingEntry


MAPPING_GENERATION_ACTIONS = frozenset({"ready", "record_generation"})


def _required_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a non-empty string")
    text = value.strip()
    if not text:
        raise ValueError(f"{field_name} must be a non-empty string")
    return text


def _anchor_tuple(value: Sequence[tuple[str, str]] | None) -> tuple[tuple[str, str], ...]:
    if value is None:
        return ()
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise TypeError("anchors must be a sequence of (market_venue, instrument)")
    return tuple((_required_text(venue, "market_venue"), _required_text(instrument, "instrument")) for venue, instrument in value)


@dataclass(frozen=True)
class WorldScopeMappingGenerationPlan:
    """Classify whether mapping content is unchanged or a new generation."""

    action: str
    mapping: WorldScopeMapping | Mapping[str, Any]
    added_anchors: Sequence[tuple[str, str]] = ()
    preserved_conflicts: Sequence[tuple[str, str]] = ()

    def __post_init__(self) -> None:
        action = _required_text(self.action, "action")
        if action not in MAPPING_GENERATION_ACTIONS:
            allowed = ", ".join(sorted(MAPPING_GENERATION_ACTIONS))
            raise ValueError(f"generation action must be one of: {allowed}")
        mapping = WorldScopeMapping.from_mapping(self.mapping)
        if mapping.mapping_id != WORLD_SCOPE_MAPPING_SCHEMA:
            raise ValueError(f"mapping_id must be {WORLD_SCOPE_MAPPING_SCHEMA}")
        if mapping.schema_version != WORLD_SCOPE_MAPPING_SCHEMA:
            raise ValueError(f"schema_version must be {WORLD_SCOPE_MAPPING_SCHEMA}")
        added = _anchor_tuple(self.added_anchors)
        conflicts = _anchor_tuple(self.preserved_conflicts)
        if action == "ready" and added:
            raise ValueError("ready generation plan must not add anchors")
        if action == "record_generation" and not added:
            raise ValueError("record_generation requires at least one added anchor")
        object.__setattr__(self, "action", action)
        object.__setattr__(self, "mapping", mapping)
        object.__setattr__(self, "added_anchors", added)
        object.__setattr__(self, "preserved_conflicts", conflicts)


def plan_world_scope_mapping_generation(
    *,
    current: WorldScopeMapping,
    proposed: Sequence[WorldScopeMappingEntry] = (),
) -> WorldScopeMappingGenerationPlan:
    """Merge proposed rows append-only. Same schema id, new hash on content change."""

    if not isinstance(current, WorldScopeMapping):
        raise TypeError("current must be WorldScopeMapping")
    if current.mapping_id != WORLD_SCOPE_MAPPING_SCHEMA:
        raise ValueError(f"mapping_id must be {WORLD_SCOPE_MAPPING_SCHEMA}")
    if isinstance(proposed, (str, bytes, bytearray)) or not isinstance(proposed, Sequence):
        raise TypeError("proposed must be a sequence of WorldScopeMappingEntry")
    existing = {(entry.anchor.market_venue, entry.anchor.instrument): entry for entry in current.entries}
    added: list[WorldScopeMappingEntry] = []
    conflicts: list[tuple[str, str]] = []
    seen_new: set[tuple[str, str]] = set()
    for item in proposed:
        entry = item if isinstance(item, WorldScopeMappingEntry) else WorldScopeMappingEntry.from_mapping(item)
        key = (entry.anchor.market_venue, entry.anchor.instrument)
        current_row = existing.get(key)
        if current_row is not None:
            if current_row.output_key() != entry.output_key():
                conflicts.append(key)
            continue
        if key in seen_new:
            continue
        seen_new.add(key)
        added.append(entry)
    if not added:
        return WorldScopeMappingGenerationPlan(
            action="ready",
            mapping=current,
            added_anchors=(),
            preserved_conflicts=tuple(conflicts),
        )
    merged = WorldScopeMapping(
        mapping_id=WORLD_SCOPE_MAPPING_SCHEMA,
        entries=tuple(current.entries) + tuple(added),
        schema_version=WORLD_SCOPE_MAPPING_SCHEMA,
    )
    return WorldScopeMappingGenerationPlan(
        action="record_generation",
        mapping=merged,
        added_anchors=tuple((entry.anchor.market_venue, entry.anchor.instrument) for entry in added),
        preserved_conflicts=tuple(conflicts),
    )


__all__ = [
    "MAPPING_GENERATION_ACTIONS",
    "WorldScopeMappingGenerationPlan",
    "plan_world_scope_mapping_generation",
]
