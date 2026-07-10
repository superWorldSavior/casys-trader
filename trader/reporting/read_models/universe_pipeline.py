"""Operator read model for the universe-intelligence pipeline."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any


_UNIVERSE_PIPELINE_VENUES = ("TW", "EU", "US")


def _safe_float(value: Any, default: float | None = 0.0) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def _safe_list_of_dicts(value: Any) -> list[dict]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _read_projection_safe(path: Path) -> tuple[dict[str, Any] | None, str]:
    """Read one current/latest projection without breaking the cockpit.

    Brief projections are one-line JSONL files while the other projections are
    JSON objects. Reading the last valid non-empty line supports both forms and
    reports a present-but-unreadable file as ``unavailable``.
    """

    try:
        if not path.exists():
            return None, "pending"
        content = path.read_text(encoding="utf-8")
    except Exception:
        return None, "unavailable"
    for line in reversed(content.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except Exception:
            continue
        if isinstance(payload, dict):
            return payload, "available"
    return None, "unavailable"


def _short_pipeline_id(value: Any) -> str | None:
    """Return a compact operator label while retaining the full ID alongside it."""

    text = str(value or "").strip()
    if not text:
        return None
    tail = text.rsplit(":", 1)[-1]
    if len(tail) <= 12:
        return tail
    return f"{tail[:6]}…{tail[-4:]}"


def _safe_pipeline_count(value: Any) -> int:
    number = _safe_float(value, default=None)
    return max(0, int(number)) if number is not None else 0


def _safe_string_list(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple, set)):
        return []
    return [str(item) for item in value if item]


def _candidate_is_challenger(candidate: Any) -> bool:
    if not isinstance(candidate, dict):
        return False
    if isinstance(candidate.get("fresh_news"), dict):
        return True
    if candidate.get("candidate_source") == "fresh_news":
        return True
    sources = candidate.get("candidate_sources")
    return isinstance(sources, (list, tuple, set)) and "fresh_news" in sources


def _brief_point_count(payload: dict[str, Any]) -> int:
    total = len(_safe_list_of_dicts(payload.get("alerts")))
    for section_key in ("zones", "families", "symbols"):
        sections = payload.get(section_key)
        if isinstance(sections, dict):
            total += sum(len(_safe_list_of_dicts(points)) for points in sections.values())
        elif isinstance(sections, list):
            for section in sections:
                if isinstance(section, dict):
                    total += len(_safe_list_of_dicts(section.get("points")))
    return total


def _latest_activations_by_venue_safe(
    ledger_path: Path,
) -> tuple[dict[str, dict[str, Any]], str]:
    """Return the latest activation independently for each venue."""

    try:
        if not ledger_path.exists():
            return {}, "pending"
        lines = ledger_path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return {}, "unavailable"

    activations: dict[str, dict[str, Any]] = {}
    valid_rows = 0
    for line in reversed(lines):
        if len(activations) == len(_UNIVERSE_PIPELINE_VENUES):
            break
        try:
            row = json.loads(line)
        except Exception:
            continue
        if not isinstance(row, dict):
            continue
        valid_rows += 1
        overrides = row.get("overrides")
        overrides = overrides if isinstance(overrides, dict) else {}
        venue = str(overrides.get("venue") or row.get("venue") or "").strip().upper()
        if venue in _UNIVERSE_PIPELINE_VENUES and venue not in activations:
            activations[venue] = row
    return activations, ("available" if valid_rows else "unavailable")


def _scope_pipeline_entry(
    *,
    venue: str,
    projection: dict[str, Any] | None,
    projection_status: str,
    venue_entry: dict[str, Any],
) -> dict[str, Any]:
    if projection is not None and str(projection.get("venue") or "").upper() != venue:
        projection = None
        projection_status = "unavailable"
    venue_has_scope = bool(
        venue_entry.get("candidate_scope_id") or venue_entry.get("candidates") or venue_entry.get("default_hotlist")
    )
    payload = projection or (venue_entry if venue_has_scope else {})
    source = "projection" if projection is not None else ("venue_state" if venue_has_scope else None)
    scope_id = str(payload.get("candidate_scope_id") or "").strip() or None
    candidates = _safe_list_of_dicts(payload.get("candidates"))
    hotlist = _safe_string_list(payload.get("default_hotlist"))
    challenger_count = sum(1 for candidate in candidates if _candidate_is_challenger(candidate))
    if source is not None:
        status = "ready" if scope_id else "unavailable"
    else:
        status = projection_status
    return {
        "status": status,
        "projection_status": projection_status,
        "source": source,
        "id": scope_id,
        "id_short": _short_pipeline_id(scope_id),
        "as_of": payload.get("as_of") or payload.get("last_close_at"),
        "phase": payload.get("scope_phase") or "legacy",
        "parent_scope_id": payload.get("parent_candidate_scope_id"),
        "parent_scope_id_short": _short_pipeline_id(payload.get("parent_candidate_scope_id")),
        "parent_close_at": payload.get("parent_close_at"),
        "candidate_run_ids": _safe_string_list(payload.get("candidate_run_ids")),
        "candidate_count": len(candidates),
        "baseline_hotlist_count": len(hotlist),
        "challenger_count": challenger_count,
    }


def _scout_pipeline_entry(*, venue: str, projection: dict[str, Any] | None, projection_status: str) -> dict[str, Any]:
    if projection is not None and str(projection.get("venue") or "").upper() != venue:
        projection = None
        projection_status = "unavailable"
    if projection is None:
        return {
            "status": projection_status,
            "id": None,
            "id_short": None,
            "challenger_count": 0,
            "coverage": {},
        }
    run_id = str(projection.get("candidate_run_id") or "").strip() or None
    challengers = _safe_list_of_dicts(projection.get("challengers"))
    coverage = {
        "status": projection.get("coverage_status"),
        "eligible_symbol_count": _safe_pipeline_count(projection.get("eligible_symbol_count")),
        "radar_symbol_count": _safe_pipeline_count(projection.get("radar_symbol_count")),
        "archived_items_read": _safe_pipeline_count(projection.get("archived_items_read")),
        "items_read": _safe_pipeline_count(projection.get("items_read")),
        "eligible_items": _safe_pipeline_count(projection.get("eligible_items")),
        "rejection_counts": (
            dict(projection.get("rejection_counts")) if isinstance(projection.get("rejection_counts"), dict) else {}
        ),
    }
    return {
        "status": str(projection.get("status") or "available"),
        "id": run_id,
        "id_short": _short_pipeline_id(run_id),
        "as_of": projection.get("as_of"),
        "reason": projection.get("reason"),
        "challenger_count": len(challengers),
        "challengers": [str(item.get("symbol")) for item in challengers if item.get("symbol")],
        "coverage": coverage,
    }


def _brief_pipeline_entry(
    *,
    venue: str,
    projection: dict[str, Any] | None,
    projection_status: str,
    scope_id: str | None,
) -> dict[str, Any]:
    if projection is not None and str(projection.get("venue") or "").upper() != venue:
        projection = None
        projection_status = "unavailable"
    if projection is None:
        return {
            "status": projection_status,
            "id": None,
            "id_short": None,
            "scope_match": None,
            "point_count": 0,
            "alert_count": 0,
            "coverage": {},
        }
    brief_id = str(projection.get("brief_id") or "").strip() or None
    input_refs = projection.get("input_refs")
    input_refs = input_refs if isinstance(input_refs, dict) else {}
    brief_scope_id = str(input_refs.get("candidate_scope_id") or "").strip() or None
    scope_match = bool(scope_id and brief_scope_id and scope_id == brief_scope_id)
    if scope_id is None or brief_scope_id is None:
        scope_match = None
    status = "scope_mismatch" if scope_match is False else "ready"
    coverage = input_refs.get("coverage")
    return {
        "status": status,
        "id": brief_id,
        "id_short": _short_pipeline_id(brief_id),
        "as_of": projection.get("as_of"),
        "valid_until": projection.get("valid_until"),
        "scope_id": brief_scope_id,
        "scope_id_short": _short_pipeline_id(brief_scope_id),
        "scope_match": scope_match,
        "point_count": _brief_point_count(projection),
        "alert_count": len(_safe_list_of_dicts(projection.get("alerts"))),
        "coverage": dict(coverage) if isinstance(coverage, dict) else {},
        "ref": {
            "date": str(projection.get("as_of") or "")[:10],
            "venue": venue,
            "brief_id": brief_id,
            "as_of": projection.get("as_of"),
        },
    }


def _agent_pipeline_entry(*, venue: str, projection: dict[str, Any] | None, projection_status: str) -> dict[str, Any]:
    if projection is not None and str(projection.get("venue") or "").upper() != venue:
        projection = None
        projection_status = "unavailable"
    if projection is None:
        return {
            "status": projection_status,
            "id": None,
            "id_short": None,
            "hotlist_count": 0,
            "challenger_count": 0,
            "coverage": {},
        }
    run_id = str(projection.get("agent_run_id") or "").strip() or None
    hotlist = _safe_string_list(projection.get("selected_hotlist"))
    challengers = _safe_string_list(projection.get("selected_challengers"))
    coverage = projection.get("coverage")
    return {
        "status": str(projection.get("status") or "available"),
        "id": run_id,
        "id_short": _short_pipeline_id(run_id),
        "as_of": projection.get("as_of"),
        "scope_id": projection.get("candidate_scope_id"),
        "scope_id_short": _short_pipeline_id(projection.get("candidate_scope_id")),
        "brief_ref": projection.get("brief_ref"),
        "error_code": projection.get("error_code"),
        "provider": projection.get("agent_provider"),
        "model": projection.get("agent_model"),
        "provider_fallback_reason": projection.get("agent_provider_fallback_reason"),
        "hotlist_count": len(hotlist),
        "selected_hotlist": hotlist,
        "challenger_count": len(challengers),
        "selected_challengers": challengers,
        "coverage": dict(coverage) if isinstance(coverage, dict) else {},
    }


def _activation_pipeline_entry(
    *,
    venue: str,
    ledger_row: dict[str, Any] | None,
    ledger_status: str,
    venue_entry: dict[str, Any],
) -> dict[str, Any]:
    if ledger_row is not None:
        overrides = ledger_row.get("overrides")
        overrides = overrides if isinstance(overrides, dict) else {}
        hotlist = overrides.get("selected_hotlist") or ledger_row.get("final_hot_set") or []
        challengers = overrides.get("selected_challengers") or []
        fallback_used = bool(overrides.get("fallback_used", False))
        fallback_reason = overrides.get("fallback_reason")
        scope_id = overrides.get("candidate_scope_id")
        agent_run_id = overrides.get("agent_run_id")
        brief_ref = overrides.get("brief_ref")
        has_universe_lineage = any(
            key in overrides for key in ("candidate_scope_id", "agent_run_id", "brief_ref", "fallback_used")
        )
        return {
            "status": ("fallback" if fallback_used else ("activated" if has_universe_lineage else "legacy")),
            "source": "rotation_ledger",
            "as_of": ledger_row.get("as_of"),
            "scope_id": scope_id,
            "scope_id_short": _short_pipeline_id(scope_id),
            "agent_run_id": agent_run_id,
            "agent_run_id_short": _short_pipeline_id(agent_run_id),
            "brief_ref": brief_ref,
            "fallback_used": fallback_used,
            "fallback_reason": fallback_reason,
            "hotlist_count": len(hotlist) if isinstance(hotlist, list) else 0,
            "selected_hotlist": list(hotlist) if isinstance(hotlist, list) else [],
            "challenger_count": (len(challengers) if isinstance(challengers, list) else 0),
            "selected_challengers": (list(challengers) if isinstance(challengers, list) else []),
        }

    venue_has_activation = bool(
        venue_entry.get("last_universe_activation_scope_id") or venue_entry.get("last_universe_attempt_token")
    )
    if venue_has_activation:
        hotlist = venue_entry.get("hotlist") or []
        fallback_used = bool(venue_entry.get("last_universe_fallback_used", False))
        scope_id = venue_entry.get("last_universe_activation_scope_id") or venue_entry.get("candidate_scope_id")
        agent_run_id = venue_entry.get("last_universe_agent_run_id")
        return {
            "status": "fallback" if fallback_used else "activated",
            "source": "venue_state",
            "as_of": venue_entry.get("last_universe_activation_at") or venue_entry.get("last_universe_attempt_at"),
            "scope_id": scope_id,
            "scope_id_short": _short_pipeline_id(scope_id),
            "agent_run_id": agent_run_id,
            "agent_run_id_short": _short_pipeline_id(agent_run_id),
            "brief_ref": venue_entry.get("last_universe_brief_ref"),
            "fallback_used": fallback_used,
            "fallback_reason": venue_entry.get("last_universe_fallback_reason"),
            "hotlist_count": len(hotlist) if isinstance(hotlist, list) else 0,
            "selected_hotlist": list(hotlist) if isinstance(hotlist, list) else [],
            "challenger_count": 0,
            "selected_challengers": [],
        }

    return {
        "status": ledger_status if ledger_status == "unavailable" else "pending",
        "source": None,
        "as_of": None,
        "scope_id": None,
        "scope_id_short": None,
        "agent_run_id": None,
        "agent_run_id_short": None,
        "brief_ref": None,
        "fallback_used": False,
        "fallback_reason": None,
        "hotlist_count": 0,
        "selected_hotlist": [],
        "challenger_count": 0,
        "selected_challengers": [],
    }


def load_universe_pipeline(state_dir: Path, venue_state: dict[str, Any]) -> dict[str, dict]:
    """Assemble the current operator projection for each equity venue."""

    venues_state = venue_state.get("venues") if isinstance(venue_state, dict) else {}
    venues_state = venues_state if isinstance(venues_state, dict) else {}
    activations, ledger_status = _latest_activations_by_venue_safe(state_dir / "rotation_ledger.jsonl")
    pipeline: dict[str, dict] = {}
    for venue in _UNIVERSE_PIPELINE_VENUES:
        venue_entry = venues_state.get(venue)
        venue_entry = venue_entry if isinstance(venue_entry, dict) else {}
        scope_raw, scope_projection_status = _read_projection_safe(
            state_dir / "candidate_scopes" / f"current-{venue}.json"
        )
        scout_raw, scout_projection_status = _read_projection_safe(
            state_dir / "news_challenger_runs" / f"latest-{venue}.json"
        )
        brief_raw, brief_projection_status = _read_projection_safe(state_dir / "news_briefs" / f"latest-{venue}.jsonl")
        agent_raw, agent_projection_status = _read_projection_safe(state_dir / "universe_runs" / f"latest-{venue}.json")
        scope = _scope_pipeline_entry(
            venue=venue,
            projection=scope_raw,
            projection_status=scope_projection_status,
            venue_entry=venue_entry,
        )
        pipeline[venue] = {
            "venue": venue,
            "scope": scope,
            "scout": _scout_pipeline_entry(
                venue=venue,
                projection=scout_raw,
                projection_status=scout_projection_status,
            ),
            "brief": _brief_pipeline_entry(
                venue=venue,
                projection=brief_raw,
                projection_status=brief_projection_status,
                scope_id=scope.get("id"),
            ),
            "agent": _agent_pipeline_entry(
                venue=venue,
                projection=agent_raw,
                projection_status=agent_projection_status,
            ),
            "activation": _activation_pipeline_entry(
                venue=venue,
                ledger_row=activations.get(venue),
                ledger_status=ledger_status,
                venue_entry=venue_entry,
            ),
        }
    return pipeline


# Private compatibility name retained for callers of the historical assembler.
_load_universe_pipeline_safe = load_universe_pipeline


__all__ = ["load_universe_pipeline"]
