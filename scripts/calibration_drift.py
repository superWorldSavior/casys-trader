#!/usr/bin/env python3
"""Calibration de la confiance et dérive temporelle — read-model pur.

Lit ``state/decisions.jsonl`` (DuckDB ``read_json_auto`` + ``union_by_name``)
et ``state/model_performance.jsonl`` (round-trips FIFO canoniques). Aucune
écriture, aucune sous-commande de ``decisions_analytics.py``.

Usage :
    uv run python scripts/calibration_drift.py calibration
    uv run python scripts/calibration_drift.py drift
    uv run python scripts/calibration_drift.py session-window
    uv run python scripts/calibration_drift.py calibration --state-dir PATH --json

Les HOLD synthétiques infra (``benchmark_basis="infra"``) sont exclus des
stats de calibration. ``compute_round_trips`` ne persiste pas ``decision_id`` :
le join se fait ensuite via le fill d'ouverture.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import Any

import duckdb

from trader.domain.decision_benchmark import decision_benchmark_context
from trader.reporting.read_models.trade_history import compute_round_trips

ROOT = Path(__file__).resolve().parent.parent

UNKNOWN_VERSION = "(unknown)"
CONFIDENCE_MISSING = "confidence_missing"
NEAR_CLOSE_BUCKET = "to_close_m<60"

CONFIDENCE_BUCKETS: tuple[tuple[str, float, float], ...] = (
    ("0.0-0.3", 0.0, 0.3),
    ("0.3-0.5", 0.3, 0.5),
    ("0.5-0.6", 0.5, 0.6),
    ("0.6-0.7", 0.6, 0.7),
    ("0.7-0.8", 0.7, 0.8),
    ("0.8-1.0", 0.8, 1.0000001),
)
SESSION_BUCKETS: tuple[tuple[str, float, float], ...] = (
    ("0-30", 0.0, 30.0),
    ("30-90", 30.0, 90.0),
    ("90-240", 90.0, 240.0),
    ("240+", 240.0, math.inf),
)
_EU_SUFFIXES = (
    ".TWO",
    ".TW",
    ".PA",
    ".DE",
    ".AS",
    ".BR",
    ".LS",
    ".SW",
    ".MI",
    ".MC",
    ".OL",
    ".CO",
    ".ST",
    ".HE",
    ".VI",
    ".L",
)

__all__ = ["main"]


def _as_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(parsed):
        return None
    return parsed


def _as_int(value: Any) -> int | None:
    parsed = _as_float(value)
    if parsed is None:
        return None
    return int(parsed)


def _as_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in {"true", "1", "yes"}:
        return True
    if text in {"false", "0", "no", ""}:
        return False
    return None


def _as_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _as_mapping(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return {str(key): _duckdb_value(item) for key, item in value.items()}
    if hasattr(value, "_asdict"):
        return {str(key): _duckdb_value(item) for key, item in value._asdict().items()}
    return {}


def _duckdb_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _duckdb_value(item) for key, item in value.items()}
    if hasattr(value, "_asdict"):
        return {str(key): _duckdb_value(item) for key, item in value._asdict().items()}
    if isinstance(value, (list, tuple)):
        return [_duckdb_value(item) for item in value]
    try:
        from decimal import Decimal

        if isinstance(value, Decimal):
            return float(value)
    except Exception:  # noqa: BLE001 — conversion best-effort
        pass
    return value


def _parse_ts(value: Any) -> datetime | None:
    text = _as_text(value)
    if text is None:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _parse_since(value: str) -> datetime:
    raw = value.strip()
    if len(raw) == 10:
        return datetime.fromisoformat(raw).replace(tzinfo=timezone.utc)
    parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _iso_week(value: Any) -> str | None:
    parsed = _parse_ts(value)
    if parsed is None:
        return None
    iso = parsed.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def _iter_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            rows.append(obj)
    return rows


def _session_coverage(path: Path) -> dict[str, int]:
    n_rows = 0
    n_with_key = 0
    n_with_value = 0
    n_to_close_value = 0
    for row in _iter_jsonl(path):
        n_rows += 1
        snapshot = row.get("market_snapshot")
        if not isinstance(snapshot, dict):
            continue
        if "since_open_m" in snapshot:
            n_with_key += 1
            if snapshot.get("since_open_m") is not None:
                n_with_value += 1
        if snapshot.get("to_close_m") is not None:
            n_to_close_value += 1
    return {
        "n_rows": n_rows,
        "n_with_key": n_with_key,
        "n_with_value": n_with_value,
        "n_to_close_value": n_to_close_value,
    }


def _load_decisions_via_duckdb(journal: Path) -> list[dict[str, Any]]:
    """Vue append-only via le même pattern que ``decisions_analytics`` (non modifié)."""
    if not journal.is_file() or journal.stat().st_size == 0:
        return []
    escaped = journal.resolve().as_posix().replace("'", "''")
    con = duckdb.connect()
    try:
        con.execute(
            f"""
            CREATE VIEW d AS
            SELECT * FROM read_json_auto(
                '{escaped}',
                format='newline_delimited',
                union_by_name=true,
                maximum_object_size=33554432,
                ignore_errors=true
            )
            """
        )
        relation = con.sql("SELECT * FROM d")
        columns = list(relation.columns)
        loaded: list[dict[str, Any]] = []
        for tup in relation.fetchall():
            loaded.append(
                {str(col): _duckdb_value(val) for col, val in zip(columns, tup, strict=False)}
            )
        return loaded
    finally:
        con.close()


def _is_infra(row: dict[str, Any]) -> bool:
    return decision_benchmark_context(row).get("benchmark_basis") == "infra"


def _is_llm_authentic(row: dict[str, Any]) -> bool:
    if _is_infra(row):
        return False
    if row.get("model_called") is True:
        return True
    return str(row.get("decision_source") or "").strip().lower() == "llm"


def _normalize_action(value: Any) -> str:
    return str(value or "").strip().upper()


def _derive_venue(row: dict[str, Any]) -> str | None:
    snapshot = _as_mapping(row.get("market_snapshot"))
    mandate = _as_mapping(row.get("mandate_ref"))
    explicit = _as_text(snapshot.get("venue")) or _as_text(mandate.get("venue"))
    if explicit:
        return explicit.upper()
    symbol = str(row.get("symbol") or "")
    if symbol.endswith("=X"):
        return "FX"
    if symbol.endswith(".TWO") or symbol.endswith(".TW") or symbol.endswith(".T"):
        return "TW"
    for suffix in _EU_SUFFIXES:
        if suffix in {".TW", ".TWO"}:
            continue
        if symbol.endswith(suffix):
            return "EU"
    if symbol:
        return "US"
    return None


def _normalize_decision(raw: dict[str, Any]) -> dict[str, Any]:
    snapshot = _as_mapping(raw.get("market_snapshot"))
    mandate = _as_mapping(raw.get("mandate_ref"))
    code_version = raw.get("code_version")
    git_commit_short = None
    if isinstance(code_version, dict):
        git_commit_short = _as_text(code_version.get("git_commit_short"))
    elif code_version is not None:
        nested = _as_mapping(code_version)
        git_commit_short = _as_text(nested.get("git_commit_short"))
    action = _normalize_action(raw.get("action"))
    row = {
        "decision_id": _as_text(raw.get("decision_id")),
        "cycle_ts": _as_text(raw.get("cycle_ts") or raw.get("ts")),
        "symbol": _as_text(raw.get("symbol")) or "",
        "action": action,
        "confidence": _as_float(raw.get("confidence")),
        "executed": _as_bool(raw.get("executed")) is True,
        "decision_source": _as_text(raw.get("decision_source")),
        "model_called": _as_bool(raw.get("model_called")),
        "reason": _as_text(raw.get("reason") or raw.get("decision_reason_code")),
        "llm_error": raw.get("llm_error"),
        "git_commit_short": git_commit_short,
        "since_open_m": _as_int(snapshot.get("since_open_m")),
        "to_close_m": _as_int(snapshot.get("to_close_m")),
        "venue": _derive_venue(raw),
        "mandate_venue": _as_text(mandate.get("venue")),
        "market_snapshot": snapshot,
        "mandate_ref": mandate or None,
        "code_version": code_version if isinstance(code_version, dict) else _as_mapping(code_version) or None,
    }
    return row


def _confidence_bucket(confidence: float | None) -> str:
    if confidence is None:
        return CONFIDENCE_MISSING
    for name, low, high in CONFIDENCE_BUCKETS:
        if low <= confidence < high:
            return name
    return CONFIDENCE_MISSING


def _session_bucket(since_open_m: int | None) -> str | None:
    if since_open_m is None:
        return None
    value = float(since_open_m)
    for name, low, high in SESSION_BUCKETS:
        if low <= value < high:
            return name
    return None


def _annotate_trips(trips: list[dict[str, Any]], fills: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Attache ``entry_decision_id`` : le FIFO canonique ne le conserve pas."""
    by_key: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for fill in fills:
        by_key[(str(fill.get("symbol")), str(fill.get("ts")))].append(fill)
    annotated: list[dict[str, Any]] = []
    for trip in trips:
        out = dict(trip)
        want = "BUY" if str(trip.get("side")) == "LONG" else "SELL"
        candidates = by_key.get((str(trip.get("symbol")), str(trip.get("entry_ts"))), [])
        match = next(
            (fill for fill in candidates if _normalize_action(fill.get("action")) == want),
            None,
        )
        entry_fx = 1.0
        entry_decision_id = None
        if match is not None:
            entry_decision_id = _as_text(match.get("decision_id")) or _as_text(
                match.get("entry_decision_id")
            )
            parsed_fx = _as_float(match.get("fx_rate"))
            if parsed_fx is not None and parsed_fx > 0:
                entry_fx = parsed_fx
        quantity = _as_float(out.get("quantity")) or 0.0
        entry_price = _as_float(out.get("entry_price")) or 0.0
        notional = entry_price * quantity * entry_fx
        pnl = _as_float(out.get("pnl"))
        out["entry_decision_id"] = entry_decision_id
        out["entry_fx_rate"] = entry_fx
        out["notional_usd"] = notional
        out["pnl_bps"] = (pnl / notional * 10000.0) if pnl is not None and notional > 0 else None
        out["side_action"] = want
        out["venue"] = _derive_venue({"symbol": out.get("symbol"), "market_snapshot": {}, "mandate_ref": {}})
        annotated.append(out)
    return annotated


def _empty_bucket_stats() -> dict[str, Any]:
    return {
        "n_decisions": 0,
        "n_executed": 0,
        "n_closed_trips": 0,
        "win_rate": None,
        "mean_announced_confidence": None,
        "mean_pnl_bps": None,
        "calibration_gap": None,
    }


def _summarize_group(
    decisions: list[dict[str, Any]],
    trips: list[dict[str, Any]],
    *,
    announced_from: str = "decision",
) -> dict[str, Any]:
    executed = [row for row in decisions if row.get("executed")]
    pnls = [_as_float(trip.get("pnl")) for trip in trips]
    wins = [pnl for pnl in pnls if pnl is not None and pnl > 0]
    bps_values = [_as_float(trip.get("pnl_bps")) for trip in trips]
    bps_known = [value for value in bps_values if value is not None]
    if announced_from == "fill":
        announced = [_as_float(trip.get("entry_confidence")) for trip in trips]
    else:
        announced = [_as_float(trip.get("announced_confidence")) for trip in trips]
    announced_known = [value for value in announced if value is not None]
    win_rate = (len(wins) / len(trips)) if trips else None
    mean_announced = (sum(announced_known) / len(announced_known)) if announced_known else None
    gap = (mean_announced - win_rate) if mean_announced is not None and win_rate is not None else None
    return {
        "n_decisions": len(decisions),
        "n_executed": len(executed),
        "n_closed_trips": len(trips),
        "win_rate": win_rate,
        "mean_announced_confidence": mean_announced,
        "mean_pnl_bps": (sum(bps_known) / len(bps_known)) if bps_known else None,
        "calibration_gap": gap,
    }


def _passes_since(ts_value: Any, since: datetime | None) -> bool:
    if since is None:
        return True
    parsed = _parse_ts(ts_value)
    if parsed is None:
        return False
    return parsed >= since


def _load_state(state_dir: Path) -> dict[str, Any]:
    journal = state_dir / "decisions.jsonl"
    fills_path = state_dir / "model_performance.jsonl"
    notes: list[str] = []
    decisions_found = journal.is_file()
    fills_found = fills_path.is_file()
    if not decisions_found:
        notes.append(f"journal introuvable : {journal}")
    if not fills_found:
        notes.append(f"fills introuvables : {fills_path}")

    raw_decisions: list[dict[str, Any]] = []
    if decisions_found:
        try:
            raw_decisions = _load_decisions_via_duckdb(journal)
        except Exception as exc:  # noqa: BLE001 — read-model : ne jamais planter sur un JSONL incomplet
            notes.append(f"lecture DuckDB impossible ({exc!s}) ; journal ignoré")
            raw_decisions = []

    decisions = [_normalize_decision(row) for row in raw_decisions]
    trips: list[dict[str, Any]] = []
    fills: list[dict[str, Any]] = []
    if fills_found:
        fills = _iter_jsonl(fills_path)
        trips = _annotate_trips(compute_round_trips(state_dir), fills)

    return {
        "decisions": decisions,
        "trips": trips,
        "fills": fills,
        "notes": notes,
        "coverage": _session_coverage(journal),
        "inputs": {
            "decisions_path": str(journal),
            "decisions_found": decisions_found,
            "fills_path": str(fills_path),
            "fills_found": fills_found,
            "n_decision_rows": len(decisions),
            "n_fill_rows": len(fills),
        },
    }


def _filter_universe(
    state: dict[str, Any],
    *,
    since: str | None,
    action: str | None,
    venue: str | None,
    directional_only: bool,
) -> dict[str, Any]:
    since_dt = _parse_since(since) if since else None
    action_filter = _normalize_action(action) if action else None
    venue_filter = venue.strip().upper() if venue else None

    n_infra = 0
    kept: list[dict[str, Any]] = []
    for row in state["decisions"]:
        if _is_infra(row):
            n_infra += 1
            continue
        if not _passes_since(row.get("cycle_ts"), since_dt):
            continue
        if directional_only and row.get("action") not in {"BUY", "SELL"}:
            continue
        if action_filter and row.get("action") != action_filter:
            continue
        if venue_filter and (row.get("venue") or "") != venue_filter:
            continue
        kept.append(row)

    by_id = {row["decision_id"]: row for row in kept if row.get("decision_id")}
    matched: list[dict[str, Any]] = []
    unmatched: list[dict[str, Any]] = []
    for trip in state["trips"]:
        entry_id = trip.get("entry_decision_id")
        decision = by_id.get(entry_id) if entry_id else None
        if decision is not None:
            linked = dict(trip)
            linked["announced_confidence"] = decision.get("confidence")
            linked["entry_action"] = decision.get("action")
            linked["entry_venue"] = decision.get("venue")
            linked["since_open_m"] = decision.get("since_open_m")
            linked["to_close_m"] = decision.get("to_close_m")
            linked["entry_cycle_ts"] = decision.get("cycle_ts")
            linked["entry_code_version"] = decision.get("git_commit_short") or UNKNOWN_VERSION
            matched.append(linked)
            continue
        if entry_id:
            # Décision hors filtre (since/action/venue/infra) : ne pas recycler en unmatched.
            continue
        if not _passes_since(trip.get("entry_ts"), since_dt):
            continue
        if action_filter and trip.get("side_action") != action_filter:
            continue
        if venue_filter and (trip.get("venue") or "") != venue_filter:
            continue
        unmatched.append(trip)

    return {
        "decisions": kept,
        "matched_trips": matched,
        "unmatched_trips": unmatched,
        "n_infra": n_infra,
        "filters": {"since": since, "action": action_filter or None, "venue": venue_filter},
        "since_dt": since_dt,
    }


def _confidence_rows(
    decisions: list[dict[str, Any]],
    trips: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    decisions_by_bucket: dict[str, list[dict[str, Any]]] = defaultdict(list)
    trips_by_bucket: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in decisions:
        decisions_by_bucket[_confidence_bucket(row.get("confidence"))].append(row)
    for trip in trips:
        trips_by_bucket[_confidence_bucket(trip.get("announced_confidence"))].append(trip)
    names = [name for name, _, _ in CONFIDENCE_BUCKETS] + [CONFIDENCE_MISSING]
    rows: list[dict[str, Any]] = []
    for name in names:
        stats = _summarize_group(decisions_by_bucket.get(name, []), trips_by_bucket.get(name, []))
        rows.append({"bucket": name, **stats})
    return rows


def run_calibration(
    state_dir: Path,
    *,
    since: str | None = None,
    action: str | None = None,
    venue: str | None = None,
) -> dict[str, Any]:
    state = _load_state(state_dir)
    universe = _filter_universe(
        state,
        since=since,
        action=action,
        venue=venue,
        directional_only=True,
    )
    decisions = universe["decisions"]
    matched = universe["matched_trips"]
    unmatched = universe["unmatched_trips"]
    n_confidence_missing = sum(1 for row in decisions if row.get("confidence") is None)
    buckets = _confidence_rows(decisions, matched)
    overall = _summarize_group(decisions, matched)
    by_action: dict[str, Any] = {}
    for side in ("BUY", "SELL"):
        side_decisions = [row for row in decisions if row.get("action") == side]
        side_trips = [trip for trip in matched if trip.get("entry_action") == side]
        if side_decisions or side_trips:
            by_action[side] = {
                **_summarize_group(side_decisions, side_trips),
                "buckets": _confidence_rows(side_decisions, side_trips),
            }
    notes = list(state["notes"])
    if universe["n_infra"]:
        notes.append(
            f"{universe['n_infra']} décision(s) infra/machine exclue(s) "
            "(benchmark_basis=infra, cf. decision_benchmark)."
        )
    if n_confidence_missing:
        notes.append(f"{n_confidence_missing} décision(s) directionnelle(s) sans confiance numérique.")
    if unmatched:
        notes.append(
            f"{len(unmatched)} round-trip(s) clos sans décision appariée "
            "(fill sans decision_id, ou décision absente du journal)."
        )
    if overall["n_closed_trips"] and overall["n_closed_trips"] < 10:
        notes.append("Effectif de trips clos < 10 : le gap de calibration est indicatif.")
    return {
        "command": "calibration",
        "filters": universe["filters"],
        "inputs": state["inputs"],
        "exclusions": {
            "n_infra": universe["n_infra"],
            "n_confidence_missing": n_confidence_missing,
        },
        "buckets": buckets,
        "by_action": by_action,
        "overall": overall,
        "unmatched_trips": _summarize_group([], unmatched, announced_from="fill"),
        "method": {
            "round_trips": "trader.reporting.read_models.trade_history.compute_round_trips",
            "decision_id": "fill d'ouverture (decision_id ou entry_decision_id)",
            "infra": "decision_benchmark_context.benchmark_basis == infra",
        },
        "notes": notes,
    }


def _action_mix(rows: list[dict[str, Any]]) -> dict[str, float]:
    counts = {"BUY": 0, "SELL": 0, "HOLD": 0}
    for row in rows:
        action = row.get("action")
        if action in counts:
            counts[action] += 1
        else:
            counts["HOLD"] += 1
    total = len(rows)
    if not total:
        return {key: 0.0 for key in counts}
    return {key: round(100.0 * n / total, 1) for key, n in counts.items()}


def _week_decision_stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    confidences = [row["confidence"] for row in rows if row.get("confidence") is not None]
    n_llm = len(rows)
    n_executed = sum(1 for row in rows if row.get("executed"))
    return {
        "n_llm": n_llm,
        "n_executed": n_executed,
        "action_mix": _action_mix(rows),
        "confidence_mean": (sum(confidences) / len(confidences)) if confidences else None,
        "confidence_median": median(confidences) if confidences else None,
        "execution_rate": (n_executed / n_llm) if n_llm else None,
    }


def run_drift(
    state_dir: Path,
    *,
    since: str | None = None,
    action: str | None = None,
    venue: str | None = None,
) -> dict[str, Any]:
    state = _load_state(state_dir)
    since_dt = _parse_since(since) if since else None
    action_filter = _normalize_action(action) if action else None
    venue_filter = venue.strip().upper() if venue else None

    by_week_all: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_week_llm: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_week_infra: dict[str, int] = defaultdict(int)
    by_week_version: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))
    authentic_ids: dict[str, dict[str, Any]] = {}

    for row in state["decisions"]:
        if not _passes_since(row.get("cycle_ts"), since_dt):
            continue
        if venue_filter and (row.get("venue") or "") != venue_filter:
            continue
        week = _iso_week(row.get("cycle_ts"))
        if week is None:
            continue
        by_week_all[week].append(row)
        if _is_infra(row):
            by_week_infra[week] += 1
            continue
        if action_filter and row.get("action") != action_filter:
            continue
        if not _is_llm_authentic(row):
            continue
        version = row.get("git_commit_short") or UNKNOWN_VERSION
        by_week_llm[week].append(row)
        by_week_version[week][version].append(row)
        if row.get("decision_id"):
            authentic_ids[row["decision_id"]] = row

    trips_by_week: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for trip in state["trips"]:
        if not _passes_since(trip.get("exit_ts"), since_dt):
            continue
        week = _iso_week(trip.get("exit_ts"))
        if week is None:
            continue
        entry_id = trip.get("entry_decision_id")
        decision = authentic_ids.get(entry_id) if entry_id else None
        linked = dict(trip)
        if decision is not None:
            if venue_filter and (decision.get("venue") or "") != venue_filter:
                continue
            if action_filter and decision.get("action") != action_filter:
                continue
            linked["announced_confidence"] = decision.get("confidence")
            linked["entry_code_version"] = decision.get("git_commit_short") or UNKNOWN_VERSION
            trips_by_week[week].append(linked)
        elif not entry_id:
            if action_filter and trip.get("side_action") != action_filter:
                continue
            if venue_filter and (trip.get("venue") or "") != venue_filter:
                continue
            trips_by_week[week].append(linked)

    weeks: list[dict[str, Any]] = []
    previous_dominant: str | None = None
    for week in sorted(set(by_week_all) | set(trips_by_week)):
        llm_rows = by_week_llm.get(week, [])
        version_rows = by_week_version.get(week, {})
        if version_rows:
            dominant = sorted(
                version_rows.items(),
                key=lambda item: (-len(item[1]), item[0]),
            )[0][0]
        else:
            dominant = None
        changed = bool(previous_dominant and dominant and dominant != previous_dominant)
        week_trips = trips_by_week.get(week, [])
        wins = [trip for trip in week_trips if (_as_float(trip.get("pnl")) or 0.0) > 0]
        by_code_version = []
        for version, rows in sorted(version_rows.items(), key=lambda item: (-len(item[1]), item[0])):
            version_trips = [
                trip for trip in week_trips if trip.get("entry_code_version") == version
            ]
            version_stats = _week_decision_stats(rows)
            version_stats.update(
                {
                    "code_version": version,
                    "n_closed_trips": len(version_trips),
                    "win_rate": (sum(1 for trip in version_trips if (_as_float(trip.get("pnl")) or 0) > 0) / len(version_trips))
                    if version_trips
                    else None,
                }
            )
            by_code_version.append(version_stats)
        payload = {
            "iso_week": week,
            "dominant_code_version": dominant,
            "dominant_changed": changed,
            "previous_dominant": previous_dominant if changed else None,
            "n_infra_excluded": by_week_infra.get(week, 0),
            **_week_decision_stats(llm_rows),
            "n_closed_trips": len(week_trips),
            "win_rate": (len(wins) / len(week_trips)) if week_trips else None,
            "by_code_version": by_code_version,
            "fill_only": not llm_rows and bool(week_trips),
        }
        weeks.append(payload)
        if dominant:
            previous_dominant = dominant

    notes = list(state["notes"])
    notes.append(
        "Une semaine est marquée quand le code_version dominant des décisions LLM "
        "authentiques change par rapport à la semaine précédente observée."
    )
    n_fill_only = sum(1 for week in weeks if week.get("fill_only"))
    if n_fill_only:
        notes.append(
            f"{n_fill_only} semaine(s) n'ont que des trips clos (fills d'avant le journal "
            "vif, ou decision_id manquant) : pas de code_version dominant."
        )
    return {
        "command": "drift",
        "filters": {"since": since, "action": action_filter or None, "venue": venue_filter},
        "inputs": state["inputs"],
        "weeks": weeks,
        "notes": notes,
    }


def run_session_window(
    state_dir: Path,
    *,
    since: str | None = None,
    action: str | None = None,
    venue: str | None = None,
) -> dict[str, Any]:
    state = _load_state(state_dir)
    universe = _filter_universe(
        state,
        since=since,
        action=action,
        venue=venue,
        directional_only=True,
    )
    decisions = universe["decisions"]
    matched = universe["matched_trips"]
    by_bucket_decisions: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_bucket_trips: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in decisions:
        bucket = _session_bucket(row.get("since_open_m"))
        if bucket:
            by_bucket_decisions[bucket].append(row)
    for trip in matched:
        bucket = _session_bucket(trip.get("since_open_m"))
        if bucket:
            by_bucket_trips[bucket].append(trip)

    by_since_open = []
    for name, _, _ in SESSION_BUCKETS:
        stats = _summarize_group(by_bucket_decisions.get(name, []), by_bucket_trips.get(name, []))
        by_since_open.append({"bucket": name, **stats})

    near_decisions = [row for row in decisions if row.get("to_close_m") is not None and row["to_close_m"] < 60]
    near_trips = [trip for trip in matched if trip.get("to_close_m") is not None and trip["to_close_m"] < 60]
    missing_decisions = [row for row in decisions if row.get("since_open_m") is None]
    missing_trips = [trip for trip in matched if trip.get("since_open_m") is None]
    coverage = state["coverage"]
    field_too_recent = coverage.get("n_with_value", 0) == 0
    notes = list(state["notes"])
    notes.append(
        "since_open_m / to_close_m (commit 4cab3ea) : "
        f"{coverage.get('n_with_key', 0)} ligne(s) portent la clé, "
        f"{coverage.get('n_with_value', 0)} ont une valeur non nulle "
        f"(to_close_m renseigné : {coverage.get('n_to_close_value', 0)}). "
        "Après union_by_name, une clé apparue tardivement est NULL sur les "
        "vieilles lignes : on ne distingue pas « champ absent » de « session fermée » "
        "dans la vue DuckDB ; la couverture ci-dessus vient d'un scan JSONL brut."
    )
    if field_too_recent:
        notes.append(
            "Champ since_open_m trop récent ou jamais renseigné sur ce journal : "
            "pas assez de volume pour conclure sur le fakeout open EU. "
            "Les effectifs sont affichés malgré tout."
        )
    elif by_bucket_trips.get("0-30"):
        open_stats = _summarize_group(by_bucket_decisions.get("0-30", []), by_bucket_trips["0-30"])
        wr = open_stats["win_rate"]
        notes.append(
            "Entrées 0-30 min après l'open : "
            f"n={open_stats['n_closed_trips']}, win_rate={wr}, "
            f"pnl_bps={open_stats['mean_pnl_bps']}."
        )
    return {
        "command": "session-window",
        "filters": universe["filters"],
        "inputs": state["inputs"],
        "by_since_open": by_since_open,
        "near_close": {"bucket": NEAR_CLOSE_BUCKET, **_summarize_group(near_decisions, near_trips)},
        "coverage": coverage,
        "missing": {
            "n_decisions": len(missing_decisions),
            "n_trips": len(missing_trips),
        },
        "field_too_recent": field_too_recent,
        "overall": _summarize_group(decisions, matched),
        "exclusions": {"n_infra": universe["n_infra"]},
        "notes": notes,
    }


def _fmt_pct(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value * 100:.1f}%"


def _fmt_num(value: float | None, digits: int = 2) -> str:
    if value is None:
        return "—"
    return f"{value:.{digits}f}"


def _print_table(headers: list[str], rows: list[list[str]]) -> None:
    widths = [len(header) for header in headers]
    for row in rows:
        for idx, cell in enumerate(row):
            widths[idx] = max(widths[idx], len(cell))
    header_line = "  ".join(header.ljust(widths[idx]) for idx, header in enumerate(headers))
    print(header_line)
    print("  ".join("-" * width for width in widths))
    for row in rows:
        print("  ".join(cell.ljust(widths[idx]) for idx, cell in enumerate(row)))


def _render_calibration(payload: dict[str, Any]) -> None:
    overall = payload["overall"]
    print("=== Calibration de confiance ===")
    filters = payload["filters"]
    print(
        f"Filtres : since={filters.get('since') or '—'}  "
        f"action={filters.get('action') or '—'}  venue={filters.get('venue') or '—'}"
    )
    print(
        f"Décisions directionnelles authentiques : {overall['n_decisions']}  "
        f"(infra exclues : {payload['exclusions']['n_infra']})"
    )
    print(
        f"Exécutées : {overall['n_executed']}   "
        f"trips clos joints : {overall['n_closed_trips']}   "
        f"win rate : {_fmt_pct(overall['win_rate'])}   "
        f"gap : {_fmt_num(overall['calibration_gap'], 3)}"
    )
    print()
    rows = []
    for item in payload["buckets"]:
        rows.append(
            [
                item["bucket"],
                str(item["n_executed"]),
                str(item["n_closed_trips"]),
                _fmt_pct(item["win_rate"]),
                _fmt_num(item["mean_pnl_bps"], 1),
                _fmt_num(item["mean_announced_confidence"], 3),
                _fmt_num(item["calibration_gap"], 3),
            ]
        )
    _print_table(
        ["tranche", "n_exec", "n_trips", "win_rate", "pnl_bps", "conf_moy", "gap"],
        rows,
    )
    by_action = payload.get("by_action") or {}
    if (
        "BUY" in by_action
        and "SELL" in by_action
        and by_action["BUY"]["n_executed"] >= 1
        and by_action["SELL"]["n_executed"] >= 1
    ):
        print()
        print("Séparation BUY / SELL :")
        for side in ("BUY", "SELL"):
            stats = by_action[side]
            print(
                f"  {side}: n_exec={stats['n_executed']}  trips={stats['n_closed_trips']}  "
                f"wr={_fmt_pct(stats['win_rate'])}  gap={_fmt_num(stats['calibration_gap'], 3)}"
            )
    elif by_action:
        print()
        print("Effectifs insuffisants pour une lecture séparée BUY/SELL au-delà du JSON.")
    unmatched = payload.get("unmatched_trips") or {}
    if unmatched.get("n_closed_trips"):
        print()
        print(
            f"Trips clos non joints au ledger : {unmatched['n_closed_trips']}  "
            f"wr={_fmt_pct(unmatched.get('win_rate'))}  "
            f"pnl_bps={_fmt_num(unmatched.get('mean_pnl_bps'), 1)}"
        )
    _render_notes(payload.get("notes") or [])


def _render_drift(payload: dict[str, Any]) -> None:
    print("=== Dérive hebdomadaire (LLM authentiques × code_version) ===")
    filters = payload["filters"]
    print(
        f"Filtres : since={filters.get('since') or '—'}  "
        f"action={filters.get('action') or '—'}  venue={filters.get('venue') or '—'}"
    )
    print()
    rows = []
    for week in payload.get("weeks") or []:
        mix = week.get("action_mix") or {}
        marker = ""
        if week.get("dominant_changed"):
            marker = (
                f"◆ changement de code_version dominant "
                f"({week.get('previous_dominant')} → {week.get('dominant_code_version')})"
            )
        elif week.get("fill_only"):
            marker = "fills seuls (pas de ledger LLM cette semaine)"
        rows.append(
            [
                week["iso_week"],
                str(week.get("dominant_code_version") or "—"),
                str(week.get("n_llm") or 0),
                f"{mix.get('BUY', 0):.1f}/{mix.get('SELL', 0):.1f}/{mix.get('HOLD', 0):.1f}",
                _fmt_num(week.get("confidence_mean"), 3),
                _fmt_num(week.get("confidence_median"), 3),
                _fmt_pct(week.get("execution_rate")),
                str(week.get("n_closed_trips") or 0),
                _fmt_pct(week.get("win_rate")),
                marker.strip(),
            ]
        )
    _print_table(
        [
            "semaine",
            "code_version",
            "n_llm",
            "%B/%S/%H",
            "conf_moy",
            "conf_med",
            "tx_exec",
            "trips",
            "win_rate",
            "marqueur",
        ],
        rows,
    )
    _render_notes(payload.get("notes") or [])


def _render_session(payload: dict[str, Any]) -> None:
    print("=== Fenêtre de session (since_open_m à l'entrée) ===")
    filters = payload["filters"]
    print(
        f"Filtres : since={filters.get('since') or '—'}  "
        f"action={filters.get('action') or '—'}  venue={filters.get('venue') or '—'}"
    )
    coverage = payload.get("coverage") or {}
    print(
        f"Couverture 4cab3ea : {coverage.get('n_with_key', 0)}/{coverage.get('n_rows', 0)} "
        f"lignes ont la clé since_open_m, {coverage.get('n_with_value', 0)} renseignée(s)."
    )
    if payload.get("field_too_recent"):
        print(
            "Champ trop récent ou jamais valorisé : pas de conclusion sur le fakeout open EU."
        )
    print()
    rows = []
    for item in payload.get("by_since_open") or []:
        rows.append(
            [
                item["bucket"],
                str(item["n_decisions"]),
                str(item["n_executed"]),
                str(item["n_closed_trips"]),
                _fmt_pct(item["win_rate"]),
                _fmt_num(item["mean_pnl_bps"], 1),
            ]
        )
    near = payload.get("near_close") or {}
    rows.append(
        [
            near.get("bucket") or NEAR_CLOSE_BUCKET,
            str(near.get("n_decisions") or 0),
            str(near.get("n_executed") or 0),
            str(near.get("n_closed_trips") or 0),
            _fmt_pct(near.get("win_rate")),
            _fmt_num(near.get("mean_pnl_bps"), 1),
        ]
    )
    _print_table(
        ["bucket", "n_dec", "n_exec", "n_trips", "win_rate", "pnl_bps"],
        rows,
    )
    missing = payload.get("missing") or {}
    print(
        f"Sans since_open_m : {missing.get('n_decisions', 0)} décision(s), "
        f"{missing.get('n_trips', 0)} trip(s)."
    )
    _render_notes(payload.get("notes") or [])


def _render_notes(notes: list[str]) -> None:
    if not notes:
        return
    print()
    print("Notes :")
    for note in notes:
        print(f"  - {note}")


def _render(payload: dict[str, Any]) -> None:
    command = payload.get("command")
    if command == "calibration":
        _render_calibration(payload)
    elif command == "drift":
        _render_drift(payload)
    else:
        _render_session(payload)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "command",
        choices=("calibration", "drift", "session-window"),
        help="calibration (courbe de fiabilité), drift (semaine × code_version), session-window (4cab3ea).",
    )
    parser.add_argument(
        "--state-dir",
        default=str(ROOT / "state"),
        help="Répertoire d'état (défaut : state/ à la racine du projet).",
    )
    parser.add_argument(
        "--since",
        help="Inclure les décisions / trips à partir de cette date ISO (YYYY-MM-DD ou datetime).",
    )
    parser.add_argument(
        "--action",
        choices=("BUY", "SELL"),
        help="Restreindre aux décisions directionnelles BUY ou SELL.",
    )
    parser.add_argument(
        "--venue",
        help="Filtrer par venue dérivée (market_snapshot.venue, mandate_ref.venue, sinon suffixe symbole).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Sortie JSON structurée (sinon tableau lisible).",
    )
    args = parser.parse_args(argv)

    if args.since:
        try:
            _parse_since(args.since)
        except ValueError:
            print(f"since invalide : {args.since!r} (date/datetime ISO attendu)", file=sys.stderr)
            return 2

    state_dir = Path(args.state_dir)
    runners = {
        "calibration": run_calibration,
        "drift": run_drift,
        "session-window": run_session_window,
    }
    payload = runners[args.command](
        state_dir,
        since=args.since,
        action=args.action,
        venue=args.venue,
    )
    if args.as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    else:
        _render(payload)
    return 0


if __name__ == "__main__":
    sys.exit(main())
