"""Append-only ledger for runtime agent decisions.

The cycle reports are useful live views. This module keeps the durable audit
trail that can later be labelled against future market movement.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import code_version, decision_reason

SCHEMA_VERSION = 1
DEFAULT_LEDGER_FILENAME = "decisions.jsonl"
DEFAULT_REPORT_NAMES = ("last_report.json", "current_report.json")
DEFAULT_EVENT_NAMES = ("events.jsonl",)
LEGACY_DUPLICATE_WINDOW_S = 120.0
UNKNOWN_CODE_VERSION = {
    "schema_version": 1,
    "source": "unknown",
    "git_commit": None,
    "git_commit_short": None,
    "git_branch": None,
    "git_dirty": None,
    "git_dirty_files": [],
}


def _as_dict(value: Any) -> dict:
    return dict(value) if isinstance(value, dict) else {}


def _as_list(value: Any) -> list:
    return list(value) if isinstance(value, list) else []


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _optional_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    return None


def _decision_id(cycle_ts: str, sequence: int, symbol: str) -> str:
    return f"{cycle_ts}|{sequence}|{symbol}"


def _parse_ts(raw: Any) -> datetime | None:
    if not raw:
        return None
    try:
        value = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def build_decision_row(
    report: dict,
    decision: dict,
    *,
    sequence: int,
    source: str = "daemon",
) -> dict:
    cycle_ts = str(report.get("ts") or decision.get("ts") or "")
    symbol = str(decision.get("symbol") or "")
    if not cycle_ts or not symbol:
        raise ValueError("decision row requires report ts and decision symbol")

    prices = _as_dict(report.get("prices"))
    stale_market_data = _as_dict(report.get("stale_market_data"))
    price = _optional_float(decision.get("price"))
    if price is None:
        price = _optional_float(prices.get(symbol))

    original_decision = dict(decision)
    reason_code = decision_reason.infer_reason_code(
        {**decision, "decision": original_decision}
    )
    code_version = _as_dict(decision.get("code_version")) or _as_dict(report.get("code_version")) or dict(UNKNOWN_CODE_VERSION)
    indicator_watch = _as_dict(decision.get("indicator_watch"))
    return {
        "schema_version": SCHEMA_VERSION,
        "decision_id": _decision_id(cycle_ts, sequence, symbol),
        "cycle_ts": cycle_ts,
        "sequence": sequence,
        "source": source,
        "code_version": code_version,
        "symbol": symbol,
        "action": decision.get("action"),
        "intent": decision.get("intent"),
        "qty": _optional_float(decision.get("qty")),
        "confidence": _optional_float(decision.get("confidence")),
        "rationale": decision.get("rationale"),
        "next_wake_in_minutes": _optional_float(decision.get("next_wake_in_minutes")),
        "executed": decision.get("executed"),
        "reason": decision.get("reason"),
        "decision_reason_code": reason_code,
        "decision_source": decision.get("decision_source"),
        "model_called": _optional_bool(decision.get("model_called")),
        "price": price,
        "llm_provider": decision.get("llm_provider"),
        "llm_model": decision.get("llm_model"),
        "llm_fallback_reason": decision.get("llm_fallback_reason"),
        "llm_error": decision.get("llm_error"),
        "learning": decision.get("learning"),
        "decision": original_decision,
        "market_snapshot": {
            "price": price,
            "stale_market_data": stale_market_data.get(symbol),
            "symbols_due": _as_list(report.get("symbols_due")),
            "model_calls_used": report.get("model_calls_used"),
        },
        "portfolio_snapshot": _as_dict(report.get("portfolio")),
        "runtime": {
            "dry_run": report.get("dry_run"),
            "trade_plan_created": decision.get("trade_plan_created"),
            "indicator_watch_created": decision.get("indicator_watch_created"),
            "indicator_watch_requested": decision.get("indicator_watch_requested"),
            "indicator_watch_rejections": decision.get("indicator_watch_rejections"),
            "indicator_watch_order": indicator_watch.get("order"),
            "armed_plan_id": decision.get("armed_plan_id"),
            "armed_plan_order": decision.get("armed_plan_order"),
            "context_request": decision.get("context_request"),
            "next_wake_requested": decision.get("next_wake_requested"),
            "exit_plan": decision.get("exit_plan"),
            "risk_pct": decision.get("risk_pct"),
            "stop_distance": decision.get("stop_distance"),
            "risk_clamped": decision.get("risk_clamped"),
            "risk_unbounded_no_stop": decision.get("risk_unbounded_no_stop"),
            "data_source": decision.get("data_source"),
            "tool_rounds": decision.get("tool_rounds"),
            "tool_calls": decision.get("tool_calls"),
        },
        "news": _as_dict(decision.get("news")),
        "labels": {},
    }


class DecisionLedgerStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._ids_cache: set[str] | None = None

    def read_all(self, *, symbol: str | None = None, limit: int | None = None) -> list[dict]:
        rows = self._read_rows()
        if symbol:
            rows = [row for row in rows if row.get("symbol") == symbol]
        if limit is not None and limit >= 0:
            rows = rows[-limit:]
        return rows

    def append(self, row: dict) -> bool:
        decision_id = str(row.get("decision_id") or "")
        if not decision_id:
            raise ValueError("decision row requires decision_id")
        if decision_id in self._existing_ids():
            return False
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        if self._ids_cache is not None:
            self._ids_cache.add(decision_id)
        return True

    def append_from_report(self, report: dict, *, source: str = "report_seed") -> tuple[int, int]:
        appended = 0
        skipped = 0
        for sequence, decision in enumerate(_as_list(report.get("decisions"))):
            if not isinstance(decision, dict):
                skipped += 1
                continue
            row = build_decision_row(report, decision, sequence=sequence, source=source)
            if self.append(row):
                appended += 1
            else:
                skipped += 1
        return appended, skipped

    def append_from_event_file(self, path: str | Path) -> tuple[int, int]:
        event_path = Path(path)
        appended = 0
        skipped = 0
        for sequence, event in enumerate(_read_events(event_path)):
            if event.get("event") != "decision_recorded":
                continue
            row = build_legacy_event_row(
                event,
                sequence=sequence,
                source=f"event_seed:{event_path.name}",
            )
            if self._has_near_duplicate(row):
                skipped += 1
                continue
            if self.append(row):
                appended += 1
            else:
                skipped += 1
        return appended, skipped

    def _read_rows(self) -> list[dict]:
        if not self.path.exists():
            return []
        rows: list[dict] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                rows.append(row)
        return rows

    def _existing_ids(self) -> set[str]:
        if self._ids_cache is None:
            self._ids_cache = {
                str(row.get("decision_id"))
                for row in self._read_rows()
                if row.get("decision_id")
            }
        return self._ids_cache

    def _has_near_duplicate(self, row: dict) -> bool:
        row_ts = _parse_ts(row.get("cycle_ts"))
        if row_ts is None:
            return False
        for existing in self._read_rows():
            if existing.get("source", "").startswith("event_seed:"):
                continue
            if existing.get("symbol") != row.get("symbol"):
                continue
            if existing.get("action") != row.get("action"):
                continue
            if existing.get("reason") != row.get("reason"):
                continue
            if existing.get("executed") != row.get("executed"):
                continue
            existing_ts = _parse_ts(existing.get("cycle_ts"))
            if existing_ts is None:
                continue
            if abs((row_ts - existing_ts).total_seconds()) <= LEGACY_DUPLICATE_WINDOW_S:
                return True
        return False

    def replace_all(self, rows: list[dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = "\n".join(json.dumps(row, ensure_ascii=False) for row in rows)
        # with_name (pas with_suffix) : decisions.jsonl.tmp, l'idiome du repo —
        # with_suffix écraserait l'extension et créerait un tmp partagé par stem.
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(payload + ("\n" if payload else ""), encoding="utf-8")
        os.replace(tmp, self.path)
        self._ids_cache = {
            str(row.get("decision_id"))
            for row in rows
            if row.get("decision_id")
        }


def _read_report(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return payload if isinstance(payload, dict) else None


def _read_events(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def build_legacy_event_row(event: dict, *, sequence: int, source: str) -> dict:
    ts = str(event.get("ts") or "")
    symbol = str(event.get("symbol") or "")
    if not ts or not symbol:
        raise ValueError("legacy event row requires ts and symbol")
    action = event.get("action")
    reason = event.get("reason")
    decision = {
        "symbol": symbol,
        "action": action,
        "qty": None,
        "confidence": None,
        "rationale": f"legacy_event:{reason}",
        "intent": "HOLD" if action == "HOLD" else "UNKNOWN",
        "executed": event.get("executed"),
        "reason": reason,
    }
    reason_code = decision_reason.infer_reason_code(
        {**decision, "decision": decision}
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "decision_id": f"{ts}|legacy|{sequence}|{symbol}",
        "cycle_ts": ts,
        "sequence": sequence,
        "source": source,
        "quality": "legacy_event_summary",
        "code_version": dict(UNKNOWN_CODE_VERSION),
        "symbol": symbol,
        "action": action,
        "intent": decision["intent"],
        "qty": None,
        "confidence": None,
        "rationale": decision["rationale"],
        "next_wake_in_minutes": None,
        "executed": event.get("executed"),
        "reason": reason,
        "decision_reason_code": reason_code,
        "price": None,
        "llm_provider": None,
        "llm_model": None,
        "llm_fallback_reason": None,
        "llm_error": None,
        "learning": None,
        "decision": decision,
        "market_snapshot": {"price": None, "stale_market_data": None, "symbols_due": [], "model_calls_used": None},
        "portfolio_snapshot": {},
        "runtime": {
            "dry_run": None,
            "trade_plan_created": None,
            "indicator_watch_created": None,
            "indicator_watch_requested": None,
            "indicator_watch_rejections": None,
            "context_request": None,
            "next_wake_requested": None,
        },
        "labels": {},
    }


def seed_existing_reports(
    state_dir: str | Path,
    *,
    ledger_path: str | Path | None = None,
    report_names: tuple[str, ...] = DEFAULT_REPORT_NAMES,
) -> dict:
    state_path = Path(state_dir)
    store = DecisionLedgerStore(ledger_path or state_path / DEFAULT_LEDGER_FILENAME)
    reports = 0
    candidates = 0
    appended = 0
    skipped = 0
    for report_name in report_names:
        report = _read_report(state_path / report_name)
        if report is None:
            continue
        reports += 1
        decisions = _as_list(report.get("decisions"))
        candidates += len(decisions)
        source = f"report_seed:{report_name}"
        added, ignored = store.append_from_report(report, source=source)
        appended += added
        skipped += ignored
    return {"reports": reports, "candidates": candidates, "appended": appended, "skipped": skipped}


def seed_existing_events(
    state_dir: str | Path,
    *,
    ledger_path: str | Path | None = None,
    event_paths: list[str | Path] | None = None,
) -> dict:
    state_path = Path(state_dir)
    paths = [Path(path) for path in event_paths] if event_paths is not None else [
        state_path / name for name in DEFAULT_EVENT_NAMES
    ]
    store = DecisionLedgerStore(ledger_path or state_path / DEFAULT_LEDGER_FILENAME)
    event_files = 0
    candidates = 0
    appended = 0
    skipped = 0
    for event_path in paths:
        events = _read_events(event_path)
        if not events:
            continue
        event_files += 1
        candidates += sum(1 for event in events if event.get("event") == "decision_recorded")
        added, ignored = store.append_from_event_file(event_path)
        appended += added
        skipped += ignored
    return {"event_files": event_files, "candidates": candidates, "appended": appended, "skipped": skipped}


def _needs_code_version_backfill(row: dict, *, overwrite: bool) -> bool:
    if overwrite:
        return True
    version = row.get("code_version")
    if not isinstance(version, dict):
        return True
    return not version.get("git_commit")


def backfill_code_versions(
    state_dir: str | Path,
    *,
    repo_root: str | Path,
    ledger_path: str | Path | None = None,
    overwrite: bool = False,
    ref: str = "HEAD",
) -> dict:
    state_path = Path(state_dir)
    store = DecisionLedgerStore(ledger_path or state_path / DEFAULT_LEDGER_FILENAME)
    rows = store.read_all()
    updated = 0
    skipped = 0
    unresolved = 0
    cache: dict[str, dict] = {}

    for row in rows:
        if not _needs_code_version_backfill(row, overwrite=overwrite):
            skipped += 1
            continue
        decision_ts = str(row.get("cycle_ts") or "")
        if not decision_ts:
            unresolved += 1
            continue
        if decision_ts not in cache:
            cache[decision_ts] = code_version.historical_code_version(repo_root, decision_ts, ref=ref)
        version = cache[decision_ts]
        if not version.get("git_commit"):
            unresolved += 1
            continue
        row["code_version"] = dict(version)
        updated += 1

    if updated:
        store.replace_all(rows)
    return {
        "rows": len(rows),
        "updated": updated,
        "skipped": skipped,
        "unresolved": unresolved,
        "overwrite": overwrite,
        "ref": ref,
    }
