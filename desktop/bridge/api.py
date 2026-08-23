"""Read-only HTTP resources for the browser desk. Never writes."""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
BRIDGE_DIR = Path(__file__).resolve().parent
if str(BRIDGE_DIR) not in sys.path:
    sys.path.insert(0, str(BRIDGE_DIR))

from common import as_dict, as_list, dump, load_state  # noqa: E402
from snapshot import build_snapshot  # noqa: E402

UTC = timezone.utc


def _now() -> datetime:
    return datetime.now(UTC)


def _kill_active() -> bool:
    return (REPO_ROOT / "KILL").exists()


def _optional_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _optional_bool(value: object) -> bool | None:
    return value if isinstance(value, bool) else None


def _optional_finite_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _receipt_map(container: object, key: str) -> dict[str, Any] | None:
    if not isinstance(container, dict):
        return None
    value = container.get(key)
    return value if isinstance(value, dict) else None


def _execution_status(source: dict[str, Any]) -> str | None:
    """Project execution_status only: confirmed, recorded, or omitted. No receipts."""

    if source.get("executed") is not True:
        return None
    nested = source.get("decision")
    decision = nested if isinstance(nested, dict) else {}
    refs = decision.get("effect_refs")
    source_symbol = _optional_text(source.get("symbol"))
    source_decision_id = _optional_text(source.get("decision_id"))
    process_instance_id = _optional_text(decision.get("process_instance_id"))
    attempt_id = _optional_text(decision.get("attempt_id"))
    nested_decision_id = _optional_text(decision.get("decision_id"))
    broker_fill = _receipt_map(refs, "broker_fill")
    portfolio_readback = _receipt_map(refs, "portfolio_readback")
    if (
        source_symbol is not None
        and source_decision_id is not None
        and process_instance_id is not None
        and attempt_id is not None
        and nested_decision_id is not None
        and nested_decision_id == source_decision_id
        and _optional_text(decision.get("effect_status")) == "verified"
        and _optional_bool(decision.get("queue_fill_verified")) is True
        and _optional_text(decision.get("queue_terminal")) == "done"
        and broker_fill is not None
        and _optional_text(broker_fill.get("symbol")) == source_symbol
        and _optional_text(broker_fill.get("process_instance_id")) == process_instance_id
        and _optional_text(broker_fill.get("attempt_id")) == attempt_id
        and _optional_text(broker_fill.get("decision_id")) == nested_decision_id
        and _optional_text(broker_fill.get("ts")) is not None
        and portfolio_readback is not None
        and _optional_finite_number(portfolio_readback.get("cash")) is not None
        and _optional_finite_number(portfolio_readback.get("equity")) is not None
        and _optional_finite_number(portfolio_readback.get("position_quantity")) is not None
    ):
        return "confirmed"
    return "recorded"


def handle_snapshot(_args: argparse.Namespace) -> dict[str, Any]:
    payload = build_snapshot()
    payload["kill_active"] = _kill_active()
    return payload


def handle_decisions(args: argparse.Namespace) -> dict[str, Any]:
    from trader.interfaces.cockpit.projections.decisions import (
        build_ledger_rows,
        project_decision_ledger,
    )

    state = load_state()
    active = str(args.filter or "all")
    projection = project_decision_ledger(state, active)
    rows = build_ledger_rows(projection.grouped_rows)
    limit = max(1, min(int(args.limit or 80), 200))
    symbol = (args.symbol or "").strip().upper()
    slim_rows = []
    for row in rows:
        source = row.source_row
        if symbol and str(source.get("symbol") or "").upper() != symbol:
            continue
        slim_rows.append(
            {
                "row_key": row.row_key,
                "is_batch": row.is_batch,
                "utc_text": row.utc_text,
                "symbol": row.symbol,
                "action": row.action,
                "action_text": row.action_text,
                "confidence_text": row.confidence_text,
                "source_text": row.source_text,
                "effect_text": row.effect_text,
                "effect_kind": row.effect_kind,
                "has_detail": row.has_detail,
                "cycle_ts": source.get("cycle_ts") or source.get("ts"),
                "rationale": source.get("rationale") or source.get("reason"),
                "reason": source.get("reason"),
                "executed": source.get("executed"),
                "decision_source": source.get("decision_source"),
                "model_called": source.get("model_called"),
                "summary_kind": source.get("summary_kind"),
                "llm_model": source.get("llm_model"),
                "confidence": source.get("confidence"),
                "qty": source.get("qty"),
                "price": source.get("price"),
                "decision_reason_code": source.get("decision_reason_code"),
                "runtime": as_dict(source.get("runtime")),
            }
        )
        execution_status = _execution_status(source)
        if execution_status is not None:
            slim_rows[-1]["execution_status"] = execution_status
        if len(slim_rows) >= limit:
            break
    return {
        "filter": active,
        "counts": projection.filter_counts,
        "rows": slim_rows,
    }


def handle_plans(_args: argparse.Namespace) -> dict[str, Any]:
    from trader.interfaces.cockpit.derive import next_to_fire
    from trader.interfaces.cockpit.projections.plans import (
        project_active_watches,
        project_armed_orders,
        project_exit_plans,
        project_exit_watches,
    )

    state = load_state()
    now = _now()
    fire = [
        {
            "label": item.label,
            "detail": item.detail,
            "at": item.at.isoformat() if item.at else None,
            "countdown": item.countdown,
            "kind": item.kind,
        }
        for item in next_to_fire(state, now=now, limit=8)
    ]
    return {
        "armed": project_armed_orders(state, now=now),
        "exit_plans": project_exit_plans(state),
        "watches": project_active_watches(state, now=now),
        "exit_watches": project_exit_watches(state, now=now),
        "next_to_fire": fire,
        "default_next_wake": state.get("default_next_wake"),
    }


def handle_universe(_args: argparse.Namespace) -> dict[str, Any]:
    from trader.infrastructure.files.universe_config import load_user_overrides
    from trader.interfaces.cockpit.projections.universe import build_symbol_rows

    state = load_state()
    overrides = load_user_overrides(REPO_ROOT / "config" / "universe.yaml")
    rows = [
        {
            "symbol": row.symbol,
            "venue": row.venue,
            "name": row.name,
            "state_label": row.state_label,
            "pos": row.pos,
            "last_decision": row.last_decision,
            "last_decision_action": row.last_decision_action,
            "wake_text": row.wake_text,
            "wake_urgent": row.wake_urgent,
            "data_text": row.data_text,
            "data_stale": row.data_stale,
        }
        for row in build_symbol_rows(state, overrides, now=_now())
    ]
    venue_state = as_dict(state.get("venue_state"))
    venues = as_dict(venue_state.get("venues"))
    hotset = {
        venue: as_list(as_dict(payload).get("hotlist"))
        for venue, payload in venues.items()
    }
    return {
        "rows": rows,
        "hotset": hotset,
        "overrides": {
            "pinned": list(getattr(overrides, "pin", ()) or []),
            "banned": list(getattr(overrides, "ban", ()) or []),
        },
        "universe_symbols": as_list(state.get("universe_symbols")),
    }


def _risk_gate(state: dict[str, Any]) -> dict[str, Any]:
    from trader.interfaces.cockpit.pages.settings import load_risk_settings
    from trader.reporting.read_models.decision_filters import is_risk_row

    rejects = [
        {
            "symbol": row.get("symbol"),
            "action": row.get("action"),
            "reason": row.get("reason"),
            "cycle_ts": row.get("cycle_ts") or row.get("ts"),
        }
        for row in as_list(state.get("recent_decisions"))
        if isinstance(row, dict) and is_risk_row(row)
    ]
    return {
        "reject_count": len(rejects),
        "recent_rejects": rejects[:8],
        "caps": load_risk_settings(REPO_ROOT / "config"),
    }


def _model_panel(state: dict[str, Any]) -> dict[str, Any]:
    kpis = as_dict(state.get("kpis"))
    perfs = as_list(kpis.get("model_performance"))
    if perfs:
        return {"model_performance": perfs}
    seen: dict[str, int] = {}
    for row in as_list(state.get("recent_decisions")):
        if not isinstance(row, dict):
            continue
        provider = str(row.get("llm_provider") or "")
        model = str(row.get("llm_model") or "")
        if not provider and not model:
            continue
        key = f"{provider}·{model}" if provider and model else (provider or model)
        seen[key] = seen.get(key, 0) + 1
    return {"providers": seen, "model_performance": []}


def handle_health(_args: argparse.Namespace) -> dict[str, Any]:
    from trader.interfaces.cockpit.projections.health import (
        project_freshness,
        project_fx_rates,
        project_learnings,
        project_llm_health,
        project_memory,
        project_sources,
        project_universe_health,
    )

    state = load_state()
    return {
        "freshness": project_freshness(state),
        "fx": project_fx_rates(state),
        "sources": project_sources(
            state,
            ib_host=os.environ.get("IB_HOST", "127.0.0.1"),
            ib_port=os.environ.get("IB_PORT", "4002"),
            ib_client_id=os.environ.get("IB_CLIENT_ID", "1"),
        ),
        "llm": project_llm_health(state),
        "learnings": project_learnings(state),
        "memory": project_memory(state, now=_now()),
        "universe": project_universe_health(state),
        "risk": _risk_gate(state),
        "model": _model_panel(state),
        "queue": as_dict(state.get("queue_worker_activity")),
        "kill_active": _kill_active(),
    }


def handle_reports(_args: argparse.Namespace) -> dict[str, Any]:
    from trader.interfaces.cockpit.projections.reports import collect_report_items

    state = load_state()
    items = collect_report_items(
        REPO_ROOT / "state",
        company_names=as_dict(state.get("company_map")),
    )
    return {
        "items": [
            {
                "kind": item.kind,
                "key": item.key,
                "label": item.label,
                "as_of": item.as_of,
                "depth": item.depth,
            }
            for item in items
        ]
    }


def handle_report(args: argparse.Namespace) -> dict[str, Any]:
    from trader.interfaces.cockpit.projections.reports import collect_report_items

    key = unquote(str(args.key or ""))
    state = load_state()
    items = collect_report_items(
        REPO_ROOT / "state",
        company_names=as_dict(state.get("company_map")),
    )
    for item in items:
        if item.key == key:
            return {
                "kind": item.kind,
                "key": item.key,
                "label": item.label,
                "as_of": item.as_of,
                "depth": item.depth,
                "payload": item.payload,
            }
    return {"error": "not_found", "key": key}


def handle_logs_events(args: argparse.Namespace) -> dict[str, Any]:
    from trader.interfaces.cockpit.events import format_event_line

    path = REPO_ROOT / "state" / "events.jsonl"
    limit = max(1, min(int(args.limit or 200), 500))
    cursor = int(args.cursor or 0)
    events: list[dict[str, Any]] = []
    next_cursor = cursor
    if path.exists():
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            if cursor > size:
                cursor = 0
            if cursor <= 0:
                start = max(0, size - 512_000)
                handle.seek(start)
                raw = handle.read()
                if start > 0:
                    newline = raw.find(b"\n")
                    if newline >= 0:
                        raw = raw[newline + 1 :]
                next_cursor = size
            else:
                handle.seek(cursor)
                raw = handle.read()
                next_cursor = cursor + len(raw)
        for line in raw.decode("utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            if not isinstance(row, dict):
                continue
            formatted = format_event_line(row)
            events.append(
                {
                    "text": formatted.text,
                    "klass": formatted.markup_class.name.lower(),
                    "event": row.get("event"),
                    "symbol": row.get("symbol"),
                    "ts": row.get("ts"),
                }
            )
    return {"events": events[-limit:], "cursor": next_cursor}


def handle_logs_trace(args: argparse.Namespace) -> dict[str, Any]:
    path = REPO_ROOT / "state" / "agent_trace.log"
    limit = max(1, min(int(args.limit or 200), 400))
    if not path.exists():
        return {"lines": []}
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return {"lines": []}
    return {"lines": lines[-limit:]}


def handle_settings(_args: argparse.Namespace) -> dict[str, Any]:
    from trader.interfaces.cockpit.pages.settings import (
        EDITABLE_ROWS,
        load_data_settings,
        load_env_display,
        load_portfolio_settings,
        load_radar_settings,
        load_risk_settings,
    )

    config_dir = REPO_ROOT / "config"
    return {
        "env": load_env_display(REPO_ROOT),
        "portfolio": load_portfolio_settings(config_dir),
        "radar": load_radar_settings(config_dir),
        "risk": load_risk_settings(config_dir),
        "data_sources": load_data_settings(config_dir),
        "editable": [
            {
                "key": row.key,
                "label": row.label,
                "yaml_file": row.yaml_file,
                "effect": row.effect,
                "vtype": row.vtype,
            }
            for row in EDITABLE_ROWS
        ],
        "kill_active": _kill_active(),
    }


def handle_symbol(args: argparse.Namespace) -> dict[str, Any]:
    from trader.interfaces.cockpit import format as cockpit_format
    from trader.interfaces.cockpit.derive import positions_by_pnl
    from trader.interfaces.cockpit.projections.plans import (
        project_active_watches,
        project_armed_orders,
        project_exit_plans,
        project_exit_watches,
    )

    symbol = str(args.symbol or "").strip()
    state = load_state()
    now = _now()
    holdings = [
        row
        for row in positions_by_pnl(state)
        if str(row.get("symbol") or "") == symbol
    ]
    decisions = []
    why = None
    for row in reversed(as_list(state.get("recent_decisions"))):
        if not isinstance(row, dict) or str(row.get("symbol") or "") != symbol:
            continue
        item = {
            "cycle_ts": row.get("cycle_ts") or row.get("ts"),
            "action": row.get("action"),
            "rationale": row.get("rationale") or row.get("reason"),
            "reason": row.get("reason"),
            "confidence": row.get("confidence"),
            "executed": row.get("executed"),
            "decision_source": row.get("decision_source"),
            "model_called": row.get("model_called"),
            "llm_model": row.get("llm_model"),
        }
        decisions.append(item)
        if why is None and str(row.get("rationale") or "").strip():
            why = item
        if len(decisions) >= 20:
            break
    return {
        "symbol": symbol,
        "name": as_dict(state.get("company_map")).get(symbol),
        "holding": holdings[0] if holdings else None,
        "decisions": decisions,
        "why": why,
        "exit_plans": [
            row for row in project_exit_plans(state).rows if row.symbol == symbol
        ],
        "armed": [
            row for row in project_armed_orders(state, now=now).rows if row.symbol == symbol
        ],
        "watches": [
            row for row in project_active_watches(state, now=now).rows if row.symbol == symbol
        ],
        "exit_watches": [
            row for row in project_exit_watches(state, now=now).rows if row.symbol == symbol
        ],
        "wake": as_dict(state.get("symbol_wakes")).get(symbol),
        "stale": as_dict(state.get("stale_market_data")).get(symbol),
        "last_price": cockpit_format.price_for_symbol(state, symbol),
    }


def _load_company_intel(symbols: set[str]) -> dict[str, dict[str, Any]]:
    """Load company intelligence fields for a set of symbols (best-effort, never raises)."""
    intel_dir = REPO_ROOT / "state" / "company_intelligence" / "current"
    result: dict[str, dict[str, Any]] = {}
    if not intel_dir.exists() or not symbols:
        return result
    for path in intel_dir.glob("*.json"):
        try:
            raw = json.loads(path.read_text(encoding="utf-8", errors="replace"))
            symbol = str(raw.get("symbol") or "")
            if symbol not in symbols:
                continue
            briefs = raw.get("briefs") or {}
            # Prefer deep brief for richer text, fall back to screen
            brief: dict[str, Any] = {}
            if isinstance(briefs, dict):
                brief = briefs.get("deep") or briefs.get("screen") or {}
            thesis_raw = brief.get("company_thesis") or {}
            business_raw = brief.get("business") or {}
            result[symbol] = {
                "thesis": str(thesis_raw.get("summary") or "").strip() or None,
                "thesis_status": str(thesis_raw.get("status") or "").strip() or None,
                "business": str(business_raw.get("summary") or "").strip() or None,
            }
        except Exception:
            continue
    return result


def handle_portfolio(args: argparse.Namespace) -> dict[str, Any]:
    from trader.interfaces.cockpit.derive import equity_snapshot, exposure
    from trader.interfaces.cockpit.projections.health import project_fx_rates
    from trader.interfaces.cockpit.projections.portfolio import project_portfolio_positions
    from trader.interfaces.cockpit.projections.universe import venue_of_safe

    state = load_state()
    sort_mode = int(args.sort or 0)
    snap = equity_snapshot(state)
    exp = exposure(state)
    trips = as_list(state.get("recent_trips")) or as_list(
        as_dict(state.get("attribution")).get("recent_trips")
    )
    positions = project_portfolio_positions(state, sort_mode=sort_mode)
    company_map = as_dict(state.get("company_map"))

    # Symbols currently held
    symbols_in_pos = {row.symbol for row in positions.rows}

    # Last LLM rationale per symbol (scan recent_decisions newest-first)
    why_map: dict[str, dict[str, Any]] = {}
    for row in reversed(as_list(state.get("recent_decisions"))):
        if not isinstance(row, dict):
            continue
        sym = str(row.get("symbol") or "")
        if sym not in symbols_in_pos or sym in why_map:
            continue
        if str(row.get("decision_source") or "").lower() != "llm" or row.get("model_called") is not True:
            continue
        rationale = str(row.get("rationale") or "").strip()
        if rationale:
            why_map[sym] = {
                "why": rationale,
                "why_at": row.get("cycle_ts") or row.get("ts"),
            }

    # Company intelligence: thesis + business (open only matched files)
    intel = _load_company_intel(symbols_in_pos)

    # Build stories in the same order as positions
    stories: list[dict[str, Any]] = []
    for row in positions.rows:
        sym = row.symbol
        ci = intel.get(sym, {})
        why_entry = why_map.get(sym, {})
        stories.append(
            {
                "symbol": sym,
                "name": company_map.get(sym),
                "side": row.side,
                "qty": row.qty,
                "pnl": row.pnl,
                "pnl_pct": row.pnl_pct,
                "venue": venue_of_safe(sym),
                "why": why_entry.get("why"),
                "why_at": why_entry.get("why_at"),
                "thesis": ci.get("thesis"),
                "thesis_status": ci.get("thesis_status"),
                "business": ci.get("business"),
            }
        )

    return {
        "sort_mode": sort_mode,
        "positions": positions,
        "equity": {
            "equity": snap.equity,
            "cash": snap.cash,
            "cash_available": snap.cash_available,
            "return_pct": snap.return_pct,
            "pnl_usd": snap.pnl_usd,
            "unrealized": snap.unrealized,
        },
        "exposure": {
            "long_usd": exp.long_usd,
            "short_usd": exp.short_usd,
            "gross": exp.gross,
            "net": exp.net,
            "by_venue": exp.by_venue,
        },
        "fx": project_fx_rates(state),
        "closed_trips": trips[:20],
        "stories": stories,
    }


def _clip_text(value: object, limit: int) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def handle_overview(_args: argparse.Namespace) -> dict[str, Any]:
    from trader.infrastructure.files.universe_config import load_user_overrides
    from trader.interfaces.cockpit import format as cockpit_format
    from trader.interfaces.cockpit.derive import next_to_fire, positions_by_pnl
    from trader.interfaces.cockpit.projections.reports import collect_report_items
    from trader.interfaces.cockpit.projections.universe import build_symbol_rows, venue_of_safe

    state = load_state()
    now = _now()
    overrides = load_user_overrides(REPO_ROOT / "config" / "universe.yaml")
    rows = build_symbol_rows(state, overrides, now=now)
    open_venues = {str(item) for item in as_list(state.get("open_venues_list"))}
    holdings = {
        cockpit_format.holding_symbol(row): row for row in positions_by_pnl(state)
    }
    row_by_symbol = {row.symbol: row for row in rows}
    venue_state = as_dict(as_dict(state.get("venue_state")).get("venues"))
    company_map = as_dict(state.get("company_map"))
    stale_map = as_dict(state.get("stale_market_data"))

    reports = collect_report_items(REPO_ROOT / "state", company_names=company_map)
    by_key = {item.key: item for item in reports}
    global_item = by_key.get("global:current")
    global_payload = as_dict(global_item.payload if global_item else None)
    families = as_dict(global_payload.get("family_priority"))
    venue_posture = as_dict(global_payload.get("venue_posture"))

    def pack_fire(item: Any) -> dict[str, Any]:
        symbol = item.label
        row = row_by_symbol.get(symbol)
        venue = None if symbol in {"wake", "—"} else venue_of_safe(symbol)
        return {
            "label": symbol,
            "detail": item.detail,
            "at": item.at.isoformat() if item.at else None,
            "countdown": item.countdown,
            "kind": item.kind,
            "venue": venue,
            "on_book": symbol in holdings,
            "state_label": row.state_label if row else None,
        }

    fires = [pack_fire(item) for item in next_to_fire(state, now=now, limit=10)]
    venues: list[dict[str, Any]] = []
    for venue in ("TW", "EU", "US"):
        venue_rows = [row for row in rows if row.venue == venue]
        hotlist = [str(symbol) for symbol in as_list(as_dict(venue_state.get(venue)).get("hotlist"))]
        stale_n = sum(1 for row in venue_rows if row.data_stale)
        if venue in open_venues:
            session = "open"
        elif venue_rows and stale_n == len(venue_rows):
            session = "stale"
        else:
            session = "closed"

        on_book: list[dict[str, Any]] = []
        for row in venue_rows:
            if row.pos not in {"L", "S"}:
                continue
            holding = holdings.get(row.symbol) or {}
            on_book.append(
                {
                    "symbol": row.symbol,
                    "name": row.name,
                    "side": row.pos,
                    "state_label": row.state_label,
                    "pnl": cockpit_format.holding_pnl(holding) if holding else None,
                    "stale": row.data_stale,
                    "wake_text": row.wake_text,
                    "last_decision": row.last_decision,
                }
            )

        seen = {item["symbol"] for item in on_book}
        watchlist: list[dict[str, Any]] = []
        for row in venue_rows:
            if row.symbol in seen:
                continue
            if not (row.wake_urgent or row.state_label == "hot"):
                continue
            watchlist.append(
                {
                    "symbol": row.symbol,
                    "name": row.name,
                    "state_label": row.state_label,
                    "wake_text": row.wake_text,
                    "wake_urgent": row.wake_urgent,
                    "last_decision": row.last_decision,
                    "stale": row.data_stale,
                }
            )
            seen.add(row.symbol)
            if len(watchlist) >= 8:
                break

        overnight = []
        if not venue_rows:
            overnight = [
                {"symbol": symbol, "name": str(company_map.get(symbol) or "")}
                for symbol in hotlist[:10]
            ]

        regional = by_key.get(f"regional:{venue}")
        regional_payload = as_dict(regional.payload if regional else None)
        venues.append(
            {
                "venue": venue,
                "session": session,
                "posture": venue_posture.get(venue),
                "roster": len(venue_rows),
                "hot": sum(1 for row in venue_rows if row.state_label == "hot"),
                "pool": sum(1 for row in venue_rows if row.state_label == "pool"),
                "stale": stale_n,
                "overnight_hot": len(hotlist),
                "on_book": on_book,
                "watchlist": watchlist,
                "overnight": overnight,
                "fire": [item for item in fires if item["venue"] == venue][:5],
                "brief": _clip_text(regional_payload.get("summary"), 280),
                "brief_as_of": regional.as_of if regional else None,
            }
        )

    placed = {item["symbol"] for venue in venues for item in venue["on_book"]}
    placement: list[dict[str, Any]] = []
    for venue in venues:
        for item in venue["on_book"]:
            placement.append({**item, "venue": venue["venue"]})
    for symbol, holding in holdings.items():
        if symbol in placed:
            continue
        qty = cockpit_format.holding_quantity(holding)
        placement.append(
            {
                "symbol": symbol,
                "name": str(company_map.get(symbol) or ""),
                "side": "L" if qty >= 0 else "S",
                "state_label": "off-roster",
                "pnl": cockpit_format.holding_pnl(holding),
                "stale": symbol in stale_map,
                "venue": venue_of_safe(symbol),
                "wake_text": "",
                "last_decision": "",
            }
        )

    return {
        "thesis": {
            "gross_mode": global_payload.get("gross_mode"),
            "net_bias": global_payload.get("net_bias"),
            "rationale": global_payload.get("rationale") or "",
            "favored": as_list(families.get("favored")),
            "deprioritized": as_list(families.get("deprioritized")),
            "venue_posture": venue_posture,
            "as_of": global_item.as_of if global_item else None,
        },
        "venues": venues,
        "placement": placement,
        "next_to_fire": fires,
        "open_venues": sorted(open_venues),
    }


def handle_symbol_bars(args: argparse.Namespace) -> dict[str, Any]:
    symbol = str(args.symbol or "").strip().upper()
    radar_dir = REPO_ROOT / "state" / "radar_cache"
    empty: dict[str, Any] = {"symbol": symbol, "as_of": None, "bars": []}
    if not symbol or not radar_dir.exists():
        return empty
    candidates = sorted(radar_dir.glob("*.json"), reverse=True)[:5]
    for path in candidates:
        try:
            raw = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            continue
        if not isinstance(raw, dict):
            continue
        entry = raw.get(symbol)
        if not entry or not isinstance(entry, list):
            continue
        bars = []
        for bar in entry:
            if not isinstance(bar, dict):
                continue
            try:
                bars.append(
                    {
                        "ts": str(bar.get("ts") or ""),
                        "open": float(bar.get("open") or 0),
                        "high": float(bar.get("high") or 0),
                        "low": float(bar.get("low") or 0),
                        "close": float(bar.get("close") or 0),
                        "volume": float(bar["volume"]) if bar.get("volume") is not None else None,
                    }
                )
            except (TypeError, ValueError):
                # Barre corrompue → ignorée, on continue vers la suivante.
                continue
        bars.sort(key=lambda b: b["ts"])
        return {"symbol": symbol, "as_of": path.stem, "bars": bars}
    return empty


def handle_intelligence_briefing(_args: argparse.Namespace) -> dict[str, Any]:
    from trader.interfaces.cockpit.projections.universe import venue_of_safe
    from trader.market.macro_calendar import load_calendar, macro_next
    from trader.reporting.read_models.intelligence_briefing import load_daily_briefing

    state = load_state()
    company_names = as_dict(state.get("company_map"))
    symbol_venues = {sym: venue_of_safe(sym) for sym in company_names}
    payload = load_daily_briefing(
        REPO_ROOT / "state",
        now=_now(),
        company_names=company_names,
        symbol_venues=symbol_venues,
    )
    try:
        calendar = load_calendar(REPO_ROOT / "state" / "macro_calendar.json")
        events = macro_next(_now(), calendar, limit=5)
        payload["calendar"] = [
            {"event": e["event"], "at": e["at"], "in_h": e["in_h"]}
            for e in events
        ]
    except Exception as exc:
        print(f"[intelligence-briefing] calendar unavailable: {exc}", file=sys.stderr)
        payload["calendar"] = []
    return payload


def handle_intelligence_news(args: argparse.Namespace) -> dict[str, Any]:
    from trader.interfaces.cockpit.projections.universe import venue_of_safe
    from trader.reporting.read_models.intelligence_briefing import load_news_feed

    state = load_state()
    company_names = as_dict(state.get("company_map"))
    symbol_venues = {sym: venue_of_safe(sym) for sym in company_names}
    venue = str(args.venue or "").strip().upper() or None
    symbol = str(args.symbol or "").strip().upper() or None
    limit = max(1, min(int(args.limit or 60), 200))
    return load_news_feed(
        REPO_ROOT / "state",
        now=_now(),
        venue=venue,
        symbol=symbol,
        limit=limit,
        company_names=company_names,
        symbol_venues=symbol_venues,
    )


def handle_intelligence_world(args: argparse.Namespace) -> dict[str, Any]:
    from trader.reporting.read_models.intelligence_timeline import load_world_timeline

    limit = max(1, min(int(args.limit or 240), 500))
    return load_world_timeline(REPO_ROOT / "state", now=_now(), limit=limit)


def handle_intelligence_regions(args: argparse.Namespace) -> dict[str, Any]:
    from trader.reporting.read_models.intelligence_timeline import (
        load_region_intelligence,
    )

    limit = max(1, min(int(args.limit or 240), 500))
    venue = str(args.venue or "").strip().upper()
    return load_region_intelligence(
        REPO_ROOT / "state",
        now=_now(),
        venue=venue or None,
        limit=limit,
    )


def handle_intelligence_companies(args: argparse.Namespace) -> dict[str, Any]:
    from trader.reporting.read_models.intelligence_timeline import (
        load_company_intelligence,
    )

    limit = max(1, min(int(args.limit or 320), 500))
    symbol = str(args.symbol or "").strip().upper()
    venue = str(args.venue or "").strip().upper()
    return load_company_intelligence(
        REPO_ROOT / "state",
        now=_now(),
        symbol=symbol or None,
        venue=venue or None,
        runtime_state=load_state(),
        limit=limit,
    )


HANDLERS = {
    "snapshot": handle_snapshot,
    "decisions": handle_decisions,
    "plans": handle_plans,
    "universe": handle_universe,
    "health": handle_health,
    "reports": handle_reports,
    "report": handle_report,
    "logs-events": handle_logs_events,
    "logs-trace": handle_logs_trace,
    "settings": handle_settings,
    "symbol": handle_symbol,
    "symbol-bars": handle_symbol_bars,
    "portfolio": handle_portfolio,
    "overview": handle_overview,
    "intelligence-world": handle_intelligence_world,
    "intelligence-regions": handle_intelligence_regions,
    "intelligence-companies": handle_intelligence_companies,
    "intelligence-briefing": handle_intelligence_briefing,
    "intelligence-news": handle_intelligence_news,
}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("resource")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--symbol", default="")
    parser.add_argument("--venue", default="")
    parser.add_argument("--key", default="")
    parser.add_argument("--cursor", type=int, default=0)
    parser.add_argument("--filter", default="all")
    parser.add_argument("--sort", type=int, default=0)
    args = parser.parse_args()
    handler = HANDLERS.get(args.resource)
    if handler is None:
        raise SystemExit(f"unknown resource: {args.resource}")
    dump(handler(args))


if __name__ == "__main__":
    main()
