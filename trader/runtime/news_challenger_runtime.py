"""Runtime adapter for fresh-news universe challengers."""

from __future__ import annotations

import json
import logging
import math
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import yaml

from trader.domain.universe import NewsChallengerSelection, select_news_challengers
from trader.infrastructure.state_db.news_challenger_run_store import NewsChallengerRunStore
from trader.infrastructure.state_db.situation_brief_store import NewsMacroBriefStore

NewsChallengerFn = Callable[..., list[dict[str, Any]]]
SelectNewsChallengersFn = Callable[..., NewsChallengerSelection]

DEFAULT_MAX_AGE_HOURS = 72.0
NEWS_CHALLENGER_RUN_SCHEMA_VERSION = 1
SYMBOL_NAMES_FILE = "symbol_names.yaml"
SYMBOL_NEWS_ALIASES_FILE = "symbol_news_aliases.yaml"
log = logging.getLogger("casys-trader")


def build_news_challenger_fn(
    *,
    config_dir: str | Path,
    state_dir: str | Path,
    as_of: datetime,
    max_age_hours: float = DEFAULT_MAX_AGE_HOURS,
    select_fn: SelectNewsChallengersFn = select_news_challengers,
    run_store: NewsChallengerRunStore | None = None,
) -> NewsChallengerFn:
    """Build a fail-safe provider scoped by each venue's eligible radar ranking."""

    config_path = Path(config_dir)
    state_path = Path(state_dir)
    now = _ensure_utc(as_of)
    news_items = tuple(
        _read_recent_news_items(
            state_path / "news_items",
            now=now,
            max_age_hours=max_age_hours,
        )
    )
    symbol_names = _load_string_mapping(config_path / SYMBOL_NAMES_FILE)
    manual_aliases = _load_alias_mapping(config_path / SYMBOL_NEWS_ALIASES_FILE)
    brief_store = NewsMacroBriefStore(state_path / "news_briefs")
    observability_store = run_store or NewsChallengerRunStore(state_path / "news_challenger_runs")
    candidate_run_ids: dict[str, str] = {}

    def _challengers(
        *,
        venue: str,
        venue_ranked: list[dict[str, Any]],
        radar_symbols: set[str],
    ) -> list[dict[str, Any]]:
        candidate_run_id = _new_candidate_run_id(now, venue)
        candidate_run_ids[str(venue)] = candidate_run_id
        normalized_radar_symbols: set[str] = set()
        try:
            normalized_radar_symbols = _normalized_symbols(radar_symbols)
            eligible_symbols = {
                str(item.get("symbol") or "").strip()
                for item in venue_ranked
                if isinstance(item, Mapping) and item.get("symbol")
            }
        except Exception as exc:  # noqa: BLE001 - malformed advisory input must remain fail-safe
            _append_run_safely(
                observability_store,
                _run_record(
                    candidate_run_id=candidate_run_id,
                    now=now,
                    venue=venue,
                    eligible_symbol_count=0,
                    radar_symbol_count=len(normalized_radar_symbols),
                    archived_items_read=len(news_items),
                    status="error",
                    reason=f"input_error:{exc.__class__.__name__}",
                ),
            )
            return []

        base_record = {
            "candidate_run_id": candidate_run_id,
            "now": now,
            "venue": venue,
            "eligible_symbol_count": len(eligible_symbols),
            "radar_symbol_count": len(normalized_radar_symbols),
            "archived_items_read": len(news_items),
        }
        degraded_reason = _degraded_reason(
            eligible_symbols=eligible_symbols,
            news_items=news_items,
            symbol_names=symbol_names,
        )
        if degraded_reason is not None:
            _append_run_safely(
                observability_store,
                _run_record(
                    **base_record,
                    status="degraded",
                    reason=degraded_reason,
                ),
            )
            return []

        try:
            selection = select_fn(
                news_items,
                symbol_names=symbol_names,
                manual_aliases=manual_aliases,
                eligible_symbols=eligible_symbols,
                radar_symbols=normalized_radar_symbols,
                seen_source_refs=_seen_source_refs(brief_store, venue),
                as_of=now,
                max_age_hours=max_age_hours,
            )
            candidates = [
                _candidate_with_run_id(challenger.to_candidate(), candidate_run_id)
                for challenger in selection.challengers
            ]
            compact_challengers = [
                {
                    "symbol": challenger.symbol,
                    "score": challenger.score,
                    "valid_until": challenger.valid_until,
                    "publishers": list(challenger.publishers),
                    "source_refs": list(challenger.source_refs),
                }
                for challenger in selection.challengers
            ]
        except Exception as exc:  # noqa: BLE001 - challengers are advisory; rotation remains deterministic
            _append_run_safely(
                observability_store,
                _run_record(
                    **base_record,
                    status="error",
                    reason=f"selector_error:{exc.__class__.__name__}",
                ),
            )
            return []

        _append_run_safely(
            observability_store,
            _run_record(
                **base_record,
                status="success",
                reason="selection_complete",
                items_read=selection.items_read,
                eligible_items=selection.eligible_items,
                rejection_counts=selection.rejection_counts,
                challengers=compact_challengers,
            ),
        )
        return candidates

    # The list return contract remains backward compatible; this side-channel
    # carries run lineage even when zero challengers are selected.
    _challengers.candidate_run_ids = candidate_run_ids  # type: ignore[attr-defined]
    return _challengers


def _read_recent_news_items(
    base_dir: Path,
    *,
    now: datetime,
    max_age_hours: float,
) -> list[dict[str, Any]]:
    """Read newest-first JSONL rows; semantic freshness stays in the pure selector."""

    if not base_dir.is_dir():
        return []
    lookback_days = max(0, math.ceil(max(0.0, float(max_age_hours)) / 24.0))
    rows: list[dict[str, Any]] = []
    seen_refs: set[str] = set()
    for offset in range(lookback_days + 1):
        day = (now - timedelta(days=offset)).strftime("%Y-%m-%d")
        path = base_dir / f"{day}.jsonl"
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for raw_line in reversed(lines):
            try:
                row = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            if not isinstance(row, dict):
                continue
            source_ref = str(row.get("uuid") or "").strip()
            if source_ref and source_ref in seen_refs:
                continue
            if source_ref:
                seen_refs.add(source_ref)
            rows.append(row)
    return rows


def _load_string_mapping(path: Path) -> dict[str, str]:
    payload = _load_yaml_mapping(path)
    return {
        str(key).strip(): str(value).strip()
        for key, value in payload.items()
        if str(key).strip() and str(value).strip()
    }


def _load_alias_mapping(path: Path) -> dict[str, tuple[str, ...]]:
    payload = _load_yaml_mapping(path)
    aliases: dict[str, tuple[str, ...]] = {}
    for raw_symbol, raw_values in payload.items():
        if isinstance(raw_values, str):
            values: Iterable[Any] = (raw_values,)
        elif isinstance(raw_values, (list, tuple, set)):
            values = raw_values
        else:
            continue
        symbol = str(raw_symbol).strip()
        cleaned = tuple(dict.fromkeys(str(value).strip() for value in values if str(value).strip()))
        if symbol and cleaned:
            aliases[symbol] = cleaned
    return aliases


def _load_yaml_mapping(path: Path) -> dict[Any, Any]:
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _seen_source_refs(store: NewsMacroBriefStore, venue: str) -> set[str]:
    try:
        brief = store.read_latest(venue)
    except Exception:  # noqa: BLE001 - derived latest cache is advisory
        return set()
    refs = brief.input_refs if brief is not None and isinstance(brief.input_refs, dict) else {}
    raw_refs = refs.get("news_item_uuids") if isinstance(refs, dict) else None
    if not isinstance(raw_refs, (list, tuple, set)):
        return set()
    return {str(value).strip() for value in raw_refs if str(value).strip()}


def _ensure_utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc) if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _new_candidate_run_id(now: datetime, venue: str) -> str:
    timestamp = now.strftime("%Y%m%dT%H%M%S.%fZ")
    venue_key = "".join(char for char in str(venue) if char.isalnum() or char in ("_", "-")) or "UNKNOWN"
    return f"{timestamp}:{venue_key}:{uuid4().hex}"


def _normalized_symbols(values: Iterable[Any]) -> set[str]:
    return {str(value).strip() for value in values if str(value).strip()}


def _degraded_reason(
    *,
    eligible_symbols: set[str],
    news_items: tuple[dict[str, Any], ...],
    symbol_names: Mapping[str, str],
) -> str | None:
    if not eligible_symbols:
        return "no_eligible_symbols"
    if not news_items:
        return "no_archived_news_items"
    if not symbol_names:
        return "missing_symbol_names"
    return None


def _run_record(
    *,
    candidate_run_id: str,
    now: datetime,
    venue: str,
    eligible_symbol_count: int,
    radar_symbol_count: int,
    archived_items_read: int,
    status: str,
    reason: str,
    items_read: int = 0,
    eligible_items: int = 0,
    rejection_counts: Mapping[str, int] | None = None,
    challengers: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": NEWS_CHALLENGER_RUN_SCHEMA_VERSION,
        "candidate_run_id": candidate_run_id,
        "as_of": now.isoformat(),
        "venue": str(venue),
        "coverage_status": "partial",
        "eligible_symbol_count": int(eligible_symbol_count),
        "radar_symbol_count": int(radar_symbol_count),
        "archived_items_read": int(archived_items_read),
        "items_read": int(items_read),
        "eligible_items": int(eligible_items),
        "rejection_counts": dict(rejection_counts or {}),
        "challengers": list(challengers or []),
        "status": status,
        "reason": reason,
    }


def _candidate_with_run_id(candidate: dict[str, Any], candidate_run_id: str) -> dict[str, Any]:
    metadata = candidate.get("metadata")
    normalized_metadata = dict(metadata) if isinstance(metadata, Mapping) else {}
    normalized_metadata["candidate_run_id"] = candidate_run_id
    return {**candidate, "metadata": normalized_metadata}


def _append_run_safely(store: Any, record: Mapping[str, Any]) -> None:
    try:
        store.append(record)
    except Exception as exc:  # noqa: BLE001 - observability must never affect top-40
        log.warning(
            "news challenger observability write failed venue=%s run=%s: %s",
            record.get("venue"),
            record.get("candidate_run_id"),
            exc,
        )
