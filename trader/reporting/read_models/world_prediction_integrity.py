"""Shared integrity policy for persisted World shadow-prediction envelopes.

The policy is intentionally path-aware: ``target`` remains a forbidden Trader
feature everywhere except as the canonical endpoint of a serialized ontology
edge carried by a World context snapshot.
"""

from __future__ import annotations

from collections.abc import Mapping

from trader.domain.world_context import TopologyEdge


_FORBIDDEN_TRADER_FEATURE_KEYS = frozenset(
    {
        "decision",
        "decision_id",
        "fill",
        "order_id",
        "pnl",
        "policy",
        "portfolio",
        "position",
        "target",
    }
)
_TOPOLOGY_COLLECTION_PATHS = frozenset(
    {
        ("input", "context", "topology_edges"),
        ("input", "context", "topology_path"),
    }
)
_TOPOLOGY_EDGE_KEYS = frozenset(
    {
        "kind",
        "source",
        "target",
        "effective_from",
        "effective_until",
        "ready_at",
        "ontology_revision",
        "source_refs",
        "content_sha256",
    }
)
_ENTITY_REF_KEYS = frozenset({"kind", "entity_id"})
_PathPart = str | int


def contains_forbidden_trader_feature(value: object) -> bool:
    """Return whether a persisted prediction leaks Trader-owned information.

    Canonical ontology edges are domain objects, not prediction targets.  Their
    ``target`` key is allowed only at the two exact snapshot collection paths;
    malformed, relocated, or extended edge-shaped mappings remain forbidden.
    """

    return _contains_forbidden(value, path=())


def _contains_forbidden(value: object, *, path: tuple[_PathPart, ...]) -> bool:
    if isinstance(value, Mapping):
        canonical_edge = _is_canonical_topology_edge(value, path=path)
        for raw_key, item in value.items():
            key = str(raw_key)
            if key in _FORBIDDEN_TRADER_FEATURE_KEYS and not (key == "target" and canonical_edge):
                return True
            if _contains_forbidden(item, path=(*path, key)):
                return True
        return False
    if isinstance(value, (list, tuple)):
        return any(_contains_forbidden(item, path=(*path, index)) for index, item in enumerate(value))
    return False


def _is_canonical_topology_edge(value: Mapping[object, object], *, path: tuple[_PathPart, ...]) -> bool:
    if len(path) != 4 or not isinstance(path[-1], int) or tuple(path[:3]) not in _TOPOLOGY_COLLECTION_PATHS:
        return False
    if set(value) != _TOPOLOGY_EDGE_KEYS:
        return False
    for endpoint_name in ("source", "target"):
        endpoint = value.get(endpoint_name)
        if not isinstance(endpoint, Mapping) or set(endpoint) != _ENTITY_REF_KEYS:
            return False
    try:
        TopologyEdge.from_mapping(value)
    except (TypeError, ValueError):
        return False
    return True


__all__ = ["contains_forbidden_trader_feature"]
