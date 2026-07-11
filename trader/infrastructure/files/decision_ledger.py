"""JSONL adapter and operator migrations for the durable decision ledger."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from trader.application.record.decision_ledger_rows import (
    SCHEMA_VERSION,
    UNKNOWN_CODE_VERSION,
    build_decision_row,
)
from trader.domain import decision_reason
from trader.support.metadata import code_version

DEFAULT_LEDGER_FILENAME = "decisions.jsonl"
DEFAULT_REPORT_NAMES = ("last_report.json", "current_report.json")
DEFAULT_EVENT_NAMES = ("events.jsonl",)
LEGACY_DUPLICATE_WINDOW_S = 120.0

__all__ = [
    "DEFAULT_EVENT_NAMES",
    "DEFAULT_LEDGER_FILENAME",
    "DEFAULT_REPORT_NAMES",
    "LEGACY_DUPLICATE_WINDOW_S",
    "DecisionLedgerStore",
    "backfill_code_versions",
    "build_legacy_event_row",
    "seed_existing_events",
    "seed_existing_reports",
]


def _as_list(value: Any) -> list:
    return list(value) if isinstance(value, list) else []


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


class DecisionLedgerStore:
    """Append-only JSONL adapter with in-process decision-id deduplication."""

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
    """Project a pre-ledger event into the historical compatibility schema."""

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
        "market_snapshot": {
            "price": None,
            "stale_market_data": None,
            "symbols_due": [],
            "model_calls_used": None,
        },
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
    return {
        "reports": reports,
        "candidates": candidates,
        "appended": appended,
        "skipped": skipped,
    }


def seed_existing_events(
    state_dir: str | Path,
    *,
    ledger_path: str | Path | None = None,
    event_paths: list[str | Path] | None = None,
) -> dict:
    state_path = Path(state_dir)
    paths = (
        [Path(path) for path in event_paths]
        if event_paths is not None
        else [state_path / name for name in DEFAULT_EVENT_NAMES]
    )
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
        candidates += sum(
            1 for event in events if event.get("event") == "decision_recorded"
        )
        added, ignored = store.append_from_event_file(event_path)
        appended += added
        skipped += ignored
    return {
        "event_files": event_files,
        "candidates": candidates,
        "appended": appended,
        "skipped": skipped,
    }


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
            cache[decision_ts] = code_version.historical_code_version(
                repo_root,
                decision_ts,
                ref=ref,
            )
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
