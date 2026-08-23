"""Read-only snapshot for the desktop cockpit.

Reuses the TUI read model, then slims blobs so the UI can poll every few seconds.
Never writes. Never talks to the broker.
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

UTC = timezone.utc
REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from trader.reporting.read_models.runtime_state import load_runtime_state  # noqa: E402

DECISION_KEYS = (
    "action",
    "confidence",
    "cycle_ts",
    "decision_id",
    "decision_reason_code",
    "decision_source",
    "executed",
    "intent",
    "llm_model",
    "llm_provider",
    "model_called",
    "next_wake_in_minutes",
    "opportunity_side",
    "price",
    "qty",
    "rationale",
    "reason",
    "sequence",
    "symbol",
    "ts",
)
RUNTIME_KEYS = (
    "armed_plan_id",
    "blocked",
    "data_source",
    "indicator_watch_created",
    "reject_reason",
    "trade_plan_created",
)
WATCH_KEYS = (
    "conditions",
    "expires_at",
    "logic",
    "purpose",
    "symbol",
    "watch_id",
)
_NEWS_MACRO_RUNNER_STATUS = "news_macro_runner_status.json"
_COMPANY_SCAN_STATUS = "company_scan_status.json"
_COMPANY_RESEARCH_DB = "company_research_tasks.db"
_COMPANY_TASK_KIND = "company_micro"
_DECISION_ACTIVE_PHASES = frozenset({"cycle_started", "deciding_batch", "decision_recorded"})


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, set):
        return sorted(value)
    return str(value)


def _jsonable(value: Any) -> Any:
    """JS JSON.parse rejects NaN/Infinity that Python json.dump emits by default."""
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    return value


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _slim_decision(row: Any) -> dict[str, Any] | None:
    if not isinstance(row, dict):
        return None
    slim = {key: row.get(key) for key in DECISION_KEYS if row.get(key) is not None}
    runtime = _as_dict(row.get("runtime"))
    kept_runtime = {key: runtime[key] for key in RUNTIME_KEYS if key in runtime}
    if kept_runtime:
        slim["runtime"] = kept_runtime
    watch = _as_dict(_as_dict(row.get("decision")).get("indicator_watch"))
    if watch:
        slim["indicator_watch"] = {key: watch.get(key) for key in WATCH_KEYS if watch.get(key) is not None}
    return slim


def _slim_watch(row: Any) -> dict[str, Any] | None:
    if not isinstance(row, dict):
        return None
    return {key: row.get(key) for key in WATCH_KEYS if row.get(key) is not None}


def _pid_alive(pid: int | None) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _read_pid_file(state_dir: Path) -> int | None:
    raw = (state_dir / "daemon.pid").read_text(encoding="utf-8").strip()
    if not raw:
        return None
    return int(raw)


def _read_activity_status(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _owned_running(payload: dict[str, Any], *, daemon_pid: int | None) -> bool:
    if not isinstance(daemon_pid, int) or daemon_pid <= 0:
        return False
    if payload.get("status") != "running":
        return False
    return payload.get("pid") == daemon_pid


def _due_company_queue_counts(path: Path, *, now_ms: int | None = None) -> dict[str, int]:
    empty = {"running_tasks": 0, "pending_tasks": 0}
    if not path.exists():
        return empty
    if now_ms is None:
        now_ms = int(time.time() * 1000)
    try:
        connection = sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True, timeout=0.2)
        try:
            running = connection.execute(
                "SELECT COUNT(*) FROM tasks WHERE kind=? AND status=?",
                (_COMPANY_TASK_KIND, "running"),
            ).fetchone()
            pending = connection.execute(
                "SELECT COUNT(*) FROM tasks WHERE kind=? AND status=? AND scheduled_at <= ?",
                (_COMPANY_TASK_KIND, "pending", int(now_ms)),
            ).fetchone()
        finally:
            connection.close()
    except (OSError, sqlite3.Error, TypeError, ValueError):
        return empty
    return {
        "running_tasks": max(0, int(running[0] if running else 0)),
        "pending_tasks": max(0, int(pending[0] if pending else 0)),
    }


def _lane(
    *,
    active: bool,
    running_tasks: int | None = None,
    pending_tasks: int | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"active": bool(active)}
    if running_tasks is not None:
        payload["running_tasks"] = int(running_tasks)
    if pending_tasks is not None:
        payload["pending_tasks"] = int(pending_tasks)
    return payload


def _intelligence_activity(
    state_dir: Path,
    *,
    daemon_alive: bool,
    daemon_pid: int | None,
    daemon_phase: Any,
    now_ms: int | None = None,
) -> dict[str, Any]:
    alive = bool(daemon_alive)
    context_active = alive and _owned_running(
        _read_activity_status(state_dir / _NEWS_MACRO_RUNNER_STATUS),
        daemon_pid=daemon_pid,
    )
    queue = _due_company_queue_counts(state_dir / _COMPANY_RESEARCH_DB, now_ms=now_ms)
    scan_active = alive and _owned_running(
        _read_activity_status(state_dir / _COMPANY_SCAN_STATUS),
        daemon_pid=daemon_pid,
    )
    companies_active = scan_active or (alive and (queue["running_tasks"] > 0 or queue["pending_tasks"] > 0))
    return {
        "context": _lane(active=context_active),
        "companies": _lane(
            active=companies_active,
            running_tasks=queue["running_tasks"],
            pending_tasks=queue["pending_tasks"],
        ),
        "decisions": _lane(active=alive and daemon_phase in _DECISION_ACTIVE_PHASES),
    }


def _equity_series(state_dir: Path, *, max_points: int = 480) -> list[dict[str, Any]]:
    path = state_dir / "history.jsonl"
    if not path.exists():
        return []
    points: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict) or row.get("equity") is None:
            continue
        try:
            equity = float(row["equity"])
        except (TypeError, ValueError):
            continue
        if not math.isfinite(equity) or equity <= 0:
            continue
        points.append(
            {
                "ts": str(row.get("ts") or ""),
                "equity": equity,
                "cash": row.get("cash"),
            }
        )
    if len(points) <= max_points:
        return points
    stride = max(1, len(points) // max_points)
    sampled = points[::stride]
    if sampled[-1] is not points[-1]:
        sampled.append(points[-1])
    return sampled[-max_points:]


def build_snapshot() -> dict[str, Any]:
    state_dir = REPO_ROOT / "state"
    raw = load_runtime_state(state_dir=state_dir, config_dir=str(REPO_ROOT))
    status = _as_dict(raw.get("daemon_status"))
    try:
        pid = status.get("pid") if isinstance(status.get("pid"), int) else _read_pid_file(state_dir)
    except (OSError, ValueError):
        pid = status.get("pid") if isinstance(status.get("pid"), int) else None

    daemon_pid = pid if isinstance(pid, int) else None
    daemon_alive = _pid_alive(daemon_pid)
    decisions = [slim for row in _as_list(raw.get("decisions")) if (slim := _slim_decision(row))]
    recent = [slim for row in _as_list(raw.get("recent_decisions")) if (slim := _slim_decision(row))]
    watches = [slim for row in _as_list(raw.get("indicator_watches")) if (slim := _slim_watch(row))]
    plans = []
    for row in _as_list(raw.get("trade_plans")):
        if isinstance(row, dict):
            plans.append(row)

    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "repo_root": str(REPO_ROOT),
        "source": raw.get("source"),
        "dry_run": raw.get("dry_run"),
        "starting_cash": raw.get("starting_cash"),
        "ts": raw.get("ts"),
        "daemon": {
            "pid": pid,
            "alive": daemon_alive,
            "phase": status.get("phase"),
            "dry_run": status.get("dry_run", raw.get("dry_run")),
            "current_symbol": status.get("current_symbol"),
            "decisions_done": status.get("decisions_done"),
            "symbols_due": status.get("symbols_due"),
            "symbols_total": status.get("symbols_total"),
            "model_calls_used": status.get("model_calls_used"),
            "max_model_calls_per_cycle": status.get("max_model_calls_per_cycle"),
            "ts": status.get("ts"),
        },
        "intelligence_activity": _intelligence_activity(
            state_dir,
            daemon_alive=daemon_alive,
            daemon_pid=daemon_pid,
            daemon_phase=status.get("phase"),
        ),
        "portfolio": raw.get("portfolio") if isinstance(raw.get("portfolio"), dict) else {},
        "kpis": raw.get("kpis") if isinstance(raw.get("kpis"), dict) else {},
        "equity_series": _equity_series(state_dir),
        "decisions": decisions,
        "recent_decisions": recent,
        "trade_plans": plans,
        "indicator_watches": watches,
        "armed_plans": [slim for row in _as_list(raw.get("armed_plans")) if (slim := _slim_watch(row))],
        "default_next_wake": raw.get("default_next_wake"),
        "symbol_wakes": raw.get("symbol_wakes") if isinstance(raw.get("symbol_wakes"), dict) else {},
        "stale_market_data": (raw.get("stale_market_data") if isinstance(raw.get("stale_market_data"), dict) else {}),
        "queue_worker_activity": (
            raw.get("queue_worker_activity") if isinstance(raw.get("queue_worker_activity"), dict) else {}
        ),
        "open_venues_list": _as_list(raw.get("open_venues_list")),
        "company_map": raw.get("company_map") if isinstance(raw.get("company_map"), dict) else {},
        "universe_symbols": _as_list(raw.get("universe_symbols")),
        "attribution": {"recent_trips": _as_list(_as_dict(raw.get("attribution")).get("recent_trips"))[:12]},
        "kill_active": (REPO_ROOT / "KILL").exists(),
    }


def main() -> None:
    json.dump(
        _jsonable(build_snapshot()),
        sys.stdout,
        default=_json_default,
        ensure_ascii=True,
        allow_nan=False,
    )
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
