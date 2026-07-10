"""Pure cross-venue family context for independent universe agents."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from typing import Any

from trader.domain.universe.intelligence import (
    UniverseSituationContext,
    build_family_snapshot,
)

DEFAULT_EXPECTED_VENUES = ("TW", "EU", "US")
DEFAULT_MAX_OBSERVATIONS_PER_FAMILY = 2


def build_global_family_board(
    *,
    as_of: str,
    scopes: Mapping[str, Mapping[str, Any]],
    situations: Mapping[str, UniverseSituationContext],
    expected_venues: Iterable[str] = DEFAULT_EXPECTED_VENUES,
    max_observations_per_family: int = DEFAULT_MAX_OBSERVATIONS_PER_FAMILY,
) -> dict[str, Any]:
    """Build a bounded comparison board with no allocation authority."""

    expected = tuple(
        dict.fromkeys(
            str(venue or "").strip().upper()
            for venue in expected_venues
            if str(venue or "").strip()
        )
    )
    venue_payloads: dict[str, dict[str, Any]] = {}
    scope_venues: list[str] = []
    active_brief_venues: list[str] = []
    stale_scope_venues: list[str] = []
    expired_scope_venues: list[str] = []
    observation_limit = max(0, int(max_observations_per_family))

    for venue in expected:
        scope = scopes.get(venue)
        if not isinstance(scope, Mapping):
            venue_payloads[venue] = {
                "status": "scope_missing",
                "scope_id": None,
                "brief_status": "not_available",
                "families": {},
            }
            continue
        scope_venues.append(venue)
        situation = situations.get(venue) or UniverseSituationContext.not_available(
            candidate_count=len(scope.get("candidates") or ())
        )
        if situation.status == "active":
            active_brief_venues.append(venue)
        freshness, scope_age_hours = _scope_freshness(scope.get("as_of"), as_of=as_of)
        if freshness == "stale":
            stale_scope_venues.append(venue)
        elif freshness == "expired":
            expired_scope_venues.append(venue)
        family_snapshot = build_family_snapshot(
            scope.get("candidates") or (),
            scope.get("default_hotlist") or (),
            scope.get("sticky_context_at_close") or (),
        )
        ranked_families = sorted(
            family_snapshot,
            key=lambda family: (
                family_snapshot[family].get("average_attractiveness") is None,
                -float(family_snapshot[family].get("average_attractiveness") or 0.0),
                family,
            ),
        )
        venue_families: dict[str, dict[str, Any]] = {}
        for rank, family in enumerate(ranked_families, start=1):
            snapshot = family_snapshot[family]
            points = situation.families.get(family) or ()
            venue_families[family] = {
                "radar_rank_within_venue": rank,
                "candidate_count": snapshot["candidate_count"],
                "baseline_count": len(snapshot["baseline_symbols"]),
                "challenger_count": len(snapshot["challenger_symbols"]),
                "average_attractiveness": snapshot["average_attractiveness"],
                "bias_counts": dict(snapshot["bias_counts"]),
                "situation_status": (
                    "observed"
                    if points
                    else "not_reported"
                    if situation.status == "active"
                    else situation.status
                ),
                "situation": _summarize_points(
                    points,
                    max_observations=observation_limit,
                ),
            }
        venue_payloads[venue] = {
            "status": "ready",
            "scope_id": scope.get("candidate_scope_id"),
            "scope_as_of": scope.get("as_of"),
            "scope_freshness": freshness,
            "scope_age_hours": scope_age_hours,
            "parent_scope_id": scope.get("parent_candidate_scope_id"),
            "parent_close_at": scope.get("parent_close_at"),
            "brief_status": situation.status,
            "brief_ref": dict(situation.brief_ref or {}),
            "candidate_count": len(scope.get("candidates") or ()),
            "baseline_count": len(scope.get("default_hotlist") or ()),
            "family_count": len(venue_families),
            "families": venue_families,
        }

    missing_scope_venues = [venue for venue in expected if venue not in scope_venues]
    missing_brief_venues = [venue for venue in expected if venue not in active_brief_venues]
    if not scope_venues:
        status = "unavailable"
    elif not missing_scope_venues and not missing_brief_venues:
        status = "complete"
    else:
        status = "partial"
    material = {
        "schema_version": 1,
        "status": status,
        "role": "comparative_context_not_capital_allocation",
        "coverage": {
            "expected_venues": list(expected),
            "scope_venues": scope_venues,
            "active_brief_venues": active_brief_venues,
            "missing_scope_venues": missing_scope_venues,
            "missing_brief_venues": missing_brief_venues,
            "stale_scope_venues": stale_scope_venues,
            "expired_scope_venues": expired_scope_venues,
        },
        "venues": venue_payloads,
    }
    board_id = _board_id(material)
    return {**material, "board_id": board_id, "as_of": str(as_of or "").strip()}


def _summarize_points(
    points: Iterable[Mapping[str, Any]],
    *,
    max_observations: int,
) -> dict[str, Any]:
    rows = [dict(point) for point in points if isinstance(point, Mapping)]
    direction_counts = Counter(
        str(point.get("direction") or "unspecified") for point in rows
    )
    signal_counts = Counter(str(point.get("signal") or "weak") for point in rows)
    severity_counts = Counter(str(point.get("severity") or "info") for point in rows)
    observations = [
        {
            key: point[key]
            for key in (
                "point",
                "direction",
                "signal",
                "severity",
                "horizon",
                "sources",
                "source_refs",
            )
            if key in point
        }
        for point in rows[:max_observations]
    ]
    return {
        "point_count": len(rows),
        "direction_counts": dict(sorted(direction_counts.items())),
        "signal_counts": dict(sorted(signal_counts.items())),
        "severity_counts": dict(sorted(severity_counts.items())),
        "observations": observations,
        "truncated": len(rows) > max_observations,
    }


def _board_id(material: Mapping[str, Any]) -> str:
    identity = json.loads(json.dumps(material, ensure_ascii=False))
    for venue_state in (identity.get("venues") or {}).values():
        if isinstance(venue_state, dict):
            venue_state.pop("scope_age_hours", None)
    encoded = json.dumps(
        identity,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return f"global_family_board:v1:{hashlib.sha256(encoded).hexdigest()}"


def _scope_freshness(value: Any, *, as_of: str) -> tuple[str, float | None]:
    scope_at = _parse_datetime(value)
    board_at = _parse_datetime(as_of)
    if scope_at is None or board_at is None:
        return "unknown", None
    age_hours = max(0.0, (board_at - scope_at).total_seconds() / 3600.0)
    if age_hours <= 36:
        status = "fresh"
    elif age_hours <= 96:
        status = "stale"
    else:
        status = "expired"
    return status, round(age_hours, 3)


def _parse_datetime(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return (
        parsed.astimezone(timezone.utc)
        if parsed.tzinfo is not None
        else parsed.replace(tzinfo=timezone.utc)
    )
