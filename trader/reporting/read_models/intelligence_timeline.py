"""Read-only intelligence timelines for the browser desk.

The canonical intelligence stores are append-only JSONL ledgers.  This module
projects them into bounded, presentation-ready views without moving business
logic to TypeScript or mutating any runtime state.
"""

from __future__ import annotations

import gzip
import json
import zlib
from collections import Counter
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from trader.application.record.decision_ledger_rows import decision_row_mandate_ref
from trader.domain.semantic.catalog import FAMILIES
from trader.infrastructure.state_db._jsonl_store import (
    read_json_object,
    read_jsonl_objects,
)
from trader.interfaces.cockpit.projections.universe import venue_of_safe

DEFAULT_INTELLIGENCE_LOOKBACK_DAYS = 30
DEFAULT_EVENT_LIMIT = 240
VENUES = ("TW", "EU", "US")
VENUE_FAMILY_TAXONOMY = {
    "TW": frozenset(key for key in FAMILIES if key.startswith("tw_")),
    "EU": frozenset(
        [key for key in FAMILIES if key.startswith("eu_")] + ["defense"]
    ),
    "US": frozenset(
        [key for key in FAMILIES if key.startswith("us_")]
        + ["defense", "nasdaq_single_names"]
    ),
}

FAMILY_COMPARISON_GROUPS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (
        "technology",
        "Technology & semiconductors",
        (
            "tw_osat",
            "tw_thermal",
            "tw_pcb",
            "tw_ic_design",
            "tw_memory",
            "tw_connectors",
            "tw_components",
            "tw_ems_odm",
            "tw_foundry",
            "tw_wafer",
            "tw_equipment",
            "tw_services",
            "eu_tech",
            "us_tech",
            "nasdaq_single_names",
        ),
    ),
    (
        "financials",
        "Financials",
        (
            "tw_financials",
            "eu_financials",
            "eu_financials_nordic",
            "us_financials",
        ),
    ),
    (
        "industrials_defense",
        "Industrials & defense",
        (
            "tw_industrials",
            "eu_industrials",
            "eu_industrials_nordic",
            "us_industrials",
            "defense",
        ),
    ),
    (
        "materials",
        "Materials",
        (
            "tw_materials",
            "eu_materials",
            "eu_materials_nordic",
            "us_materials",
        ),
    ),
    (
        "consumer",
        "Consumer",
        (
            "tw_consumer",
            "eu_luxury_consumer",
            "us_consumer_disc",
            "us_consumer_staples",
        ),
    ),
    (
        "healthcare",
        "Healthcare",
        ("tw_healthcare", "eu_healthcare", "eu_healthcare_ext", "us_healthcare"),
    ),
    ("energy", "Energy", ("eu_energy", "us_energy")),
    ("communications", "Communication", ("eu_telecom", "us_communication")),
    ("real_estate", "Real estate", ("eu_real_estate", "us_real_estate")),
    ("utilities", "Utilities", ("eu_utilities", "us_utilities")),
)


def _dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, (list, tuple)) else []


def _text(value: Any) -> str:
    return str(value or "").strip()


def _clip(value: Any, limit: int = 280) -> str:
    text = _text(value)
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _parse_ts(value: Any) -> datetime | None:
    text = _text(value)
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _age_hours(value: Any, *, now: datetime) -> float | None:
    parsed = _parse_ts(value)
    if parsed is None:
        return None
    return max(0.0, (now - parsed).total_seconds() / 3600.0)


def _recent_rows(
    base_dir: Path,
    *,
    now: datetime,
    lookback_days: int,
) -> list[dict[str, Any]]:
    threshold = now - timedelta(days=max(0, lookback_days))
    rows: list[dict[str, Any]] = []
    for offset in range(max(0, lookback_days) + 1):
        day = (now - timedelta(days=offset)).date().isoformat()
        for row in read_jsonl_objects(base_dir / f"{day}.jsonl"):
            parsed = _parse_ts(
                row.get("as_of")
                or row.get("ts")
                or row.get("cycle_ts")
                or row.get("failed_at")
            )
            if parsed is not None and threshold <= parsed <= now:
                rows.append(row)
    return rows


def _dedupe(
    rows: Iterable[dict[str, Any]],
    *,
    identity_fields: tuple[str, ...],
) -> list[dict[str, Any]]:
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for row in sorted(rows, key=_sort_ts, reverse=True):
        identity = next((_text(row.get(field)) for field in identity_fields if _text(row.get(field))), "")
        if not identity:
            identity = "|".join(
                (
                    _text(row.get("as_of") or row.get("ts") or row.get("cycle_ts")),
                    _text(row.get("venue")),
                    _text(row.get("symbol")),
                    _text(row.get("status")),
                )
            )
        if identity in seen:
            continue
        seen.add(identity)
        result.append(row)
    return result


def _sort_ts(row: Mapping[str, Any]) -> float:
    parsed = _parse_ts(
        row.get("as_of")
        or row.get("ts")
        or row.get("cycle_ts")
        or row.get("failed_at")
    )
    return parsed.timestamp() if parsed else 0.0


def _point_summary(payload: Mapping[str, Any]) -> str:
    alerts = _list(payload.get("alerts"))
    for raw in alerts:
        point = _text(_dict(raw).get("point"))
        if point:
            return point
    for section in ("zones", "families", "symbols"):
        for raw_rows in _dict(payload.get(section)).values():
            for raw in _list(raw_rows):
                point = _text(_dict(raw).get("point"))
                if point:
                    return point
    return ""


def _source_refs(payload: Any) -> set[str]:
    refs: set[str] = set()
    if isinstance(payload, Mapping):
        for key, value in payload.items():
            if key == "source_refs":
                refs.update(_text(item) for item in _list(value) if _text(item))
            else:
                refs.update(_source_refs(value))
    elif isinstance(payload, list):
        for item in payload:
            refs.update(_source_refs(item))
    return refs


def _coverage_status(payload: Mapping[str, Any]) -> str | None:
    coverage = payload.get("coverage")
    if isinstance(coverage, Mapping):
        return _text(coverage.get("status")) or None
    input_coverage = _dict(_dict(payload.get("input_refs")).get("coverage"))
    return _text(input_coverage.get("status")) or None


def _changes(
    current: Mapping[str, Any],
    previous: Mapping[str, Any] | None,
    fields: tuple[str, ...],
) -> list[dict[str, Any]]:
    if previous is None:
        return []
    changes: list[dict[str, Any]] = []
    for field in fields:
        before = previous.get(field)
        after = current.get(field)
        if before != after:
            changes.append({"field": field, "from": before, "to": after})
    return changes


def _event(
    *,
    event_id: str,
    kind: str,
    layer: str,
    row: Mapping[str, Any],
    title: str,
    summary: str = "",
    venue: str | None = None,
    symbol: str | None = None,
    status: str = "success",
    linkage: str | None = None,
    refs: Mapping[str, Any] | None = None,
    changes: list[dict[str, Any]] | None = None,
    payload: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "event_id": event_id,
        "kind": kind,
        "layer": layer,
        "as_of": _text(row.get("as_of") or row.get("ts") or row.get("cycle_ts")),
        "status": status,
        "linkage": linkage,
        "venue": venue,
        "symbol": symbol,
        "scope_id": _text(row.get("candidate_scope_id")) or None,
        "mandate_id": _text(row.get("mandate_id")) or None,
        "agent_run_id": _text(row.get("agent_run_id")) or None,
        "title": title,
        "summary": _clip(summary),
        "coverage": _coverage_status(row),
        "source_count": len(_source_refs(row)),
        "refs": {key: value for key, value in dict(refs or {}).items() if value not in (None, "")},
        "changes": list(changes or []),
        "payload": dict(payload or {}),
    }


def _read_latest_jsonl(path: Path) -> dict[str, Any] | None:
    rows = read_jsonl_objects(path)
    return rows[-1] if rows else None


def _current_macro_by_venue(state_dir: Path) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for path in sorted((state_dir / "news_briefs").glob("latest-*.jsonl")):
        row = _read_latest_jsonl(path)
        venue = path.name.removeprefix("latest-").removesuffix(".jsonl")
        if row is not None:
            result[venue] = row
    return result


def _posture_events(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    previous: dict[str, Any] | None = None
    for row in sorted(rows, key=_sort_ts):
        posture_id = _text(row.get("posture_id"))
        events.append(
            _event(
                event_id=posture_id or f"global-posture:{_text(row.get('as_of'))}",
                kind="global_posture",
                layer="world",
                row=row,
                title=f"{_text(row.get('gross_mode')) or 'Global'} · {_text(row.get('net_bias')) or 'neutral'}",
                summary=_text(row.get("rationale")),
                status="success",
                refs={"posture_id": posture_id},
                changes=_changes(
                    row,
                    previous,
                    ("gross_mode", "net_bias", "venue_posture", "family_priority"),
                ),
                payload={
                    "gross_mode": row.get("gross_mode"),
                    "net_bias": row.get("net_bias"),
                    "venue_posture": _dict(row.get("venue_posture")),
                    "family_priority": _dict(row.get("family_priority")),
                    "valid_until": row.get("valid_until"),
                },
            )
        )
        previous = row
    return events


def _digest_events(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    previous: dict[str, Any] | None = None
    for row in sorted(rows, key=_sort_ts):
        digest_id = _text(row.get("digest_id"))
        points = [_dict(item) for item in _list(row.get("points"))]
        events.append(
            _event(
                event_id=digest_id or f"global-digest:{_text(row.get('as_of'))}",
                kind="global_digest",
                layer="world",
                row=row,
                title=f"Regime · {_text(row.get('regime')) or 'unknown'}",
                summary=_text(points[0].get("point")) if points else "",
                status=_text(row.get("status")) or "success",
                refs={"digest_id": digest_id},
                changes=_changes(row, previous, ("regime", "rates_bias", "usd_bias", "status")),
                payload={
                    "regime": row.get("regime"),
                    "rates_bias": row.get("rates_bias"),
                    "usd_bias": row.get("usd_bias"),
                    "points": points,
                    "coverage": _dict(row.get("coverage")),
                    "valid_until": row.get("valid_until"),
                },
            )
        )
        previous = row
    return events


def _board_events(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        _event(
            event_id=_text(row.get("board_id")) or f"family-board:{_text(row.get('as_of'))}",
            kind="family_board",
            layer="family",
            row=row,
            title="Cross-region family board",
            summary=f"{len(_dict(row.get('venues')))} venues · {_text(row.get('status')) or 'unknown'}",
            status=_text(row.get("status")) or "success",
            refs={"board_id": row.get("board_id")},
            payload={"coverage": _dict(row.get("coverage")), "role": row.get("role")},
        )
        for row in rows
    ]


def _macro_events(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for row in rows:
        venue = _text(row.get("venue")) or "GLOBAL"
        brief_id = _text(row.get("brief_id"))
        events.append(
            _event(
                event_id=brief_id or f"macro:{venue}:{_text(row.get('as_of'))}",
                kind="macro_brief",
                layer="world" if venue == "GLOBAL" else "region",
                row=row,
                title=f"{venue} macro brief",
                summary=_point_summary(row),
                venue=None if venue == "GLOBAL" else venue,
                refs={
                    "brief_id": brief_id,
                    "candidate_scope_id": _dict(row.get("input_refs")).get("candidate_scope_id"),
                },
                payload={
                    "alerts": _list(row.get("alerts")),
                    "zones": _dict(row.get("zones")),
                    "families": _dict(row.get("families")),
                    "symbols": _dict(row.get("symbols")),
                    "valid_until": row.get("valid_until"),
                },
            )
        )
    return events


def _failure_events(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        _event(
            event_id=f"global-failure:{_text(row.get('as_of') or row.get('failed_at'))}:{index}",
            kind="global_failure",
            layer="world",
            row=row,
            title=f"Global refresh failed · {_text(row.get('error_code')) or 'unknown'}",
            summary=_text(row.get("error_message") or row.get("message")),
            status="error",
            payload=dict(row),
        )
        for index, row in enumerate(rows)
    ]


def _event_counts(events: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    return dict(Counter(_text(event.get("kind")) for event in events))


def load_world_timeline(
    state_dir: str | Path,
    *,
    now: datetime | None = None,
    lookback_days: int = DEFAULT_INTELLIGENCE_LOOKBACK_DAYS,
    limit: int = DEFAULT_EVENT_LIMIT,
) -> dict[str, Any]:
    """Return current world intelligence and a bounded historical timeline."""

    root = Path(state_dir)
    clock = (now or datetime.now(UTC)).astimezone(UTC)
    posture_rows = _dedupe(
        _recent_rows(root / "global_universe_postures", now=clock, lookback_days=lookback_days),
        identity_fields=("posture_id",),
    )
    digest_rows = _dedupe(
        _recent_rows(root / "global_situation_digests", now=clock, lookback_days=lookback_days),
        identity_fields=("digest_id",),
    )
    board_rows = _dedupe(
        _recent_rows(root / "global_family_boards", now=clock, lookback_days=lookback_days),
        identity_fields=("board_id",),
    )
    macro_rows = _dedupe(
        _recent_rows(root / "news_briefs", now=clock, lookback_days=lookback_days),
        identity_fields=("brief_id",),
    )
    failure_rows = _recent_rows(
        root / "global_universe_postures" / "failures",
        now=clock,
        lookback_days=lookback_days,
    )

    events = (
        _posture_events(posture_rows)
        + _digest_events(digest_rows)
        + _board_events(board_rows)
        + _macro_events(macro_rows)
        + _failure_events(failure_rows)
    )
    events.sort(key=_sort_ts, reverse=True)

    posture = read_json_object(root / "global_universe_postures" / "current.json")
    digest = read_json_object(root / "global_situation_digests" / "current.json")
    board = read_json_object(root / "global_family_boards" / "current.json")
    failure = read_json_object(root / "global_universe_postures" / "latest_failure.json")
    macro = _current_macro_by_venue(root)
    current_as_of = [
        _text(_dict(item).get("as_of"))
        for item in (posture, digest, board, *macro.values())
        if _text(_dict(item).get("as_of"))
    ]
    return {
        "generated_at": _iso(clock),
        "window_days": lookback_days,
        "as_of_max": max(current_as_of, default=None),
        "current": {
            "posture": posture,
            "digest": digest,
            "family_board": {
                "board_id": _dict(board).get("board_id"),
                "as_of": _dict(board).get("as_of"),
                "status": _dict(board).get("status"),
                "coverage": _dict(_dict(board).get("coverage")),
            }
            if board
            else None,
            "macro": macro,
            "latest_failure": failure,
        },
        "events": events[: max(1, limit)],
        "counts": _event_counts(events),
    }


def _latest_by_venue(
    rows: Iterable[dict[str, Any]],
    venue: str,
) -> dict[str, Any] | None:
    matching = [row for row in rows if _text(row.get("venue")).upper() == venue]
    return max(matching, key=_sort_ts, default=None)


def _latest_success_by_venue(
    rows: Iterable[dict[str, Any]],
    venue: str,
) -> dict[str, Any] | None:
    matching = [
        row
        for row in rows
        if _text(row.get("venue")).upper() == venue
        and _text(row.get("status")).lower() == "success"
    ]
    return max(matching, key=_sort_ts, default=None)


def _family_is_native(family: str, venue: str) -> bool:
    return family in VENUE_FAMILY_TAXONOMY.get(venue, frozenset())


def _family_comparison(
    families: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    comparison: list[dict[str, Any]] = []
    for group, label, family_keys in FAMILY_COMPARISON_GROUPS:
        venue_cells: dict[str, Any] = {}
        for venue in VENUES:
            mapped_keys = [
                family
                for family in family_keys
                if family in VENUE_FAMILY_TAXONOMY[venue]
            ]
            components = [
                row
                for row in families
                if row["venue"] == venue and row["family"] in mapped_keys
            ]
            by_family = {row["family"]: row for row in components}
            ranked = [
                row for row in components if isinstance(row.get("rank"), (int, float))
            ]
            attractiveness = [
                float(row["attractiveness"])
                for row in components
                if isinstance(row.get("attractiveness"), (int, float))
            ]
            candidate_count = sum(
                int(row["candidate_count"])
                for row in components
                if isinstance(row.get("candidate_count"), (int, float))
            )
            if not mapped_keys:
                status = "not_in_taxonomy"
                reason = "No governed family maps this theme to the venue."
            elif not ranked:
                status = "not_observed"
                reason = "The venue taxonomy includes this theme, but the current board has no ranked observation."
            elif candidate_count == 0:
                status = "observed"
                reason = "Observed for comparison, with no active venue candidate in the current board."
            else:
                status = "active"
                reason = (
                    f"{candidate_count} active candidate"
                    f"{'' if candidate_count == 1 else 's'} across {len(mapped_keys)} mapped "
                    f"famil{'y' if len(mapped_keys) == 1 else 'ies'}."
                )
            leader = min(ranked, key=lambda row: float(row["rank"]), default=None)
            venue_cells[venue] = {
                "status": status,
                "best_rank": leader.get("rank") if leader else None,
                "leader": leader.get("family") if leader else None,
                "average_attractiveness": (
                    sum(attractiveness) / len(attractiveness)
                    if attractiveness
                    else None
                ),
                "candidate_count": candidate_count,
                "reason": reason,
                "families": [
                    {
                        "family": family,
                        "rank": by_family.get(family, {}).get("rank"),
                        "attractiveness": by_family.get(family, {}).get(
                            "attractiveness"
                        ),
                        "candidate_count": by_family.get(family, {}).get(
                            "candidate_count"
                        ),
                        "situation_status": by_family.get(family, {}).get(
                            "situation_status"
                        ),
                        "summary": by_family.get(family, {}).get("summary"),
                    }
                    for family in mapped_keys
                ],
            }
        comparison.append(
            {
                "group": group,
                "label": label,
                "venues": venue_cells,
            }
        )
    return comparison


def _family_matrix(
    boards: list[dict[str, Any]],
    runs: list[dict[str, Any]],
    *,
    venues: tuple[str, ...],
    posture: Mapping[str, Any],
    current_board: Mapping[str, Any] | None = None,
    current_runs: Mapping[str, Mapping[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    sorted_boards = sorted(boards, key=_sort_ts)
    projected_board = _dict(current_board) or (sorted_boards[-1] if sorted_boards else {})
    projected_board_identity = (
        _text(projected_board.get("board_id")),
        _text(projected_board.get("as_of")),
    )
    priorities = _dict(posture.get("family_priority"))
    favored = {_text(item) for item in _list(priorities.get("favored"))}
    deprioritized = {_text(item) for item in _list(priorities.get("deprioritized"))}
    result: list[dict[str, Any]] = []
    for venue in venues:
        venue_boards = [
            (
                board,
                _dict(_dict(_dict(board.get("venues")).get(venue)).get("families")),
            )
            for board in sorted_boards
        ]
        current_families = _dict(
            _dict(_dict(projected_board.get("venues")).get(venue)).get("families")
        )
        latest_run = (
            _dict(_dict(current_runs).get(venue))
            or _latest_success_by_venue(runs, venue)
            or {}
        )
        run_postures = _dict(latest_run.get("family_postures"))
        run_snapshot = _dict(latest_run.get("family_snapshot"))
        historical_names = {
            family
            for _, historical_families in venue_boards
            for family in historical_families
        }
        family_names = {
            family
            for family in (
                set(current_families)
                | set(run_postures)
                | set(run_snapshot)
                | historical_names
            )
            if _family_is_native(family, venue)
        }
        for family in family_names:
            snapshot = _dict(run_snapshot.get(family))
            prior_observed: list[dict[str, Any]] = []
            history: list[dict[str, Any]] = []
            for historical_board, historical_families in venue_boards:
                point = _dict(historical_families.get(family))
                history.append(
                    {
                        "as_of": historical_board.get("as_of"),
                        "rank": (
                            point.get("radar_rank_within_venue") if point else None
                        ),
                        "attractiveness": (
                            point.get("average_attractiveness") if point else None
                        ),
                        "candidate_count": (
                            point.get("candidate_count") if point else None
                        ),
                    }
                )
                board_identity = (
                    _text(historical_board.get("board_id")),
                    _text(historical_board.get("as_of")),
                )
                if point and board_identity != projected_board_identity:
                    prior_observed.append(point)
            current = _dict(current_families.get(family))
            previous = prior_observed[-1] if prior_observed else {}
            rank = current.get("radar_rank_within_venue")
            previous_rank = previous.get("radar_rank_within_venue")
            attractiveness = current.get("average_attractiveness")
            previous_attractiveness = previous.get("average_attractiveness")
            priority = "favored" if family in favored else ("deprioritized" if family in deprioritized else "neutral")
            if rank is None:
                persistence = "unavailable"
            elif previous_rank is None:
                persistence = "new"
            elif rank == previous_rank:
                persistence = "unchanged"
            else:
                persistence = "moved"
            result.append(
                {
                    "venue": venue,
                    "family": family,
                    "priority": priority,
                    "rank": rank,
                    "rank_delta": (
                        previous_rank - rank
                        if isinstance(rank, (int, float)) and isinstance(previous_rank, (int, float))
                        else None
                    ),
                    "persistence": persistence,
                    "history": history[-80:],
                    "attractiveness": attractiveness,
                    "attractiveness_delta": (
                        attractiveness - previous_attractiveness
                        if isinstance(attractiveness, (int, float))
                        and isinstance(previous_attractiveness, (int, float))
                        else None
                    ),
                    "candidate_count": current.get("candidate_count", snapshot.get("candidate_count")),
                    "bias_counts": _dict(current.get("bias_counts") or snapshot.get("bias_counts")),
                    "situation_status": current.get("situation_status") or snapshot.get("situation_status"),
                    "direction_counts": _dict(_dict(current.get("situation")).get("direction_counts")),
                    "severity_counts": _dict(_dict(current.get("situation")).get("severity_counts")),
                    "observations": _list(_dict(current.get("situation")).get("observations")),
                    "regime": _dict(snapshot.get("regime")),
                    "regime_status": snapshot.get("regime_status"),
                    "summary": _clip(run_postures.get(family), 240),
                    "symbols": _list(snapshot.get("symbols")),
                    "sticky_symbols": _list(snapshot.get("sticky_symbols")),
                }
            )
    result.sort(
        key=lambda row: (
            VENUES.index(row["venue"]) if row["venue"] in VENUES else len(VENUES),
            row["rank"] if isinstance(row["rank"], (int, float)) else 10_000,
            row["family"],
        )
    )
    return result


def load_region_intelligence(
    state_dir: str | Path,
    *,
    venue: str | None = None,
    now: datetime | None = None,
    lookback_days: int = DEFAULT_INTELLIGENCE_LOOKBACK_DAYS,
    limit: int = DEFAULT_EVENT_LIMIT,
) -> dict[str, Any]:
    """Return region/family evolution, current matrices and pipeline events."""

    root = Path(state_dir)
    clock = (now or datetime.now(UTC)).astimezone(UTC)
    selected = _text(venue).upper()
    venues = (selected,) if selected in VENUES else VENUES
    brief_rows = _dedupe(
        _recent_rows(root / "news_briefs", now=clock, lookback_days=lookback_days),
        identity_fields=("brief_id",),
    )
    run_rows = _dedupe(
        _recent_rows(root / "universe_runs", now=clock, lookback_days=lookback_days),
        identity_fields=("agent_run_id",),
    )
    boards = _dedupe(
        _recent_rows(root / "global_family_boards", now=clock, lookback_days=lookback_days),
        identity_fields=("board_id",),
    )
    mandates = [
        row
        for row in read_jsonl_objects(root / "universe_mandates" / "history.jsonl")
        if (parsed := _parse_ts(row.get("as_of"))) is not None
        and clock - timedelta(days=max(0, lookback_days)) <= parsed <= clock
    ]
    posture = read_json_object(root / "global_universe_postures" / "current.json") or {}
    projected_board = read_json_object(root / "global_family_boards" / "current.json")
    projected_macro = _current_macro_by_venue(root)

    filtered_briefs = [row for row in brief_rows if _text(row.get("venue")).upper() in venues]
    filtered_runs = [row for row in run_rows if _text(row.get("venue")).upper() in venues]
    filtered_mandates = [row for row in mandates if _text(row.get("venue")).upper() in venues]
    events = _macro_events(filtered_briefs)
    for row in filtered_runs:
        venue_key = _text(row.get("venue")).upper()
        run_status = _text(row.get("status")) or "unknown"
        events.append(
            _event(
                event_id=_text(row.get("agent_run_id")) or f"regional-run:{venue_key}:{_text(row.get('as_of'))}",
                kind="regional_run" if run_status == "success" else "regional_failure",
                layer="region",
                row=row,
                title=f"{venue_key} universe analysis · {_text(row.get('status')) or 'unknown'}",
                summary=_text(row.get("summary")),
                venue=venue_key,
                status=run_status,
                refs={
                    "agent_run_id": row.get("agent_run_id"),
                    "candidate_scope_id": row.get("candidate_scope_id"),
                    "brief_id": _dict(row.get("brief_ref")).get("brief_id"),
                    "mandate_id": _dict(row.get("mandate_prepared_ref")).get("mandate_id"),
                },
                payload={
                    "selected_hotlist": _list(row.get("selected_hotlist")),
                    "family_postures": _dict(row.get("family_postures")),
                    "symbol_rationales": _dict(row.get("symbol_rationales")),
                    "coverage": _dict(row.get("coverage")),
                    "fallback_used": row.get("fallback_used"),
                },
            )
        )
    for row in filtered_mandates:
        venue_key = _text(row.get("venue")).upper()
        events.append(
            _event(
                event_id=f"{_text(row.get('mandate_id'))}:{_text(row.get('status'))}:{_text(row.get('as_of'))}",
                kind="mandate",
                layer="region",
                row=row,
                title=f"{venue_key} mandate · {_text(row.get('status')) or 'unknown'}",
                summary=f"{len(_dict(row.get('symbols')))} symbols",
                venue=venue_key,
                status=_text(row.get("status")) or "unknown",
                refs={
                    "mandate_id": row.get("mandate_id"),
                    "candidate_scope_id": row.get("candidate_scope_id"),
                    "agent_run_id": row.get("agent_run_id"),
                },
                payload={
                    "symbols": list(_dict(row.get("symbols"))),
                    "fallback_reason": row.get("fallback_reason"),
                    "family_postures": _dict(row.get("family_postures")),
                },
            )
        )
    events.sort(key=_sort_ts, reverse=True)

    current: dict[str, Any] = {}
    current_runs: dict[str, dict[str, Any]] = {}
    for venue_key in venues:
        projected_run = read_json_object(root / "universe_runs" / f"latest-{venue_key}.json")
        run = (
            projected_run
            if _text(_dict(projected_run).get("status")).lower() == "success"
            else _latest_success_by_venue(run_rows, venue_key)
        )
        failure = max(
            (
                row
                for row in run_rows
                if _text(row.get("venue")).upper() == venue_key
                and _text(row.get("status")).lower() in {"error", "invalid"}
            ),
            key=_sort_ts,
            default=None,
        )
        if failure is not None and run is not None and _sort_ts(failure) <= _sort_ts(run):
            failure = None
        brief = projected_macro.get(venue_key) or _latest_by_venue(brief_rows, venue_key)
        mandate = read_json_object(
            root / "universe_mandates" / "active" / "venues" / f"{venue_key}.json"
        ) or _latest_by_venue(mandates, venue_key)
        if run is not None:
            current_runs[venue_key] = run
        current[venue_key] = {
            "posture": _dict(posture.get("venue_posture")).get(venue_key),
            "macro_brief": brief,
            "regional_run": run,
            "latest_failure": failure,
            "mandate": mandate,
            "freshness_hours": _age_hours(
                _dict(run).get("as_of") or _dict(brief).get("as_of"),
                now=clock,
            ),
        }
    families = _family_matrix(
        boards,
        run_rows,
        venues=venues,
        posture=posture,
        current_board=projected_board,
        current_runs=current_runs,
    )
    return {
        "generated_at": _iso(clock),
        "window_days": lookback_days,
        "venue": selected if selected in VENUES else None,
        "current": current,
        "families": families,
        "comparison": _family_comparison(families),
        "events": events[: max(1, limit)],
        "counts": _event_counts(events),
    }


def _preferred_company_brief(envelope: Mapping[str, Any]) -> dict[str, Any] | None:
    briefs = _dict(envelope.get("briefs"))
    candidates = [_dict(value) for value in briefs.values() if isinstance(value, Mapping)]
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda row: (
            _sort_ts(row),
            1 if _text(row.get("depth")) == "deep" else 0,
        ),
    )


def _company_summary(brief: Mapping[str, Any]) -> str:
    thesis = _dict(brief.get("company_thesis"))
    for value in (
        thesis.get("summary"),
        _dict(brief.get("business")).get("summary"),
        _dict(brief.get("selection_view")).get("summary"),
    ):
        if _text(value):
            return _clip(value)
    return ""


def _company_history_by_symbol(
    state_dir: Path,
    *,
    now: datetime,
    lookback_days: int,
) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {}
    threshold = now - timedelta(days=max(0, lookback_days))
    current_dir = state_dir / "company_intelligence" / "current"
    history_dir = state_dir / "company_intelligence" / "history"
    for path in current_dir.glob("*.json"):
        envelope = read_json_object(path)
        symbol = _text(_dict(envelope).get("symbol"))
        if not symbol:
            continue
        result[symbol] = [
            row
            for row in read_jsonl_objects(history_dir / f"{path.stem}.jsonl")
            if (parsed := _parse_ts(row.get("as_of"))) is not None
            and threshold <= parsed <= now
        ]
    return result


def _decision_rows(
    state_dir: Path,
    *,
    now: datetime,
    lookback_days: int,
) -> list[dict[str, Any]]:
    threshold = now - timedelta(days=max(0, lookback_days))
    month_keys: set[str] = set()
    cursor = threshold.replace(day=1)
    while cursor <= now:
        month_keys.add(f"{cursor.year:04d}-{cursor.month:02d}")
        if cursor.month == 12:
            cursor = cursor.replace(year=cursor.year + 1, month=1)
        else:
            cursor = cursor.replace(month=cursor.month + 1)

    rows: list[dict[str, Any]] = []
    for month in sorted(month_keys):
        path = state_dir / "archive" / f"decisions-{month}.jsonl.gz"
        if not path.exists():
            continue
        try:
            with gzip.open(path, "rt", encoding="utf-8") as handle:
                for line in handle:
                    try:
                        row = json.loads(line)
                    except (json.JSONDecodeError, TypeError):
                        continue
                    if isinstance(row, dict):
                        rows.append(row)
        except (OSError, EOFError, gzip.BadGzipFile, zlib.error):
            continue
    rows.extend(read_jsonl_objects(state_dir / "decisions.jsonl"))

    result: list[dict[str, Any]] = []
    for row in rows:
        parsed = _parse_ts(row.get("cycle_ts") or row.get("ts"))
        if parsed is not None and threshold <= parsed <= now:
            result.append(row)
    return result


def _company_brief_refs(
    row: Mapping[str, Any],
    *,
    symbol: str,
) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    sources = (row, _dict(row.get("decision")))
    for source in sources:
        raw_values: list[Any] = []
        raw_values.extend(_list(source.get("company_brief_refs")))
        singular = source.get("company_brief_ref")
        if singular:
            raw_values.append(singular)
        for raw in raw_values:
            ref = _dict(raw) if isinstance(raw, Mapping) else {"brief_id": _text(raw)}
            brief_id = _text(ref.get("brief_id"))
            ref_symbol = _text(ref.get("symbol"))
            if not brief_id or (ref_symbol and ref_symbol != symbol) or brief_id in seen:
                continue
            seen.add(brief_id)
            normalized.append(ref)
    return normalized


def _valid_mandate_ref(row: Mapping[str, Any]) -> dict[str, Any] | None:
    ref = _dict(decision_row_mandate_ref(dict(row)))
    if not _text(ref.get("mandate_id")) and not _text(ref.get("candidate_scope_id")):
        return None
    return ref


def load_company_intelligence(
    state_dir: str | Path,
    *,
    symbol: str | None = None,
    venue: str | None = None,
    runtime_state: Mapping[str, Any] | None = None,
    now: datetime | None = None,
    lookback_days: int = DEFAULT_INTELLIGENCE_LOOKBACK_DAYS,
    limit: int = 320,
) -> dict[str, Any]:
    """Return company theses first, with agent decisions as linked expression."""

    root = Path(state_dir)
    clock = (now or datetime.now(UTC)).astimezone(UTC)
    threshold = clock - timedelta(days=max(0, lookback_days))
    wanted_symbol = _text(symbol).upper()
    wanted_venue = _text(venue).upper()
    company_names = _dict(_dict(runtime_state).get("company_map"))
    stale_symbols = set(_dict(_dict(runtime_state).get("stale_market_data")))
    holdings = {
        _text(row.get("symbol")): row
        for row in _list(_dict(_dict(runtime_state).get("portfolio")).get("holdings"))
        if isinstance(row, Mapping)
    }
    histories = _company_history_by_symbol(
        root,
        now=clock,
        lookback_days=lookback_days,
    )
    decisions = _decision_rows(root, now=clock, lookback_days=lookback_days)
    decisions_by_symbol: dict[str, list[dict[str, Any]]] = {}
    for row in decisions:
        decisions_by_symbol.setdefault(_text(row.get("symbol")), []).append(row)

    companies: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    for current_path in sorted((root / "company_intelligence" / "current").glob("*.json")):
        envelope = read_json_object(current_path)
        brief = _preferred_company_brief(_dict(envelope))
        if brief is None:
            continue
        company_symbol = _text(brief.get("symbol") or _dict(envelope).get("symbol"))
        if not company_symbol or (wanted_symbol and company_symbol.upper() != wanted_symbol):
            continue
        issuer = _dict(brief.get("issuer_identity"))
        company_venue = _text(issuer.get("venue")) or venue_of_safe(company_symbol)
        if wanted_venue and company_venue != wanted_venue:
            continue
        history = histories.get(company_symbol, [])
        previous_candidates = [
            row
            for row in history
            if _text(row.get("brief_id")) != _text(brief.get("brief_id"))
        ]
        previous = max(previous_candidates, key=_sort_ts, default=None)
        thesis = _dict(brief.get("company_thesis"))
        previous_thesis = _dict(_dict(previous).get("company_thesis"))
        selection = _dict(brief.get("selection_view"))
        raw_confidence = selection.get("confidence")
        confidence = (
            float(raw_confidence)
            if isinstance(raw_confidence, (int, float)) and not isinstance(raw_confidence, bool)
            else None
        )
        confidence_label = _text(raw_confidence) if isinstance(raw_confidence, str) else None
        coverage = brief.get("coverage")
        coverage_status = (
            _text(_dict(coverage).get("status"))
            if isinstance(coverage, Mapping)
            else _text(coverage)
        )
        company_decisions = sorted(
            decisions_by_symbol.get(company_symbol, []),
            key=_sort_ts,
            reverse=True,
        )
        linked_decisions = [
            row
            for row in company_decisions
            if _valid_mandate_ref(row) is not None
            or _company_brief_refs(row, symbol=company_symbol)
        ]
        holding = _dict(holdings.get(company_symbol))
        qty = holding.get("quantity")
        on_book = isinstance(qty, (int, float)) and abs(qty) > 1e-9
        item = {
            "symbol": company_symbol,
            "name": _text(company_names.get(company_symbol) or issuer.get("issuer_name")),
            "venue": company_venue,
            "as_of": brief.get("as_of"),
            "age_hours": _age_hours(brief.get("as_of"), now=clock),
            "depth": brief.get("depth"),
            "coverage": coverage_status or None,
            "source_count": len(_source_refs(brief)),
            "thesis_status": _text(thesis.get("status")) or "unknown",
            "previous_thesis_status": _text(previous_thesis.get("status")) or None,
            "thesis_changed": bool(previous and thesis.get("status") != previous_thesis.get("status")),
            "summary": _company_summary(brief),
            "business_summary": _clip(_dict(brief.get("business")).get("summary")),
            "selection_posture": _text(selection.get("posture")) or None,
            "selection_confidence": confidence,
            "selection_confidence_label": confidence_label,
            "catalyst_count": len(_list(brief.get("catalysts"))),
            "risk_count": len(_list(brief.get("risks"))),
            "open_question_count": len(_list(brief.get("open_questions"))),
            "history_count": len(history),
            "on_book": on_book,
            "side": ("L" if qty > 0 else "S") if on_book else None,
            "stale_market": company_symbol in stale_symbols,
            "decision_count": len(company_decisions),
            "linked_decision_count": len(linked_decisions),
            "latest_action": _text(company_decisions[0].get("action")) if company_decisions else None,
            "latest_decision_at": (
                company_decisions[0].get("cycle_ts") or company_decisions[0].get("ts")
                if company_decisions
                else None
            ),
            "brief": brief,
        }
        companies.append(item)
        brief_ts = _parse_ts(brief.get("as_of"))
        if brief_ts is not None and threshold <= brief_ts <= clock:
            events.append(
                _event(
                    event_id=_text(brief.get("brief_id"))
                    or f"company:{company_symbol}:{_text(brief.get('as_of'))}",
                    kind="company_brief",
                    layer="company",
                    row=brief,
                    title=f"{company_symbol} · {item['thesis_status']}",
                    summary=item["summary"],
                    venue=company_venue,
                    symbol=company_symbol,
                    refs={"brief_id": brief.get("brief_id")},
                    changes=_changes(
                        {"thesis_status": item["thesis_status"]},
                        (
                            {"thesis_status": item["previous_thesis_status"]}
                            if previous
                            else None
                        ),
                        ("thesis_status",),
                    ),
                    payload={
                        "depth": item["depth"],
                        "coverage": item["coverage"],
                        "selection_posture": item["selection_posture"],
                        "catalyst_count": item["catalyst_count"],
                        "risk_count": item["risk_count"],
                    },
                )
            )
        for row in company_decisions[:20]:
            mandate_ref = _valid_mandate_ref(row)
            company_brief_refs = _company_brief_refs(row, symbol=company_symbol)
            linked = mandate_ref is not None or bool(company_brief_refs)
            decision_with_refs = {
                **row,
                "candidate_scope_id": _dict(mandate_ref).get("candidate_scope_id"),
                "mandate_id": _dict(mandate_ref).get("mandate_id"),
                "agent_run_id": _dict(mandate_ref).get("agent_run_id"),
            }
            events.append(
                _event(
                    event_id=_text(row.get("decision_id"))
                    or f"decision:{company_symbol}:{_text(row.get('cycle_ts') or row.get('ts'))}",
                    kind="decision",
                    layer="decision",
                    row=decision_with_refs,
                    title=f"{company_symbol} · {_text(row.get('action')) or 'decision'}",
                    summary=_text(row.get("rationale") or row.get("reason")),
                    venue=company_venue,
                    symbol=company_symbol,
                    status="executed" if row.get("executed") else "observed",
                    linkage="linked" if linked else "unlinked",
                    refs={
                        "decision_id": row.get("decision_id"),
                        "mandate_id": _dict(mandate_ref).get("mandate_id"),
                        "candidate_scope_id": _dict(mandate_ref).get("candidate_scope_id"),
                        "agent_run_id": _dict(mandate_ref).get("agent_run_id"),
                        "company_brief_refs": company_brief_refs,
                    },
                    payload={
                        "action": row.get("action"),
                        "confidence": row.get("confidence"),
                        "executed": row.get("executed"),
                        "linked": linked,
                    },
                )
            )

    companies.sort(
        key=lambda item: (
            not bool(item["on_book"]),
            not bool(item["thesis_changed"]),
            -(item["selection_confidence"] if isinstance(item["selection_confidence"], (int, float)) else -1),
            item["symbol"],
        )
    )
    events.sort(key=_sort_ts, reverse=True)
    return {
        "generated_at": _iso(clock),
        "window_days": lookback_days,
        "filters": {
            "symbol": wanted_symbol or None,
            "venue": wanted_venue or None,
        },
        "companies": companies[: max(1, limit)],
        "events": events[: max(1, min(limit, DEFAULT_EVENT_LIMIT))],
        "counts": {
            "companies": len(companies),
            "on_book": sum(bool(item["on_book"]) for item in companies),
            "changed": sum(bool(item["thesis_changed"]) for item in companies),
            "stale": sum(bool(item["stale_market"]) for item in companies),
            "decisions": sum(int(item["decision_count"]) for item in companies),
            "unlinked_decisions": sum(
                int(item["decision_count"]) - int(item["linked_decision_count"])
                for item in companies
            ),
        },
    }


__all__ = [
    "DEFAULT_INTELLIGENCE_LOOKBACK_DAYS",
    "load_company_intelligence",
    "load_region_intelligence",
    "load_world_timeline",
]
